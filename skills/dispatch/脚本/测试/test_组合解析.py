"""组合文件与 CLI 展示格式的定向测试。"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from 组合解析 import CommandFailure, load_current_combination, parse_combination_file, run_command


class 组合解析测试(unittest.TestCase):
    def test_解析成员保留原写法并提供落点标识(self):
        with tempfile.TemporaryDirectory() as 临时目录:
            文件 = Path(临时目录) / "组合.txt"
            文件.write_text(
                "# 注释\n"
                "magpie group add research models=codex/模型:medium+high:fast,来源乙/兜底:high routing=order\n"
                "magpie claude opus group/research[1m]\n",
                encoding="utf-8",
            )
            结果 = parse_combination_file(文件)
        self.assertEqual(结果["groups"]["research"], ["codex/模型", "来源乙/兜底"])
        self.assertEqual(结果["member_specs"]["research"], ["codex/模型:medium+high:fast", "来源乙/兜底:high"])
        self.assertEqual(结果["claude"]["opus"], "group/research")

    def test_当前组合按脚本根目录定位(self):
        with tempfile.TemporaryDirectory() as 临时目录:
            根 = Path(临时目录)
            (根 / "组合").mkdir()
            (根 / "组合" / "当前组合.txt").write_text("示例\n", encoding="utf-8")
            (根 / "组合" / "示例.txt").write_text("magpie group add explore models=来源/模型:high\n", encoding="utf-8")
            with patch.dict("os.environ", {"DISPATCH_CONFIG_DIR": str(根 / "没有个人配置")}):
                self.assertEqual(load_current_combination(根)["groups"], {"explore": ["来源/模型"]})

    def test_个人配置目录里的在用组合优先(self):
        with tempfile.TemporaryDirectory() as 临时目录:
            根 = Path(临时目录)
            for 目录, 成员 in ((根 / "skill" / "组合", "示例/模型"), (根 / "个人" / "组合", "个人/模型")):
                目录.mkdir(parents=True)
                (目录 / "当前组合.txt").write_text("示例\n", encoding="utf-8")
                (目录 / "示例.txt").write_text(f"magpie group add explore models={成员}:high\n", encoding="utf-8")
            with patch.dict("os.environ", {"DISPATCH_CONFIG_DIR": str(根 / "个人")}):
                self.assertEqual(load_current_combination(根 / "skill")["groups"], {"explore": ["个人/模型"]})

    def test_组合损坏不能当成空组合正常(self):
        with tempfile.TemporaryDirectory() as 临时目录:
            文件 = Path(临时目录) / "组合.txt"
            文件.write_text("magpie group add explore models=\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                parse_combination_file(文件)

    def test_审查回归_清理再次超时关闭原始管道并继续后项(self):
        卡住 = Mock()
        卡住.pid = 12345
        卡住.communicate.side_effect = [
            subprocess.TimeoutExpired("被检查命令", 0.01),
            subprocess.TimeoutExpired("被检查命令", 5),
        ]
        后项 = Mock()
        后项.communicate.return_value = ("后项正常", "")
        后项.returncode = 0
        with patch("组合解析.shutil.which", return_value=sys.executable), patch("组合解析.subprocess.Popen", side_effect=[卡住, 后项]), patch("组合解析.subprocess.run") as 清理, patch("组合解析.os.killpg", create=True):
            with self.assertRaises(CommandFailure) as 异常:
                run_command(["被检查命令"], 0.01)
            self.assertIn("超时", str(异常.exception))
            卡住.stdout.buffer.raw.close.assert_called_once()
            卡住.stderr.buffer.raw.close.assert_called_once()
            self.assertEqual(run_command(["下一项"], 0.01), "后项正常")
            if sys.platform == "win32":
                self.assertEqual(清理.call_args.args[0], ["taskkill", "/PID", "12345", "/T", "/F"])
            self.assertEqual(卡住.communicate.call_args_list, [call(timeout=0.01), call(timeout=5)])


if __name__ == "__main__":
    unittest.main()
