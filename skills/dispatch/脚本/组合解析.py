"""共享组合解析、本机配置和带超时的只读命令执行。"""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict


class CombinationCommand(TypedDict):
    kind: str
    args: list[str]


class Combination(TypedDict):
    groups: dict[str, list[str]]
    member_specs: dict[str, list[str]]
    claude: dict[str, str]
    claude_effort: dict[str, str]
    commands: list[CombinationCommand]


class CommandFailure(RuntimeError):
    """外部检查失败；异常消息不包含命令输出或账户信息。"""


def configure_utf8_streams() -> None:
    """入口统一使用 UTF-8，避免 Windows 默认 GBK 无法输出检查符号。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def run_cli(main: Callable[[], int]) -> int:
    """统一 CLI 编码与意外错误退出码，不泄露异常原文中的敏感数据。"""
    try:
        configure_utf8_streams()
        return main()
    except Exception as exc:
        try:
            print(f"脚本错误：未预期异常（{type(exc).__name__}）", file=sys.stderr)
        except (OSError, UnicodeError, ValueError):
            pass
        return 2


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)


def normalize_model(value: str) -> str:
    """落点只比较来源和模型，体检另外比较完整成员写法。"""
    return value.strip().split(":", 1)[0]


def normalize_target(value: str) -> str:
    return value.strip().removesuffix("[1m]")


COMBINATION_COMMAND = re.compile(r"magpie\s+(group\s+(?:rule\s+(?:add|rm)|add|set)|model\s+disable|claude)(?=\s|$)")
COMBINATION_COMMAND_FAMILY = re.compile(r"magpie(?:\s+(?:group(?:\s+rule)?|model|claude))?(?=\s|$)")
# 组行认的键（依据 magpie 0.1.4940 的 applyGroupPairs：group add 和 group set 共用，键名不分大小写）。
# 每组第一个是正名，其余是别名。
GROUP_FIELD_GROUPS = (
    ("id",), ("name",),
    ("models", "members", "model"),
    ("routing", "strategy"),
    ("stays", "stay", "affinity"),
    ("context",),
    ("compact", "compact-at"),
    ("levels", "level"),
    ("family", "tag"),
    ("note", "description", "desc", "remark"),
    ("listed",),
    ("effort", "reasoning"),
    ("classifier", "classify", "classifiers"),
    ("intent-sure", "intentsure"),
    ("classify-prompt", "classifyprompt", "classifier-prompt"),
    ("fast",), ("ultra",),
    ("weight", "weights"),
    ("pick", "use"),
)
GROUP_FIELD_ALIASES = {spelling: field for field, *aliases in GROUP_FIELD_GROUPS for spelling in (field, *aliases)}
GROUP_FIELDS = frozenset(GROUP_FIELD_ALIASES.values())
# group add 里这几个键指向成员，magpie 按书写顺序执行，要等 models 把成员加进组里才认得。
GROUP_MEMBER_POINTERS = frozenset(("fast", "ultra", "weight", "pick"))
# models= 换的是整份名单，会清空 fast 和 ultra：写在它前面的 fast、ultra 会被悄悄清掉，group add、group set 都这样
# （group add 已由上面那条更严的检查管住）。group set 里的 weight、pick 不查：它们所指的成员可能本来就在组里，
# 不在时 magpie 让整条命令失败，不会悄悄丢东西。
GROUP_CLEARED_BY_MODELS = frozenset(("fast", "ultra"))
# 追加、移除成员的写法在 magpie 里是独立的键，不是 models 的别名：members+= 和 models+= 是同一个键，但和 models= 不是。
GROUP_MEMBER_APPEND_REMOVE = frozenset(f"{spelling}{mark}" for spelling in ("models", "members", "model") for mark in "+-")
RULE_FLAGS = {"images", "compact"}
RULE_CONDITIONS = frozenset(("tokens", "effort", "agents", "intent", "time", "days", "folders", "except-folders", "when"))
RULE_FIELDS = RULE_CONDITIONS | {"use", "pause", "use-effort", "tz", "classifier", "at"}
FIELD_NAME = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]*")
CLAUDE_ALIASES = {"opus", "sonnet", "haiku", "fable", "subagent"}


def redact_account_lock(value: str) -> str:
    """展示成员时，账号锁只显示井号后的前四位。"""
    model, marker, account = value.partition("#")
    return f"{model}{marker}{account[:4]}…" if account else value


def _split_arguments(text: str) -> list[str]:
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        field, separator, _ = lexer.token.partition("=")
        label = f"字段 {field}" if separator and FIELD_NAME.fullmatch(field) else "参数"
        raise ValueError(f"{label}的引号或转义不完整") from None


def _option_pairs(args: list[str], keys: frozenset[str], aliases: dict[str, str] | None = None) -> list[tuple[str, str, str]]:
    """拆出 键=值，返回（键名, 小写的写法, 值）；给了别名表就不分大小写，别名归成正名。"""
    options: list[tuple[str, str, str]] = []
    for arg in args:
        key, separator, value = arg.partition("=")
        if not separator or not FIELD_NAME.fullmatch(key):
            raise ValueError("字段须写成 键=值")
        written = key.lower()
        field = aliases.get(written, written) if aliases is not None else key
        if field not in keys:
            raise ValueError(f"不支持字段 {key}")
        options.append((field, written, value))
    return options


def _group_pairs(fields: list[str]) -> list[tuple[str, str, str]]:
    """group add / group set 的键值对。追加、移除成员的键是在现有成员上增减，体检没法据此核对，拒绝并说明原因。"""
    for field in fields:
        key, separator, _ = field.partition("=")
        if separator and key.lower() in GROUP_MEMBER_APPEND_REMOVE:
            raise ValueError(f"不支持 {key}=：它是在现有成员上追加或移除，体检没法据此核对组合是否一致，请写完整的 models=")
    return _option_pairs(fields, GROUP_FIELDS, GROUP_FIELD_ALIASES)


def _require_models_first(pairs: list[tuple[str, str, str]], keys: frozenset[str], reason: str) -> None:
    """keys 里的键得写在成员清单后面，reason 说明原因；写了别名时注明它对应的正名。"""
    members = next((written for key, written, _ in pairs if key == "models"), "models")
    for key, written, _ in pairs:
        if key == "models":
            return
        if key in keys:
            notes = [f"{spelling} 是 {field} 的别名" for field, spelling in (("models", members), (key, written)) if spelling != field]
            hint = f"（{'，'.join(notes)}）" if notes else ""
            raise ValueError(f"{written} 须写在 {members} 后面：{reason}{hint}")


def _parse_members(value: str, field: str = "models") -> list[str]:
    members = value.split(",")
    for index, member in enumerate(members, 1):
        if not re.fullmatch(r"[^\s/:,#]+/[^\s/:,#]+(?::[^\s,#]+)?(?:#[^\s,#]+)?", member):
            _, marker, account = member.partition("#")
            lock = f"（账号锁 {redact_account_lock(marker + account)}）" if marker else ""
            raise ValueError(f"{field} 第 {index} 个成员格式错误{lock}")
    return members


def _validate_rule_add(args: list[str]) -> None:
    flags = RULE_FLAGS.intersection(args)
    options = _option_pairs([arg for arg in args if arg not in flags], RULE_FIELDS)
    keys = {key for key, _, _ in options}
    if len(keys & {"use", "pause"}) != 1:
        raise ValueError("须提供 use 或 pause 字段，两者只能选一项")
    if not flags and not keys & RULE_CONDITIONS:
        raise ValueError("缺少规则条件，至少提供一个条件（如 tokens 或 when）")
    for key, _, value in options:
        if not value.strip():
            raise ValueError(f"{key} 字段不能为空")
        if key == "when":
            parts = value.split(maxsplit=2)
            if len(parts) != 3 and parts not in (["subagent"], ["not", "subagent"]):
                raise ValueError("when 字段须包含字段、操作符和值，或使用 subagent 条件")


def parse_combination_file(path: Path) -> Combination:
    """提取成员和别名，并按文件顺序保留已经拆分参数的命令。"""
    specs: dict[str, list[str]] = {}
    added_groups: set[str] = set()
    aliases: dict[str, str] = {}
    efforts: dict[str, str] = {}
    commands: list[CombinationCommand] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = COMBINATION_COMMAND.match(line)
        if not match:
            family = COMBINATION_COMMAND_FAMILY.match(line)
            label = f"（{' '.join(family[0].split())}）" if family else ""
            raise ValueError(f"组合文件第 {number} 行{label}：不支持的命令")
        kind = " ".join(match[1].split())
        try:
            args = _split_arguments(line[match.end():])
            if kind in ("group add", "group set") and args and "=" in args[0]:
                # 组名里不能有等号；这个位置上出现了 键=值，多半是漏了组名。
                raise ValueError(f"组名里不能有等号：magpie {kind} 后面第一个词是组名，这一行一上来就是 键=值，多半是漏了组名")
            if kind.startswith("group ") and (not args or not args[0] or "=" in args[0]):
                raise ValueError("缺少组名")
            if kind in ("group add", "group set"):
                name, *fields = args
                pairs = _group_pairs(fields)
                options: dict[str, str] = {}
                spellings: dict[str, str] = {}
                for key, written, value in pairs:
                    if key in spellings:
                        first = spellings[key]
                        same = f"（{first} 和 {written} 是同一个键）" if first != written else ""
                        raise ValueError(f"字段 {key} 重复{same}")
                    spellings[key] = written
                    options[key] = value
                if kind == "group add":
                    if "models" not in options:
                        raise ValueError("缺少 models 字段")
                    _require_models_first(pairs, GROUP_MEMBER_POINTERS, "group add 按书写顺序执行，fast、ultra、weight、pick 指向的成员要先由 models 加进组里")
                    if name in added_groups:
                        raise ValueError("重复定义组")
                    added_groups.add(name)
                elif not options:
                    raise ValueError("缺少要设置的字段")
                elif "models" in options:
                    _require_models_first(pairs, GROUP_CLEARED_BY_MODELS, "models= 会清空 fast 和 ultra，先写的会被清掉")
                if "models" in options:
                    specs[name] = _parse_members(options["models"], spellings["models"])
            elif kind == "model disable":
                if len(args) != 1 or not args[0]:
                    raise ValueError("须提供一个模型通配")
            elif kind == "group rule add":
                _validate_rule_add(args[1:])
            elif kind == "group rule rm":
                if len(args) != 2:
                    raise ValueError("须只带一个规则序号")
                if not re.fullmatch(r"0*[1-9][0-9]*", args[1]):
                    raise ValueError("规则序号须为正整数")
            else:
                if not args:
                    raise ValueError("须写成 magpie claude <别名> <目标>；别名不带值是写操作，会把 Claude Code 切出 magpie，组合文件里不能这么写")
                field = args[0]
                if field.removesuffix("_effort") not in CLAUDE_ALIASES:
                    raise ValueError("不支持的 Claude 档位字段")
                if len(args) == 1:
                    # 四个别名不带值是写操作（手册：会把 Claude Code 切出 magpie），所以要和“多带参数”分开说。
                    if field.endswith("_effort"):
                        raise ValueError(f"{field} 后面要跟一个推理档（medium、high 或 xhigh）")
                    raise ValueError(f"{field} 不带值是写操作，会把 Claude Code 切出 magpie，组合文件里不能这么写")
                if len(args) != 2 or not args[1] or not re.fullmatch(r"\S+", args[1]):
                    raise ValueError(f"{field} 只接受一个值")
                value = args[1]
                if field.endswith("_effort"):
                    if value not in ("medium", "high", "xhigh"):
                        raise ValueError(f"{field} 的推理档位无效")
                    efforts[field.removesuffix("_effort")] = value
                else:
                    aliases[field] = normalize_target(value)
        except ValueError as exc:
            raise ValueError(f"组合文件第 {number} 行（magpie {kind}）：{exc}") from None
        commands.append({"kind": kind, "args": args})
    if not any(command["kind"] in ("group add", "group set") for command in commands):
        raise ValueError("组合文件没有有效的 magpie group add 或 group set 行")
    return {
        "groups": {name: [normalize_model(member.partition("#")[0]) for member in members] for name, members in specs.items()},
        "member_specs": specs,
        "claude": aliases,
        "claude_effort": efforts,
        "commands": commands,
    }


def config_root(dispatch_root: Path) -> Path:
    """环境变量 → 含 config.toml 的新目录 → 两处旧目录 → 新目录落点。"""
    env = os.environ.get("DISPATCH_CONFIG_DIR")
    if env:
        return Path(env).expanduser()
    default = Path.home() / ".dispatch"
    if (default / "config.toml").is_file():
        return default
    for legacy in (Path.home() / ".claude" / "派活配置", dispatch_root / "本机"):
        if legacy.is_dir():
            return legacy
    return default


def combination_dir(dispatch_root: Path) -> Path:
    """在用组合放在个人配置的 组合/ 下；那里还没有 当前组合.txt 时用 skill 自带的 组合/。"""
    personal = config_root(dispatch_root) / "组合"
    return personal if (personal / "当前组合.txt").is_file() else dispatch_root / "组合"


def load_current_combination(dispatch_root: Path) -> Combination:
    folder = combination_dir(dispatch_root)
    names = [line.strip() for line in (folder / "当前组合.txt").read_text(encoding="utf-8-sig").splitlines() if line.strip() and not line.strip().startswith("#")]
    if len(names) != 1 or re.search(r'[\\/:*?"<>|]', names[0]) or names[0] in (".", ".."):
        raise ValueError("当前组合.txt 必须只有一个有效组合名")
    return parse_combination_file(folder / f"{names[0]}.txt")


def parse_magpie_groups_output(output: str) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for line in strip_ansi(output).splitlines():
        match = re.search(r"(?:^|\s)group/([^\s]+)\s+(.+)", line)
        if match:
            name, tail = match.groups()
            tail = tail.split("←", 1)[0]
            members = re.findall(r"(?<!\S)([^\s/,]+/[^\s,·→]+)", tail)
            if members:
                groups[name] = members
    return groups


def parse_magpie_claude_output(output: str) -> dict[str, str]:
    mappings: dict[str, str] = {}
    for alias, target in re.findall(r"\b(opus|sonnet|haiku|fable|subagents?)\s+(\S+/\S+)", strip_ansi(output)):
        mappings["subagent" if alias == "subagents" else alias] = normalize_target(target)
    return mappings


def parse_magpie_claude_main_model(output: str) -> str:
    match = re.search(r"(?:^|\n)\s*Claude Code\s+(\S+/\S+)", strip_ansi(output))
    if match is None:
        raise ValueError("无法解析 magpie claude 的主会话模型")
    return normalize_target(match.group(1))


def parse_magpie_claude_effort_output(output: str) -> dict[str, str]:
    """仅提取具名别名的推理档，不把主会话 effort 和 auto mode 当成别名。"""
    return {"subagent" if alias == "subagents" else alias: level for alias, level in re.findall(r"\b(opus|sonnet|haiku|fable|subagents?)\s+effort\s+(\S+)", strip_ansi(output))}


def load_config(path: Path) -> dict:
    if not path.exists():
        raise ValueError('缺少 个人配置目录（~/.claude/派活配置）下的 检查配置.json；示例：{"检查超时秒":30,"远程环境":[],"扩展检查":[],"落点检查":{"兜底最少请求数":10,"兜底占比阈值":0.3}}')
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError("检查配置.json 无法读取或不是有效 JSON") from exc
    if not isinstance(config, dict):
        raise ValueError("检查配置必须是 JSON 对象")
    timeout = config.setdefault("检查超时秒", 30)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("检查超时秒必须是正数")
    worker_limit = config.setdefault("worker上限", 7)
    if type(worker_limit) is not int or worker_limit <= 0:
        raise ValueError("worker上限必须是正整数")
    landing = config.setdefault("落点检查", {})
    if not isinstance(landing, dict):
        raise ValueError("落点检查配置必须是对象")
    for key, default, lower, upper in (("兜底最少请求数", 10, 0, math.inf), ("兜底占比阈值", 0.3, 0, 1), ("通知间隔秒", 3600, 1, math.inf)):
        value = landing.setdefault(key, default)
        if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper:
            raise ValueError(f"落点检查配置中的 {key} 超出有效范围")
    if type(landing["兜底最少请求数"]) is not int:
        raise ValueError("兜底最少请求数必须是整数")
    environments = config.setdefault("远程环境", [])
    if not isinstance(environments, list) or any(not isinstance(name, str) or not name.strip() for name in environments):
        raise ValueError("远程环境必须是环境名列表")
    rules = config.setdefault("扩展检查", [])
    if not isinstance(rules, list):
        raise ValueError("扩展检查必须是规则列表")
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get("名称"), str) or not rule["名称"].strip():
            raise ValueError("扩展检查缺少名称")
        command = rule.get("命令")
        if not isinstance(command, list) or not command or any(not isinstance(arg, str) or not arg for arg in command):
            raise ValueError("扩展检查命令必须是非空参数列表，不经 shell 执行")
        try:
            regex = re.compile(rule["正则"])
        except (KeyError, TypeError, re.error) as exc:
            raise ValueError("扩展检查正则无效") from exc
        if regex.groups != 1:
            raise ValueError("扩展检查正则必须有且仅有一个数值捕获组")
        threshold = rule.get("阈值")
        if type(threshold) not in (int, float) or not math.isfinite(threshold):
            raise ValueError("扩展检查阈值必须是有限数值")
        if rule.get("比较", ">=") not in (">=", ">", "<=", "<", "==", "!="):
            raise ValueError("扩展检查比较符只支持 >=、>、<=、<、==、!=")
    return config


def write_state(path: Path, data: dict) -> None:
    """用原子替换避免中断后留下半截 JSON；临时文件也只在本机状态目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def run_command(command: list[str], timeout: float = 30, *, environment_updates: dict[str, str | None] | None = None) -> str:
    executable = shutil.which(command[0])
    if executable is None:
        raise CommandFailure(f"命令 {Path(command[0]).name} 未找到")
    environment = os.environ.copy()
    for key, value in (environment_updates or {}).items():
        if value is None:
            environment.pop(key, None)
        else:
            environment[key] = value
    environment["NO_COLOR"] = "1"
    process = subprocess.Popen([executable, *command[1:]], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env=environment, start_new_session=os.name != "nt")
    try:
        stdout, _stderr = process.communicate(timeout=max(0.001, timeout))
    except subprocess.TimeoutExpired as exc:
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        # 读线程可能持有缓冲层锁，直接关闭原始管道句柄。
                        getattr(getattr(stream, "buffer", stream), "raw", stream).close()
                    except (OSError, ValueError):
                        pass
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=1)
            except (subprocess.TimeoutExpired, OSError):
                pass
        raise CommandFailure(f"命令 {Path(command[0]).name} 超时（{timeout:g} 秒）") from exc
    if process.returncode != 0:
        raise CommandFailure(f"命令 {Path(command[0]).name} 失败，退出码 {process.returncode}")
    return strip_ansi(stdout)


if __name__ == "__main__":
    sys.exit(run_cli(lambda: 0))
