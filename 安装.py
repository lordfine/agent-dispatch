#!/usr/bin/env python3
"""安装或升级 agent-dispatch，支持 Claude Code、Codex 和双宿主。

--宿主 claude-code|codex|both 可显式选择；不传时按命令和宿主目录检测。
--codex 兼容旧用法，等同 --宿主 both；--演练 只打印，--覆盖agent 先备份再覆盖。
所有旧文件统一备份到 ~/.dispatch/备份/<时间>-安装前/，不改宿主全局配置。
"""
import argparse
import filecmp
import json
import os
import shutil
import sys
import time
from pathlib import Path

仓库 = Path(__file__).resolve().parent
技能列表 = ["dispatch", "dispatch-retro", "dispatch-feedback"]
忽略 = shutil.ignore_patterns("__pycache__", "*.pyc", "本机", "当前组合.txt")


def Codex家目录(家目录: Path, 指定家目录: bool = False) -> Path:
    return Path(os.environ["CODEX_HOME"]).expanduser() if not 指定家目录 and os.environ.get("CODEX_HOME") else 家目录 / ".codex"


def 层级判断(已有配置: bool = False):
    有 = {名: shutil.which(名) is not None for 名 in ("claude", "codex", "orca", "magpie")}
    print("检测到的工具：" + "、".join(f"{名}{'✓' if 在 else '✗'}" for 名, 在 in 有.items()))
    层 = "底座层" if 有["orca"] and 有["magpie"] else "基础层"
    确认处 = "个人配置" if 已有配置 else "初始化引导"
    print(f"  按现有工具能用到{层}；WSL 或远程执行机、多模型组合在{确认处}里再确认。")
    return 有


def 生成角色toml(文本: str) -> str:
    """读取简单 frontmatter，去掉正文首部空行，其余正文逐字保留并转义。"""
    行 = 文本.splitlines(keepends=True)
    if not 行 or 行[0].strip() != "---":
        raise ValueError("角色文件缺少 frontmatter")
    结束 = next((i for i in range(1, len(行)) if 行[i].strip() == "---"), None)
    if 结束 is None:
        raise ValueError("角色文件 frontmatter 没有结束标记")
    元数据: dict[str, str] = {}
    for 项 in 行[1:结束]:
        if ":" in 项:
            键, 值 = 项.split(":", 1)
            元数据[键.strip()] = 值.strip()
    禁用 = {项.strip() for 项 in 元数据.get("disallowedTools", "").split(",")}
    if {"Edit", "Write"} <= 禁用:
        前言 = "本角色只读：不修改、新建或删除任何文件，不改任何配置，不 commit、不 push；结论直接写在回复里交回。主会话会在派你前后各看一次 git status，出现新改动会当作违规上报。"
    else:
        前言 = "只写任务明确指定的输出路径，不改别的文件和配置，不 commit、不 push。"
    if any(项.startswith("mcp__") for 项 in 禁用):
        前言 += "不调用会写入外部系统的工具（上传或更新文档、生成图片、往远程机器上传文件）。"
    开始 = 结束 + 1
    while 开始 < len(行) and not 行[开始].strip():
        开始 += 1
    正文 = "".join(行[开始:])
    数据 = {"name": 元数据["name"], "description": 元数据["description"], "developer_instructions": 前言 + "\n\n" + 正文}
    return "".join(f"{键} = {json.dumps(值, ensure_ascii=False)}\n" for 键, 值 in 数据.items())


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
        self.备份根 = 家目录 / ".dispatch" / "备份" / f"{time.strftime('%Y%m%d-%H%M%S')}-安装前"
        # 同一秒内再次升级也不能覆盖上一批备份；没有备份动作时不创建目录。
        序号 = 1
        while self.备份根.exists():
            self.备份根 = 家目录 / ".dispatch" / "备份" / f"{time.strftime('%Y%m%d-%H%M%S')}-{序号}-安装前"
            序号 += 1
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
                print(f"  agents/{源.name} 已是最新，跳过")
                continue
            if 目标.exists() and not 覆盖:
                print(f"  agents/{源.name} 已存在且内容不同，保留你的版本（要覆盖加 --覆盖agent，旧的会备份）")
                continue
            if 目标.exists() or 目标.is_symlink():
                self.备份(目标, f".claude/agents/{源.name}")
            self.做(f"安装 agents/{源.name}", lambda 源=源, 目标=目标: (目标根.mkdir(parents=True, exist_ok=True), shutil.copy2(源, 目标)))

    def 装Codex角色(self, 目标根: Path, 覆盖: bool):
        for 源 in sorted((仓库 / "agents").glob("*.md")):
            内容 = 生成角色toml(源.read_text(encoding="utf-8"))
            目标 = 目标根 / f"{源.stem}.toml"
            if 目标.exists() and 目标.read_text(encoding="utf-8") == 内容:
                print(f"  Codex agents/{目标.name} 已是最新，跳过")
                continue
            if 目标.exists() and not 覆盖:
                print(f"  Codex agents/{目标.name} 已存在且内容不同，保留你的版本（要覆盖加 --覆盖agent，旧的会备份）")
                continue
            if 目标.exists() or 目标.is_symlink():
                self.备份(目标, f".codex/agents/{目标.name}")
            self.做(f"安装 Codex agents/{目标.name}", lambda 目标=目标, 内容=内容: (目标根.mkdir(parents=True, exist_ok=True), 目标.write_text(内容, encoding="utf-8", newline="\n")))

    def 装默认配置(self) -> Path | None:
        新文件 = self.家目录 / ".dispatch" / "config.toml"
        旧目录 = self.家目录 / ".claude" / "派活配置"
        环境 = os.environ.get("DISPATCH_CONFIG_DIR")
        if 新文件.is_symlink():
            print(f"个人配置 {新文件} 是链接，未改动。")
            return None
        if 环境 or 新文件.exists() or 旧目录.is_dir():
            位置 = Path(环境).expanduser() if 环境 else (新文件 if 新文件.exists() else 旧目录)
            print(f"个人配置在 {位置}，保留现有配置。")
            return None
        模板 = 仓库 / "skills" / "dispatch" / "模板" / "个人配置-Codex默认.toml"
        if self.演练:
            self.做(f"写入默认个人配置 {新文件}")
            return 新文件
        内容 = 模板.read_bytes()
        新文件.parent.mkdir(parents=True, exist_ok=True)
        try:
            with 新文件.open("xb") as 文件:
                文件.write(内容)
        except FileExistsError:
            if 新文件.is_symlink():
                print(f"个人配置 {新文件} 是链接，未改动。")
            else:
                print(f"个人配置在 {新文件}，保留现有配置。")
            return None
        self.做(f"写入默认个人配置 {新文件}")
        return 新文件


def main(argv=None):
    p = argparse.ArgumentParser(description="安装或升级 agent-dispatch", allow_abbrev=False)
    p.add_argument("--宿主", choices=("claude-code", "codex", "both"), help="选择安装宿主，默认自动检测")
    p.add_argument("--演练", action="store_true", help="只打印要做什么，不改文件")
    p.add_argument("--codex", action="store_true", help="兼容旧参数，等同 --宿主 both")
    p.add_argument("--覆盖agent", action="store_true", help="覆盖内容不同的已有 agent（先备份）")
    p.add_argument("--家目录", help="装到别的家目录（测试用），忽略 CODEX_HOME")
    参数 = p.parse_args(argv)
    家目录 = Path(参数.家目录).expanduser() if 参数.家目录 else Path.home()
    Codex家 = Codex家目录(家目录, 参数.家目录 is not None)
    环境配置 = os.environ.get("DISPATCH_CONFIG_DIR")
    新目录 = 家目录 / ".dispatch"
    旧目录 = 家目录 / ".claude" / "派活配置"
    个人配置 = (Path(环境配置).expanduser() if 环境配置 else
                新目录 if (新目录 / "config.toml").is_file() else
                旧目录 if 旧目录.is_dir() else None)
    print(f"agent-dispatch 安装：{仓库} → {家目录}")
    有 = 层级判断(个人配置 is not None)
    宿主 = 参数.宿主
    if 参数.codex:
        print("--codex 已改为 --宿主 both")
        宿主 = "both"
    if 宿主 is None:
        有Claude = 有["claude"] or (家目录 / ".claude").exists()
        有Codex = 有["codex"] or Codex家.exists()
        if not 有Claude and not 有Codex:
            print("没有检测到 Claude Code 或 Codex，请用 --宿主 claude-code|codex|both 指定。")
            return 2
        宿主 = "claude-code" if 有Claude else "codex"
        if 有Claude and 有Codex:
            print("要同时给 Codex 装，加 --宿主 both")
    print(f"宿主：{宿主}")
    装 = 安装器(家目录, 参数.演练)
    print("技能和角色：")
    if 宿主 in ("claude-code", "both"):
        装.装技能(家目录 / ".claude" / "skills", ".claude/skills")
        装.装agent(参数.覆盖agent)
    默认配置 = None
    if 宿主 in ("codex", "both"):
        装.装技能(家目录 / ".agents" / "skills", ".agents/skills")
        装.装Codex角色(Codex家 / "agents", 参数.覆盖agent)
        默认配置 = 装.装默认配置()
    print()
    if not 装.记录:
        print("没有需要改动的文件。")
    elif 参数.演练:
        print("以上是演练，没有改任何文件。")
    else:
        print("完成。" + (f"被替换的旧文件在 {装.备份根}" if 装.备份根.exists() else ""))
    if 宿主 in ("claude-code", "both"):
        if 个人配置 is not None:
            print(f"个人配置 {个人配置} 已存在，没动它。升级后可以输入 /dispatch-retro 复盘，看看要不要调整。")
            print(f"全局规则片段 {仓库 / 'CLAUDE.md片段.md'} 可以加进 ~/.claude/CLAUDE.md。")
        else:
            print("下一步：新开一个 Claude Code 会话，输入 /dispatch-retro，按引导生成个人配置。")
            print(f"全局规则片段在 {仓库 / 'CLAUDE.md片段.md'}，初始化引导会问你要不要加进 ~/.claude/CLAUDE.md。")
    if 宿主 in ("codex", "both"):
        if 个人配置 is not None:
            print(f"个人配置 {个人配置} 已存在，没动它。升级后可以输入 $dispatch-retro 复盘，看看要不要调整。")
        else:
            print("下一步：新开 Codex 会话，输入 $dispatch-retro 走初始化引导。")
        if 默认配置:
            print(f"默认个人配置{'将写在' if 参数.演练 else '写在'} {默认配置}")
        print(f"全局规则片段 {仓库 / 'AGENTS.md片段.md'} 可以加进 {Codex家 / 'AGENTS.md'}。")
    if 个人配置 == 旧目录:
        print(f"个人配置的新位置是 {家目录 / '.dispatch'}，旧位置照样能用；要搬就把整个目录挪过去并在里面放一份 config.toml。")
    return 0


if __name__ == "__main__":
    for 流 in (sys.stdout, sys.stderr):
        重设编码 = getattr(流, "reconfigure", None)
        if 重设编码 is not None:
            重设编码(encoding="utf-8", errors="replace")
    sys.exit(main())
