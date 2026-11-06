#!/usr/bin/env python3
"""切换派活组合：逐行执行组合文件里的 magpie 命令，任何一行失败就停下并报错。

Windows：python  "%USERPROFILE%\\.claude\\skills\\dispatch\\组合\\切换组合.py" Codex
WSL：    python3 ~/.claude/skills/dispatch/组合/切换组合.py Codex

组合文件就是同目录下的 <组合名>.txt，每行一条 magpie 命令，# 开头是注释。
WSL 里没有 Linux 版 magpie 时，自动改用 Windows 的 magpie.exe（经 WSL 互通调用）。
"""
import glob
import os
import shlex
import shutil
import subprocess
import sys

目录 = os.path.dirname(os.path.abspath(__file__))


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
        可选 = sorted(os.path.splitext(f)[0] for f in os.listdir(目录) if f.endswith(".txt") and f != "当前组合.txt")
        sys.exit(f"用法：切换组合.py <组合名>，可选：{'、'.join(可选)}")
    组合名 = sys.argv[1]
    文件 = os.path.join(目录, f"{组合名}.txt")
    if not os.path.isfile(文件):
        sys.exit(f"找不到组合文件：{文件}")
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
    with open(os.path.join(目录, "当前组合.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write(组合名 + "\n")
    print(f"已切换到：{组合名}")
    subprocess.run([magpie, "groups"])


if __name__ == "__main__":
    main()
