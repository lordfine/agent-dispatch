#!/usr/bin/env python3
"""只读查看派活生效配置，不调用模型，也不探测模型或执行机。

个人层为 config_root()/config.toml；项目层向上寻找 .dispatch.toml，含 .git
的目录检查后停止。先逐文件校验结构，坏文件整层不合并，再校验合并后的语义。
load_effective_config、read_layer、merge_layers、validate_semantics 均可导入，
不打印、不退出；错误为带文件路径和键路径的字符串列表。

未设置：mode 表示七个角色按原生子代理、implementer 按 Orca；host 表示
主会话所在的宿主；model 表示宿主默认；effort 由主会话每次决定。

--json 输出结构（缺失值用 null，缺失文件用 null）：
{
  "roles": {"explorer": {"mode": {"value": "native", "source": "个人"},
    "host": {"value": null, "source": "未设置"}, "model": {...}, "effort": {...}}, ...},
  "executor": {"value": null, "source": "未设置"},
  "files": {"personal": "个人文件路径或 null", "project": "项目文件路径或 null"},
  "errors": ["文件路径：键路径 错误说明", ...]
}
source 只取 个人、项目、未设置；未设置的值不擅自填入默认值。
退出码：正常 0，配置错误 2。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tomllib
from typing import Any, TypedDict

from 组合解析 import config_root, run_cli

class ConfigValue(TypedDict):
    value: str | None
    source: str


class EffectiveConfig(TypedDict):
    roles: dict[str, dict[str, ConfigValue]]
    executor: ConfigValue
    files: dict[str, str | None]
    errors: list[str]


ROLES = ("explorer", "researcher", "extractor", "reviewer", "diagnoser", "writer", "verifier", "implementer")
FIELDS = ("mode", "host", "model", "effort")
CHOICES = {"mode": ("native", "orca"), "host": ("codex", "claude-code"), "effort": ("medium", "high", "xhigh")}
DISPATCH_ROOT = Path(__file__).resolve().parents[1]


def find_project_config(project_dir: Path) -> Path | None:
    """从指定目录向上找，含 .git 的目录本身仍参加查找。"""
    current = Path(project_dir).resolve()
    for folder in (current, *current.parents):
        candidate = folder / ".dispatch.toml"
        if candidate.is_file():
            return candidate
        if (folder / ".git").exists():
            break
    return None


def validate_structure(data: dict[str, Any], path: Path) -> list[str]:
    """逐文件校验；version 是整数，其余叶子值必须为字符串。"""
    errors = []

    def error(key: str, message: str):
        errors.append(f"{path}：{key} {message}")

    if "version" not in data:
        error("version", "缺少必填字段，目前只支持整数 1")
    elif type(data["version"]) is not int or data["version"] != 1:
        error("version", "必须是整数 1")
    for key in data:
        if key not in ("version", "executor", "roles"):
            error(key, "不认识的字段")
    if "executor" in data and not isinstance(data["executor"], str):
        error("executor", "值必须是字符串")
    roles = data.get("roles", {})
    if not isinstance(roles, dict):
        error("roles", "必须是角色表")
        return errors
    for role, values in roles.items():
        key = f"roles.{role}"
        if role not in ROLES:
            error(key, "不认识的角色名")
        if not isinstance(values, dict):
            error(key, "必须是角色字段表")
            continue
        for field, value in values.items():
            field_key = f"{key}.{field}"
            if field not in FIELDS:
                error(field_key, "不认识的字段")
            if not isinstance(value, str):
                error(field_key, "值必须是字符串")
            elif field in CHOICES and value not in CHOICES[field]:
                error(field_key, "只能是 " + "、".join(CHOICES[field]))
    return errors


def read_layer(path: Path | None) -> tuple[dict[str, Any] | None, list[str]]:
    """没有文件是空层；有错误返回 None，禁止该文件参与合并。"""
    if path is None:
        return {}, []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, []
    except (OSError, UnicodeError) as exc:
        return None, [f"{path}：无法读取配置（{type(exc).__name__}）"]
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return None, [f"{path}：TOML 语法错误：{exc}"]
    errors = validate_structure(data, path)
    return (None if errors else data), errors


def merge_layers(personal: dict[str, Any] | None, project: dict[str, Any] | None) -> EffectiveConfig:
    """按角色、按字段覆盖，并为所有值保留来源。只接收通过结构校验的层。"""
    result: EffectiveConfig = {
        "roles": {role: {field: {"value": None, "source": "未设置"} for field in FIELDS} for role in ROLES},
        "executor": {"value": None, "source": "未设置"},
        "files": {"personal": None, "project": None},
        "errors": [],
    }
    for source, layer in (("个人", personal), ("项目", project)):
        if not layer:
            continue
        if "executor" in layer:
            result["executor"] = {"value": layer["executor"], "source": source}
        for role, values in layer.get("roles", {}).items():
            for field, value in values.items():
                result["roles"][role][field] = {"value": value, "source": source}
    return result


def validate_semantics(config: EffectiveConfig, personal_path: Path, project_path: Path | None) -> list[str]:
    """仅检查合并结果；错误定位到该字段最后生效的文件。"""
    errors = []
    for role, fields in config["roles"].items():
        mode = fields["mode"]["value"]
        effective_mode = mode if mode is not None else ("orca" if role == "implementer" else "native")
        model = fields["model"]["value"]
        field = None
        message = ""
        if role == "implementer" and mode is not None and mode != "orca":
            field, message = "mode", "实现只走 Orca worker，必须为 orca"
        if field:
            path = project_path if fields[field]["source"] == "项目" else personal_path
            errors.append(f"{path}：roles.{role}.{field} {message}")
        if fields["host"]["value"] == "claude-code" and effective_mode == "native" and model is not None and model not in ("opus", "sonnet", "haiku", "fable"):
            path = project_path if fields["model"]["source"] == "项目" else personal_path
            errors.append(f"{path}：roles.{role}.model Claude 原生子代理只接受 opus、sonnet、haiku、fable；其他模型请用 orca")
    return errors


def load_effective_config(project_dir: Path | None = None, personal_file: Path | None = None) -> EffectiveConfig:
    """读取两层、合并和校验；调用方统一处理输出及退出码。"""
    personal_path = Path(personal_file) if personal_file is not None else config_root(DISPATCH_ROOT) / "config.toml"
    project_path = find_project_config(project_dir if project_dir is not None else Path.cwd())
    personal, personal_errors = read_layer(personal_path)
    project, project_errors = read_layer(project_path)
    result = merge_layers(personal, project)
    result["files"] = {"personal": str(personal_path) if personal_path.exists() else None, "project": str(project_path) if project_path is not None else None}
    result["errors"] = personal_errors + project_errors + validate_semantics(result, personal_path, project_path)
    return result


def format_value(role: str, field: str, cell: ConfigValue) -> str:
    value = cell["value"]
    if value is None:
        defaults = {
            "mode": "Orca worker" if role == "implementer" else "原生子代理",
            "host": "主会话所在的宿主",
            "model": "宿主默认",
            "effort": "由主会话每次决定",
        }
        value = defaults[field]
    return f"{value}（{cell['source']}）"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="查看生效派活配置，不调用模型", allow_abbrev=False)
    parser.add_argument("--项目", type=Path, default=None, help="从这个目录向上寻找 .dispatch.toml，默认当前目录")
    parser.add_argument("--json", action="store_true", help="输出含来源、文件路径和错误的 JSON")
    args = parser.parse_args(argv)
    result = load_effective_config(args.项目)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("角色：方式 / 宿主 / 模型 / 档位")
        for role, fields in result["roles"].items():
            print(f"{role}：" + " / ".join(format_value(role, field, fields[field]) for field in FIELDS))
        executor = result["executor"]
        print(f"默认执行机：{executor['value'] if executor['value'] is not None else '未设置'}（{executor['source']}）")
        print(f"个人文件：{result['files']['personal'] or '没有'}")
        print(f"项目文件：{result['files']['project'] or '没有'}")
        for error in result["errors"]:
            print(error)
    return 2 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(run_cli(lambda: main()))
