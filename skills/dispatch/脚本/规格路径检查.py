"""只读核验规格引用的路径及行号是否存在于指定 Git 基线。"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TypedDict

from 组合解析 import CommandFailure, run_cli, run_command


class SpecPath(TypedDict):
    path: str
    line: int
    start: int | None
    end: int | None
    new: bool


def extract_spec_paths(text: str) -> list[SpecPath]:
    paths: list[SpecPath] = []
    for number, line in enumerate(text.splitlines(), 1):
        for match in re.finditer(r"(?<!`)`([^`\n]+)`(?!`)", line):
            value = match.group(1)
            if re.search(r"\s|[\x00-\x1f$(){}<>|;&=?*\"#]", value):
                continue
            location = re.fullmatch(r"(.+?)(?::(\d+)(?:-(\d+))?)?", value)
            if location is None:
                continue
            path, start, end = location.groups()
            if ":" in path or path.startswith(("/", "\\", "~")) or PureWindowsPath(path).drive:
                continue
            path = path.replace("\\", "/")
            relative = PurePosixPath(path)
            if ".." in relative.parts or not ("/" in path or relative.suffix):
                continue
            paths.append({"path": relative.as_posix(), "line": number, "start": int(start) if start else None,
                          "end": int(end or start) if start else None,
                          "new": any(word in line for word in ("新建", "新增", "新文件"))})
    return paths


def check_line_range(total: int, start: int | None, end: int | None) -> bool:
    if start is None:
        return end is None
    return end is not None and 1 <= start <= end <= total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="核对规格中的仓库相对路径与行号，不修改仓库", allow_abbrev=False)
    parser.add_argument("规格", type=Path, help="规格文件路径")
    parser.add_argument("--repo", required=True, type=Path, help="仓库路径")
    parser.add_argument("--base", default="HEAD", help="基线提交，默认 HEAD")
    args = parser.parse_args(argv)
    git = ["git", "-C", str(args.repo), "-c", "core.quotePath=false"]
    try:
        # 先固定为提交号，避免逐条核验时 HEAD 变化，也避免无效基线被误报为路径缺失。
        base = run_command([*git, "rev-parse", "--verify", "--end-of-options", f"{args.base}^{{commit}}"], 30).strip()
        paths = extract_spec_paths(args.规格.read_text(encoding="utf-8-sig"))
        errors = 0
        skipped = 0
        for item in paths:
            label = f"规格第 {item['line']} 行 {item['path']}"
            if item["start"] is not None:
                label += f":{item['start']}" + (f"-{item['end']}" if item["start"] != item["end"] else "")
            if item["new"]:
                skipped += 1
                print(f"{label}：新文件，跳过")
                continue
            target = f"{base}:{item['path']}"
            try:
                run_command([*git, "cat-file", "-e", target], 30)
            except CommandFailure as exc:
                # cat-file -e 对缺失对象返回 1 或 128；其他执行错误交给入口报告。
                if not str(exc).endswith(("退出码 1", "退出码 128")):
                    raise
                errors += 1
                print(f"{label}：基线缺失")
                continue
            if item["start"] is not None:
                content = run_command([*git, "cat-file", "-p", target], 30)
                total = len(content.splitlines())
                if not check_line_range(total, item["start"], item["end"]):
                    errors += 1
                    print(f"{label}：行号越界或范围无效（文件共 {total} 行）")
                    continue
            print(f"{label}：存在，行号有效" if item["start"] is not None else f"{label}：存在")
        print(f"汇总：{len(paths)} 条路径，{len(paths) - skipped - errors} 条正常，{skipped} 条新文件跳过，{errors} 条异常")
        return 1 if errors else 0
    except (OSError, ValueError, CommandFailure, subprocess.SubprocessError) as exc:
        message = str(exc) if isinstance(exc, CommandFailure) else "无法读取规格或基线，请核对参数"
        print(f"规格路径检查错误：{message}")
        return 2


if __name__ == "__main__":
    sys.exit(run_cli(lambda: main()))
