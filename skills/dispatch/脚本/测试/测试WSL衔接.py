"""用临时仓库验证 WSL 衔接；运行时不访问真实项目。"""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
脚本路径 = Path(__file__).resolve().parents[1] / "WSL衔接.py"



def 真实WSL发行版(用例):
    """真实 WSL 往返测试用的发行版名，从环境变量 DISPATCH_TEST_WSL_DISTRO 读；没设就跳过这类用例。"""
    名 = os.environ.get("DISPATCH_TEST_WSL_DISTRO")
    if not 名:
        用例.skipTest("没设 DISPATCH_TEST_WSL_DISTRO，跳过真实 WSL 往返测试")
    return 名

class 衔接测试(unittest.TestCase):
    def setUp(self):
        规格 = importlib.util.spec_from_file_location("被测衔接", 脚本路径)
        self.模块 = importlib.util.module_from_spec(规格)
        规格.loader.exec_module(self.模块)
        self.临时 = tempfile.TemporaryDirectory(prefix="wsl-bridge-")
        self.addCleanup(self.临时.cleanup)
        self.根 = Path(self.临时.name)
        self.环境 = os.environ.copy()
        for 键 in list(self.环境):
            if 键.startswith("GIT_"):
                self.环境.pop(键)
        self.环境.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                          "GIT_TERMINAL_PROMPT": "0", "GIT_MERGE_AUTOEDIT": "no"})
        self.Windows = self.根 / "windows"
        self.WSL = self.根 / "wsl"
        self.git(self.根, "init", "--initial-branch=main", str(self.Windows))
        self.git(self.Windows, "config", "user.name", "测试作者")
        self.git(self.Windows, "config", "user.email", "test@example.invalid")
        self.提交(self.Windows, "共同.txt", "初始\n")
        self.git(self.根, "clone", "--origin", "windows", str(self.Windows), str(self.WSL))
        self.git(self.WSL, "config", "user.name", "测试作者")
        self.git(self.WSL, "config", "user.email", "test@example.invalid")
        self.项目 = {"名字": "测试项目", "Windows检出路径": str(self.Windows),
                    "WSL发行版": "测试发行版", "WSL检出路径": "/虚拟WSL/测试项目",
                    "Orca远程环境": "测试环境", "WSL仓库ID": "测试仓库"}
        self.配置 = self.根 / "项目.json"
        self.配置.write_text(json.dumps({"项目": [self.项目]}, ensure_ascii=False), encoding="utf-8")
        self.调用记录 = []

    def git(self, 路径, *参数, 检查=True):
        return subprocess.run(["git", "-C", str(路径), *参数], env=self.环境,
                              capture_output=True, text=True, encoding="utf-8", check=检查)

    def 提交(self, 路径, 文件, 内容):
        (路径 / 文件).write_text(内容, encoding="utf-8")
        self.git(路径, "add", "--", 文件)
        self.git(路径, "commit", "-m", "测试提交")
        return self.git(路径, "rev-parse", "HEAD").stdout.strip()

    def 调用器(self, 命令, **选项):
        self.调用记录.append(命令)
        if 命令[0] == "wsl.exe":
            self.assertEqual(命令[:4], ["wsl.exe", "-d", "测试发行版", "--exec"])
            命令 = 命令[4:]
            if 命令[0] == "python3":
                内层 = 命令[4:]
                内层[内层.index("-C") + 1] = str(self.WSL)
                if 命令[2] == self.模块.索引锁等待器:
                    return subprocess.run([sys.executable, *命令[1:4], *内层], env=self.环境,
                                          capture_output=True, text=True, encoding="utf-8", check=False)
                结果 = subprocess.run(内层, env=self.环境, capture_output=True, text=True,
                                      encoding="utf-8", check=False)
                回执 = json.dumps({"状态": "完成", "已停止": True, "退出码": 结果.returncode,
                                  "输出": 结果.stdout}, ensure_ascii=False)
                return subprocess.CompletedProcess(命令, 0, 回执, 结果.stderr)
            命令[命令.index("-C") + 1] = str(self.WSL)
        self.assertEqual(命令[0], "git")
        # 只替换测试项目的 UNC 地址，真实 fetch 在临时仓库之间执行。
        命令 = [str(self.WSL) if 参数 == "//wsl.localhost/测试发行版/虚拟WSL/测试项目" else 参数
                for 参数 in 命令]
        选项["env"] = self.环境
        return subprocess.run(命令, **选项)

    def 运行(self, *参数):
        输出 = io.StringIO()
        with contextlib.redirect_stdout(输出), contextlib.redirect_stderr(输出):
            退出码 = self.模块.主函数([参数[0], "--项目", "测试项目", *参数[1:]],
                                      配置路径=self.配置, 运行命令=self.调用器)
        return 退出码, 输出.getvalue()

    def Windows分支(self, 名字, 文件=None, 内容="分支\n"):
        self.git(self.Windows, "switch", "-c", 名字, "main")
        self.提交(self.Windows, 文件 or (名字 + ".txt"), 内容)
        self.git(self.Windows, "switch", "main")

    def test_同步基线快进当前main(self):
        最新 = self.提交(self.Windows, "更新.txt", "更新\n")
        退出码, 输出 = self.运行("同步基线")
        self.assertEqual(退出码, 0, 输出)
        self.assertIn("windows/main", 输出)
        self.assertIn(最新, 输出)
        self.assertEqual(self.git(self.WSL, "rev-parse", "main").stdout.strip(), 最新)
        self.assertEqual((self.WSL / "更新.txt").read_text(encoding="utf-8"), "更新\n")

    def test_同步基线不切换当前工作分支(self):
        self.git(self.WSL, "switch", "-c", "工作")
        工作提交 = self.git(self.WSL, "rev-parse", "HEAD").stdout
        最新 = self.提交(self.Windows, "更新.txt", "更新\n")
        退出码, 输出 = self.运行("同步基线")
        self.assertEqual(退出码, 0, 输出)
        self.assertEqual(self.git(self.WSL, "branch", "--show-current").stdout.strip(), "工作")
        self.assertEqual(self.git(self.WSL, "rev-parse", "HEAD").stdout, 工作提交)
        self.assertEqual(self.git(self.WSL, "rev-parse", "main").stdout.strip(), 最新)

    def test_同步基线拒绝分叉(self):
        原提交 = self.提交(self.WSL, "本地.txt", "本地\n")
        self.提交(self.Windows, "远端.txt", "远端\n")
        退出码, 输出 = self.运行("同步基线")
        self.assertEqual(退出码, 1, 输出)
        self.assertIn("快进", 输出)
        self.assertEqual(self.git(self.WSL, "rev-parse", "main").stdout.strip(), 原提交)

    def test_同步基线保留本地领先提交(self):
        原提交 = self.提交(self.WSL, "本地.txt", "本地\n")
        退出码, 输出 = self.运行("同步基线")
        self.assertEqual(退出码, 0, 输出)
        self.assertEqual(self.git(self.WSL, "rev-parse", "main").stdout.strip(), 原提交)

    def test_同步基线拒绝覆盖脏工作区(self):
        self.提交(self.Windows, "共同.txt", "更新\n")
        (self.WSL / "共同.txt").write_text("未提交\n", encoding="utf-8")
        原提交 = self.git(self.WSL, "rev-parse", "main").stdout
        退出码, 输出 = self.运行("同步基线")
        self.assertEqual(退出码, 1, 输出)
        self.assertEqual(self.git(self.WSL, "rev-parse", "main").stdout, 原提交)
        self.assertEqual((self.WSL / "共同.txt").read_text(encoding="utf-8"), "未提交\n")

    def test_取分支支持多个分支并快进已有分支(self):
        self.Windows分支("功能甲")
        self.Windows分支("功能乙")
        退出码, 输出 = self.运行("取分支", "功能甲", "功能乙")
        self.assertEqual(退出码, 0, 输出)
        self.git(self.Windows, "switch", "功能甲")
        最新 = self.提交(self.Windows, "甲更新.txt", "更新\n")
        self.git(self.Windows, "switch", "main")
        退出码, 输出 = self.运行("取分支", "功能甲")
        self.assertEqual(退出码, 0, 输出)
        self.assertEqual(self.git(self.WSL, "rev-parse", "功能甲").stdout.strip(), 最新)
        self.assertEqual(self.git(self.WSL, "rev-parse", "功能乙").stdout,
                         self.git(self.Windows, "rev-parse", "功能乙").stdout)

    def test_取分支拒绝非快进且批量不部分更新(self):
        self.Windows分支("功能甲")
        self.Windows分支("功能乙")
        self.assertEqual(self.运行("取分支", "功能甲", "功能乙")[0], 0)
        self.git(self.WSL, "switch", "功能甲")
        本地 = self.提交(self.WSL, "本地.txt", "本地\n")
        self.git(self.WSL, "switch", "main")
        self.git(self.Windows, "switch", "功能甲")
        self.提交(self.Windows, "远端.txt", "远端\n")
        self.git(self.Windows, "switch", "功能乙")
        self.提交(self.Windows, "乙更新.txt", "更新\n")
        self.git(self.Windows, "switch", "main")
        乙原提交 = self.git(self.WSL, "rev-parse", "功能乙").stdout
        退出码, 输出 = self.运行("取分支", "功能乙", "功能甲")
        self.assertEqual(退出码, 1, 输出)
        self.assertEqual(self.git(self.WSL, "rev-parse", "功能甲").stdout.strip(), 本地)
        self.assertEqual(self.git(self.WSL, "rev-parse", "功能乙").stdout, 乙原提交)

    def test_取回新建多个分支且不切换Windows分支(self):
        self.git(self.WSL, "switch", "-c", "集成甲")
        最新 = self.提交(self.WSL, "集成.txt", "已集成\n")
        self.git(self.WSL, "branch", "集成乙")
        原主分支 = self.git(self.Windows, "rev-parse", "main").stdout
        退出码, 输出 = self.运行("取回", "集成甲", "集成乙")
        self.assertEqual(退出码, 0, 输出)
        for 分支 in ("集成甲", "集成乙"):
            self.assertEqual(self.git(self.Windows, "rev-parse", 分支).stdout.strip(), 最新)
        self.assertEqual(self.git(self.Windows, "branch", "--show-current").stdout.strip(), "main")
        self.assertEqual(self.git(self.Windows, "rev-parse", "main").stdout, 原主分支)
        self.assertFalse((self.Windows / "集成.txt").exists())
        self.assertEqual(self.调用记录, [["git", "-c", "core.quotePath=false", "-C", str(self.Windows),
                                        "for-each-ref", "--format=%(refname)%00%(symref)", "refs/heads/"],
                                       ["git", "-c", "core.quotePath=false", "-C", str(self.Windows),
                                        "fetch", "--atomic", "//wsl.localhost/测试发行版/虚拟WSL/测试项目",
                                        "refs/heads/集成甲:refs/heads/集成甲",
                                        "refs/heads/集成乙:refs/heads/集成乙"]])

    def test_取回整批拒绝main和master且演练也拒绝(self):
        for 主分支 in ("main", "master", "Main", "MAIN", "Master", "MASTER"):
            for 演练 in ((), ("--演练",)):
                with self.subTest(主分支=主分支, 演练=演练):
                    self.调用记录.clear()
                    退出码, 输出 = self.运行("取回", "待取分支", 主分支, *演练)
                    self.assertEqual(退出码, 1, 输出)
                    self.assertIn("main/master", 输出)
                    self.assertEqual(self.调用记录, [])

    def test_取回演练只打印Windows命令(self):
        退出码, 输出 = self.运行("取回", "--演练", "集成甲", "集成乙")
        self.assertEqual(退出码, 0, 输出)
        self.assertEqual(self.调用记录, [])
        self.assertEqual(len(输出.strip().splitlines()), 2)
        self.assertIn("for-each-ref", 输出.splitlines()[0])
        self.assertIn("--format=%(refname)%00%(symref)", 输出.splitlines()[0])
        self.assertIn("refs/heads/", 输出.splitlines()[0])
        self.assertIn("大小写冲突", 输出.splitlines()[1])
        self.assertIn("符号引用", 输出.splitlines()[1])
        self.assertIn("实际执行时", 输出.splitlines()[1])
        self.assertIn("-C " + self.模块.命令文本([str(self.Windows)]), 输出)
        self.assertIn("fetch --atomic //wsl.localhost/测试发行版/虚拟WSL/测试项目", 输出)
        self.assertIn("refs/heads/集成甲:refs/heads/集成甲 refs/heads/集成乙:refs/heads/集成乙", 输出)
        self.assertNotIn("wsl.exe", 输出)

    def test_取回快进已有Windows分支(self):
        self.git(self.Windows, "branch", "集成")
        self.git(self.WSL, "switch", "-c", "集成")
        最新 = self.提交(self.WSL, "更新.txt", "WSL 更新\n")
        退出码, 输出 = self.运行("取回", "集成")
        self.assertEqual(退出码, 0, 输出)
        self.assertEqual(self.git(self.Windows, "rev-parse", "集成").stdout.strip(), 最新)
        self.assertEqual(self.git(self.Windows, "branch", "--show-current").stdout.strip(), "main")
        self.assertFalse((self.Windows / "更新.txt").exists())

    def test_取回非快进时整批不新建也不更新(self):
        self.Windows分支("分叉", "本地.txt", "Windows 独立提交\n")
        self.git(self.Windows, "branch", "可快进")
        self.git(self.WSL, "switch", "-c", "分叉")
        self.提交(self.WSL, "远端.txt", "WSL 独立提交\n")
        self.git(self.WSL, "switch", "-c", "可快进", "main")
        self.提交(self.WSL, "更新.txt", "WSL 更新\n")
        self.git(self.WSL, "branch", "新分支")
        原引用 = self.git(self.Windows, "show-ref", "--heads").stdout
        退出码, 输出 = self.运行("取回", "新分支", "可快进", "分叉")
        self.assertEqual(退出码, 1, 输出)
        self.assertEqual(self.git(self.Windows, "show-ref", "--heads").stdout, 原引用)
        self.assertEqual(self.git(self.Windows, "branch", "--show-current").stdout.strip(), "main")
        self.assertEqual(self.git(self.Windows, "status", "--porcelain").stdout, "")

    def test_取回拒绝更新Windows已检出分支(self):
        self.git(self.Windows, "switch", "-c", "工作")
        self.git(self.WSL, "switch", "-c", "工作")
        self.提交(self.WSL, "更新.txt", "WSL 更新\n")
        原提交 = self.git(self.Windows, "rev-parse", "HEAD").stdout
        退出码, 输出 = self.运行("取回", "工作")
        self.assertEqual(退出码, 1, 输出)
        self.assertEqual(self.git(self.Windows, "rev-parse", "HEAD").stdout, 原提交)
        self.assertEqual(self.git(self.Windows, "branch", "--show-current").stdout.strip(), "工作")
        self.assertFalse((self.Windows / "更新.txt").exists())

    def test_取回大小写碰撞整批拒绝且覆盖打包引用(self):
        for 打包 in (False, True):
            with self.subTest(打包引用=打包):
                后缀 = "打包" if 打包 else "松散"
                本地名 = "功能a" + 后缀
                目标名 = "功能A" + 后缀
                新分支 = "新分支" + 后缀
                self.git(self.Windows, "branch", 本地名)
                if 打包:
                    self.git(self.Windows, "pack-refs", "--all")
                self.git(self.WSL, "switch", "-c", 目标名, "main")
                self.提交(self.WSL, 后缀 + ".txt", "WSL 更新\n")
                self.git(self.WSL, "branch", 新分支)
                原引用 = self.git(self.Windows, "show-ref", "--heads").stdout
                self.调用记录.clear()
                退出码, 输出 = self.运行("取回", 新分支, 目标名)
                self.assertEqual(退出码, 1, 输出)
                self.assertIn("大小写", 输出)
                self.assertEqual(self.git(self.Windows, "show-ref", "--heads").stdout, 原引用)
                self.assertFalse(any("fetch" in 命令 for 命令 in self.调用记录))

    def test_取回拒绝符号引用且不穿透主分支或普通分支(self):
        self.git(self.Windows, "branch", "普通")
        for 指向 in ("main", "普通"):
            with self.subTest(符号目标=指向):
                别名 = "别名" + 指向
                新分支 = "新分支" + 指向
                self.git(self.Windows, "symbolic-ref", "refs/heads/" + 别名, "refs/heads/" + 指向)
                self.git(self.WSL, "switch", "-c", 别名, "main")
                self.提交(self.WSL, 别名 + ".txt", "WSL 更新\n")
                self.git(self.WSL, "branch", 新分支)
                原引用 = self.git(self.Windows, "show-ref", "--heads").stdout
                self.调用记录.clear()
                退出码, 输出 = self.运行("取回", 新分支, 别名)
                self.assertEqual(退出码, 1, 输出)
                self.assertIn("符号引用", 输出)
                self.assertEqual(self.git(self.Windows, "show-ref", "--heads").stdout, 原引用)
                self.assertEqual(self.git(self.Windows, "symbolic-ref", "refs/heads/" + 别名).stdout.strip(),
                                 "refs/heads/" + 指向)
                self.assertFalse(any("fetch" in 命令 for 命令 in self.调用记录))

    def test_取回沿用危险分支名校验(self):
        for 分支 in ("--force", "+main", "main:master", "a..b", "a b", "功能*", "a.lock"):
            with self.subTest(分支=分支):
                self.调用记录.clear()
                退出码, 输出 = self.运行("取回", "--", 分支)
                self.assertEqual(退出码, 1, 输出)
                self.assertIn("分支名不合法", 输出)
                self.assertEqual(self.调用记录, [])

    def test_合并使用新基线且生成双亲提交(self):
        旧main = self.git(self.WSL, "rev-parse", "main").stdout
        最新 = self.提交(self.Windows, "更新.txt", "更新\n")
        self.Windows分支("功能甲")
        self.Windows分支("功能乙")
        self.assertEqual(self.运行("取分支", "功能甲", "功能乙")[0], 0)
        self.git(self.WSL, "fetch", "windows")
        退出码, 输出 = self.运行("合并", "--目标", "集成", "--基线", "windows/main", "功能甲", "功能乙")
        self.assertEqual(退出码, 0, 输出)
        self.assertEqual(self.git(self.WSL, "rev-parse", "main").stdout, 旧main)
        self.assertEqual(self.git(self.WSL, "rev-parse", "HEAD~2").stdout.strip(), 最新)
        self.assertEqual(len(self.git(self.WSL, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()), 3)
        self.assertEqual(self.git(self.WSL, "show", "-s", "--format=%an <%ae>", "HEAD").stdout.strip(),
                         "测试作者 <test@example.invalid>")

    def test_合并冲突打印文件并中止(self):
        self.Windows分支("冲突甲", "共同.txt", "甲\n")
        self.Windows分支("冲突乙", "共同.txt", "乙\n")
        self.assertEqual(self.运行("取分支", "冲突甲", "冲突乙")[0], 0)
        退出码, 输出 = self.运行("合并", "--目标", "集成", "--基线", "windows/main", "冲突甲", "冲突乙")
        self.assertEqual(退出码, 1, 输出)
        self.assertIn("共同.txt", 输出)
        self.assertNotEqual(self.git(self.WSL, "rev-parse", "--verify", "MERGE_HEAD", 检查=False).returncode, 0)
        self.assertEqual(self.git(self.WSL, "status", "--porcelain").stdout, "")
        self.assertEqual((self.WSL / "共同.txt").read_text(encoding="utf-8"), "甲\n")

    def test_已有集成分支拒绝错误基线(self):
        self.git(self.WSL, "switch", "-c", "集成")
        原提交 = self.提交(self.WSL, "旧集成.txt", "旧\n")
        self.git(self.WSL, "switch", "main")
        self.提交(self.Windows, "新基线.txt", "新\n")
        self.Windows分支("功能甲")
        self.assertEqual(self.运行("取分支", "功能甲")[0], 0)
        self.git(self.WSL, "fetch", "windows")
        退出码, 输出 = self.运行("合并", "--目标", "集成", "--基线", "windows/main", "功能甲")
        self.assertEqual(退出码, 1, 输出)
        self.assertEqual(self.git(self.WSL, "rev-parse", "集成").stdout.strip(), 原提交)
        self.assertEqual(self.git(self.WSL, "branch", "--show-current").stdout.strip(), "main")

    def test_已有集成分支可快进至基线(self):
        self.git(self.WSL, "branch", "集成")
        最新 = self.提交(self.Windows, "新基线.txt", "新\n")
        self.Windows分支("功能甲")
        self.assertEqual(self.运行("取分支", "功能甲")[0], 0)
        self.git(self.WSL, "fetch", "windows")
        退出码, 输出 = self.运行("合并", "--目标", "集成", "--基线", "windows/main", "功能甲")
        self.assertEqual(退出码, 0, 输出)
        self.assertEqual(self.git(self.WSL, "rev-parse", "HEAD~1").stdout.strip(), 最新)

    def test_身份不一致阻止合并且给出设置命令(self):
        self.git(self.WSL, "config", "user.name", "错误作者")
        退出码, 输出 = self.运行("检查身份")
        self.assertEqual(退出码, 1, 输出)
        self.assertIn("user.name", 输出)
        self.assertIn("测试作者", 输出)
        原提交 = self.git(self.WSL, "rev-parse", "HEAD").stdout
        退出码, 输出 = self.运行("合并", "--目标", "集成", "--基线", "windows/main", "main")
        self.assertEqual(退出码, 1, 输出)
        self.assertEqual(self.git(self.WSL, "rev-parse", "HEAD").stdout, 原提交)
        self.assertEqual(self.git(self.WSL, "config", "user.name").stdout.strip(), "错误作者")

    def test_身份一致(self):
        退出码, 输出 = self.运行("检查身份")
        self.assertEqual(退出码, 0, 输出)

    def test_脏工作区阻止合并(self):
        (self.WSL / "未跟踪.txt").write_text("保留", encoding="utf-8")
        退出码, 输出 = self.运行("合并", "--目标", "集成", "--基线", "windows/main", "main")
        self.assertEqual(退出码, 1, 输出)
        self.assertFalse(self.git(self.WSL, "show-ref", "--verify", "refs/heads/集成", 检查=False).returncode == 0)
        self.assertEqual((self.WSL / "未跟踪.txt").read_text(encoding="utf-8"), "保留")

    def test_合并拒绝主分支目标(self):
        退出码, 输出 = self.运行("合并", "--目标", "main", "--基线", "windows/main", "main")
        self.assertEqual(退出码, 1, 输出)

    def test_演练完全不执行命令(self):
        用例 = [("同步基线",), ("取分支", "功能甲", "功能乙"), ("检查身份",),
                ("合并", "--目标", "集成", "--基线", "windows/main", "功能甲"),
                ("初始化基线",)]
        for 参数 in 用例:
            with self.subTest(参数=参数):
                self.调用记录.clear()
                退出码, 输出 = self.运行(*参数, "--演练")
                self.assertEqual(退出码, 0, 输出)
                self.assertEqual(self.调用记录, [])
                self.assertTrue(输出.strip())

    def test_初始化只打印命令(self):
        退出码, 输出 = self.运行("初始化基线")
        self.assertEqual(退出码, 0, 输出)
        self.assertEqual(self.调用记录, [])
        self.assertIn("orca repo set-base-ref --environment", 输出)
        self.assertIn("--repo id:测试仓库 --ref windows/main --json", 输出)

    def test_缺失配置给出示例(self):
        self.配置.unlink()
        退出码, 输出 = self.运行("检查身份")
        self.assertEqual(退出码, 2, 输出)
        self.assertIn("示例", 输出)
        self.assertEqual(self.调用记录, [])

    def test_拒绝危险分支参数(self):
        for 分支 in ("--force", "+main", "main:main", "a..b", "a b"):
            with self.subTest(分支=分支):
                退出码, 输出 = self.运行("取分支", 分支)
                self.assertIn(退出码, (1, 2), 输出)
                self.assertEqual(self.调用记录, [])

    def test_返工一真实WSL保留特殊分支参数(self):
        # 真实启动器往返验证：目录有效，Git 必须逐字返回整个分支名。
        发行版 = 真实WSL发行版(self)
        项目 = dict(self.项目, WSL发行版=发行版, WSL检出路径="/tmp")
        衔接 = self.模块.Git衔接(项目, subprocess.run)
        for 分支 in ("功能;printf${IFS}注入命中", "功能$(printf${IFS}替换命中)"):
            with self.subTest(分支=分支):
                self.模块.检查分支名(分支)
                命令 = 衔接.命令(["check-ref-format", "--branch", 分支])
                if os.name != "nt":
                    命令[0] = "/mnt/c/Windows/System32/wsl.exe"
                结果 = subprocess.run(命令, capture_output=True, text=True, encoding="utf-8", timeout=30)
                self.assertEqual(结果.returncode, 0, 结果.stderr)
                self.assertEqual(结果.stdout.strip(), 分支)
                # 同时验证受控执行器的成功回执，以及它的参数边界。
                受控命令 = [*命令[:4], "python3", "-c", self.模块.合并执行器, "10", *命令[4:]]
                回执 = subprocess.run(受控命令, capture_output=True, text=True, encoding="utf-8", timeout=30)
                self.assertEqual(回执.returncode, 0, 回执.stderr)
                数据 = json.loads(回执.stdout)
                self.assertEqual(数据["状态"], "完成")
                self.assertTrue(数据["已停止"])
                self.assertEqual(数据["退出码"], 0)
                self.assertEqual(数据["输出"].strip(), 分支)

    def 准备忽略文件(self, 模式):
        # 先让两端都忽略本地.txt，再由目标分支开始跟踪同名文件。
        self.提交(self.Windows, ".gitignore", "本地.txt\n")
        self.git(self.WSL, "fetch", "windows")
        self.git(self.WSL, "merge", "--ff-only", "windows/main")
        self.git(self.Windows, "switch", "-c", "功能甲")
        (self.Windows / "本地.txt").write_text("分支中的内容\n", encoding="utf-8")
        self.git(self.Windows, "add", "-f", "本地.txt")
        self.git(self.Windows, "commit", "-m", "测试跟踪忽略文件")
        self.git(self.Windows, "switch", "main")
        if 模式 == "合并分支":
            self.assertEqual(self.运行("取分支", "功能甲")[0], 0)
            命令 = ("合并", "--目标", "集成", "--基线", "windows/main", "功能甲")
        else:
            # main 新增被跟踪文件，集成目标从旧基线创建时必须保护本地忽略文件。
            if 模式 == "合并基线":
                self.git(self.WSL, "branch", "集成")
            (self.Windows / "本地.txt").write_text("主线中的内容\n", encoding="utf-8")
            self.git(self.Windows, "add", "-f", "本地.txt")
            self.git(self.Windows, "commit", "-m", "主线跟踪忽略文件")
            命令 = ("合并", "--目标", "集成", "--基线", "windows/main", "main")
        self.git(self.WSL, "fetch", "windows")
        (self.WSL / "本地.txt").write_text("必须保留的本地内容\n", encoding="utf-8")
        self.assertEqual(self.git(self.WSL, "status", "--porcelain").stdout, "")
        if 模式 == "同步main":
            命令 = ("同步基线",)
        退出码, 输出 = self.运行(*命令)
        self.assertEqual(退出码, 1, 输出)
        self.assertEqual((self.WSL / "本地.txt").read_text(encoding="utf-8"), "必须保留的本地内容\n")

    def test_返工二合并保留忽略文件(self):
        self.准备忽略文件("合并分支")

    def test_返工二新建分支保留忽略文件(self):
        self.准备忽略文件("新建目标")

    def test_返工二快进目标保留忽略文件(self):
        self.准备忽略文件("合并基线")

    def test_返工二同步main保留忽略文件(self):
        self.准备忽略文件("同步main")

    def test_返工二切换命令保留忽略文件(self):
        self.提交(self.Windows, ".gitignore", "本地.txt\n")
        self.git(self.WSL, "fetch", "windows")
        self.git(self.WSL, "merge", "--ff-only", "windows/main")
        self.git(self.WSL, "switch", "-c", "跟踪")
        (self.WSL / "本地.txt").write_text("已跟踪内容\n", encoding="utf-8")
        self.git(self.WSL, "add", "-f", "本地.txt")
        self.git(self.WSL, "commit", "-m", "建立跟踪分支")
        self.git(self.WSL, "switch", "main")
        (self.WSL / "本地.txt").write_text("必须保留的本地内容\n", encoding="utf-8")
        退出码, 输出 = self.运行("合并", "--目标", "跟踪", "--基线", "windows/main", "main")
        self.assertEqual(退出码, 1, 输出)
        self.assertEqual((self.WSL / "本地.txt").read_text(encoding="utf-8"), "必须保留的本地内容\n")

    def test_返工三超时后清理本次合并(self):
        self.Windows分支("功能甲")
        self.assertEqual(self.运行("取分支", "功能甲")[0], 0)
        原调用器 = self.调用器

        def 模拟已停止的超时(命令, **选项):
            if "merge" in 命令 and "--no-ff" in 命令:
                内层 = 命令[4:]
                内层.insert(内层.index("merge") + 1, "--no-commit")
                结果 = 原调用器([*命令[:4], *内层], **选项)
                self.assertEqual(结果.returncode, 0, 结果.stderr)
                回执 = json.dumps({"状态": "超时", "已停止": True, "退出码": -9, "输出": ""}, ensure_ascii=False)
                return subprocess.CompletedProcess(命令, 0, 回执, "")
            return 原调用器(命令, **选项)

        self.调用器 = 模拟已停止的超时
        退出码, 输出 = self.运行("合并", "--目标", "集成", "--基线", "windows/main", "功能甲")
        self.assertEqual(退出码, 2, 输出)
        self.assertNotEqual(self.git(self.WSL, "rev-parse", "--verify", "MERGE_HEAD", 检查=False).returncode, 0)
        self.assertEqual(self.git(self.WSL, "status", "--porcelain").stdout, "")

    def test_返工三真实hook超时停止进程组再中止(self):
        self.Windows分支("功能甲")
        self.assertEqual(self.运行("取分支", "功能甲")[0], 0)

        def Linux路径(路径):
            if os.name == "nt":
                return "/mnt/" + 路径.drive[0].lower() + 路径.as_posix()[2:]
            return str(路径)

        启动器 = "wsl.exe" if os.name == "nt" else "/mnt/c/Windows/System32/wsl.exe"
        前缀 = [启动器, "-d", 真实WSL发行版(self), "--exec"]
        self.git(self.WSL, "config", "remote.windows.url", Linux路径(self.Windows))
        钩子 = self.WSL / ".git" / "hooks" / "prepare-commit-msg"
        钩子.write_text("#!/usr/bin/python3\nimport json, os, subprocess, sys, time\n"
                       "from pathlib import Path\n"
                       "子进程 = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                       "Path('.git/测试进程.json').write_text(json.dumps([os.getppid(), os.getpid(), 子进程.pid]))\n"
                       "time.sleep(60)\n", encoding="utf-8", newline="\n")
        subprocess.run([*前缀, "chmod", "+x", Linux路径(钩子)], check=True, capture_output=True)
        中止前检查 = []
        检查源码 = ("import json,sys; from pathlib import Path; "
                    "print(json.dumps([Path('/proc/'+str(p)+'/stat').read_text().rsplit(')',1)[1].split()[0] "
                    "if Path('/proc/'+str(p)+'/stat').exists() else '已退出' for p in json.loads(sys.argv[1])]))")

        def 检查进程():
            进程号 = (self.WSL / ".git" / "测试进程.json").read_text(encoding="utf-8")
            结果 = subprocess.run([*前缀, "python3", "-c", 检查源码, 进程号], capture_output=True,
                                  text=True, encoding="utf-8", timeout=30, check=True)
            self.assertTrue(all(状态 in ("已退出", "Z", "X") for 状态 in json.loads(结果.stdout)), 结果.stdout)

        def 真实调用器(命令, **选项):
            if 命令[0] == "wsl.exe":
                if "--abort" in 命令:
                    检查进程()
                    中止前检查.append(True)
                命令 = [*前缀, *命令[4:]]
                命令[命令.index("-C") + 1] = Linux路径(self.WSL)
                return subprocess.run(命令, **选项)
            return self.调用原Git(命令, **选项)

        self.调用原Git = self.调用器
        self.调用器 = 真实调用器
        self.模块.Git衔接.合并超时秒 = 2
        退出码, 输出 = self.运行("合并", "--目标", "集成", "--基线", "windows/main", "功能甲")
        self.assertEqual(退出码, 2, 输出)
        self.assertIn("进程组停止", 输出)
        self.assertEqual(中止前检查, [True], 输出)
        检查进程()
        self.assertNotEqual(self.git(self.WSL, "rev-parse", "--verify", "MERGE_HEAD", 检查=False).returncode, 0)
        self.assertEqual(self.git(self.WSL, "status", "--porcelain").stdout, "")

    def test_返工三保留原有进行中合并(self):
        self.Windows分支("功能甲")
        self.assertEqual(self.运行("取分支", "功能甲")[0], 0)
        self.git(self.WSL, "merge", "--no-ff", "--no-commit", "功能甲")
        原合并头 = self.git(self.WSL, "rev-parse", "MERGE_HEAD").stdout
        原状态 = self.git(self.WSL, "status", "--porcelain").stdout
        退出码, 输出 = self.运行("合并", "--目标", "集成", "--基线", "windows/main", "功能甲")
        self.assertEqual(退出码, 1, 输出)
        self.assertEqual(self.git(self.WSL, "rev-parse", "MERGE_HEAD").stdout, 原合并头)
        self.assertEqual(self.git(self.WSL, "status", "--porcelain").stdout, 原状态)
        self.assertFalse(any("--abort" in 命令 for 命令 in self.调用记录))

    def test_返工三启动器超时不贸然中止(self):
        self.Windows分支("功能甲")
        self.assertEqual(self.运行("取分支", "功能甲")[0], 0)
        原调用器 = self.调用器

        def 模拟未确认停止(命令, **选项):
            if "merge" in 命令 and "--no-ff" in 命令:
                self.调用记录.append(命令)
                raise subprocess.TimeoutExpired(命令, 1)
            return 原调用器(命令, **选项)

        self.调用器 = 模拟未确认停止
        退出码, 输出 = self.运行("合并", "--目标", "集成", "--基线", "windows/main", "功能甲")
        self.assertEqual(退出码, 2, 输出)
        self.assertIn("未正常返回", 输出)
        self.assertNotIn("请人工确认", 输出)
        self.assertFalse(any("--abort" in 命令 for 命令 in self.调用记录))

    def 复审启动器异常(self, 情况):
        self.Windows分支("功能甲")
        self.assertEqual(self.运行("取分支", "功能甲")[0], 0)
        原调用器 = self.调用器
        原提交 = self.git(self.WSL, "rev-parse", "HEAD").stdout.strip()
        目标提交 = self.git(self.WSL, "rev-parse", "功能甲").stdout.strip()
        self.模块.Git衔接.索引锁等待秒 = 0.15
        锁 = self.WSL / ".git" / "index.lock"

        def 启动器异常(命令, **选项):
            if "merge" in 命令 and "--no-ff" in 命令:
                内层 = list(命令)
                内层.insert(内层.index("merge") + 1, "--no-commit")
                结果 = 原调用器(内层, **选项)
                self.assertEqual(结果.returncode, 0, 结果.stderr)
                self.assertEqual(self.git(self.WSL, "rev-parse", "MERGE_HEAD").stdout.strip(), 目标提交)
                if 情况 == "锁未释放":
                    锁.write_text("测试保留锁", encoding="utf-8")
                elif 情况 == "合并头不同":
                    (self.WSL / ".git" / "MERGE_HEAD").write_text(原提交 + "\n", encoding="ascii")
                if 情况 == "异常退出":
                    return subprocess.CompletedProcess(命令, 139, "", "测试启动器异常退出")
                raise subprocess.TimeoutExpired(命令, 0.1)
            return 原调用器(命令, **选项)

        self.调用器 = 启动器异常
        退出码, 输出 = self.运行("合并", "--目标", "集成", "--基线", "windows/main", "功能甲")
        self.assertEqual(退出码, 2, 输出)
        if 情况 in ("锁未释放", "合并头不同"):
            self.assertIn("请人工确认", 输出)
            self.assertIn("HEAD：" + 原提交, 输出)
            self.assertIn("MERGE_HEAD：" + (原提交 if 情况 == "合并头不同" else 目标提交), 输出)
            self.assertFalse(any("--abort" in 命令 for 命令 in self.调用记录))
            if 情况 == "锁未释放":
                self.assertTrue(锁.exists(), "脚本不得强删索引锁")
        else:
            self.assertNotIn("请人工确认", 输出)
            self.assertNotEqual(self.git(self.WSL, "rev-parse", "--verify", "MERGE_HEAD", 检查=False).returncode, 0)
            self.assertEqual(self.git(self.WSL, "status", "--porcelain").stdout, "")
            self.assertTrue(any("--abort" in 命令 for 命令 in self.调用记录))

    def test_复审写入合并头后启动器超时清理(self):
        self.复审启动器异常("超时")

    def test_复审写入合并头后启动器异常退出清理(self):
        self.复审启动器异常("异常退出")

    def test_复审索引锁未释放打印状态且不强删(self):
        self.复审启动器异常("锁未释放")

    def test_复审合并头不同打印状态且不中止(self):
        self.复审启动器异常("合并头不同")

    def test_返工三冲突采集失败仍中止(self):
        self.Windows分支("冲突甲", "共同.txt", "甲\n")
        self.Windows分支("冲突乙", "共同.txt", "乙\n")
        self.assertEqual(self.运行("取分支", "冲突甲", "冲突乙")[0], 0)
        原调用器 = self.调用器

        def 模拟采集失败(命令, **选项):
            if "diff" in 命令:
                return subprocess.CompletedProcess(命令, 128, "", "测试采集失败")
            return 原调用器(命令, **选项)

        self.调用器 = 模拟采集失败
        退出码, 输出 = self.运行("合并", "--目标", "集成", "--基线", "windows/main", "冲突甲", "冲突乙")
        self.assertEqual(退出码, 1, 输出)
        self.assertNotEqual(self.git(self.WSL, "rev-parse", "--verify", "MERGE_HEAD", 检查=False).returncode, 0)
        self.assertEqual(self.git(self.WSL, "status", "--porcelain").stdout, "")


if __name__ == "__main__":
    unittest.main()
