"""检查模型请求落点并用 Windows 自带通知提醒；不调用模型。"""

from __future__ import annotations

import argparse
import base64
import csv
import html
import io
import json
import math
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from 组合解析 import config_root, load_config, load_current_combination, normalize_model, normalize_target, run_cli, run_command, write_state


DISPATCH_ROOT = Path(__file__).resolve().parents[1]


def parse_usage_csv(text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")), strict=True)
    columns = set(reader.fieldnames or [])
    if not {"requested_model", "provider", "status"} <= columns or not {"served_model", "model"} & columns:
        raise ValueError("magpie usage CSV 缺少请求、来源、状态或实际模型列")
    rows = list(reader)
    if any(None in row or any(value is None for value in row.values()) for row in rows):
        raise ValueError("magpie usage CSV 存在列数不一致的记录")
    return rows


def analyze_usage(rows: list[dict[str, str]], groups: dict[str, list[str]], fallback_min_requests: int = 10, fallback_ratio: float = 0.3) -> list[dict[str, str]]:
    grouped: dict[str, Counter[str]] = {}
    for row in rows:
        if not re.fullmatch(r"2\d\d", str(row.get("status", "")).strip()):
            continue
        requested = row.get("requested_model", "").strip()
        if not requested.startswith("group/"):
            continue
        name = normalize_target(requested[len("group/"):])
        provider = row.get("provider", "").strip()
        served = (row.get("served_model", "").strip() or row.get("model", "").strip())
        if not provider or not served:
            raise ValueError("成功的组请求缺少来源或实际模型，无法判断落点")
        grouped.setdefault(name, Counter())[normalize_model(f"{provider}/{served}")] += 1
    issues: list[dict[str, str]] = []
    for name, counts in sorted(grouped.items()):
        members = [normalize_model(member) for member in groups.get(name, [])]
        if not members:
            issues.append({"type": "unknown_group", "group": name, "message": f"落点异常：组 {name} 不在当前组合中（{sum(counts.values())} 次）"})
            continue
        unknown = sorted(model for model in counts if model not in members)
        if unknown:
            details = "、".join(f"{model}（{counts[model]} 次）" for model in unknown)
            issues.append({"type": "out_of_group", "group": name, "message": f"落点异常：组 {name} 落到组外成员 {details}"})
        total = sum(counts.values())
        fallback = total - counts[members[0]]
        if total > fallback_min_requests and fallback / total > fallback_ratio:
            issues.append({"type": "fallback_ratio", "group": name, "message": f"兜底偏高：组 {name} 共 {total} 次，非第一顺位 {fallback} 次（{fallback / total:.1%}，阈值 {fallback_ratio:.1%}）"})
    return issues


def _read_notification_state(state_path: Path) -> dict[str, int | float]:
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(state, dict):
        return {}
    return {key: value for key, value in state.items() if isinstance(key, str) and type(value) in (int, float) and math.isfinite(value)}


def filter_notification_groups(groups: list[str], state_path: Path, now: float | None = None, interval_seconds: float = 3600, record: bool = True) -> list[str]:
    current = time.time() if now is None else now
    state = _read_notification_state(state_path)
    allowed = []
    for group in dict.fromkeys(groups):
        previous = state.get(group)
        if previous is None or current < previous or current - previous >= interval_seconds:
            allowed.append(group)
            state[group] = current
    if allowed and record:
        # 计算告警期间可能有另一进程写入；写入前重新读并合并，避免覆盖它新增的组。
        latest = _read_notification_state(state_path)
        latest.update({group: current for group in allowed})
        write_state(state_path, latest)
    return allowed


def show_toast(messages: list[str]) -> tuple[bool, str]:
    """通知文字通过 Base64 传给 PowerShell，不拼入可执行语句。"""
    xml = '<toast><visual><binding template="ToastGeneric"><text>落点检查提醒</text><text>' + html.escape("；".join(messages), quote=True) + "</text></binding></visual></toast>"
    payload = base64.b64encode(xml.encode("utf-8")).decode("ascii")
    script = (
        "$ErrorActionPreference='Stop';"
        "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime] > $null;"
        "[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime] > $null;"
        "$x=New-Object Windows.Data.Xml.Dom.XmlDocument;"
        f"$x.LoadXml([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}')));"
        "$n=[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe');"
        "$n.Show([Windows.UI.Notifications.ToastNotification]::new($x));"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    try:
        run_command(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], 10)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return False, "Windows 通知未弹出：系统通知调用失败或通知被禁用，问题已打印"
    return True, "Windows 通知已提交（是否显示未确认）"


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description="检查当前组合的实际落点，不调用模型", allow_abbrev=False)
    parser.add_argument("--窗口", default="1h", help="magpie 用量窗口，默认 1h")
    parser.add_argument("--不通知", action="store_true", help="仅打印，不写通知去重状态")
    parser.add_argument("--precheck", action="store_true", help="始终退出 1，阻止 Orca 定时任务唤起代理")
    try:
        args = parser.parse_args(arguments)
    except SystemExit as exc:
        return 1 if exc.code == 0 else 2
    precheck = args.precheck
    try:
        config = load_config(config_root(DISPATCH_ROOT) / "检查配置.json")
        combination = load_current_combination(DISPATCH_ROOT)
        text = run_command(["magpie", "usage", "--csv", "--last", args.窗口], config["检查超时秒"])
        landing = config["落点检查"]
        issues = analyze_usage(parse_usage_csv(text), combination["groups"], landing["兜底最少请求数"], landing["兜底占比阈值"])
        if not issues:
            print("落点正常")
            return 1 if precheck else 0
        for issue in issues:
            print(issue["message"])
        if not args.不通知:
            state = config_root(DISPATCH_ROOT) / "状态" / "落点通知.json"
            interval = landing["通知间隔秒"]
            names = filter_notification_groups([issue["group"] for issue in issues], state, interval_seconds=interval, record=False)
            if names:
                notified, message = show_toast([issue["message"] for issue in issues if issue["group"] in names])
                print(message)
                if notified:
                    filter_notification_groups(names, state, interval_seconds=interval)
            else:
                print("通知已去重：同组问题仍在通知间隔内")
        return 1
    except (OSError, ValueError, RuntimeError, csv.Error, subprocess.SubprocessError) as exc:
        message = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "读取文件或执行检查失败，请检查本机环境"
        print(f"落点检查错误：{message}")
        return 1 if precheck else 2


if __name__ == "__main__":
    sys.exit(run_cli(lambda: main()))
