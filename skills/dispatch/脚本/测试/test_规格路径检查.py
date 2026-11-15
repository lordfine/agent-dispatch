"""规格路径提取与基线文件核验，不修改真实仓库。"""

import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class 规格路径检查测试(unittest.TestCase):
    def test_抽取中文相对路径与行号并忽略命令绝对路径和URL(self):
        from 规格路径检查 import extract_spec_paths
        文本 = (
            "读取 `后端/应用.py:12-18` 和 `AGENTS.md:2`、`./前端/页面.tsx`。\n"
            "新增 `后端/新文件.py`\n"
            "忽略 `/tmp/a.py` `C:\\临时\\a.py` `C:/临时/a.py` `~/a.py` `https://example.invalid/a.py`\n"
            "命令 `git show HEAD:a.py` `$(pwd)/a.py` `../a.py` `FORGE_TEST_DATABASE_URL=x.py` `impl`\n"
        )
        self.assertEqual(extract_spec_paths(文本), [
            {"path": "后端/应用.py", "line": 1, "start": 12, "end": 18, "new": False},
            {"path": "AGENTS.md", "line": 1, "start": 2, "end": 2, "new": False},
            {"path": "前端/页面.tsx", "line": 1, "start": None, "end": None, "new": False},
            {"path": "后端/新文件.py", "line": 2, "start": None, "end": None, "new": True},
        ])

    def test_行号边界与倒序范围(self):
        from 规格路径检查 import check_line_range
        self.assertTrue(check_line_range(3, None, None))
        self.assertTrue(check_line_range(3, 1, 3))
        self.assertFalse(check_line_range(3, 1, 4))
        self.assertFalse(check_line_range(3, 0, 1))
        self.assertFalse(check_line_range(3, 3, 2))

    def test_基线检查忽略工作区同名文件且逐条输出(self):
        from 规格路径检查 import main
        with tempfile.TemporaryDirectory() as 临时目录:
            根 = Path(临时目录)
            环境 = os.environ.copy()
            for 键 in list(环境):
                if 键.startswith("GIT_"):
                    环境.pop(键)
            环境.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})

            def git(*参数: str) -> str:
                结果 = subprocess.run(["git", "-C", str(根), *参数], capture_output=True, text=True, encoding="utf-8", env=环境, check=True)
                return 结果.stdout.strip()

            git("init", "--initial-branch=main")
            (根 / "后端").mkdir()
            文件 = 根 / "后端" / "应用.py"
            文件.write_text("第一行\n第二行\n", encoding="utf-8")
            git("add", "--", "后端/应用.py")
            git("-c", "user.name=测试作者", "-c", "user.email=test@example.invalid", "commit", "-m", "测试基线")
            基线 = git("rev-parse", "HEAD")
            文件.write_text("本地第一行\n本地第二行\n本地第三行\n", encoding="utf-8")
            (根 / "仅本地.py").write_text("不在基线\n", encoding="utf-8")
            规格 = 根 / "规格.md"
            规格.write_text("`后端/应用.py:2`\n`后端/应用.py:1-3`\n`仅本地.py`\n新建 `后端/未来.py`\n", encoding="utf-8")
            with patch("sys.stdout", new_callable=io.StringIO) as 输出:
                退出码 = main([str(规格), "--repo", str(根), "--base", 基线])
            self.assertEqual(退出码, 1)
            self.assertIn("存在", 输出.getvalue())
            self.assertIn("行号越界", 输出.getvalue())
            self.assertIn("缺失", 输出.getvalue())
            self.assertIn("新文件，跳过", 输出.getvalue())
            规格.write_text("`后端/应用.py:2`\n新增 `后端/未来.py`\n", encoding="utf-8")
            with patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(main([str(规格), "--repo", str(根)]), 0)
                self.assertEqual(main([str(规格), "--repo", str(根), "--base", "不存在的基线"]), 2)


if __name__ == "__main__":
    unittest.main()
