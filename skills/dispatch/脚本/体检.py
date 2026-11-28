"""每天首次派活前的只读环境体检；由调用方决定运行时机。"""

from __future__ import annotations

import argparse
import json
import math
import operator
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from 组合解析 import CommandFailure, Combination, config_root, load_config, load_current_combination, parse_magpie_claude_output, parse_magpie_claude_effort_output, parse_magpie_groups_output, redact_account_lock, run_cli, run_command, write_state
from 生效配置 import load_effective_config


DISPATCH_ROOT = Path(__file__).resolve().parents[1]
CONFIG_ROOT = config_root(DISPATCH_ROOT)
STATE_ROOT = CONFIG_ROOT / "状态"


def describe_difference(expected: list[str], actual: list[str] | None, missing: str) -> str:
    """“期望 …；实际 …”：账号锁只显示前四位。遮住后两边显示一样、实际却不同时，注明差在第几个成员的账号锁上。"""
    expected_display = " → ".join(map(redact_account_lock, expected))
    if actual is None:
        return f"期望 {expected_display}；实际 {missing}"
    actual_display = " → ".join(map(redact_account_lock, actual))
    hidden = [str(number) for number, (want, got) in enumerate(zip(expected, actual), 1) if want != got and redact_account_lock(want) == redact_account_lock(got)]
    where = f"第 {'、'.join(hidden)} 个成员的" if len(expected) > 1 else ""
    note = f"（{where}账号锁不同，只显示前四位）" if hidden else ""
    return f"期望 {expected_display}；实际 {actual_display}{note}"


def compare_combination(expected_groups: dict[str, list[str]], actual_groups: dict[str, list[str]], expected_claude: dict[str, str], actual_claude: dict[str, str], expected_effort: dict[str, str] | None = None, actual_effort: dict[str, str] | None = None) -> list[dict[str, str]]:
    differences: list[dict[str, str]] = []
    for name, expected in expected_groups.items():
        actual = actual_groups.get(name)
        if actual != expected:
            differences.append({"message": f"组 {name} 成员不一致：{describe_difference(expected, actual, '缺失')}"})
    for alias, expected in expected_claude.items():
        actual = actual_claude.get(alias)
        if actual != expected:
            differences.append({"message": f"档位 {alias} 不一致：{describe_difference([expected], [actual] if actual else None, '未单独设置（跟随主模型）')}"})
    for alias, expected in (expected_effort or {}).items():
        actual = (actual_effort or {}).get(alias)
        if actual != expected:
            differences.append({"message": f"推理档 {alias} 不一致：期望 {expected}；实际 {actual or '缺失'}"})
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
    output = run_command(["magpie", "claude"], remaining)
    aliases = parse_magpie_claude_output(output)
    # 别名都没单独设置时 magpie claude 只打印主模型一行，这是合法状态（2026-11-24 实测），交给比对报“未单独设置”，不当解析失败。
    if not groups or "Claude Code" not in output:
        raise ValueError("无法解析 magpie 的组或档位输出，需确认 CLI 输出格式")
    differences = compare_combination(combination["member_specs"], groups, combination["claude"], aliases, combination["claude_effort"], parse_magpie_claude_effort_output(output))
    return (False, "；".join(item["message"] for item in differences)) if differences else (True, "当前组合成员、顺序、档位和推理档一致")


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


FINISHED_STATES = {"succeeded", "failed", "stopped", "cancelled", "canceled", "released"}
ACTIVE_STATES = {"ready", "running", "dispatched", "starting", "working"}


def count_workers(rows: list[dict]) -> dict[str, int]:
    """按任务状态计在跑：远程 worker 在本机账上的存活状态是 unverifiable，不能只认 liveness=live（2026-11-16 实测漏数）。"""
    counts = {"running": 0, "unknown": 0, "stop_unknown": 0, "exited": 0, "finished": 0}
    for row in rows:
        state = row.get("state")
        if state == "stop_unknown":
            counts["stop_unknown"] += 1
        elif row.get("liveness") == "exited":
            counts["exited"] += 1
        elif state in FINISHED_STATES:
            counts["finished"] += 1
        elif row.get("liveness") == "live" or row.get("outcome") == "in_progress" or state in ACTIVE_STATES:
            counts["running"] += 1
        else:
            counts["unknown"] += 1
    return counts


def compare_worker_limit(local: dict[str, int], remotes: dict[str, dict[str, int]], limit: int) -> tuple[bool, str]:
    counts = [local, *remotes.values()]
    remote_running = sum(item["running"] for item in remotes.values())
    message = f"本机账上 {local['running']}（含从本机派到远程机的）/ 远程机自己账上 {remote_running}，上限 {limit}"
    if remotes:
        message += "；远程分项：" + "、".join(f"{name} {item['running']}" for name, item in remotes.items())
    unknown = sum(item["unknown"] for item in counts)
    stopped = sum(item["stop_unknown"] for item in counts)
    exited = sum(item["exited"] for item in counts)
    finished = sum(item.get("finished", 0) for item in counts)
    message += f"；已退出 {exited}，已结束 {finished}，停止结果未知 {stopped}（均不计在跑）"
    if unknown:
        message += f"；状态不明 {unknown}（本机 {local['unknown']} / 远程 {sum(item['unknown'] for item in remotes.values())}），未计入"
    if local["running"] + remote_running >= limit:
        return False, message + "；已到上限，先别再派"
    return True, message


def check_workers(config: dict, timeout: float) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout
    executable = os.environ.get("ORCA_CLI_COMMAND") or ("orca-dev" if os.environ.get("ORCA_DEV_REPO_ROOT") else "orca")

    def read_workers(environment: str) -> dict[str, int]:
        rows: list[dict] = []
        seen_cursors: set[str] = set()
        cursor: str | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("在跑 worker 检查超时")
            command = [executable, "orchestration", "worker-list", "--environment", environment, "--json"]
            if cursor:
                command.extend(["--cursor", cursor])
            # 仅对子进程清除会话绑定，否则 CLI 默认只查当前 Run，漏掉其他会话。
            output = run_command(command, remaining, environment_updates={"ORCA_AGENT_SESSION_ID": None})
            try:
                data = json.loads(output)
                result = data["result"]
                page = result["workers"]
                if data.get("ok") is not True or not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
                    raise ValueError
                if result.get("scope", {}).get("source") not in (None, "all"):
                    raise ValueError
                cursor = result.get("nextCursor")
                if cursor is not None and (not isinstance(cursor, str) or not cursor or cursor in seen_cursors):
                    raise ValueError
            except (KeyError, TypeError, AttributeError, ValueError) as exc:
                raise ValueError(f"无法解析 {environment} 的全机 worker 列表或分页") from exc
            rows.extend(page)
            if cursor is None:
                return count_workers(rows)
            seen_cursors.add(cursor)

    local = read_workers("local")
    remotes = {name: read_workers(name) for name in config.get("远程环境", []) if name != "local"}
    return compare_worker_limit(local, remotes, config.get("worker上限", 7))


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


def check_dispatch_config() -> tuple[bool, str]:
    result = load_effective_config(Path.cwd(), CONFIG_ROOT / "config.toml")
    errors = result["errors"]
    if errors:
        return False, f"{errors[0]}；共 {len(errors)} 条，跑 生效配置.py 看全部"
    return True, "个人层和项目层配置正常" if any(result["files"].values()) else "没有个人或项目配置，使用默认约定"


def optional_check(command: str, check):
    """缺少可选工具时跳过；工具已装但运行失败仍交原有异常处理。"""
    return lambda: check() if shutil.which(command) is not None else (True, f"跳过（没装 {command}）")


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="派活前只读体检，不调用模型").parse_args(argv)
    try:
        if (CONFIG_ROOT / "检查配置.json").exists():
            config = load_config(CONFIG_ROOT / "检查配置.json")
        else:
            print("没有 检查配置.json，用默认值")
            config = {}
    except (OSError, ValueError) as exc:
        print(f"✗ 本机配置：{exc}")
        print("汇总：配置错误，体检未执行")
        return 2
    timeout = config.get("检查超时秒", 30)
    checks = [("派活配置", check_dispatch_config), ("magpie", optional_check("magpie", lambda: check_magpie(timeout))), ("组合一致", optional_check("magpie", lambda: check_combination(timeout))), ("Orca", optional_check("orca", lambda: check_orca(timeout)))]
    for environment in config.get("远程环境", []):
        checks.append((f"远程 {environment}", optional_check("orca", lambda name=environment: check_remote(name, timeout))))
    if not config.get("远程环境"):
        checks.append(("远程服务器", lambda: (True, "未配置远程环境")))
    checks.append(("在跑 worker", optional_check("orca", lambda: check_workers(config, timeout))))
    checks.append(("MCP", optional_check("claude", lambda: check_mcp(timeout))))
    for rule in config.get("扩展检查", []):
        check = lambda item=rule: check_extension(item, timeout)
        command = Path(rule["命令"][0]).stem
        checks.append((rule["名称"], optional_check(command, check) if command in ("magpie", "orca", "claude") else check))
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
