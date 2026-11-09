"""用假的 CSV 和临时状态验证落点与去重，不调用任何模型。"""

import base64
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from 落点检查 import analyze_usage, filter_notification_groups, main, parse_usage_csv, show_toast


class 落点判定测试(unittest.TestCase):
    def test_CSV识别越界和兜底且忽略失败与直接请求(self):
        表头 = "requested_model,provider,served_model,model,status\n"
        文本 = 表头 + (
            "group/research,codex,主模型,,200\n" * 6
            + "group/research,codex,,兜底,201\n" * 4
            + "group/research,来源乙,越界,,200\n"
            + "group/research,来源乙,失败,,500\n"
            + "codex/主模型,来源乙,直接请求,,200\n"
        )
        问题 = analyze_usage(parse_usage_csv(文本), {"research": ["codex/主模型:high:fast", "codex/兜底"]})
        self.assertEqual({项["type"] for 项 in 问题}, {"out_of_group", "fallback_ratio"})
        self.assertIn("共 11 次", 问题[1]["message"])
        self.assertNotIn("失败", str(问题))

    def test_请求数和占比阈值均严格大于(self):
        行 = list(csv.DictReader(io.StringIO("requested_model,provider,model,status\n" + "group/a,p,备,200\n" * 3 + "group/a,p,主,200\n" * 7)))
        self.assertEqual(analyze_usage(行, {"a": ["p/主", "p/备"]}), [])
        self.assertEqual(analyze_usage(行, {"a": ["p/主", "p/备"]}, fallback_min_requests=9), [])

    def test_未知组不能静默放过(self):
        问题 = analyze_usage([{"requested_model": "group/旧组", "provider": "p", "model": "m", "status": "200"}], {"新组": ["p/m"]})
        self.assertEqual(问题[0]["group"], "旧组")

    def test_无效状态不算成功请求(self):
        行 = [{"requested_model": "group/a", "provider": "p", "model": "越界", "status": 值} for 值 in ("20", "2000", "2xx", "", "500")]
        self.assertEqual(analyze_usage(行, {"a": ["p/主"]}), [])

    def test_带1m后缀的组请求按归一化组名判断(self):
        行 = [{"requested_model": "group/research[1m]", "provider": "p", "model": "主", "status": "200"}]
        self.assertEqual(analyze_usage(行, {"research": ["p/主"]}), [])

    def test_参数缩写不能误启用precheck且help不返回0(self):
        with patch("落点检查.load_config") as 配置:
            self.assertNotEqual(main(["--pre"]), 0)
            配置.assert_not_called()
        with patch("落点检查.load_config") as 配置:
            self.assertNotEqual(main(["--precheck", "--help"]), 0)
            配置.assert_not_called()

    def test_成功请求缺少落点字段必须报错(self):
        with self.assertRaises(ValueError):
            analyze_usage([{"requested_model": "group/a", "provider": "p", "status": "200"}], {"a": ["p/主"]})

    def test_CSV表头缺失必须报错(self):
        with self.assertRaises(ValueError):
            parse_usage_csv("错误的输出\n")

    def test_precheck脚本错误也必须退出1(self):
        with patch("落点检查.load_config", side_effect=ValueError("配置损坏")), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(["--precheck", "--不通知"]), 1)

    def test_GBK环境运行两个入口不崩溃且退出码符合约定(self):
        脚本目录 = Path(__file__).resolve().parents[1]
        环境 = os.environ.copy()
        环境["PYTHONIOENCODING"] = "gbk"
        命令列表 = [
            ([sys.executable, "-B", str(脚本目录 / "体检.py")], (0, 1)),
            ([sys.executable, "-B", str(脚本目录 / "落点检查.py"), "--不通知"], (0, 1)),
            ([sys.executable, "-B", str(脚本目录 / "落点检查.py"), "--precheck", "--不通知"], (1,)),
        ]
        for 命令, 允许退出码 in 命令列表:
            with self.subTest(脚本=命令[2], 参数=命令[3:]):
                结果 = subprocess.run(命令, env=环境, capture_output=True, timeout=120, check=False)
                标准输出 = 结果.stdout.decode("utf-8")
                标准错误 = 结果.stderr.decode("utf-8")
                self.assertIn(结果.returncode, 允许退出码, 标准错误)
                self.assertNotIn("UnicodeEncodeError", 标准输出 + 标准错误)
                self.assertNotIn("Traceback", 标准输出 + 标准错误)
                self.assertTrue(标准输出.strip())
        for 文件名 in ("体检.py", "落点检查.py"):
            with self.subTest(未捕获异常=文件名):
                注入代码 = (
                    "import runpy,sys; from unittest.mock import patch; "
                    "sys.path.insert(0,sys.argv[1]); script=sys.argv[2]; sys.argv=[script]; "
                    "with_patch=patch('组合解析.load_config',side_effect=KeyError('不得输出的异常内容')); "
                    "with_patch.start(); runpy.run_path(script,run_name='__main__')"
                )
                结果 = subprocess.run([sys.executable, "-B", "-c", 注入代码, str(脚本目录), str(脚本目录 / 文件名)], env=环境, capture_output=True, timeout=10, check=False)
                self.assertEqual(结果.returncode, 2)
                错误 = 结果.stderr.decode("utf-8")
                self.assertIn("脚本错误", 错误)
                self.assertIn("KeyError", 错误)
                self.assertNotIn("Traceback", 错误)
                self.assertNotIn("不得输出的异常内容", 错误)


class 通知去重测试(unittest.TestCase):
    def test_并发写入重新合并最新状态(self):
        with tempfile.TemporaryDirectory() as 临时目录:
            状态 = Path(临时目录) / "通知.json"
            状态.write_text('{"a": 1000}', encoding="utf-8")
            with patch("落点检查.Path.read_text", side_effect=['{"a": 1000}', '{"a": 1000, "c": 2000}']), patch("落点检查.write_state") as 写状态:
                self.assertEqual(filter_notification_groups(["b"], 状态, now=3000), ["b"])
            写状态.assert_called_once_with(状态, {"a": 1000, "c": 2000, "b": 3000})

    def test_通知身份使用已注册PowerShell并直接Show(self):
        with patch("落点检查.run_command", return_value="") as 运行:
            self.assertEqual(show_toast(["测试提醒"]), (True, "Windows 通知已提交（是否显示未确认）"))
        编码脚本 = base64.b64decode(运行.call_args.args[0][-1]).decode("utf-16le")
        self.assertIn("{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe", 编码脚本)
        self.assertIn("$n.Show", 编码脚本)
        self.assertNotIn("$n.Setting", 编码脚本)

    def test_一小时内同组只保留一次且不影响其他组(self):
        with tempfile.TemporaryDirectory() as 临时目录:
            状态 = Path(临时目录) / "通知.json"
            self.assertEqual(filter_notification_groups(["a", "a"], 状态, now=1000), ["a"])
            self.assertEqual(filter_notification_groups(["a", "b"], 状态, now=2000), ["b"])
            self.assertEqual(filter_notification_groups(["a"], 状态, now=4600), ["a"])

    def test_时钟回退和损坏状态均可重新通知(self):
        with tempfile.TemporaryDirectory() as 临时目录:
            状态 = Path(临时目录) / "通知.json"
            状态.write_text('{"a": 5000}', encoding="utf-8")
            self.assertEqual(filter_notification_groups(["a"], 状态, now=1000), ["a"])
            状态.write_text("损坏", encoding="utf-8")
            self.assertEqual(filter_notification_groups(["a"], 状态, now=1000), ["a"])


if __name__ == "__main__":
    unittest.main()
