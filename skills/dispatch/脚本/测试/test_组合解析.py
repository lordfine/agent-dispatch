"""组合文件与 CLI 展示格式的定向测试。"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from 组合解析 import Combination, CommandFailure, load_current_combination, parse_combination_file, run_command

# 组行认的键，依据 magpie 0.1.4940 的 applyGroupPairs：键名不分大小写，别名和正名当成同一个键。
# 这里照规格另抄一份，不引用解析器里的表，表写错了能测出来。
组键表 = {
    "id": (), "name": (),
    "models": ("members", "model"),
    "routing": ("strategy",),
    "stays": ("stay", "affinity"),
    "context": (),
    "compact": ("compact-at",),
    "levels": ("level",),
    "family": ("tag",),
    "note": ("description", "desc", "remark"),
    "listed": (),
    "effort": ("reasoning",),
    "classifier": ("classify", "classifiers"),
    "intent-sure": ("intentsure",),
    "classify-prompt": ("classifyprompt", "classifier-prompt"),
    "fast": (), "ultra": (),
    "weight": ("weights",),
    "pick": ("use",),
}


class 组合解析测试(unittest.TestCase):
    def 解析文本(self, 文本: str) -> Combination:
        with tempfile.TemporaryDirectory() as 临时目录:
            文件 = Path(临时目录) / "组合.txt"
            _ = 文件.write_text(文本, encoding="utf-8")
            return parse_combination_file(文件)

    def test_模型停用通配保留原样(self):
        结果 = self.解析文本("magpie model disable 示例来源/*\nmagpie group add impl models=来源/模型\n")
        self.assertEqual(结果["commands"][0], {"kind": "model disable", "args": ["示例来源/*"]})
        self.assertEqual(结果["groups"], {"impl": ["来源/模型"]})

    def test_嵌套组保留在成员顺序中(self):
        结果 = self.解析文本("magpie group add review routing=order models=group/示例池,来源/模型:high\n")
        self.assertEqual(结果["groups"]["review"], ["group/示例池", "来源/模型"])
        self.assertEqual(结果["member_specs"]["review"], ["group/示例池", "来源/模型:high"])

    def test_档位范围和账号锁保留在完整成员中(self):
        成员 = "来源/主力:medium+high+xhigh#demo-account,来源/兜底#test-account"
        结果 = self.解析文本(f"magpie group add 示例池 models={成员} levels=medium,high,xhigh\n")
        self.assertEqual(结果["member_specs"]["示例池"], 成员.split(","))
        self.assertEqual(结果["groups"]["示例池"], ["来源/主力", "来源/兜底"])

    def test_group_set更新成员并可单独改组属性(self):
        结果 = self.解析文本("\n".join([
            "magpie group add review models=来源/初始:medium",
            "magpie group set review routing=order models=来源/新成员:high levels=high",
            'magpie group set review stays=turn note="供审查使用"',
        ]))
        self.assertEqual(结果["member_specs"]["review"], ["来源/新成员:high"])
        self.assertEqual(结果["commands"][-1], {
            "kind": "group set", "args": ["review", "stays=turn", "note=供审查使用"],
        })

    def test_同一行重复字段报错而不是悄悄取最后一个(self):
        for 行, 命令, 字段 in (
            ("magpie group add review models=来源/甲 models=来源/乙", "magpie group add", "models"),
            ("magpie group set explore routing=order routing=smart", "magpie group set", "routing"),
        ):
            with self.subTest(行=行):
                with self.assertRaises(ValueError) as 异常:
                    _ = self.解析文本(f"magpie group add explore models=来源/模型\n{行}\n")
                消息 = str(异常.exception)
                for 字样 in ("第 2 行", 命令, 字段, "重复"):
                    self.assertIn(字样, 消息)

    def test_组行认全每个正名和它的别名且两种组行都认(self):
        for 正名, 别名们 in 组键表.items():
            for 写法 in (正名, *别名们):
                for 命令 in ("group add", "group set"):
                    with self.subTest(写法=写法, 命令=命令):
                        if 正名 == "models":
                            字段 = [f"{写法}=来源/模型:high,group/示例池"]
                            成员 = ["来源/模型:high", "group/示例池"]
                        else:
                            # 指向成员的键在 group add 里要排在 models 后面，所以 models 写在前面。
                            字段 = ["models=来源/模型:high", f"{写法}=测试值"]
                            成员 = ["来源/模型:high"]
                        结果 = self.解析文本(f"magpie group add explore models=来源/模型\nmagpie {命令} review {' '.join(字段)}\n")
                        self.assertEqual(结果["commands"][-1], {"kind": 命令, "args": ["review", *字段]})
                        self.assertEqual(结果["member_specs"]["review"], 成员)

    def test_组行键名大小写混写能解析(self):
        结果 = self.解析文本("magpie group add review Models=来源/模型:high Routing=order Stays=turn LEVELS=high Compact-At=100000 Note=说明\n")
        self.assertEqual(结果["member_specs"]["review"], ["来源/模型:high"])
        self.assertEqual(结果["commands"][0]["args"], ["review", "Models=来源/模型:high", "Routing=order", "Stays=turn", "LEVELS=high", "Compact-At=100000", "Note=说明"])

    def test_别名和正名重复或只差大小写都报重复(self):
        for 正名, 别名们 in 组键表.items():
            for 别名 in 别名们:
                # 两种先后都算重复；提示语按书写顺序点出两种写法。
                for 第一个, 第二个 in ((正名, 别名), (别名, 正名)):
                    行 = f"magpie group set review {第一个}=测试密钥甲 {第二个}=测试密钥乙"
                    with self.subTest(行=行):
                        with self.assertRaises(ValueError) as 异常:
                            _ = self.解析文本(f"magpie group add explore models=来源/模型\n{行}\n")
                        消息 = str(异常.exception)
                        for 字样 in ("第 2 行", "magpie group set", f"字段 {正名} 重复", f"{第一个} 和 {第二个} 是同一个键"):
                            self.assertIn(字样, 消息)
                        self.assertNotIn("测试密钥", 消息)
            for 第一个, 第二个 in ((正名, 正名.upper()), (正名.upper(), 正名)):
                with self.subTest(第一个=第一个, 第二个=第二个):
                    with self.assertRaises(ValueError) as 异常:
                        _ = self.解析文本(f"magpie group add explore models=来源/模型\nmagpie group set review {第一个}=测试密钥甲 {第二个}=测试密钥乙\n")
                    消息 = str(异常.exception)
                    for 字样 in ("第 2 行", "magpie group set", f"字段 {正名} 重复"):
                        self.assertIn(字样, 消息)
                    self.assertNotIn("测试密钥", 消息)

    def test_追加或移除成员的键报错说明原因(self):
        # 带加号、减号的六个键（models+、members+、model+、models-、members-、model-）在 magpie 里是独立的键，不是 models 的别名：
        # members+= 和 models+= 是同一个键，和 models= 不是。六个都拒，说明原因。
        for 行, 命令, 键 in (
            ("magpie group add review models=来源/甲 models+=来源/乙", "magpie group add", "models+="),
            ("magpie group add review models-=来源/甲 models=来源/乙", "magpie group add", "models-="),
            ("magpie group set review members+=测试密钥", "magpie group set", "members+="),
            ("magpie group set review model-=测试密钥", "magpie group set", "model-="),
            ("magpie group set review Models+=测试密钥", "magpie group set", "Models+="),
            ("magpie group set review members-=测试密钥", "magpie group set", "members-="),
            ("magpie group set review model+=测试密钥", "magpie group set", "model+="),
            # 和 models= 同在一行时报的是这个键本身，不是“字段重复”。
            ("magpie group set review models=来源/甲 members+=来源/乙", "magpie group set", "members+="),
        ):
            with self.subTest(行=行):
                with self.assertRaises(ValueError) as 异常:
                    _ = self.解析文本(f"magpie group add explore models=来源/模型\n{行}\n")
                消息 = str(异常.exception)
                for 字样 in ("第 2 行", 命令, f"不支持 {键}：", "追加或移除", "体检没法据此核对组合是否一致", "请写完整的 models="):
                    self.assertIn(字样, 消息)
                self.assertNotIn("重复", 消息)
                self.assertNotIn("来源/", 消息)
                self.assertNotIn("测试密钥", 消息)

    def test_group_add里指向成员的键须写在models后面(self):
        for 写法 in ("fast", "ultra", "weight", "pick", "use", "Use"):
            for 成员键 in ("models", "members"):
                with self.subTest(写法=写法, 成员键=成员键):
                    with self.assertRaises(ValueError) as 异常:
                        _ = self.解析文本(f"magpie group add review {写法}=来源/甲 {成员键}=来源/甲\n")
                    消息 = str(异常.exception)
                    # 报错点名写出来的键；用了别名时注明它对应的正名。
                    for 字样 in ("第 1 行", "magpie group add", f"{写法.lower()} 须写在 {成员键} 后面", "按书写顺序执行", "fast、ultra、weight、pick"):
                        self.assertIn(字样, 消息)
                    self.assertEqual("use 是 pick 的别名" in 消息, 写法.lower() == "use")
                    self.assertEqual("members 是 models 的别名" in 消息, 成员键 == "members")
                    self.assertNotIn("来源/", 消息)
                    # 写在 models（含别名）后面就通过，命令里保留原写法。
                    结果 = self.解析文本(f"magpie group add review {成员键}=来源/甲 {写法}=来源/甲\n")
                    self.assertEqual(结果["commands"][0]["args"], ["review", f"{成员键}=来源/甲", f"{写法}=来源/甲"])
        # 没有 models 时先报缺字段，不报顺序。
        with self.assertRaises(ValueError) as 异常:
            _ = self.解析文本("magpie group add review fast=来源/甲\n")
        self.assertIn("缺少 models 字段", str(异常.exception))

    def test_group_set里fast和ultra写在models前面报错weight和pick不报错(self):
        # models= 换的是整份名单，会清空 fast 和 ultra：这两个键写在它前面会被悄悄清掉，所以报错。
        for 键 in ("fast", "ultra", "Ultra"):
            for 成员键 in ("models", "members"):
                with self.subTest(键=键, 成员键=成员键):
                    with self.assertRaises(ValueError) as 异常:
                        _ = self.解析文本(f"magpie group set review {键}=来源/甲 {成员键}=来源/甲\n")
                    消息 = str(异常.exception)
                    for 字样 in ("第 1 行", "magpie group set", f"{键.lower()} 须写在 {成员键} 后面", "models= 会清空 fast 和 ultra，先写的会被清掉"):
                        self.assertIn(字样, 消息)
                    self.assertEqual("members 是 models 的别名" in 消息, 成员键 == "members")
                    # 原因是 models= 会清空它们，不是 group add 那条“成员要先加进组里”。
                    self.assertNotIn("按书写顺序执行", 消息)
                    self.assertNotIn("来源/", 消息)
                    # 写在后面就通过，命令里保留原写法。
                    结果 = self.解析文本(f"magpie group set review {成员键}=来源/甲 {键}=来源/甲\n")
                    self.assertEqual(结果["commands"][0]["args"], ["review", f"{成员键}=来源/甲", f"{键}=来源/甲"])
        # weight、pick 所指的成员可能本来就在组里，不查顺序。
        for 键 in ("weight", "weights", "pick", "use"):
            with self.subTest(不查顺序的键=键):
                结果 = self.解析文本(f"magpie group set review {键}=来源/甲 models=来源/甲\n")
                self.assertEqual(结果["commands"][0]["args"], ["review", f"{键}=来源/甲", "models=来源/甲"])
                self.assertEqual(结果["member_specs"]["review"], ["来源/甲"])
        # 命令里没有 models= 时没有东西会清空它们，不报错。
        for 键 in ("fast", "ultra", "weight", "pick"):
            with self.subTest(单独设置的键=键):
                结果 = self.解析文本(f"magpie group set review {键}=来源/甲\n")
                self.assertEqual(结果["commands"][0], {"kind": "group set", "args": ["review", f"{键}=来源/甲"]})

    def test_组名位置上是键值对时报错说清多半是漏了组名(self):
        说明 = "组名里不能有等号：{命令} 后面第一个词是组名，这一行一上来就是 键=值，多半是漏了组名"
        for 行, 命令 in (
            ("magpie group add models=来源/甲", "magpie group add"),
            ("magpie group set models=来源/甲", "magpie group set"),
            ("magpie group add token=测试密钥不能显示", "magpie group add"),
            ('magpie group add "组 名=甲" models=来源/甲', "magpie group add"),
        ):
            with self.subTest(行=行):
                with self.assertRaises(ValueError) as 异常:
                    _ = self.解析文本(行 + "\n")
                消息 = str(异常.exception)
                self.assertEqual(消息, f"组合文件第 1 行（{命令}）：{说明.format(命令=命令)}")
                self.assertNotIn("测试密钥", 消息)
                self.assertNotIn("来源/", 消息)
        # 真的没写组名（什么都没有，或者空的）仍是原来的说法。
        for 行, 命令 in (("magpie group add", "magpie group add"), ('magpie group add "" models=来源/甲', "magpie group add"), ("magpie group set", "magpie group set")):
            with self.subTest(行=行):
                with self.assertRaises(ValueError) as 异常:
                    _ = self.解析文本(行 + "\n")
                self.assertEqual(str(异常.exception), f"组合文件第 1 行（{命令}）：缺少组名")
        # 规则行的说法不变。
        for 行, 命令 in (("magpie group rule add use=来源/模型 tokens=1", "magpie group rule add"), ("magpie group rule rm use=1", "magpie group rule rm")):
            with self.subTest(行=行):
                with self.assertRaises(ValueError) as 异常:
                    _ = self.解析文本(行 + "\n")
                self.assertEqual(str(异常.exception), f"组合文件第 1 行（{命令}）：缺少组名")

    def test_规则行里的use仍是规则的键不认组的别名(self):
        # group rule add 的 use= 是规则用的成员，和 group set 里作为 pick 别名的 use= 不是一回事。
        for 参数, 拆分后 in (
            ("use=来源/模型 tokens=1", ["use=来源/模型", "tokens=1"]),
            ('use=group/示例池 use-effort=high when="effort <= medium"', ["use=group/示例池", "use-effort=high", "when=effort <= medium"]),
        ):
            with self.subTest(参数=参数):
                结果 = self.解析文本(f"magpie group add review models=来源/模型\nmagpie group rule add review {参数}\n")
                self.assertEqual(结果["commands"][-1], {"kind": "group rule add", "args": ["review", *拆分后]})
        for 键 in ("pick", "members", "weights"):
            with self.subTest(键=键):
                with self.assertRaises(ValueError) as 异常:
                    _ = self.解析文本(f"magpie group add review models=来源/模型\nmagpie group rule add review use=来源/模型 tokens=1 {键}=测试值\n")
                self.assertEqual(str(异常.exception), f"组合文件第 2 行（magpie group rule add）：不支持字段 {键}")

    def test_magpie不认的键在组行里仍然拒绝(self):
        for 键 in ("off", "member", "memberships", "strategies", "intent", "uses", "use-effort", "pause", "tokens"):
            with self.subTest(键=键):
                with self.assertRaises(ValueError) as 异常:
                    _ = self.解析文本(f"magpie group set review models=来源/模型 {键}=1\n")
                self.assertEqual(str(异常.exception), f"组合文件第 1 行（magpie group set）：不支持字段 {键}")

    def test_成员清单写成别名时格式错误报的是写出来的键(self):
        for 写法 in ("members", "model", "Models"):
            with self.subTest(写法=写法):
                with self.assertRaises(ValueError) as 异常:
                    _ = self.解析文本(f"magpie group add review {写法}=错误成员#demo-account\n")
                消息 = str(异常.exception)
                for 字样 in ("第 1 行", "magpie group add", f"{写法.lower()} 第 1 个成员格式错误", "#demo…"):
                    self.assertIn(字样, 消息)
                self.assertNotIn("-account", 消息)

    def test_Claude别名不带值和多带参数都被拒并说明原因(self):
        for 行, 说明 in (
            ("magpie claude", "须写成 magpie claude <别名> <目标>；别名不带值是写操作，会把 Claude Code 切出 magpie，组合文件里不能这么写"),
            ("magpie claude opus", "opus 不带值是写操作，会把 Claude Code 切出 magpie，组合文件里不能这么写"),
            ("magpie claude opus_effort", "opus_effort 后面要跟一个推理档（medium、high 或 xhigh）"),
            ("magpie claude opus 来源/模型 多余", "opus 只接受一个值"),
            ("magpie claude opus_effort high 多余", "opus_effort 只接受一个值"),
            ('magpie claude opus ""', "opus 只接受一个值"),
            ('magpie claude opus "带 空格"', "opus 只接受一个值"),
            ("magpie claude default", "不支持的 Claude 档位字段"),
        ):
            with self.subTest(行=行):
                with self.assertRaises(ValueError) as 异常:
                    _ = self.解析文本(f"magpie group add review models=来源/模型\n{行}\n")
                self.assertEqual(str(异常.exception), f"组合文件第 2 行（magpie claude）：{说明}")

    def test_模型停用只接受一个非空通配(self):
        for 行 in ("magpie model disable", 'magpie model disable ""', "magpie model disable 来源/甲 来源/乙"):
            with self.subTest(行=行):
                with self.assertRaises(ValueError) as 异常:
                    _ = self.解析文本(f"magpie group add review models=来源/模型\n{行}\n")
                self.assertEqual(str(异常.exception), "组合文件第 2 行（magpie model disable）：须提供一个模型通配")

    def test_规则删除只接受组名和一个正整数序号(self):
        for 行, 说明 in (
            ("magpie group rule rm review", "须只带一个规则序号"),
            ("magpie group rule rm review 1 2", "须只带一个规则序号"),
            ("magpie group rule rm review 0", "规则序号须为正整数"),
            ("magpie group rule rm review abc", "规则序号须为正整数"),
            ("magpie group rule rm review -1", "规则序号须为正整数"),
            ("magpie group rule rm review 1.5", "规则序号须为正整数"),
            ("magpie group rule rm", "缺少组名"),
        ):
            with self.subTest(行=行):
                with self.assertRaises(ValueError) as 异常:
                    _ = self.解析文本(f"magpie group add review models=来源/模型\n{行}\n")
                self.assertEqual(str(异常.exception), f"组合文件第 2 行（magpie group rule rm）：{说明}")

    def test_不支持的命令带命令族标签且不回显参数(self):
        for 行, 消息 in (
            ("magpie group rm review 1", "组合文件第 1 行（magpie group）：不支持的命令"),
            ("magpie group rule mv review 1 2", "组合文件第 1 行（magpie group rule）：不支持的命令"),
            ("magpie model enable 来源/模型", "组合文件第 1 行（magpie model）：不支持的命令"),
            ("magpie provider add 自定义 key=测试密钥", "组合文件第 1 行（magpie）：不支持的命令"),
            ("python 别的脚本.py 测试密钥", "组合文件第 1 行：不支持的命令"),
        ):
            with self.subTest(行=行):
                with self.assertRaises(ValueError) as 异常:
                    _ = self.解析文本(行 + "\n")
                self.assertEqual(str(异常.exception), 消息)

    def test_规则条件保留引号里的空格和多个比较符(self):
        结果 = self.解析文本("\n".join([
            "magpie group add review models=group/示例池,来源/模型:high",
            'magpie group rule add review use=group/示例池 use-effort=high when="effort <= medium" when="tokens >= 1"',
            "magpie group rule add review use=来源/模型:high tokens=1",
        ]))
        self.assertEqual(结果["commands"][-2:], [
            {"kind": "group rule add", "args": ["review", "use=group/示例池", "use-effort=high", "when=effort <= medium", "when=tokens >= 1"]},
            {"kind": "group rule add", "args": ["review", "use=来源/模型:high", "tokens=1"]},
        ])

    def test_规则按序号删除(self):
        结果 = self.解析文本("magpie group add review models=来源/模型\nmagpie group rule rm review 2\n")
        self.assertEqual(结果["commands"][-1], {"kind": "group rule rm", "args": ["review", "2"]})

    def test_规则必须带条件且报错不显示条件值(self):
        for 参数, 字段 in (("use-effort=high", "条件"), ('when="测试密钥 不能显示"', "when"), ('when="测试密钥', "when")):
            with self.subTest(字段=字段):
                with self.assertRaises(ValueError) as 异常:
                    _ = self.解析文本(f"magpie group add review models=来源/模型\nmagpie group rule add review use=来源/模型 {参数}\n")
                消息 = str(异常.exception)
                for 字样 in ("第 2 行", "magpie group rule add", 字段):
                    self.assertIn(字样, 消息)
                self.assertNotIn("测试密钥", 消息)
                self.assertNotIn("不能显示", 消息)

    def test_成员错误定位行命令和字段并遮住账号锁(self):
        with self.assertRaises(ValueError) as 异常:
            _ = self.解析文本("# 注释\n\nmagpie group add review models=错误成员#demo-account\n")
        消息 = str(异常.exception)
        for 字样 in ("第 3 行", "magpie group add", "models", "#demo…"):
            self.assertIn(字样, 消息)
        self.assertNotIn("-account", 消息)

    def test_未知字段提示字段名而不输出密钥值(self):
        with self.assertRaises(ValueError) as 异常:
            _ = self.解析文本('magpie group add review models=来源/模型 token="测试密钥 不能显示"\n')
        消息 = str(异常.exception)
        for 字样 in ("第 1 行", "magpie group add", "token"):
            self.assertIn(字样, 消息)
        self.assertNotIn("测试密钥", 消息)
        self.assertNotIn("不能显示", 消息)

    def test_组合保留声明的别名推理档(self):
        with tempfile.TemporaryDirectory() as 临时目录:
            文件 = Path(临时目录) / "组合.txt"
            文件.write_text("magpie group add impl models=p/m\nmagpie claude opus_effort high\nmagpie claude fable_effort medium\n", encoding="utf-8")
            self.assertEqual(parse_combination_file(文件)["claude_effort"], {"opus": "high", "fable": "medium"})

    def test_展示推理档不混入主会话和auto(self):
        from 组合解析 import parse_magpie_claude_effort_output
        输出 = "Claude Code p/main · effort xhigh · opus group/impl · opus effort medium · sonnet effort high · auto mode group/impl-lite"
        self.assertEqual(parse_magpie_claude_effort_output(输出), {"opus": "medium", "sonnet": "high"})

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


class 配置目录测试(unittest.TestCase):
    def 核对目录(self, 预期, *, 新文件=False, 旧目录=False, 随技能=False, 环境=False):
        from 组合解析 import config_root
        with tempfile.TemporaryDirectory() as 临时目录:
            家 = Path(临时目录)
            skill = 家 / "skill"
            # 仅有备份的新目录不能抢走兼容目录。
            (家 / ".dispatch/备份").mkdir(parents=True)
            if 新文件:
                (家 / ".dispatch/config.toml").write_text("version=1", encoding="utf-8")
            if 旧目录:
                (家 / ".claude/派活配置").mkdir(parents=True)
            if 随技能:
                (skill / "本机").mkdir(parents=True)
            with patch("组合解析.Path.home", return_value=家), patch.dict("os.environ", {"DISPATCH_CONFIG_DIR": str(家 / "指定")} if 环境 else {}, clear=True):
                self.assertEqual(config_root(skill), 家 / 预期)

    def test_环境变量优先(self):
        self.核对目录("指定", 新文件=True, 旧目录=True, 随技能=True, 环境=True)

    def test_含配置的新目录优先(self):
        self.核对目录(".dispatch", 新文件=True, 旧目录=True, 随技能=True)

    def test_旧配置优先且备份目录不抢配置(self):
        self.核对目录(".claude/派活配置", 旧目录=True, 随技能=True)

    def test_随技能旧目录回退(self):
        self.核对目录("skill/本机", 随技能=True)

    def test_无配置回退到新目录(self):
        self.核对目录(".dispatch")


class 示例组合测试(unittest.TestCase):
    def test_两份示例都能完整解析且命令条数对得上(self):
        # 只断言条数和能解析：示例里的具体命令由主脑随时调整。
        for 名称, 条数 in (("Codex", 16), ("国产", 15)):
            with self.subTest(示例=名称):
                文件 = Path(__file__).resolve().parents[2] / "组合" / f"{名称}.txt"
                结果 = parse_combination_file(文件)
                命令行 = [行 for 行 in 文件.read_text(encoding="utf-8-sig").splitlines() if (去空白 := 行.strip()) and not 去空白.startswith("#")]
                self.assertEqual(len(结果["commands"]), 条数)
                self.assertEqual(len(命令行), 条数)


if __name__ == "__main__":
    unittest.main()
