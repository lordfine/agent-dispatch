#!/usr/bin/env python3
"""安装或升级 agent-dispatch：把 skills/ 和 agents/ 拷到 Claude Code（可选 Codex）的目录里。

用法：
  python 安装.py            安装或升级（Windows 也用 python；Linux/macOS/WSL 用 python3）
  python 安装.py --演练     只打印要做什么，不改任何文件
  python 安装.py --codex    另外把 skills 装到 ~/.agents/skills，给 Codex 用
  python 安装.py --覆盖agent  已存在且内容不同的 agent 也覆盖（旧的先备份）

只动框架文件：~/.claude/skills/ 下的 dispatch、dispatch-retro、dispatch-feedback 三个目录，
~/.claude/agents/ 下的 7 个 agent。被替换的旧文件先备份到 ~/.claude/备份/<时间>-安装前/。
个人配置 ~/.claude/派活配置/ 和 ~/.claude/CLAUDE.md 不碰；装完新开会话说「初始化派活」，按引导生成个人配置。
"""
import argparse
import filecmp
import shutil
import sys
import time
from pathlib import Path

仓库 = Path(__file__).resolve().parent
技能列表 = ["dispatch", "dispatch-retro", "dispatch-feedback"]
忽略 = shutil.ignore_patterns("__pycache__", "*.pyc", "本机", "当前组合.txt")


def 层级判断():
    有 = {名: shutil.which(名) is not None for 名 in ("claude", "codex", "orca", "magpie")}
    print("检测到的工具：" + "、".join(f"{名}{'✓' if 在 else '✗'}" for 名, 在 in 有.items()))
    if not (有["claude"] or 有["codex"]):
        print("  没找到 claude 或 codex 命令。框架是给它们用的，请先装一个（也可能只是不在 PATH 里）。")
    层 = "底座层" if 有["orca"] and 有["magpie"] else "基础层"
    print(f"  按现有工具能用到{层}；WSL 或远程执行机、多模型组合在初始化引导里再确认。")


def 目录相同(甲: Path, 乙: Path) -> bool:
    比较 = filecmp.dircmp(甲, 乙, ignore=["__pycache__", "本机", "当前组合.txt"])

    def 递归(c):
        if c.left_only or c.right_only or c.diff_files or c.funny_files:
            return False
        return all(递归(子) for 子 in c.subdirs.values())

    return 递归(比较)


class 安装器:
    def __init__(self, 家目录: Path, 演练: bool):
        self.家目录 = 家目录
        self.演练 = 演练
        self.备份根 = 家目录 / ".claude" / "备份" / f"{time.strftime('%Y%m%d-%H%M%S')}-安装前"
        self.记录: list[str] = []

    def 做(self, 说明: str, 动作=None):
        self.记录.append(说明)
        print(("  [演练] " if self.演练 else "  ") + 说明)
        if not self.演练 and 动作:
            动作()

    def 备份(self, 原: Path, 相对: str):
        目标 = self.备份根 / 相对
        def 动作():
            目标.parent.mkdir(parents=True, exist_ok=True)
            if 原.is_symlink():
                目标.with_name(目标.name + ".链接.txt").write_text(str(原.readlink()), encoding="utf-8")
                原.unlink()
            else:
                shutil.move(str(原), str(目标))
        self.做(f"备份 {原} → {目标}", 动作)

    def 装技能(self, 目标根: Path, 标签: str):
        for 名 in 技能列表:
            源 = 仓库 / "skills" / 名
            目标 = 目标根 / 名
            if not 源.is_dir():
                sys.exit(f"仓库里缺 skills/{名}，安装中止")
            if 目标.exists() and not 目标.is_symlink() and 目录相同(源, 目标):
                print(f"  {标签}/{名} 已是最新，跳过")
                continue
            if 目标.exists() or 目标.is_symlink():
                self.备份(目标, f"{标签}/{名}")
            self.做(f"安装 {标签}/{名}", lambda 源=源, 目标=目标: (目标.parent.mkdir(parents=True, exist_ok=True), shutil.copytree(源, 目标, ignore=忽略)))

    def 装agent(self, 覆盖: bool):
        目标根 = self.家目录 / ".claude" / "agents"
        for 源 in sorted((仓库 / "agents").glob("*.md")):
            目标 = 目标根 / 源.name
            if 目标.exists() and filecmp.cmp(源, 目标, shallow=False):
                continue
            if 目标.exists() and not 覆盖:
                print(f"  agents/{源.name} 已存在且内容不同，保留你的版本（要覆盖加 --覆盖agent，旧的会备份）")
                continue
            if 目标.exists():
                def 动作(目标=目标):
                    备份 = self.备份根 / "agents" / 目标.name
                    备份.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(目标, 备份)
                self.做(f"备份 {目标}", 动作)
            self.做(f"安装 agents/{源.name}", lambda 源=源, 目标=目标: (目标根.mkdir(parents=True, exist_ok=True), shutil.copy2(源, 目标)))


def main():
    p = argparse.ArgumentParser(description="安装或升级 agent-dispatch", allow_abbrev=False)
    p.add_argument("--演练", action="store_true", help="只打印要做什么，不改文件")
    p.add_argument("--codex", action="store_true", help="另外装到 ~/.agents/skills 给 Codex 用")
    p.add_argument("--覆盖agent", action="store_true", help="覆盖内容不同的已有 agent（先备份）")
    p.add_argument("--家目录", help="装到别的家目录（测试用），默认当前用户家目录")
    参数 = p.parse_args()
    家目录 = Path(参数.家目录).expanduser() if 参数.家目录 else Path.home()

    print(f"agent-dispatch 安装：{仓库} → {家目录}")
    层级判断()
    装 = 安装器(家目录, 参数.演练)
    print("技能：")
    装.装技能(家目录 / ".claude" / "skills", ".claude/skills")
    if 参数.codex:
        装.装技能(家目录 / ".agents" / "skills", ".agents/skills")
    print("agent：")
    装.装agent(参数.覆盖agent)

    个人配置 = 家目录 / ".claude" / "派活配置"
    print()
    if not 装.记录:
        print("没有需要改动的文件。")
    elif 参数.演练:
        print("以上是演练，没有改任何文件。")
    else:
        print(f"完成。被替换的旧文件在 {装.备份根}")
    if 个人配置.is_dir():
        print(f"个人配置 {个人配置} 已存在，没动它。升级后可以说「复盘」看看要不要调整。")
    else:
        print("下一步：新开一个 Claude Code 会话，说「初始化派活」（或输入 /dispatch-retro），按引导生成个人配置。")
    print(f"全局规则片段在 {仓库 / 'CLAUDE.md片段.md'}，初始化引导会问你要不要加进 ~/.claude/CLAUDE.md。")


if __name__ == "__main__":
    main()
