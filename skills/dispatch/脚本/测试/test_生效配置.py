"""分层配置的读取、校验、来源和命令行输出。"""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from 生效配置 import load_effective_config, main, find_project_config, read_layer


class 生效配置测试(unittest.TestCase):
    def setUp(self):
        self.临时 = tempfile.TemporaryDirectory()
        self.addCleanup(self.临时.cleanup)
        self.根 = Path(self.临时.name)
        self.个人 = self.根 / "个人/config.toml"
        self.个人.parent.mkdir()
        self.项目 = self.根 / "项目"
        self.项目.mkdir()
        (self.项目 / ".git").mkdir()
        self.环境 = patch.dict(os.environ, {"DISPATCH_CONFIG_DIR": str(self.个人.parent)})
        self.环境.start()
        self.addCleanup(self.环境.stop)

    def 写(self, 文本, 项目=False):
        文件 = self.项目 / ".dispatch.toml" if 项目 else self.个人
        文件.write_text(文本, encoding="utf-8")
        return 文件

    def 读(self):
        return load_effective_config(self.项目)

    def test_项目按字段覆盖及来源(self):
        self.写('version=1\nexecutor="个人机"\n[roles.reviewer]\nmode="native"\nhost="codex"\nmodel="个人模型"\neffort="high"\n')
        self.写('version=1\nexecutor="项目机"\n[roles.reviewer]\neffort="xhigh"\n', True)
        结果 = self.读()
        self.assertEqual(结果["errors"], [])
        self.assertEqual(结果["roles"]["reviewer"]["model"], {"value": "个人模型", "source": "个人"})
        self.assertEqual(结果["roles"]["reviewer"]["effort"], {"value": "xhigh", "source": "项目"})
        self.assertEqual(结果["executor"], {"value": "项目机", "source": "项目"})
        self.assertEqual(结果["roles"]["writer"]["model"], {"value": None, "source": "未设置"})

    def test_部分字段与implementer只覆盖档位(self):
        self.写('version=1\n[roles.implementer]\nmode="orca"\n')
        self.写('version=1\n[roles.implementer]\neffort="high"\n[roles.writer]\nmodel="示例"\n', True)
        结果 = self.读()
        self.assertEqual(结果["errors"], [])
        self.assertEqual(结果["roles"]["writer"]["mode"]["source"], "未设置")
        self.assertEqual(结果["roles"]["implementer"]["mode"]["value"], "orca")
        self.assertEqual(结果["roles"]["implementer"]["host"]["source"], "未设置")

    def test_结构校验每类错误(self):
        用例 = [
            ('version=', 'TOML'), ('[roles]', 'version'), ('version=2', 'version'), ('version=true', 'version'),
            ('version="1"', 'version'), ('version=1\nmax_workers=7', 'max_workers'),
            ('version=1\n[roles.unknown]', 'roles.unknown'), ('version=1\n[roles.writer]\nmodle="x"', 'roles.writer.modle'),
            ('version=1\nexecutor=7', 'executor'), ('version=1\nroles="x"', 'roles'),
            ('version=1\n[roles]\nwriter="x"', 'roles.writer'),
            ('version=1\n[roles.writer]\nmode=7', 'roles.writer.mode'),
            ('version=1\n[roles.writer]\nmode="随便"', 'roles.writer.mode'),
            ('version=1\n[roles.writer]\nhost="其他"', 'roles.writer.host'),
            ('version=1\n[roles.writer]\neffort="low"', 'roles.writer.effort'),
        ]
        for 文本, 键 in 用例:
            with self.subTest(文本=文本):
                文件 = self.写(文本)
                数据, 错误 = read_layer(文件)
                self.assertIsNone(数据)
                self.assertTrue(any(键 in e and str(文件) in e for e in 错误), 错误)

    def test_全部字段的值类型都校验(self):
        for 字段 in ("mode", "host", "model", "effort"):
            for 值 in ('false', '[]', '{}', '3'):
                with self.subTest(字段=字段, 值=值):
                    self.写(f'version=1\n[roles.writer]\n{字段}={值}\n')
                    self.assertTrue(self.读()["errors"])

    def test_结构错误与合并后语义错误一次全部列出(self):
        self.写('version=1\nmax_workers=3\n[roles.reviewer]\nmodle="拼错"\n')
        self.写('version=1\n[roles.implementer]\nmode="native"\n[roles.reviewer]\nmode="native"\nhost="claude-code"\nmodel="不是别名"\n', True)
        错误 = self.读()["errors"]
        self.assertEqual(len(错误), 4, 错误)
        for 键 in ("max_workers", "modle", "roles.implementer.mode", "roles.reviewer.model"):
            self.assertTrue(any(键 in e for e in 错误), 错误)

    def test_只对合并结果查语义且坏文件整体不参与(self):
        self.写('version=1\n[roles.reviewer]\nmode="native"\nhost="claude-code"\nmodel="不是别名"\n')
        self.写('version=1\n[roles.reviewer]\nmode="orca"\n', True)
        self.assertEqual(self.读()["errors"], [])
        self.写('version=1\n"拼错"="x"\n[roles.writer]\nmodel="坏文件模型"\n', True)
        结果 = self.读()
        self.assertEqual(结果["roles"]["writer"]["model"]["source"], "未设置")
        self.assertEqual(len(结果["errors"]), 2)

    def test_Claude原生接受四别名以及不指定模型(self):
        for 模型 in (None, "opus", "sonnet", "haiku", "fable"):
            self.写('version=1\n[roles.reviewer]\nhost="claude-code"\nmode="native"\n' + (f'model="{模型}"\n' if 模型 else ''))
            self.assertEqual(self.读()["errors"], [])

    def test_缺省原生方式仍校验Claude模型且不填充来源(self):
        for 角色 in ("explorer", "researcher", "extractor", "reviewer", "diagnoser", "writer", "verifier"):
            with self.subTest(角色=角色):
                self.写(f'version=1\n[roles.{角色}]\nhost="claude-code"\nmodel="group/impl"\n')
                结果 = self.读()
                self.assertEqual(len(结果["errors"]), 1, 结果["errors"])
                self.assertIn(f"roles.{角色}.model", 结果["errors"][0])
                self.assertEqual(结果["roles"][角色]["mode"], {"value": None, "source": "未设置"})
        输出 = io.StringIO()
        with contextlib.redirect_stdout(输出):
            self.assertEqual(main(["--项目", str(self.项目), "--json"]), 2)
        数据 = json.loads(输出.getvalue())
        self.assertEqual(数据["roles"]["verifier"]["mode"], {"value": None, "source": "未设置"})

    def test_缺省方式下Claude别名和实现的Orca默认不受影响(self):
        for 模型 in (None, "opus", "sonnet", "haiku", "fable"):
            with self.subTest(模型=模型):
                self.写('version=1\n[roles.reviewer]\nhost="claude-code"\n' + (f'model="{模型}"\n' if 模型 else ''))
                self.assertEqual(self.读()["errors"], [])
        self.写('version=1\n[roles.implementer]\nhost="claude-code"\nmodel="group/impl"\n')
        self.写('version=1\n[roles.implementer]\neffort="high"\n', True)
        self.assertEqual(self.读()["errors"], [])
        self.assertEqual(self.读()["roles"]["implementer"]["mode"], {"value": None, "source": "未设置"})
        self.写('version=1\n[roles.implementer]\nmode="native"\n', True)
        错误 = self.读()["errors"]
        self.assertTrue(any("roles.implementer.mode" in 项 for 项 in 错误), 错误)

    def test_缺省方式也以合并结果校验且保留字段来源(self):
        self.写('version=1\n[roles.reviewer]\nmodel="group/impl"\n')
        self.写('version=1\n[roles.reviewer]\nhost="claude-code"\n', True)
        结果 = self.读()
        self.assertEqual(len(结果["errors"]), 1)
        self.assertIn(str(self.个人), 结果["errors"][0])
        self.assertEqual(结果["roles"]["reviewer"]["host"]["source"], "项目")
        self.写('version=1\n[roles.reviewer]\nhost="claude-code"\nmode="orca"\n', True)
        self.assertEqual(self.读()["errors"], [])

    def test_查找项目配置在git目录停含git文件(self):
        外层 = self.根 / ".dispatch.toml"
        外层.write_text("version=1", encoding="utf-8")
        子 = self.项目 / "甲/乙"
        子.mkdir(parents=True)
        self.assertIsNone(find_project_config(子))
        项目配置 = self.写("version=1", True)
        self.assertEqual(find_project_config(子), 项目配置.resolve())
        项目配置.unlink()
        (self.项目 / ".git").rmdir()
        (self.项目 / ".git").write_text("gitdir: 其他", encoding="utf-8")
        self.assertIsNone(find_project_config(子))
        (self.项目 / ".git").unlink()
        self.assertEqual(find_project_config(子), 外层.resolve())

    def test_json结构与无配置正常(self):
        输出 = io.StringIO()
        with contextlib.redirect_stdout(输出):
            self.assertEqual(main(["--项目", str(self.项目), "--json"]), 0)
        数据 = json.loads(输出.getvalue())
        self.assertEqual(set(数据), {"roles", "executor", "files", "errors"})
        self.assertEqual(len(数据["roles"]), 8)
        self.assertEqual(数据["files"], {"personal": None, "project": None})
        self.assertEqual(数据["executor"], {"value": None, "source": "未设置"})
        输出 = io.StringIO()
        with contextlib.redirect_stdout(输出):
            self.assertEqual(main(["--项目", str(self.项目)]), 0)
        for 文本 in ("原生子代理", "Orca", "主会话所在的宿主", "宿主默认", "由主会话每次决定", "未设置", "没有"):
            self.assertIn(文本, 输出.getvalue())

    def test_三种错误CLI一次列全退出二(self):
        self.写('version=2\nmax_workers=8\n[roles.reviewer]\neffort="low"\n')
        输出 = io.StringIO()
        with contextlib.redirect_stdout(输出):
            self.assertEqual(main(["--项目", str(self.项目)]), 2)
        for 键 in ("version", "max_workers", "roles.reviewer.effort"):
            self.assertIn(键, 输出.getvalue())

    def test_默认模板通过校验(self):
        模板 = Path(__file__).resolve().parents[2] / "模板/个人配置-Codex默认.toml"
        self.个人.write_bytes(模板.read_bytes())
        self.assertEqual(self.读()["errors"], [])
        self.assertEqual(self.读()["roles"]["implementer"]["mode"]["value"], "orca")


if __name__ == "__main__":
    unittest.main()
