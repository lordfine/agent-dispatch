"""共享组合解析、本机配置和带超时的只读命令执行。"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict


class Combination(TypedDict):
    groups: dict[str, list[str]]
    member_specs: dict[str, list[str]]
    claude: dict[str, str]


class CommandFailure(RuntimeError):
    """外部检查失败；异常消息不包含命令输出或账户信息。"""


def configure_utf8_streams() -> None:
    """入口统一使用 UTF-8，避免 Windows 默认 GBK 无法输出检查符号。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def run_cli(main: Callable[[], int]) -> int:
    """统一 CLI 编码与意外错误退出码，不泄露异常原文中的敏感数据。"""
    try:
        configure_utf8_streams()
        return main()
    except Exception as exc:
        try:
            print(f"脚本错误：未预期异常（{type(exc).__name__}）", file=sys.stderr)
        except (OSError, UnicodeError, ValueError):
            pass
        return 2


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)


def normalize_model(value: str) -> str:
    """落点只比较来源和模型，体检另外比较完整成员写法。"""
    return value.strip().split(":", 1)[0]


def normalize_target(value: str) -> str:
    return value.strip().removesuffix("[1m]")


def parse_combination_file(path: Path) -> Combination:
    specs: dict[str, list[str]] = {}
    aliases: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        group = re.fullmatch(r"magpie\s+group\s+add\s+(\S+)\s+models=(\S+)(?:\s+.*)?", line)
        alias = re.fullmatch(r"magpie\s+claude\s+(opus|sonnet|haiku|fable|subagent)\s+(\S+)", line)
        if re.fullmatch(r"magpie\s+claude\s+(opus|sonnet|haiku|fable|subagent)_effort\s+(medium|high|xhigh)", line):
            continue  # 档位的推理档只由切换脚本执行，体检不比对
        if group:
            name, members = group.groups()
            parts = members.split(",")
            if any(not re.fullmatch(r"[^\s/:,]+/[^\s/:,]+(?::[^\s,]+)*", part) for part in parts):
                raise ValueError(f"组合文件第 {number} 行的成员格式错误")
            if name in specs:
                raise ValueError(f"组合文件第 {number} 行重复定义组")
            specs[name] = parts
        elif alias:
            name, target = alias.groups()
            aliases[name] = normalize_target(target)
        else:
            raise ValueError(f"组合文件第 {number} 行不是支持的组或档位命令")
    if not specs:
        raise ValueError("组合文件没有有效的 magpie group add 行")
    return {"groups": {name: [normalize_model(member) for member in members] for name, members in specs.items()}, "member_specs": specs, "claude": aliases}


def config_root(dispatch_root: Path) -> Path:
    """个人配置目录：环境变量 DISPATCH_CONFIG_DIR，其次 ~/.claude/派活配置，最后是旧位置 <skill>/本机。"""
    env = os.environ.get("DISPATCH_CONFIG_DIR")
    if env:
        return Path(env).expanduser()
    default = Path.home() / ".claude" / "派活配置"
    return default if default.is_dir() else dispatch_root / "本机"


def combination_dir(dispatch_root: Path) -> Path:
    """在用组合放在个人配置的 组合/ 下；那里还没有 当前组合.txt 时用 skill 自带的 组合/。"""
    personal = config_root(dispatch_root) / "组合"
    return personal if (personal / "当前组合.txt").is_file() else dispatch_root / "组合"


def load_current_combination(dispatch_root: Path) -> Combination:
    folder = combination_dir(dispatch_root)
    names = [line.strip() for line in (folder / "当前组合.txt").read_text(encoding="utf-8-sig").splitlines() if line.strip() and not line.strip().startswith("#")]
    if len(names) != 1 or re.search(r'[\\/:*?"<>|]', names[0]) or names[0] in (".", ".."):
        raise ValueError("当前组合.txt 必须只有一个有效组合名")
    return parse_combination_file(folder / f"{names[0]}.txt")


def parse_magpie_groups_output(output: str) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for line in strip_ansi(output).splitlines():
        match = re.search(r"(?:^|\s)group/([^\s]+)\s+(.+)", line)
        if match:
            name, tail = match.groups()
            tail = tail.split("←", 1)[0]
            members = re.findall(r"(?<!\S)([^\s/,]+/[^\s,·→]+)", tail)
            if members:
                groups[name] = members
    return groups


def parse_magpie_claude_output(output: str) -> dict[str, str]:
    mappings: dict[str, str] = {}
    for alias, target in re.findall(r"\b(opus|sonnet|haiku|fable|subagents?)\s+(\S+/\S+)", strip_ansi(output)):
        mappings["subagent" if alias == "subagents" else alias] = normalize_target(target)
    return mappings


def load_config(path: Path) -> dict:
    if not path.exists():
        raise ValueError('缺少 个人配置目录（~/.claude/派活配置）下的 检查配置.json；示例：{"检查超时秒":30,"远程环境":[],"扩展检查":[],"落点检查":{"兜底最少请求数":10,"兜底占比阈值":0.3}}')
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError("检查配置.json 无法读取或不是有效 JSON") from exc
    if not isinstance(config, dict):
        raise ValueError("检查配置必须是 JSON 对象")
    timeout = config.setdefault("检查超时秒", 30)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("检查超时秒必须是正数")
    landing = config.setdefault("落点检查", {})
    if not isinstance(landing, dict):
        raise ValueError("落点检查配置必须是对象")
    for key, default, lower, upper in (("兜底最少请求数", 10, 0, math.inf), ("兜底占比阈值", 0.3, 0, 1), ("通知间隔秒", 3600, 1, math.inf)):
        value = landing.setdefault(key, default)
        if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper:
            raise ValueError(f"落点检查配置中的 {key} 超出有效范围")
    if type(landing["兜底最少请求数"]) is not int:
        raise ValueError("兜底最少请求数必须是整数")
    environments = config.setdefault("远程环境", [])
    if not isinstance(environments, list) or any(not isinstance(name, str) or not name.strip() for name in environments):
        raise ValueError("远程环境必须是环境名列表")
    rules = config.setdefault("扩展检查", [])
    if not isinstance(rules, list):
        raise ValueError("扩展检查必须是规则列表")
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get("名称"), str) or not rule["名称"].strip():
            raise ValueError("扩展检查缺少名称")
        command = rule.get("命令")
        if not isinstance(command, list) or not command or any(not isinstance(arg, str) or not arg for arg in command):
            raise ValueError("扩展检查命令必须是非空参数列表，不经 shell 执行")
        try:
            regex = re.compile(rule["正则"])
        except (KeyError, TypeError, re.error) as exc:
            raise ValueError("扩展检查正则无效") from exc
        if regex.groups != 1:
            raise ValueError("扩展检查正则必须有且仅有一个数值捕获组")
        threshold = rule.get("阈值")
        if type(threshold) not in (int, float) or not math.isfinite(threshold):
            raise ValueError("扩展检查阈值必须是有限数值")
        if rule.get("比较", ">=") not in (">=", ">", "<=", "<", "==", "!="):
            raise ValueError("扩展检查比较符只支持 >=、>、<=、<、==、!=")
    return config


def write_state(path: Path, data: dict) -> None:
    """用原子替换避免中断后留下半截 JSON；临时文件也只在本机状态目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def run_command(command: list[str], timeout: float = 30) -> str:
    executable = shutil.which(command[0])
    if executable is None:
        raise CommandFailure(f"命令 {Path(command[0]).name} 未找到")
    environment = os.environ.copy()
    environment["NO_COLOR"] = "1"
    process = subprocess.Popen([executable, *command[1:]], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env=environment, start_new_session=os.name != "nt")
    try:
        stdout, _stderr = process.communicate(timeout=max(0.001, timeout))
    except subprocess.TimeoutExpired as exc:
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        # 读线程可能持有缓冲层锁，直接关闭原始管道句柄。
                        getattr(getattr(stream, "buffer", stream), "raw", stream).close()
                    except (OSError, ValueError):
                        pass
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=1)
            except (subprocess.TimeoutExpired, OSError):
                pass
        raise CommandFailure(f"命令 {Path(command[0]).name} 超时（{timeout:g} 秒）") from exc
    if process.returncode != 0:
        raise CommandFailure(f"命令 {Path(command[0]).name} 失败，退出码 {process.returncode}")
    return strip_ansi(stdout)


if __name__ == "__main__":
    sys.exit(run_cli(lambda: 0))
