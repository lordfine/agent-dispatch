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
            with patch("体检.load_config", return_value=配置), patch("体检.load_current_combination", return_value={"groups": {}, "member_specs": {}, "claude": {}}), patch("体检.check_magpie", side_effect=TimeoutError("超时")), patch("体检.check_combination", return_value=(True, "一致")), patch("体检.check_orca", return_value=(True, "运行中")), patch("体检.check_mcp", return_value=(True, "正常")) as 后项, patch("体检.STATE_ROOT", Path(临时目录)), patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(main([]), 1)
                后项.assert_called_once()


if __name__ == "__main__":
    unittest.main()
