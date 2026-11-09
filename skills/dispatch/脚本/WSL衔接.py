"""在 Windows 上衔接 Windows 与 WSL 的 Git 检出，仅使用 Python 标准库。"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


sys.path.insert(0, str(Path(__file__).resolve().parent))
from 组合解析 import config_root  # noqa: E402

默认配置 = config_root(Path(__file__).resolve().parents[1]) / "项目.json"
配置示例 = {"项目": [{"名字": "示例项目", "Windows检出路径": "D:/项目/示例项目",
                    "WSL发行版": "示例发行版", "WSL检出路径": "/home/用户/work/示例项目",
                    "Orca远程环境": "远程环境名", "WSL仓库ID": "仓库ID"}]}


class 发现问题(Exception):
    """用户需要处理的仓库或身份问题，退出码为 1。"""


class 脚本错误(Exception):
    """配置、运行环境或调用失败，退出码为 2。"""


def 命令文本(命令):
    """生成可粘贴到 PowerShell 的参数，不通过 shell 执行。"""
    return " ".join(参数 if re.fullmatch(r"[\w./:\\@=+-]+", 参数)
                    else "'" + 参数.replace("'", "''") + "'" for 参数 in 命令)


def 读取项目(路径, 名字):
    if not 路径.is_file():
        raise 脚本错误(f"配置文件缺失：{路径}；示例：{json.dumps(配置示例, ensure_ascii=False)}")
    try:
        配置 = json.loads(路径.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise 脚本错误("项目配置无法读取或不是有效的 UTF-8 JSON") from None
    if not isinstance(配置, dict) or not isinstance(配置.get("项目"), list):
        raise 脚本错误("项目配置需要包含“项目”数组；示例：" + json.dumps(配置示例, ensure_ascii=False))
    匹配 = [项目 for 项目 in 配置["项目"] if isinstance(项目, dict) and 项目.get("名字") == 名字]
    if len(匹配) != 1:
        raise 脚本错误(f"项目“{名字}”必须在配置中恰好出现一次")
    项目 = 匹配[0]
    for 字段 in 配置示例["项目"][0]:
        值 = 项目.get(字段)
        if not isinstance(值, str) or not 值.strip() or any(ord(字符) < 32 for 字符 in 值):
            raise 脚本错误(f"项目字段“{字段}”需要非空、无控制字符的字符串")
    if not 项目["WSL检出路径"].startswith("/") or 项目["WSL发行版"].startswith("-"):
        raise 脚本错误("WSL 检出路径必须为绝对路径，发行版名不能以横线开头")
    return 项目


def 检查分支名(名字):
    # 拒绝选项、强制 refspec 和表达式；Git 执行前还会校验完整引用格式。
    if (not 名字 or 名字.startswith(("-", "+", "/", ".")) or 名字.endswith(("/", "."))
            or 名字 == "@" or ".." in 名字 or "@{" in 名字 or "//" in 名字
            or re.search(r"[\s\x00-\x20\x7f~^:?*\[\\]", 名字)
            or any(段.startswith(".") or 段.endswith(".lock") for 段 in 名字.split("/"))):
        raise 发现问题(f"分支名不合法：{名字}")


# 在 Linux 内管理 Git 的进程组；终止 Windows 侧 wsl.exe 不能证明 Linux 子进程停止。
合并执行器 = r'''
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

超时 = float(sys.argv[1])
进程 = subprocess.Popen(sys.argv[2:], stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, encoding="utf-8", errors="replace", start_new_session=True)
状态 = "完成"
已停止 = False
try:
    输出, _ = 进程.communicate(timeout=超时)
    已停止 = True
except subprocess.TimeoutExpired:
    状态 = "超时"
    # 该会话仅属于本次 Git；一起终止 hook，随后等待 Git 被回收。
    try:
        os.killpg(进程.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    输出, _ = 进程.communicate(timeout=5)
    进程.wait(timeout=5)
    # 排除仍可继续写入仓库的进程；僵尸进程已停止执行。
    截止 = time.monotonic() + 3
    while True:
        存活 = False
        for 文件 in Path("/proc").glob("[0-9]*/stat"):
            try:
                字段 = 文件.read_text().rsplit(")", 1)[1].split()
                if int(字段[2]) == 进程.pid and 字段[0] not in ("Z", "X"):
                    存活 = True
            except FileNotFoundError:
                continue
        if not 存活:
            已停止 = True
            break
        if time.monotonic() >= 截止:
            break
        time.sleep(0.02)
print(json.dumps({"状态": 状态, "已停止": 已停止,
                  "退出码": 进程.returncode, "输出": 输出}, ensure_ascii=False))
'''


索引锁等待器 = r'''
from pathlib import Path
import subprocess
import sys
import time

截止 = time.monotonic() + float(sys.argv[1])
结果 = subprocess.run([*sys.argv[2:], "rev-parse", "--path-format=absolute", "--git-path", "index.lock"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=max(0.001, 截止 - time.monotonic()))
if 结果.returncode:
    sys.exit(2)
锁 = Path(结果.stdout.strip())
while 锁.exists():
    剩余 = 截止 - time.monotonic()
    if 剩余 <= 0:
        sys.exit(1)
    time.sleep(min(0.05, 剩余))
sys.exit(0)
'''


class Git衔接:
    合并超时秒 = 120
    索引锁等待秒 = 10

    def __init__(self, 项目, 运行命令):
        self.项目 = 项目
        self.运行命令 = 运行命令
        self.子进程已停止 = True

    def 命令(self, 参数, Windows=False):
        路径 = self.项目["Windows检出路径" if Windows else "WSL检出路径"]
        命令 = ["git", "-c", "core.quotePath=false", "-C", 路径, *参数]
        return 命令 if Windows else ["wsl.exe", "-d", self.项目["WSL发行版"], "--exec", *命令]

    def git(self, *参数, Windows=False, 允许失败=False):
        受控合并 = not Windows and 参数[0] == "merge" and "--abort" not in 参数
        命令 = self.命令(list(参数), Windows)
        if 受控合并:
            self.子进程已停止 = False
            命令 = [*命令[:4], "python3", "-c", 合并执行器, str(self.合并超时秒), *命令[4:]]
        try:
            环境 = os.environ.copy()
            环境["GIT_TERMINAL_PROMPT"] = "0"
            结果 = self.运行命令(命令, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                                   timeout=self.合并超时秒 + 15 if 受控合并 else 120,
                                   check=False, env=环境)
        except (OSError, subprocess.TimeoutExpired):
            if 受控合并:
                raise 脚本错误("WSL 合并执行器未正常返回，等待锁释放后检查合并状态") from None
            raise 脚本错误("无法运行 Git/WSL 或命令超过 120 秒，请检查程序、发行版与检出路径") from None
        if 受控合并:
            try:
                回执 = json.loads(结果.stdout) if 结果.returncode == 0 else {}
                if 回执.get("已停止") is not True:
                    raise ValueError
                self.子进程已停止 = True
                if 回执["状态"] == "超时":
                    raise 脚本错误("Git 合并超时，已确认 Git 与 hook 进程组停止")
                结果 = subprocess.CompletedProcess(命令, 回执["退出码"], 回执["输出"], "")
            except (ValueError, KeyError, TypeError):
                raise 脚本错误("WSL 合并执行器未确认子进程停止，等待锁释放后检查合并状态") from None
        if 结果.returncode != 0 and not 允许失败:
            # 不转印 stderr：远程地址或认证错误可能包含凭据。
            raise 发现问题(f"Git {参数[0]} 失败（退出码 {结果.returncode}），请检查仓库状态与引用")
        return 结果

    def 引用(self, 名字, 可缺失=False):
        结果 = self.git("rev-parse", "--verify", "--quiet", "--end-of-options", 名字 + "^{commit}",
                        允许失败=可缺失)
        if 结果.returncode:
            if 结果.returncode == 1:
                return None
            raise 发现问题(f"无法读取提交引用：{名字}")
        return 结果.stdout.strip()

    def 是祖先(self, 旧, 新):
        结果 = self.git("merge-base", "--is-ancestor", 旧, 新, 允许失败=True)
        if 结果.returncode not in (0, 1):
            raise 发现问题("无法判断提交的快进关系")
        return 结果.returncode == 0

    def 确认干净(self):
        if self.git("status", "--porcelain", "--untracked-files=normal").stdout:
            raise 发现问题("工作区有未提交或未跟踪文件，请先处理后再运行")
        for 引用 in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
            if self.引用(引用, 可缺失=True):
                raise 发现问题("仓库已有未完成操作，请先处理，脚本不会中止已有操作")

    def 检查身份(self, 安静=False):
        问题 = []
        Windows身份 = {}
        for 字段 in ("user.name", "user.email"):
            本机 = self.git("config", "--get", 字段, Windows=True, 允许失败=True)
            远端 = self.git("config", "--get", 字段, 允许失败=True)
            if 本机.returncode not in (0, 1) or 远端.returncode not in (0, 1):
                raise 发现问题("无法读取 Git 身份，请检查 Windows 和 WSL 检出路径")
            标准值 = 本机.stdout.strip()
            Windows身份[字段] = 标准值
            if not 标准值:
                问题.append(f"Windows 检出未设置 {字段}，请先在 Windows 检出配置身份")
            elif 远端.returncode or 远端.stdout.strip() != 标准值:
                问题.append("WSL 的 " + 字段 + " 与 Windows 不一致；设置命令（未执行）："
                            + 命令文本(self.命令(["config", "--local", 字段, 标准值])))
        if not 问题:
            # 身份环境变量可能盖过 config；同时检查实际生效的作者和提交者。
            for 字段 in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
                结果 = self.git("var", 字段, 允许失败=True)
                匹配 = re.fullmatch(r"(.*) <([^<>]+)> \d+ [+-]\d{4}", 结果.stdout.strip())
                if (结果.returncode or not 匹配 or 匹配.group(1) != Windows身份["user.name"]
                        or 匹配.group(2) != Windows身份["user.email"]):
                    问题.append(f"WSL 生效的 {字段} 与 Windows 配置不一致，请检查覆盖身份的环境变量")
        if 问题:
            raise 发现问题("\n".join(问题))
        if not 安静:
            print("Windows 与 WSL 的 Git 作者、邮箱一致，生效身份检查通过。")

    def 同步基线(self):
        self.git("fetch", "windows")
        远端 = self.引用("refs/remotes/windows/main")
        本地 = self.引用("refs/heads/main", 可缺失=True)
        print(f"windows/main：{远端}；WSL main：{本地 or '不存在'}")
        if 本地 == 远端 or (本地 and self.是祖先(远端, 本地)):
            print("WSL main 已包含 windows/main。")
            return
        if 本地 and not self.是祖先(本地, 远端):
            raise 发现问题("WSL main 与 windows/main 已分叉，不能快进；未修改 main")
        当前 = self.git("symbolic-ref", "--quiet", "HEAD", 允许失败=True)
        if 当前.returncode == 0 and 当前.stdout.strip() == "refs/heads/main":
            self.确认干净()
            self.合并一次(("merge", "--ff-only", "--no-edit", "--no-overwrite-ignore", "refs/remotes/windows/main"), 远端)
        else:
            # fetch 的快进和已检出分支保护仍然生效，也不切换当前工作分支。
            self.git("fetch", ".", "refs/remotes/windows/main:refs/heads/main")
        print("WSL main 已快进到 windows/main。")

    def 取分支(self, 分支):
        # 原子获取：任何分支不能快进时，整批都不更新；没有强制标记。
        self.git("fetch", "--atomic", "windows", *[f"refs/heads/{名}:refs/heads/{名}" for 名 in 分支])
        print(f"已取入 {len(分支)} 个分支；已有分支仅允许快进。")

    def 取回(self, 分支):
        引用信息 = self.git("for-each-ref", "--format=%(refname)%00%(symref)", "refs/heads/", Windows=True).stdout
        for 行 in 引用信息.splitlines():
            已有引用, 分隔, 符号目标 = 行.partition("\0")
            if not 分隔:
                raise 脚本错误("Windows 分支引用信息格式不正确")
            for 名字 in 分支:
                目标引用 = "refs/heads/" + 名字
                if 已有引用.casefold() == 目标引用.casefold() and 已有引用 != 目标引用:
                    raise 发现问题(f"取回目标与 Windows 已有引用大小写冲突：{目标引用} / {已有引用}")
                if 已有引用 == 目标引用 and 符号目标:
                    raise 发现问题(f"取回目标是 Windows 符号引用，拒绝穿透更新：{目标引用}")
        路径 = "//wsl.localhost/" + self.项目["WSL发行版"] + self.项目["WSL检出路径"]
        self.git("fetch", "--atomic", 路径, *[f"refs/heads/{名}:refs/heads/{名}" for 名 in 分支], Windows=True)
        print(f"已取回 {len(分支)} 个分支到 Windows；已有分支仅允许快进。")

    def 保护忽略文件(self, 目标提交):
        # merge 的某些合并策略仍会覆盖忽略文件，不能只依赖命令行选项。
        忽略项 = self.git("ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z").stdout
        if not 忽略项:
            return
        目标文件 = set(filter(None, self.git("ls-tree", "-r", "--name-only", "-z", 目标提交).stdout.split("\0")))
        目标目录 = set()
        for 路径 in 目标文件:
            段 = 路径.split("/")
            目标目录.update("/".join(段[:层]) for 层 in range(1, len(段)))
        for 路径 in filter(None, 忽略项.split("\0")):
            本地 = 路径.rstrip("/")
            段 = 本地.split("/")
            if (本地 in 目标文件 or 本地 in 目标目录
                    or any("/".join(段[:层]) in 目标文件 for 层 in range(1, len(段)))):
                raise 发现问题("目标将覆盖被忽略的本地文件，请先处理：" + 路径)

    def 等待索引锁(self):
        """重新调用 Git 定位当前工作区的锁，最多等十秒，不删除锁文件。"""
        秒数 = min(10, self.索引锁等待秒)
        原命令 = self.命令([])
        命令 = [*原命令[:4], "python3", "-c", 索引锁等待器, str(秒数), *原命令[4:]]
        try:
            结果 = self.运行命令(命令, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   stdin=subprocess.DEVNULL, timeout=秒数 + 2, check=False,
                                   env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        except (OSError, subprocess.TimeoutExpired):
            return False
        return 结果.returncode == 0

    def 当前合并状态(self):
        状态 = []
        for 名字 in ("HEAD", "MERGE_HEAD"):
            try:
                值 = self.引用(名字, 可缺失=True) or "不存在"
            except (发现问题, 脚本错误):
                值 = "读取失败"
            状态.append(f"{名字}：{值}")
        return "；".join(状态)

    def 清理本次合并(self, 目标提交):
        if not self.等待索引锁():
            raise 脚本错误("索引锁未释放或无法检查，请人工确认；" + self.当前合并状态())
        try:
            合并头 = self.引用("MERGE_HEAD", 可缺失=True)
        except (发现问题, 脚本错误):
            raise 脚本错误("无法检查合并状态，请人工确认；" + self.当前合并状态()) from None
        if 合并头 and 合并头 != 目标提交:
            raise 脚本错误("MERGE_HEAD 与本次分支提交不同，请人工确认；" + self.当前合并状态())
        if 合并头:
            try:
                中止 = self.git("merge", "--abort", 允许失败=True)
                if 中止.returncode:
                    raise 发现问题("中止失败")
            except (发现问题, 脚本错误):
                raise 脚本错误("merge --abort 失败，请人工确认；" + self.当前合并状态()) from None

    def 合并一次(self, 参数, 目标提交):
        self.保护忽略文件(目标提交)
        if self.引用("MERGE_HEAD", 可缺失=True):
            raise 发现问题("仓库已有未完成合并，脚本不会中止已有操作")
        完成 = False
        try:
            结果 = self.git(*参数, 允许失败=True)
            if 结果.returncode:
                # 采集也在 finally 保护范围内，采集失败不能跳过清理。
                冲突 = self.git("diff", "--name-only", "--diff-filter=U", "-z").stdout
                for 文件名 in filter(None, 冲突.split("\0")):
                    print("冲突文件：" + 文件名)
                raise 发现问题(f"Git 合并失败（退出码 {结果.returncode}），本次未完成合并会中止；此前成功的合并保留")
            完成 = True
        finally:
            if not 完成:
                self.清理本次合并(目标提交)

    def 合并(self, 目标, 基线, 分支):
        self.检查身份(安静=True)
        self.确认干净()
        self.git("fetch", "windows")
        基线提交 = self.引用("refs/remotes/" + 基线)
        for 名字 in 分支:
            self.引用("refs/heads/" + 名字)
        原目标 = self.引用("refs/heads/" + 目标, 可缺失=True)
        需要快进 = False
        if 原目标 and not self.是祖先(基线提交, 原目标):
            if not self.是祖先(原目标, 基线提交):
                raise 发现问题("已有集成分支未包含当前 windows/main，且不能快进到该基线；未修改目标")
            需要快进 = True
        self.保护忽略文件(原目标 or 基线提交)
        if 原目标:
            self.git("switch", "--no-overwrite-ignore", 目标)
            if 需要快进:
                self.合并一次(("merge", "--ff-only", "--no-edit", "--no-overwrite-ignore", 基线提交), 基线提交)
        else:
            self.git("switch", "--no-overwrite-ignore", "--no-track", "--create", 目标, 基线提交)
        for 名字 in 分支:
            分支提交 = self.引用("refs/heads/" + 名字)
            self.合并一次(("merge", "--no-ff", "--no-edit", "--no-overwrite-ignore", "--", "refs/heads/" + 名字),
                         分支提交)
        print(f"已在 {目标} 基于 {基线} 合并 {len(分支)} 个分支。")

    def 演练(self, 参数):
        def 打印(*命令参数, Windows=False, 条件=""):
            print((条件 + "：" if 条件 else "") + 命令文本(self.命令(list(命令参数), Windows)))

        if 参数.子命令 in ("检查身份", "合并"):
            for 字段 in ("user.name", "user.email"):
                打印("config", "--get", 字段, Windows=True)
                打印("config", "--get", 字段)
            for 字段 in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
                打印("var", 字段)
        if 参数.子命令 == "同步基线":
            打印("fetch", "windows")
            打印("rev-parse", "refs/remotes/windows/main", "refs/heads/main")
            打印("merge-base", "--is-ancestor", "refs/heads/main", "refs/remotes/windows/main")
            打印("merge", "--ff-only", "--no-edit", "--no-overwrite-ignore", "refs/remotes/windows/main", 条件="仅当前分支为 main 且可快进时")
            打印("fetch", ".", "refs/remotes/windows/main:refs/heads/main", 条件="仅 main 未被检出且可快进或缺失时")
        elif 参数.子命令 == "取分支":
            打印("fetch", "--atomic", "windows", *[f"refs/heads/{名}:refs/heads/{名}" for 名 in 参数.分支])
        elif 参数.子命令 == "取回":
            打印("for-each-ref", "--format=%(refname)%00%(symref)", "refs/heads/", Windows=True)
            路径 = "//wsl.localhost/" + self.项目["WSL发行版"] + self.项目["WSL检出路径"]
            打印("fetch", "--atomic", 路径, *[f"refs/heads/{名}:refs/heads/{名}" for 名 in 参数.分支], Windows=True,
                 条件="实际执行时先检查全部 Windows 分支，拒绝大小写冲突或符号引用，通过后再取回")
        elif 参数.子命令 == "合并":
            打印("status", "--porcelain", "--untracked-files=normal")
            打印("fetch", "windows")
            打印("switch", "--no-overwrite-ignore", "--no-track", "--create", 参数.目标, 参数.基线, 条件="目标不存在时")
            打印("switch", "--no-overwrite-ignore", 参数.目标, 条件="目标已包含基线或可快进时")
            打印("merge", "--ff-only", "--no-edit", "--no-overwrite-ignore", 参数.基线, 条件="已有目标落后基线时")
            for 名字 in 参数.分支:
                打印("merge", "--no-ff", "--no-edit", "--no-overwrite-ignore", "--", "refs/heads/" + 名字)
            打印("diff", "--name-only", "--diff-filter=U", "-z", 条件="合并失败时")
            打印("merge", "--abort", 条件="本次合并未完成时")


def 参数解析(参数):
    解析器 = argparse.ArgumentParser(description="Windows 与 WSL 检出衔接，不强推、不重置、不推送")
    子命令 = 解析器.add_subparsers(dest="子命令", required=True, title="子命令")
    for 名字 in ("同步基线", "取分支", "取回", "合并", "检查身份", "初始化基线"):
        子 = 子命令.add_parser(名字)
        子.add_argument("--项目", required=True, help="个人配置目录下 项目.json 中的项目名字")
        子.add_argument("--演练", action="store_true", help="仅打印命令，不执行任何命令")
        if 名字 in ("取分支", "取回", "合并"):
            子.add_argument("分支", nargs="+", help=("待取回 Windows 的 WSL 分支" if 名字 == "取回"
                                                  else "Windows 已取入 WSL 的分支，或待取的 Windows 分支"))
        if 名字 == "合并":
            子.add_argument("--目标", required=True, help="集成分支名")
            子.add_argument("--基线", required=True, choices=["windows/main"], help="使用 Windows 的 main 基线")
    return 解析器.parse_args(参数)


def 主函数(参数=None, *, 配置路径=None, 运行命令=subprocess.run):
    try:
        try:
            选项 = 参数解析(参数)
        except SystemExit as 错误:
            return 错误.code
        项目 = 读取项目(Path(配置路径) if 配置路径 is not None else 默认配置, 选项.项目)
        for 名字 in getattr(选项, "分支", []):
            检查分支名(名字)
        if 选项.子命令 == "取回" and any(名字.casefold() in ("main", "master") for 名字 in 选项.分支):
            raise 发现问题("取回目标不能是 main/master，合进主分支需用户同意后手动操作")
        if 选项.子命令 == "合并":
            检查分支名(选项.目标)
            if 选项.目标 in ("main", "master") or 选项.目标 in 选项.分支:
                raise 发现问题("集成目标不能是 main/master，也不能是待合并分支")
        if 选项.子命令 == "初始化基线":
            print(命令文本(["orca", "repo", "set-base-ref", "--environment", 项目["Orca远程环境"],
                            "--repo", "id:" + 项目["WSL仓库ID"], "--ref", "windows/main", "--json"]))
            return 0
        衔接 = Git衔接(项目, 运行命令)
        if 选项.演练:
            衔接.演练(选项)
        elif 选项.子命令 == "同步基线":
            衔接.同步基线()
        elif 选项.子命令 == "取分支":
            衔接.取分支(选项.分支)
        elif 选项.子命令 == "取回":
            衔接.取回(选项.分支)
        elif 选项.子命令 == "检查身份":
            衔接.检查身份()
        else:
            衔接.合并(选项.目标, 选项.基线, 选项.分支)
        return 0
    except 发现问题 as 错误:
        print(str(错误), file=sys.stderr)
        return 1
    except (脚本错误, OSError, ValueError) as 错误:
        print(str(错误), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(主函数())
