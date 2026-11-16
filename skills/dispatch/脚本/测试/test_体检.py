"""体检的组合比对、额度规则和子项隔离测试。"""

import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from 组合解析 import parse_magpie_groups_output, parse_magpie_claude_output
from 体检 import check_extension, check_mcp, compare_combination, main, parse_orca_status


class 体检组合一致测试(unittest.TestCase):
    def test_只比对已声明推理档且缺失报告(self):
        self.assertEqual(compare_combination({}, {}, {}, {}, {"opus": "medium"}, {"opus": "medium", "haiku": "high"}), [])
        差异 = compare_combination({}, {}, {}, {}, {"opus": "medium", "fable": "high"}, {"opus": "high"})
        self.assertEqual(len(差异), 2)
        self.assertIn("推理档 opus", 差异[0]["message"])
        self.assertIn("缺失", 差异[1]["message"])

    def test_真实展示格式含别名路径后缀和重复effort(self):
        预期 = {"research": ["codex/主:high:fast", "来源乙/兜底:high"]}
        实际 = parse_magpie_groups_output(
            "  研究组  group/research  order · stays turn  codex/主:high:fast → 来源乙/兜底:high ← Claude Code\n"
            "  旧组 group/old order p/m\n"
        )
        档位 = parse_magpie_claude_output(
            "Claude Code group/research[1m] · opus group/research[1m] · subagents group/research[1m] · opus effort high"
        )
        self.assertEqual(compare_combination(预期, 实际, {"opus": "group/research", "subagent": "group/research"}, 档位), [])

    def test_成员顺序档位和fast变化必须报告(self):
        for 实际 in (["p/备:high", "p/主:high:fast"], ["p/主:medium:fast", "p/备:high"], ["p/主:high", "p/备:high"]):
            with self.subTest(实际=实际):
                差异 = compare_combination({"a": ["p/主:high:fast", "p/备:high"]}, {"a": 实际}, {}, {})
                self.assertTrue(any("a" in 项["message"] for 项 in 差异))

    def test_组或档位缺失均报告(self):
        self.assertEqual(len(compare_combination({"a": ["p/m"]}, {}, {"opus": "group/a"}, {})), 2)

    def test_所有成员必须解析且不得受ANSI影响(self):
        self.assertEqual(parse_magpie_groups_output("\x1b[32m名字 group/a order 来源/主:high → 来源/备:high\x1b[0m"), {"a": ["来源/主:high", "来源/备:high"]})


class 在跑worker测试(unittest.TestCase):
    def test_按任务状态计数远程无法核实存活的在跑也算(self):
        from 体检 import count_workers
        行 = [
            {"state": "ready", "liveness": "live", "terminalState": "active"},
            {"state": "succeeded", "liveness": "live", "terminalState": "retained"},
            *[{"state": "succeeded", "liveness": "exited", "terminalState": "retained"}] * 88,
            *[{"state": "stop_unknown", "liveness": "unverifiable", "terminalState": "retained"}] * 3,
            {"state": "ready", "liveness": "unverifiable", "terminalState": "retained"},
        ]
        self.assertEqual(count_workers(行), {"running": 2, "unknown": 0, "stop_unknown": 3, "exited": 88, "finished": 1})

    def test_达到上限异常且不确定项单列(self):
        from 体检 import count_workers, compare_worker_limit
        本机 = count_workers([{"state": "ready", "liveness": "live"}] * 4)
        远程 = {"示例远程": count_workers([{"state": "ready", "liveness": "live"}] * 3)}
        正常, 消息 = compare_worker_limit(本机, 远程, 7)
        self.assertFalse(正常)
        self.assertIn("已到上限，先别再派", 消息)
        正常, 消息 = compare_worker_limit(count_workers([{"liveness": "unverifiable"}]), {}, 7)
        self.assertTrue(正常)
        self.assertIn("状态不明 1", 消息)
        self.assertIn("本机账上 0（含从本机派到远程机的）/ 远程机自己账上 0，上限 7", 消息)

    def test_列表分页且本机不受当前会话绑定(self):
        import json
        from 体检 import check_workers
        页 = [
            {"ok": True, "result": {"scope": {"source": "all"}, "workers": [{"liveness": "live"}] * 2, "nextCursor": "下一页"}},
            {"ok": True, "result": {"scope": {"source": "all"}, "workers": [{"liveness": "live"}], "nextCursor": None}},
            {"ok": True, "result": {"scope": {"source": "all"}, "workers": [{"liveness": "live"}] * 4}},
        ]
        with patch("体检.run_command", side_effect=[json.dumps(项) for 项 in 页]) as 运行:
            正常, 消息 = check_workers({"远程环境": ["示例远程"], "worker上限": 7}, 30)
        self.assertFalse(正常)
        self.assertIn("本机账上 3（含从本机派到远程机的）/ 远程机自己账上 4，上限 7", 消息)
        self.assertIn("--cursor", 运行.call_args_list[1].args[0])
        self.assertIn("示例远程", 运行.call_args_list[2].args[0])
        self.assertEqual(运行.call_args_list[0].kwargs["environment_updates"], {"ORCA_AGENT_SESSION_ID": None})

    def test_配置worker上限默认七且拒绝非正整数(self):
        from 组合解析 import load_config
        with tempfile.TemporaryDirectory() as 临时目录:
            文件 = Path(临时目录) / "配置.json"
            文件.write_text("{}", encoding="utf-8")
            self.assertEqual(load_config(文件)["worker上限"], 7)
            for 值 in ("7", 0, -1, 1.5, True):
                import json
                文件.write_text(json.dumps({"worker上限": 值}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_config(文件)


class 额度规则测试(unittest.TestCase):
    def test_多账户取最大周用量不泄露账户(self):
        规则 = {"名称": "Codex 周额度", "命令": ["magpie", "accounts", "codex"], "正则": r"\b7d\s+(\d+(?:\.\d+)?)%", "比较": ">=", "阈值": 70}
        with patch("体检.run_command", return_value="甲@example.com 7d 19%\n乙@example.com 7d 70%"):
            正常, 消息 = check_extension(规则, 30)
        self.assertFalse(正常)
        self.assertIn("70", 消息)
        self.assertNotIn("example.com", 消息)

    def test_没有匹配到额度必须报错(self):
        规则 = {"名称": "额度", "命令": ["magpie", "accounts", "codex"], "正则": r"7d (\d+)%", "比较": ">=", "阈值": 70}
        with patch("体检.run_command", return_value="尚未加载额度"):
            with self.assertRaises(ValueError):
                check_extension(规则, 30)

    def test_mcp失败只列名称(self):
        输出 = "Checking MCP server health...\n正常: secret-command - ✓ Connected\n故障: https://不要输出 - ✗ Failed to connect\n"
        with patch("体检.run_command", return_value=输出):
            正常, 消息 = check_mcp(30)
        self.assertFalse(正常)
        self.assertIn("故障", 消息)
        self.assertNotIn("https", 消息)
        self.assertNotIn("secret", 消息)

    def test_错误的Orca应用字段应作为解析错误(self):
        with self.assertRaises(ValueError):
            parse_orca_status('{"ok": true, "result": {"runtime": {}, "app": null}}')

    def test_一项超时仍然执行其他项(self):
        with tempfile.TemporaryDirectory() as 临时目录:
            配置 = {"检查超时秒": 30, "远程环境": [], "扩展检查": []}
            with patch("体检.CONFIG_ROOT", Path(临时目录)), patch("体检.shutil.which", side_effect=lambda 命令: 命令), patch("体检.load_config", return_value=配置), patch("体检.load_current_combination", return_value={"groups": {}, "member_specs": {}, "claude": {}}), patch("体检.check_magpie", side_effect=TimeoutError("超时")), patch("体检.check_combination", return_value=(True, "一致")), patch("体检.check_orca", return_value=(True, "运行中")), patch("体检.check_workers", return_value=(True, "空闲")), patch("体检.check_mcp", return_value=(True, "正常")) as 后项, patch("体检.STATE_ROOT", Path(临时目录)), patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(main([]), 1)
                后项.assert_called_once()


class 基础档体检测试(unittest.TestCase):
    def test_已装命令运行失败仍异常且坏检查配置不被放过(self):
        from 组合解析 import CommandFailure
        with tempfile.TemporaryDirectory() as 临时:
            根 = Path(临时)
            with patch("体检.CONFIG_ROOT", 根), patch("体检.shutil.which", return_value="已安装"), patch("体检.run_command", side_effect=CommandFailure("命令失败，退出码 9")), patch("体检.Path.cwd", return_value=根), patch("sys.stdout", new_callable=io.StringIO) as 输出:
                self.assertEqual(main([]), 1)
                self.assertIn("命令失败，退出码 9", 输出.getvalue())
                self.assertNotIn("跳过（没装", 输出.getvalue())
            (根 / "检查配置.json").write_text("损坏的 JSON", encoding="utf-8")
            with patch("体检.CONFIG_ROOT", 根), patch("体检.run_command") as 命令, patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(main([]), 2)
                命令.assert_not_called()

    def test_远程与扩展依赖缺命令也跳过(self):
        import json
        with tempfile.TemporaryDirectory() as 临时:
            根 = Path(临时)
            (根 / "检查配置.json").write_text(json.dumps({"远程环境": ["测试机"], "扩展检查": [{"名称": "额度", "命令": ["magpie", "accounts"], "正则": "(\\d+)", "阈值": 70}]}), encoding="utf-8")
            with patch("体检.CONFIG_ROOT", 根), patch("体检.shutil.which", return_value=None), patch("体检.Path.cwd", return_value=根), patch("体检.run_command") as 命令, patch("sys.stdout", new_callable=io.StringIO) as 输出:
                self.assertEqual(main([]), 0)
                命令.assert_not_called()
                self.assertIn("远程 测试机：跳过（没装 orca）", 输出.getvalue())
                self.assertIn("额度：跳过（没装 magpie）", 输出.getvalue())

    def test_缺检查配置和工具仍正常且全跳过(self):
        with tempfile.TemporaryDirectory() as 临时:
            根 = Path(临时)
            with patch("体检.CONFIG_ROOT", 根), patch("体检.shutil.which", return_value=None), patch.dict("os.environ", {"DISPATCH_CONFIG_DIR": str(根)}), patch("体检.Path.cwd", return_value=根), patch("体检.run_command") as 命令, patch("sys.stdout", new_callable=io.StringIO) as 输出:
                self.assertEqual(main([]), 0)
                命令.assert_not_called()
                self.assertIn("没有 检查配置.json，用默认值", 输出.getvalue())
                for 名 in ("magpie", "orca", "claude"):
                    self.assertIn(f"跳过（没装 {名}）", 输出.getvalue())
                self.assertIn("派活配置", 输出.getvalue())

    def test_派活配置缺省原生方式的非法Claude模型算异常(self):
        with tempfile.TemporaryDirectory() as 临时:
            根 = Path(临时)
            (根 / "config.toml").write_text('version=1\n[roles.reviewer]\nhost="claude-code"\nmodel="group/impl"\n', encoding="utf-8")
            with patch("体检.CONFIG_ROOT", 根), patch("体检.shutil.which", return_value=None), patch("体检.Path.cwd", return_value=根), patch("sys.stdout", new_callable=io.StringIO) as 输出:
                self.assertEqual(main([]), 1)
                self.assertIn("✗ 派活配置", 输出.getvalue())
                self.assertIn("roles.reviewer.model", 输出.getvalue())
                self.assertIn("共 1 条，跑 生效配置.py 看全部", 输出.getvalue())

    def test_派活配置出错列首条和总数(self):
        with tempfile.TemporaryDirectory() as 临时:
            根 = Path(临时)
            (根 / "config.toml").write_text('version=2\nmax_workers=3\n', encoding="utf-8")
            with patch("体检.CONFIG_ROOT", 根), patch("体检.shutil.which", return_value=None), patch.dict("os.environ", {"DISPATCH_CONFIG_DIR": str(根)}), patch("体检.Path.cwd", return_value=根), patch("sys.stdout", new_callable=io.StringIO) as 输出:
                self.assertEqual(main([]), 1)
                self.assertIn("✗ 派活配置", 输出.getvalue())
                self.assertIn("共 2 条，跑 生效配置.py 看全部", 输出.getvalue())


if __name__ == "__main__":
    unittest.main()
