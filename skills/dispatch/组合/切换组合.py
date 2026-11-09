#!/usr/bin/env python3
"""切换派活组合：逐行执行组合文件里的 magpie 命令，任何一行失败就停下并报错。

Windows：python  "%USERPROFILE%\\.claude\\skills\\dispatch\\组合\\切换组合.py" Codex
WSL：    python3 ~/.claude/skills/dispatch/组合/切换组合.py Codex

组合文件是 <组合名>.txt，每行一条 magpie 命令，# 开头是注释。先找个人配置目录（环境变量
DISPATCH_CONFIG_DIR，默认 ~/.claude/派活配置）下的 组合/，找不到再用本脚本同目录的示例组合；
当前组合.txt 写回个人配置的 组合/（没有个人配置目录时写在本脚本同目录）。
WSL 里没有 Linux 版 magpie 时，自动改用 Windows 的 magpie.exe（经 WSL 互通调用）。
"""
import glob
import os
import shlex
import shutil
import subprocess
import sys

目录 = os.path.dirname(os.path.abspath(__file__))


def 个人组合目录():
    根 = os.path.expanduser(os.environ.get("DISPATCH_CONFIG_DIR") or os.path.join("~", ".claude", "派活配置"))
    return os.path.join(根, "组合") if os.path.isdir(根) else None


def 候选目录():
    return [d for d in (个人组合目录(), 目录) if d and os.path.isdir(d)]


def 找magpie():
    for name in ("magpie", "magpie.exe"):
        p = shutil.which(name)
        if p:
            return p
    候选 = glob.glob("/mnt/c/Users/*/AppData/Local/Programs/magpie/magpie.exe")
    if 候选:
        return 候选[0]
    sys.exit("找不到 magpie：Windows 上应在 PATH 里；WSL 里应能看到 /mnt/c/Users/<用户>/AppData/Local/Programs/magpie/magpie.exe")


def main():
    if len(sys.argv) != 2:
        可选 = sorted({os.path.splitext(f)[0] for d in 候选目录() for f in os.listdir(d) if f.endswith(".txt") and f != "当前组合.txt"})
        sys.exit(f"用法：切换组合.py <组合名>，可选：{'、'.join(可选)}")
    组合名 = sys.argv[1]
    文件 = next((os.path.join(d, f"{组合名}.txt") for d in 候选目录() if os.path.isfile(os.path.join(d, f"{组合名}.txt"))), None)
    if not 文件:
        sys.exit(f"找不到组合文件 {组合名}.txt，找过：{'、'.join(候选目录())}")
    magpie = 找magpie()
    with open(文件, encoding="utf-8-sig") as f:
        行列表 = f.read().splitlines()
    for 行号, 行 in enumerate(行列表, 1):
        命令 = 行.strip()
        if not 命令 or 命令.startswith("#"):
            continue
        参数 = shlex.split(命令)
        if 参数[0] != "magpie":
            sys.exit(f"第 {行号} 行不是 magpie 命令，已停止：{命令}")
        r = subprocess.run([magpie, *参数[1:]], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if r.returncode != 0:
            sys.exit(f"第 {行号} 行执行失败（退出码 {r.returncode}）：{命令}\n{r.stderr.strip()}")
    写入目录 = 个人组合目录() or 目录
    os.makedirs(写入目录, exist_ok=True)
    with open(os.path.join(写入目录, "当前组合.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write(组合名 + "\n")
    print(f"已切换到：{组合名}")
    subprocess.run([magpie, "groups"])


if __name__ == "__main__":
    main()
