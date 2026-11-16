"""双宿主安装器：所有写入限定在临时家目录。"""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import re
import tempfile
import tomllib
import unittest
from unittest.mock import patch

根 = Path(__file__).resolve().parents[1]
规格 = importlib.util.spec_from_file_location("安装", 根 / "安装.py")
assert 规格 is not None and 规格.loader is not None
安装 = importlib.util.module_from_spec(规格)
规格.loader.exec_module(安装)

只读前言 = "本角色只读：不修改、新建或删除任何文件，不改任何配置，不 commit、不 push；结论直接写在回复里交回。主会话会在派你前后各看一次 git status，出现新改动会当作违规上报。"
可写前言 = "只写任务明确指定的输出路径，不改别的文件和配置，不 commit、不 push。"
外部前言 = "不调用会写入外部系统的工具（上传或更新文档、生成图片、往远程机器上传文件）。"


def 快照(根):
    return {str(p.relative_to(根)): ("目录" if p.is_dir() else p.read_bytes()) for p in 根.rglob("*")}


class 安装测试(unittest.TestCase):
    def setUp(self):
        self.临时 = tempfile.TemporaryDirectory()
        self.addCleanup(self.临时.cleanup)
        self.家 = Path(self.临时.name) / "家"
        self.家.mkdir()
        self.环境 = patch.dict(os.environ, {}, clear=False)
        self.环境.start()
        self.addCleanup(self.环境.stop)
        for 键 in ("CODEX_HOME", "DISPATCH_CONFIG_DIR"):
            os.environ.pop(键, None)
        self.命令 = patch.object(安装.shutil, "which", return_value=None)
        self.命令.start()
        self.addCleanup(self.命令.stop)

    def 跑(self, *参数, 用家目录=True):
        输出 = io.StringIO()
        with patch.object(Path, "home", return_value=self.家), contextlib.redirect_stdout(输出):
            码 = 安装.main([*(["--家目录", str(self.家)] if 用家目录 else []), *参数])
        return 码, 输出.getvalue()

    def 核对codex(self):
        self.assertEqual({p.name for p in (self.家 / ".agents/skills").iterdir()}, set(安装.技能列表))
        self.assertIn("allow_implicit_invocation: false", (self.家 / ".agents/skills/dispatch-retro/agents/openai.yaml").read_text(encoding="utf-8"))
        文件们 = list((self.家 / ".codex/agents").glob("*.toml"))
        self.assertEqual(len(文件们), 7)
        for 文件 in 文件们:
            数据 = tomllib.loads(文件.read_text(encoding="utf-8"))
            self.assertEqual(set(数据), {"name", "description", "developer_instructions"})
            self.assertEqual(数据["name"], 文件.stem)
            原文 = (根 / "agents" / f"{文件.stem}.md").read_text(encoding="utf-8")
            # 独立用正则移除整个 frontmatter 和正文首部空行，避免复制生成器的切分算法。
            正文 = re.sub(r"\A---\r?\n.*?^---(?:\r?\n|\Z)", "", 原文, count=1, flags=re.S | re.M)
            正文 = re.sub(r"\A(?:[ \t]*\r?\n)*", "", 正文)
            前言 = 可写前言 if 文件.stem in ("writer", "extractor") else 只读前言
            self.assertEqual(数据["developer_instructions"], 前言 + 外部前言 + "\n\n" + 正文)
            self.assertEqual(数据["developer_instructions"].split("\n\n", 1)[1], 正文)
        self.assertEqual((self.家 / ".dispatch/config.toml").read_bytes(), (根 / "skills/dispatch/模板/个人配置-Codex默认.toml").read_bytes())

    def test_codex完整安装及正文还原(self):
        self.assertEqual(self.跑("--宿主", "codex")[0], 0)
        self.核对codex()
        self.assertFalse((self.家 / ".claude").exists())

    def test_claude结果保持及干净家目录不建其他目录(self):
        self.assertEqual(self.跑("--宿主", "claude-code")[0], 0)
        for 文件 in (根 / "agents").glob("*.md"):
            self.assertEqual((self.家 / ".claude/agents" / 文件.name).read_bytes(), 文件.read_bytes())
        for 名 in 安装.技能列表:
            self.assertTrue(安装.目录相同(根 / "skills" / 名, self.家 / ".claude/skills" / 名))
        for 名 in (".codex", ".agents", ".dispatch"):
            self.assertFalse((self.家 / 名).exists())

    def test_旧配置提示与备份不挤掉旧配置定位(self):
        旧 = self.家 / ".claude/派活配置"
        旧.mkdir(parents=True)
        (旧 / "保留.txt").write_text("保留", encoding="utf-8")
        self.跑("--宿主", "claude-code")
        目标 = self.家 / ".claude/skills/dispatch/旧.txt"
        目标.write_text("旧技能", encoding="utf-8")
        原配置 = 快照(旧)
        码, 输出 = self.跑("--宿主", "claude-code")
        self.assertEqual(码, 0)
        self.assertIn("把整个目录挪过去", 输出)
        self.assertEqual(快照(旧), 原配置)
        self.assertTrue(list((self.家 / ".dispatch/备份").rglob("旧.txt")))
        import sys
        with patch.object(sys, "path", [str(根 / "skills/dispatch/脚本"), *sys.path]):
            from 组合解析 import config_root
            with patch.object(Path, "home", return_value=self.家):
                self.assertEqual(config_root(根 / "skills/dispatch"), 旧)

    def test_both和旧参数等价(self):
        for 参数 in (("--宿主", "both"), ("--codex",)):
            with self.subTest(参数=参数):
                码, 输出 = self.跑(*参数)
                self.assertEqual(码, 0)
                self.核对codex()
                self.assertEqual(len(list((self.家 / ".claude/agents").glob("*.md"))), 7)
                if 参数 == ("--codex",):
                    self.assertIn("--codex 已改为 --宿主 both", 输出)

    def test_自动判断四种情况(self):
        for 工具, 预期 in (({"codex"}, "codex"), ({"claude"}, "claude-code"), ({"claude", "codex"}, "claude-code"), (set(), None)):
            with self.subTest(工具=工具), patch.object(安装.shutil, "which", side_effect=lambda 名: 名 if 名 in 工具 else None):
                # 演练避免前一次安装目录影响后一次检测。
                码, 输出 = self.跑("--演练")
                self.assertEqual(码, 0 if 预期 else 2)
                self.assertIn(f"宿主：{预期}" if 预期 else "--宿主", 输出)
                if len(工具) == 2:
                    self.assertIn("要同时给 Codex 装，加 --宿主 both", 输出)

    def test_桌面版目录也可自动判断(self):
        (self.家 / ".codex").mkdir()
        self.assertIn("宿主：codex", self.跑("--演练")[1])
        (self.家 / ".claude").mkdir()
        self.assertIn("宿主：claude-code", self.跑("--演练")[1])

    def test_演练文件树完全不变(self):
        for 宿主 in ("codex", "claude-code", "both"):
            with self.subTest(宿主=宿主):
                前 = 快照(self.家)
                self.assertEqual(self.跑("--宿主", 宿主, "--演练", "--覆盖agent")[0], 0)
                self.assertEqual(快照(self.家), 前)
        self.跑("--宿主", "both")
        (self.家 / ".codex/agents/reviewer.toml").write_text("旧角色", encoding="utf-8")
        前 = 快照(self.家)
        self.跑("--宿主", "both", "--演练", "--覆盖agent")
        self.assertEqual(快照(self.家), 前)

    def test_重复安装不改文件不建备份(self):
        self.跑("--宿主", "both")
        前 = 快照(self.家)
        码, 输出 = self.跑("--宿主", "both")
        self.assertEqual(码, 0)
        self.assertEqual(快照(self.家), 前)
        self.assertFalse((self.家 / ".dispatch/备份").exists())
        self.assertGreaterEqual(输出.count("已是最新，跳过"), 20)

    def test_角色保留覆盖备份和无关角色(self):
        目录 = self.家 / ".codex/agents"
        目录.mkdir(parents=True)
        for 名 in ("reviewer", "其他"):
            (目录 / f"{名}.toml").write_text("用户版本", encoding="utf-8")
        self.跑("--宿主", "codex")
        self.assertEqual((目录 / "reviewer.toml").read_text(encoding="utf-8"), "用户版本")
        self.跑("--宿主", "codex", "--覆盖agent")
        self.assertEqual((目录 / "其他.toml").read_text(encoding="utf-8"), "用户版本")
        self.assertEqual(tomllib.loads((目录 / "reviewer.toml").read_text(encoding="utf-8"))["name"], "reviewer")
        备份 = list((self.家 / ".dispatch/备份").rglob("reviewer.toml"))
        self.assertEqual(len(备份), 1)
        self.assertEqual(备份[0].read_text(encoding="utf-8"), "用户版本")

    def test_已有个人配置不生成默认值(self):
        for 条件 in ("旧目录", "环境", "新文件"):
            with self.subTest(条件=条件), tempfile.TemporaryDirectory() as 临时:
                self.家 = Path(临时)
                if 条件 == "旧目录":
                    (self.家 / ".claude/派活配置").mkdir(parents=True)
                elif 条件 == "环境":
                    os.environ["DISPATCH_CONFIG_DIR"] = str(self.家 / "自定义")
                else:
                    (self.家 / ".dispatch").mkdir()
                    (self.家 / ".dispatch/config.toml").write_text("用户", encoding="utf-8")
                self.跑("--宿主", "codex")
                if 条件 == "新文件":
                    self.assertEqual((self.家 / ".dispatch/config.toml").read_text(encoding="utf-8"), "用户")
                else:
                    self.assertFalse((self.家 / ".dispatch/config.toml").exists())
                os.environ.pop("DISPATCH_CONFIG_DIR", None)

    def test_已有新个人配置时双宿主提示实际位置并复盘(self):
        新目录 = self.家 / '.dispatch'
        新目录.mkdir()
        (新目录 / 'config.toml').write_text('用户配置', encoding='utf-8')
        (self.家 / '.claude').mkdir()
        (self.家 / '.claude/派活配置').symlink_to(新目录, target_is_directory=True)
        for 宿主 in ('claude-code', 'codex', 'both'):
            with self.subTest(宿主=宿主):
                码, 输出 = self.跑('--宿主', 宿主, '--演练')
                self.assertEqual(码, 0)
                self.assertIn(f'个人配置 {新目录} 已存在，没动它', 输出)
                self.assertNotIn(f'个人配置 {self.家 / ".claude/派活配置"} 已存在', 输出)
                self.assertIn('升级后可以', 输出)
                self.assertIn('dispatch-retro 复盘', 输出)
                self.assertNotIn('初始化引导', 输出)
                self.assertNotIn('按引导生成个人配置', 输出)

    def test_收尾个人配置位置优先环境其次新目录最后旧目录(self):
        新目录 = self.家 / '.dispatch'
        新目录.mkdir()
        (新目录 / 'config.toml').write_text('用户配置', encoding='utf-8')
        旧目录 = self.家 / '.claude/派活配置'
        旧目录.mkdir(parents=True)
        自定义 = self.家 / '自定义配置'
        os.environ['DISPATCH_CONFIG_DIR'] = str(自定义)
        self.assertIn(f'个人配置 {自定义} 已存在', self.跑('--宿主', 'both', '--演练')[1])
        os.environ.pop('DISPATCH_CONFIG_DIR')
        self.assertIn(f'个人配置 {新目录} 已存在', self.跑('--宿主', 'both', '--演练')[1])
        (新目录 / 'config.toml').unlink()
        输出 = self.跑('--宿主', 'both', '--演练')[1]
        self.assertIn(f'个人配置 {旧目录} 已存在', 输出)
        self.assertNotIn('初始化引导', 输出)

    def test_没有个人配置时保留初始化提示(self):
        for 宿主, 提示 in (('claude-code', '按引导生成个人配置'), ('codex', '走初始化引导')):
            with self.subTest(宿主=宿主):
                self.assertIn(提示, self.跑('--宿主', 宿主, '--演练')[1])

    def test_断链个人配置不创建链接目标(self):
        链接 = self.家 / ".dispatch/config.toml"
        链接.parent.mkdir()
        目标 = self.家 / ".codex/config.toml"
        目标.parent.mkdir()
        链接.symlink_to(目标)
        原链接 = 链接.readlink()
        码, 输出 = self.跑("--宿主", "codex")
        self.assertEqual(码, 0)
        self.assertFalse(目标.exists(), "不得顺着断链创建 Codex 全局配置")
        self.assertTrue(链接.is_symlink())
        self.assertEqual(链接.readlink(), 原链接)
        self.assertIn("config.toml 是链接，未改动", 输出)

    def test_个人配置链接保留已有目标(self):
        链接 = self.家 / ".dispatch/config.toml"
        链接.parent.mkdir()
        目标 = self.家 / ".codex/config.toml"
        目标.parent.mkdir()
        目标.write_bytes(b"host_config = 'keep'\n")
        链接.symlink_to(目标)
        原链接 = 链接.readlink()
        码, 输出 = self.跑("--宿主", "codex")
        self.assertEqual(码, 0)
        self.assertEqual(目标.read_bytes(), b"host_config = 'keep'\n")
        self.assertTrue(链接.is_symlink())
        self.assertEqual(链接.readlink(), 原链接)
        self.assertIn("config.toml 是链接，未改动", 输出)

    def test_默认配置排他创建保留并发出现的文件(self):
        新文件 = self.家 / ".dispatch/config.toml"
        原建目录 = Path.mkdir

        def 建目录并写入(路径, *参数, **关键字):
            原建目录(路径, *参数, **关键字)
            if 路径 == 新文件.parent:
                新文件.write_bytes(b"created_by_other_process = true\n")

        with patch.object(Path, "mkdir", 建目录并写入):
            码, 输出 = self.跑("--宿主", "codex")
        self.assertEqual(码, 0)
        self.assertEqual(新文件.read_bytes(), b"created_by_other_process = true\n")
        self.assertIn("保留现有配置", 输出)
        self.assertNotIn("默认个人配置写在", 输出)

    def test_CODEX_HOME安装和检测与家目录覆盖(self):
        自定义 = Path(self.临时.name) / "Codex家"
        自定义.mkdir()
        os.environ["CODEX_HOME"] = str(自定义)
        self.assertIn("宿主：codex", self.跑("--演练", 用家目录=False)[1])
        self.跑("--宿主", "codex", 用家目录=False)
        self.assertEqual(len(list((自定义 / "agents").glob("*.toml"))), 7)
        self.assertFalse((self.家 / ".codex").exists())
        self.跑("--宿主", "codex")
        self.assertEqual(len(list((self.家 / ".codex/agents").glob("*.toml"))), 7)

    def test_假仓库特殊正文转义(self):
        假 = Path(self.临时.name) / "假仓库"
        (假 / "agents").mkdir(parents=True)
        正文 = '  正文 """ 和 ' + "'''" + ' 路径 C:\\目录\\文件\n末尾\n'
        (假 / "agents/假角色.md").write_text("---\nname: 假角色\ndescription: 说明: 值（含：冒号）\ndisallowedTools: Edit, Write\n---\n\n \t\n" + 正文, encoding="utf-8")
        for 名 in 安装.技能列表:
            (假 / "skills" / 名).mkdir(parents=True)
        (假 / "skills/dispatch/模板").mkdir()
        (假 / "skills/dispatch/模板/个人配置-Codex默认.toml").write_text("version = 1\n", encoding="utf-8")
        with patch.object(安装, "仓库", 假):
            self.跑("--宿主", "codex")
        数据 = tomllib.loads((self.家 / ".codex/agents/假角色.toml").read_text(encoding="utf-8"))
        self.assertEqual(数据["developer_instructions"], 只读前言 + "\n\n" + 正文)
        self.assertEqual(数据["description"], "说明: 值（含：冒号）")


if __name__ == "__main__":
    unittest.main()
