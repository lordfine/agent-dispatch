"""每天首次派活前的只读环境体检；由调用方决定运行时机。"""

from __future__ import annotations

import argparse
import json
import math
import operator
import re
import subprocess
import sys
import time
from pathlib import Path

from 组合解析 import CommandFailure, Combination, config_root, load_config, load_current_combination, parse_magpie_claude_output, parse_magpie_groups_output, run_cli, run_command, write_state


DISPATCH_ROOT = Path(__file__).resolve().parents[1]
CONFIG_ROOT = config_root(DISPATCH_ROOT)
STATE_ROOT = CONFIG_ROOT / "状态"


def compare_combination(expected_groups: dict[str, list[str]], actual_groups: dict[str, list[str]], expected_claude: dict[str, str], actual_claude: dict[str, str]) -> list[dict[str, str]]:
    differences: list[dict[str, str]] = []
    for name, expected in expected_groups.items():
        actual = actual_groups.get(name)
        if actual != expected:
            differences.append({"message": f"组 {name} 成员不一致：期望 {' → '.join(expected)}；实际 {' → '.join(actual) if actual is not None else '缺失'}"})
    for alias, expected in expected_claude.items():
        actual = actual_claude.get(alias)
        if actual != expected:
            differences.append({"message": f"档位 {alias} 不一致：期望 {expected}；实际 {actual or '缺失'}"})
    return differences


def check_magpie(timeout: float) -> tuple[bool, str]:
    output = run_command(["magpie", "claude"], timeout)
    if not output.strip():
        raise ValueError("magpie claude 没有返回配置")
    return True, "magpie claude 正常返回"


def check_combination(timeout: float) -> tuple[bool, str]:
    combination: Combination = load_current_combination(DISPATCH_ROOT)
    deadline = time.monotonic() + timeout
    groups = parse_magpie_groups_output(run_command(["magpie", "groups"], timeout))
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("组合一致检查超时")
    aliases = parse_magpie_claude_output(run_command(["magpie", "claude"], remaining))
    if not groups or not aliases:
        raise ValueError("无法解析 magpie 的组或档位输出，需确认 CLI 输出格式")
    differences = compare_combination(combination["member_specs"], groups, combination["claude"], aliases)
    return (False, "；".join(item["message"] for item in differences)) if differences else (True, "当前组合成员、顺序、档位及 fast 标记一致")


def parse_orca_status(output: str) -> dict:
    try:
        data = json.loads(output)
        result = data["result"]
        if data.get("ok") is not True or not isinstance(result, dict):
            raise ValueError
        if not isinstance(result.get("runtime"), dict) or not isinstance(result.get("app"), dict):
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("无法解析 Orca 状态 JSON") from exc
    return result


def check_orca(timeout: float) -> tuple[bool, str]:
    result = parse_orca_status(run_command(["orca", "status", "--json"], timeout))
    runtime = result["runtime"]
    if not result.get("app", {}).get("running") or runtime.get("reachable") is not True or runtime.get("state") != "ready":
        return False, "Orca 应用或运行时尚未就绪"
    version = runtime.get("appVersion")
    if not isinstance(version, str) or not re.fullmatch(r"\d+(?:\.\d+)+(?:[-+][\w.-]+)?", version):
        raise ValueError("Orca 状态中没有有效版本号")
    path = STATE_ROOT / "Orca版本.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        previous = state["版本"]
        if not isinstance(previous, str):
            raise ValueError
    except FileNotFoundError:
        previous = None
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("状态/Orca版本.json 损坏，请检查版本记录") from exc
    if previous != version:
        write_state(path, {"版本": version})
    if previous and previous != version:
        return True, f"Orca 从 {previous} 升到 {version}，重读 `orca skills get orchestration`"
    return True, f"Orca {version} 运行正常" + ("（首次记录版本）" if previous is None else "")


def check_remote(environment: str, timeout: float) -> tuple[bool, str]:
    result = parse_orca_status(run_command(["orca", "status", "--environment", environment, "--json"], timeout))
    runtime = result["runtime"]
    ready = runtime.get("reachable") is True and runtime.get("state") == "ready"
    return ready, "远程运行时可达" if ready else "远程运行时不可达或尚未就绪"


def check_mcp(timeout: float) -> tuple[bool, str]:
    output = run_command(["claude", "mcp", "list"], timeout)
    failures = []
    connected = 0
    for line in output.splitlines():
        name = re.match(r"^\s*([^\s:：]+)[:：]", line)
        if not name:
            continue
        if re.search(r"✗|✘|×|⚠|failed|error|disconnected|timeout|needs authentication|失败|未连接|需.*认证", line, re.IGNORECASE):
            failures.append(name.group(1))
        elif re.search(r"✓|✔|connected|已连接", line, re.IGNORECASE):
            connected += 1
    if failures:
        return False, "连接失败：" + "、".join(failures)
    if connected:
        return True, f"{connected} 个服务连接正常"
    if re.search(r"no MCP servers|未配置.*MCP|没有.*MCP", output, re.IGNORECASE):
        return True, "未配置 MCP 服务"
    raise ValueError("MCP 没有可识别的检查结果，请确认 claude mcp list 输出格式")


def check_extension(rule: dict, timeout: float) -> tuple[bool, str]:
    output = run_command(rule["命令"], timeout)
    try:
        values = [float(value) for value in re.findall(rule["正则"], output)]
    except ValueError as exc:
        raise ValueError("扩展检查正则捕获到非数值内容") from exc
    if not values or any(not math.isfinite(value) for value in values):
        raise ValueError("扩展检查未匹配到有效数值，无法判断是否正常")
    comparison = rule.get("比较", ">=")
    compare = {">=": operator.ge, ">": operator.gt, "<=": operator.le, "<": operator.lt, "==": operator.eq, "!=": operator.ne}[comparison]
    hits = [value for value in values if compare(value, rule["阈值"])]
    if hits:
        return False, f"{len(hits)}/{len(values)} 个数值触发 {comparison}{rule['阈值']:g}，触发值 {', '.join(f'{value:g}' for value in sorted(set(hits)))}"
    return True, f"{len(values)} 个数值均未触发 {comparison}{rule['阈值']:g}（范围 {min(values):g}～{max(values):g}）"


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="派活前只读体检，不调用模型").parse_args(argv)
    try:
        config = load_config(CONFIG_ROOT / "检查配置.json")
    except (OSError, ValueError) as exc:
        print(f"✗ 本机配置：{exc}")
        print("汇总：配置错误，体检未执行")
        return 2
    timeout = config.get("检查超时秒", 30)
    checks = [("magpie", lambda: check_magpie(timeout)), ("组合一致", lambda: check_combination(timeout)), ("Orca", lambda: check_orca(timeout))]
    for environment in config.get("远程环境", []):
        checks.append((f"远程 {environment}", lambda name=environment: check_remote(name, timeout)))
    if not config.get("远程环境"):
        checks.append(("远程服务器", lambda: (True, "未配置远程环境")))
    checks.append(("MCP", lambda: check_mcp(timeout)))
    for rule in config.get("扩展检查", []):
        checks.append((rule["名称"], lambda item=rule: check_extension(item, timeout)))
    if not config.get("扩展检查"):
        checks.append(("本机扩展", lambda: (True, "未配置扩展检查")))
    passed = 0
    errors = 0
    for name, check in checks:
        try:
            healthy, message = check()
        except (CommandFailure, TimeoutError) as exc:
            healthy, message = False, str(exc)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            healthy = False
            message = str(exc) if isinstance(exc, ValueError) else "无法读取检查结果或本机状态"
            errors += 1
        passed += bool(healthy)
        print(f"{'✓' if healthy else '✗'} {name}：{' '.join(message.splitlines())}")
    print(f"汇总：{passed}/{len(checks)} 项正常，{len(checks) - passed} 项异常" + (f"（其中 {errors} 项检查出错）" if errors else ""))
    return 2 if errors else (0 if passed == len(checks) else 1)


if __name__ == "__main__":
    sys.exit(run_cli(lambda: main()))
