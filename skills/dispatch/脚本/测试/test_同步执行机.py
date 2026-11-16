"""同步计划、传输协议和命令行的定向测试，不连接真实执行机。"""
import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shlex
import shutil
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import 同步执行机 as 同步


class 同步测试(unittest.TestCase):
    def setUp(self):
        self.临时 = tempfile.TemporaryDirectory()
        self.addCleanup(self.临时.cleanup)
        self.家 = Path(self.临时.name)
        self.目标 = 同步.Probe('/home/test', 'Linux', '', '', {}, {})
        self.执行机 = {'transport': 'ssh', 'ssh_host': 'test', 'platform': 'linux',
                      'magpie_url': 'http://192.0.2.10:18080'}

    def 写(self, 路径, 内容):
        文件 = self.家 / 路径
        文件.parent.mkdir(parents=True, exist_ok=True)
        文件.write_text(内容 if isinstance(内容, str) else json.dumps(内容), encoding='utf-8')
        return 文件

    def 主机(self):
        return 同步.collect_host(self.家, personal=self.家 / '.dispatch', environ={})

    def 计划(self, **参数):
        return 同步.build_plan(self.主机(), self.目标, self.执行机, **参数)

    def 项(self, 计划, 名称):
        return next(项 for 项 in 计划.items if 项.path == 名称)

    def test_settings白名单和权限收紧(self):
        self.写('.claude/settings.json', {'env': {'LANG': 'zh'}, 'language': '中文',
                'permissions': {'defaultMode': 'default', 'allow': ['Bash(*)']},
                'hooks': {'Stop': []}, 'statusLine': {}, 'enabledPlugins': {}})
        内容 = json.loads(self.项(self.计划(), '.claude/settings.json').content)
        self.assertEqual(内容, {'env': {'LANG': 'zh'}, 'language': '中文',
                                'permissions': {'defaultMode': 'default'}})

    def test_清单聚合所有错误(self):
        文本 = '''version = 2
unknown = true
[executors.a]
transport = "ssh"
platform = "linux"
magpie_url = "http://127.0.0.2:18080"
x = true
[executors.b]
transport = "wsl"
platform = "linux"
'''
        with self.assertRaises(同步.SyncError) as 异常:
            同步.parse_executors(文本)
        for 关键字 in ('version', 'unknown', 'ssh_host', 'distro', 'magpie_url', 'x'):
            self.assertIn(关键字, str(异常.exception))

    def test_仅Claude或仅Codex缺项都跳过(self):
        self.写('.claude/settings.json', {'language': '中文'})
        计划 = self.计划()
        self.assertFalse(计划.errors)
        self.assertEqual(self.项(计划, '.codex/config.toml').action, '跳过')
        self.assertIn('主机上没有', self.项(计划, '.claude.json').reason)
        (self.家 / '.claude/settings.json').unlink()
        self.写('.codex/config.toml', 'model = "m"\n')
        计划 = self.计划()
        self.assertFalse(计划.errors)
        self.assertEqual(self.项(计划, '.claude/settings.json').action, '跳过')
        self.assertEqual(tomllib.loads(self.项(计划, '.codex/config.toml').content.decode()), {'model': 'm'})

    def test_计划没有认证私钥会话缓存(self):
        for 路径 in ('.codex/auth.json', '.claude/.credentials.json', '.ssh/id_ed25519',
                     '.codex/hooks.json', '.claude/projects/x', '.codex/sessions/x'):
            self.写(路径, '禁止同步')
        路径集 = {项.path for 项 in self.计划().items}
        self.assertEqual(路径集 & {'.codex/auth.json', '.claude/.credentials.json', '.ssh',
                        '.codex/hooks.json', '.claude/projects', '.codex/sessions'}, set())

    def test_回环地址按解析出的端口替换(self):
        for 地址 in ('http://127.0.0.1:18080/v1', 'http://127.0.0.2:18080/v1',
                     'http://LOCALHOST:18080/v1', 'http://[::1]:18080/v1'):
            with self.subTest(地址=地址):
                self.assertEqual(同步.rewrite_url(地址, self.执行机['magpie_url'], 'env.URL'),
                                 'http://192.0.2.10:18080/v1')
        self.assertEqual(同步.rewrite_url('http://localhost/v1', 'https://192.0.2.10:80', 'URL'),
                         'https://192.0.2.10:80/v1')
        self.assertEqual(同步.rewrite_url('https://[::1]/v1', 'http://192.0.2.10:443', 'URL'),
                         'http://192.0.2.10:443/v1')
        self.assertEqual(同步.rewrite_url('http://192.168.1.2:123/a', None, 'URL'),
                         'http://192.168.1.2:123/a')
        for 网关 in (None, 'http://192.0.2.10:123'):
            with self.assertRaises(同步.SyncError) as 异常:
                同步.rewrite_url('http://127.0.0.2:18080/不应输出', 网关, 'env.URL')
            self.assertIn('env.URL', str(异常.exception))
            self.assertNotIn('不应输出', str(异常.exception))

    def test_回环和未指定地址的别名共用安全判断(self):
        for 主机名 in ('0.0.0.0', '[::]', 'LOCALHOST.', '127.1', '2130706433'):
            with self.subTest(主机名=主机名):
                地址 = 'http://' + 主机名 + ':18080/v1'
                self.assertEqual(同步.rewrite_url(地址, self.执行机['magpie_url'], 'env.URL'),
                                 'http://192.0.2.10:18080/v1')
                清单 = 'version=1\n[executors.test]\ntransport="ssh"\nplatform="linux"\nssh_host="test"\nmagpie_url=' + json.dumps('http://' + 主机名 + ':18080')
                with self.assertRaises(同步.SyncError):
                    同步.parse_executors(清单)

    def test_网关密钥优先级和其他密钥去除(self):
        self.写('.claude/settings.json', {'env': {'ANTHROPIC_AUTH_TOKEN': '主机假密钥',
                  'ANTHROPIC_API_KEY': '主机假密钥', 'OTHER_TOKEN': '主机其他密钥',
                  'PASSWORD': '主机密码', 'URL': 'http://127.0.0.1:18080/v1'}})
        self.目标.documents['.claude/settings.json'] = json.dumps({'env': {
            'ANTHROPIC_AUTH_TOKEN': '目标值', 'OTHER_TOKEN': '目标其他值'}})
        for 环境值, 输入值, 期待 in (('环境值', '输入值', '环境值'), ('', '输入值', '输入值')):
            计划 = self.计划(gateway_key=环境值, stdin_key=输入值)
            self.assertFalse(计划.errors)
            内容 = json.loads(self.项(计划, '.claude/settings.json').content)
            self.assertEqual(内容['env']['ANTHROPIC_AUTH_TOKEN'], '目标值')
            self.assertEqual(内容['env']['ANTHROPIC_API_KEY'], 期待)
            self.assertEqual(内容['env']['OTHER_TOKEN'], '目标其他值')
            self.assertNotIn('PASSWORD', 内容['env'])
            self.assertNotIn('主机假密钥', repr(计划))
        失败 = self.计划()
        self.assertTrue(失败.errors)
        self.assertEqual(失败.writes, [])

    def test_SSH代理环境只用目标同名非空值(self):
        for 键 in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
                   'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'):
            for 目标值 in ('http://localhost:7890', None, '', '  '):
                with self.subTest(键=键, 目标值=目标值):
                    self.写('.claude/settings.json', {'env': {键: 'http://localhost:9999'}})
                    self.目标.documents['.claude/settings.json'] = json.dumps({
                        'env': {} if 目标值 is None else {键: 目标值}})
                    执行机 = dict(self.执行机)
                    执行机.pop('magpie_url')
                    计划 = 同步.build_plan(self.主机(), self.目标, 执行机)
                    self.assertFalse(计划.errors)
                    环境 = json.loads(self.项(计划, '.claude/settings.json').content)['env']
                    if 目标值 and 目标值.strip():
                        self.assertEqual(环境[键], 目标值)
                    else:
                        self.assertNotIn(键, 环境)
                        self.assertIn(f'env.{键}：代理设置跟随目标机，目标没有同名值，已去掉', 计划.notices)

    def test_WSL代理环境保持主机值(self):
        环境 = {键: 值 for 键, 值 in (('HTTP_PROXY', 'http://localhost:7890'),
                                     ('https_proxy', 'http://localhost:7891'),
                                     ('ALL_PROXY', 'socks5://localhost:7892'), ('no_proxy', '*'))}
        self.写('.claude/settings.json', {'env': 环境})
        self.目标.documents['.claude/settings.json'] = json.dumps({'env': {'no_proxy': '目标'}})
        计划 = 同步.build_plan(self.主机(), self.目标, {'transport': 'wsl'})
        self.assertFalse(计划.errors)
        self.assertEqual(json.loads(self.项(计划, '.claude/settings.json').content)['env'], 环境)
        self.assertFalse(any('代理设置跟随目标机' in 通知 for 通知 in 计划.notices))

    def test_个人配置顶层备份不进SSH复制清单且只提示一次(self):
        self.写('.dispatch/config.toml', 'version=1\n')
        self.写('.dispatch/备份/安装前/技能/说明.md', '旧技能副本')
        self.写('.dispatch/备份/安装前/技能/__pycache__/缓存.pyc', '缓存')
        for 路径 in ('组合/备份/说明.md', '反馈/反馈.md', '状态/状态.txt'):
            self.写('.dispatch/' + 路径, '保留')
        计划 = self.计划()
        self.assertFalse(计划.errors)
        条目 = {条目.name for 条目 in self.项(计划, '.dispatch').entries}
        self.assertFalse(any(名 == '备份' or 名.startswith('备份/') for 名 in 条目))
        for 路径 in ('组合/备份/说明.md', '反馈/反馈.md', '状态/状态.txt'):
            self.assertIn(路径, 条目)
        self.assertEqual([通知 for 通知 in 计划.notices if '备份' in 通知],
                         ['跳过：.dispatch/备份（本机的安装和复盘备份，不同步）'])
        with tarfile.open(fileobj=io.BytesIO(同步.make_archive(计划)), mode='r:gz') as 归档:
            self.assertFalse(any('/备份/安装前' in 成员.name for 成员 in 归档))
        WSL计划 = 同步.build_plan(self.主机(), self.目标, {'transport': 'wsl'})
        self.assertEqual(self.项(WSL计划, '.dispatch').kind, 'link')
        self.assertEqual(self.项(WSL计划, '.dispatch').link, 同步.wsl_path(str((self.家 / '.dispatch').absolute())))
        self.assertFalse(any('本机的安装和复盘备份' in 通知 for 通知 in WSL计划.notices))

    def test_MCP敏感头取目标值缺失则跳过(self):
        self.写('.claude.json', {'mcpServers': {
            '保留': {'type': 'http', 'url': 'http://localhost:18080/mcp',
                    'headers': {'Authorization': '主机头值', 'X-Token': '主机令牌', 'Accept': '主机普通头值'}},
            '缺头': {'type': 'sse', 'url': 'https://example.org', 'headers': {'Cookie': '主机Cookie'}},
            '端口不同': {'type': 'http', 'url': 'http://127.0.0.2:999/mcp'},
            'stdio': {'command': 'uvx', 'args': ['mcp']}}})
        self.目标.documents['.claude.json'] = json.dumps({'other': 7, 'mcpServers': {
            '保留': {'headers': {'Authorization': '目标头值', 'X-Token': '目标令牌'}}}})
        计划 = self.计划()
        内容 = json.loads(self.项(计划, '.claude.json').content)
        self.assertEqual(set(内容['mcpServers']), {'保留'})
        self.assertEqual(内容['mcpServers']['保留']['headers']['Authorization'], '目标头值')
        self.assertEqual(内容['mcpServers']['保留']['url'], 'http://192.0.2.10:18080/mcp')
        self.assertEqual(内容['other'], 7)
        for 名 in ('缺头', '端口不同', 'stdio'):
            self.assertTrue(any(名 in 通知 for 通知 in 计划.notices))
        self.assertNotIn('主机头值', self.项(计划, '.claude.json').content.decode())

    def test_SSH的HTTP及SSEMCP环境密钥只用目标值(self):
        for 类型 in ('http', 'sse'):
            with self.subTest(类型=类型):
                self.写('.claude.json', {'mcpServers': {'测试': {'type': 类型, 'url': 'https://example.org',
                         'env': {'API_TOKEN': '主机敏感值', 'PASSWORD': '主机口令', 'NORMAL': '保留'}}}})
                self.目标.documents['.claude.json'] = json.dumps({'mcpServers': {'测试': {
                    'env': {'API_TOKEN': '目标替换值'}}}})
                计划 = self.计划()
                环境 = json.loads(self.项(计划, '.claude.json').content)['mcpServers']['测试']['env']
                self.assertTrue(环境.get('API_TOKEN') == '目标替换值', 'MCP 环境密钥必须使用目标值')
                self.assertNotIn('PASSWORD', 环境)
                self.assertEqual(环境['NORMAL'], '保留')
                self.assertTrue(any('测试.env.PASSWORD' in 通知 and '去掉' in 通知 for 通知 in 计划.notices))

    def test_WSL过滤和首次标记(self):
        exe = self.写('mcp.exe', '假程序')
        self.写('.claude.json', {'hasCompletedOnboarding': True, 'lastReleaseNotesSeen': '1',
                  '普通主机键': '不复制', 'mcpServers': {
            'HTTP': {'type': 'http', 'url': 'http://localhost:18080'},
            'exe': {'command': str(exe)},
            'uvx': {'command': 'uvx'}, 'npx': {'command': 'npx'}, 'uv': {'command': 'uv'},
            'Windows参数': {'command': 'npx', 'args': ['C:\\x']},
            'cmd': {'command': 'cmd', 'args': ['/c']}}})
        self.目标.documents['.claude.json'] = '{"existing":true}'
        with patch.object(同步, 'wsl_path', wraps=同步.wsl_path):
            计划 = 同步.build_plan(self.主机(), self.目标, {'transport': 'wsl'})
        内容 = json.loads(self.项(计划, '.claude.json').content)
        self.assertEqual(set(内容['mcpServers']), {'HTTP', 'exe', 'uvx', 'npx', 'uv'})
        self.assertTrue(内容['existing'])
        self.assertTrue(内容['hasSeenAutoDefaultNudge'])
        self.assertTrue(内容['hasCompletedOnboarding'])
        self.assertNotIn('普通主机键', 内容)

    def test_Codex只改托管根键且保留其他字节(self):
        原来 = '# 目标注释\nmodel = "old"\nmodel_catalog_json = "missing"\nservice_tier = "fast"\ncustom = "保留" # 注释\n[projects."/tmp/x"]\ntrust_level = "trusted"\n[model_providers.local]\nname = "本机"\n'
        self.写('.codex/config.toml', 'model = "new"\nmodel_reasoning_effort = "high"\nmodel_catalog_json = "不带"\n[model_providers.host]\napi_key = "主机假令牌"\n')
        self.目标.documents['.codex/config.toml'] = 原来
        内容 = self.项(self.计划(), '.codex/config.toml').content.decode()
        解析 = tomllib.loads(内容)
        self.assertEqual(解析['model'], 'new')
        self.assertNotIn('model_catalog_json', 解析)
        self.assertNotIn('service_tier', 解析)
        self.assertIn('custom = "保留" # 注释\n[projects."/tmp/x"]\ntrust_level = "trusted"', 内容)
        self.assertNotIn('主机假令牌', 内容)
        self.目标.documents['.codex/config.toml'] = '[model]\nx=1\n'
        失败 = self.计划()
        self.assertTrue(失败.errors)
        self.assertIsNone(self.项(失败, '.codex/config.toml').content)

    def test_自定义Codex源目录(self):
        自定义 = self.家 / '自定义'
        self.写('自定义/AGENTS.md', '规则')
        self.写('自定义/config.toml', 'model="custom"\n')
        主机 = 同步.collect_host(self.家, personal=self.家 / '.dispatch', environ={'CODEX_HOME': str(自定义)})
        计划 = 同步.build_plan(主机, self.目标, self.执行机)
        self.assertEqual(tomllib.loads(self.项(计划, '.codex/config.toml').content.decode())['model'], 'custom')
        self.assertEqual(self.项(计划, '.codex/AGENTS.md').action, '新建')

    def test_目标自定义配置目录拒绝写入(self):
        self.写('.claude/settings.json', {})
        self.目标.codex_home = '/custom'
        失败 = self.计划()
        self.assertTrue(失败.errors)
        self.assertEqual(失败.writes, [])
        self.assertFalse(self.计划(override_home=True).errors)

    def test_探测协议严格解析和脱敏(self):
        文本 = 同步.encode_probe_fixture(self.目标)
        self.assertEqual(同步.parse_probe(文本).home, '/home/test')
        with self.assertRaises(同步.SyncError):
            同步.parse_probe(文本 + 'unexpected\n')
        with self.assertRaises(同步.SyncError):
            同步.parse_probe(文本.replace('DISPATCH1', 'DISPATCH2', 1))
        self.目标.documents['.claude/settings.json'] = json.dumps({'env': {'NORMAL': '独特假值', 'KEY': '假密钥'}})
        self.目标.documents['.claude.json'] = json.dumps({'mcpServers': {'x': {'headers': {'Accept': '假头值'}}}, 'password': '假密码'})
        self.目标.documents['.codex/config.toml'] = '[model_providers.x]\napi_key="Codex假令牌"\n'
        脱敏 = 同步.sanitize_probe(同步.encode_probe_fixture(self.目标))
        self.assertNotIn(base64.b64encode('独特假值'.encode()).decode(), 脱敏)
        解码 = 同步.parse_probe(脱敏)
        for 内容 in 解码.documents.values():
            for 秘密 in ('独特假值', '假密钥', '假头值', '假密码', 'Codex假令牌'):
                self.assertNotIn(秘密, 内容)
        tomllib.loads(解码.documents['.codex/config.toml'])

    def test_验证脚本显式隔离家目录(self):
        脚本 = 同步.validation_script('/tmp/目标')
        for 名 in ('HOME', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR', '.local/bin', '180', '只回复 OK'):
            self.assertIn(名, 脚本)
        self.assertNotIn('bash -c', 脚本)

    def test_源配置缺失时仍隐藏目标已有凭据(self):
        秘密 = [hashlib.sha256(('目标凭据' + str(号)).encode()).hexdigest() for 号 in range(3)]
        self.目标.documents = {
            '.claude/settings.json': json.dumps({'env': {'API_TOKEN': 秘密[0]}}),
            '.codex/config.toml': 'api_key = ' + json.dumps(秘密[1]),
            '.claude.json': json.dumps({'mcpServers': {'x': {'headers': {'Authorization': 秘密[2]}}}})}
        class 假传输:
            def probe(内, paths, home=None):
                return self.目标
            def apply(内, plan):
                return {'ok': True, 'completed': [], 'failed': '', 'backup': '', 'backed': [], 'error': ''}
            def validate(内, plan):
                return [{'name': 'claude', 'status': '失败', 'code': 1,
                         'output': ' '.join(秘密 + [base64.b64encode(值.encode()).decode() for 值 in 秘密])}]
        输出 = io.StringIO()
        with contextlib.redirect_stdout(输出), contextlib.redirect_stderr(输出):
            码 = 同步.synchronize(self.主机(), self.执行机, 假传输(), execute=True)
        self.assertEqual(码, 0)
        self.assertFalse(any(值 in 输出.getvalue() or base64.b64encode(值.encode()).decode() in 输出.getvalue()
                             for 值 in 秘密), '验证输出不得泄露目标已有凭据')

    def test_预览只探测且不泄漏密钥(self):
        假密钥 = '独特假密钥_不得回显_362194'
        self.写('.claude/settings.json', {'env': {'ANTHROPIC_AUTH_TOKEN': 假密钥}})
        记录 = []
        class 假传输:
            def probe(内, paths, home=None):
                记录.append('探测')
                return self.目标
            def apply(内, plan):
                记录.append('写入')
                return {'ok': True, 'completed': [], 'failed': '', 'backup': '', 'backed': [], 'error': ''}
            def validate(内, plan):
                记录.append('验证')
                return []
        for 执行, 提供 in ((False, True), (True, True), (True, False)):
            输出 = io.StringIO()
            with contextlib.redirect_stdout(输出), contextlib.redirect_stderr(输出):
                码 = 同步.synchronize(self.主机(), self.执行机, 假传输(), execute=执行,
                                      gateway_key=假密钥 if 提供 else '', validate=False)
            self.assertEqual(码, 0 if 提供 else 2)
            self.assertFalse(假密钥 in 输出.getvalue(), '输出不应包含测试密钥')
            self.assertFalse(base64.b64encode(假密钥.encode()).decode() in 输出.getvalue(), '输出不应包含测试密钥编码')
            if not 执行 or not 提供:
                self.assertEqual(记录, ['探测'])
            记录.clear()

    def test_传输只用参数列表和标准输入(self):
        for 执行机, 命令 in (({'transport': 'wsl', 'distro': 'test'}, ['wsl.exe', '-d', 'test', '--exec', 'sh', '-s']),
                               ({'transport': 'ssh', 'ssh_host': 'test'}, ['ssh', '-o', 'BatchMode=yes', 'test', 'sh', '-s']),
                               ({'transport': 'local'}, ['sh', '-s'])):
            调用 = []
            def 运行(cmd, **参数):
                调用.append((cmd, 参数))
                return subprocess.CompletedProcess(cmd, 0, '结果', '')
            self.assertEqual(同步.Transport(执行机, 运行).run('测试脚本'), '结果')
            self.assertEqual(调用[0][0], 命令)
            self.assertEqual(调用[0][1]['input'], '测试脚本'.encode('utf-8'))
            self.assertNotIn('text', 调用[0][1])
            self.assertNotIn('shell', 调用[0][1])

    def test_按生成步骤真实恢复文件目录和链接(self):
        self.写('.claude/CLAUDE.md', '新规则')
        self.写('.claude/agents/新.md', '新代理')
        self.写('.claude/settings.json', {'language': '中文'})
        目标家 = self.家 / '恢复目标 含空格'
        self.写('恢复目标 含空格/.claude/settings.json', '{"language":"旧设置"}')
        self.写('恢复目标 含空格/.claude/agents/旧.md', '旧代理')
        原指向 = str(self.家 / "原指向 含空格与'引号")
        (目标家 / '.claude/CLAUDE.md').symlink_to(原指向)
        self.目标.home = 目标家.as_posix()
        self.目标.documents['.claude/settings.json'] = '{"language":"旧设置"}'
        命名空间 = {}
        exec(同步.REMOTE_COMMON, 命名空间)
        for 路径 in ('.claude/CLAUDE.md', '.claude/agents', '.claude/settings.json'):
            self.目标.items[路径] = 命名空间['describe'](目标家 / 路径)
        结果 = 同步.Transport(self.执行机, self.脚本运行器).apply(self.计划(override_home=True))
        self.assertTrue(结果['ok'])
        shell = shutil.which('sh')
        if shell is None and os.name == 'nt':
            git = shutil.which('git')
            self.assertIsNotNone(git, 'Windows 恢复实测需要已安装的 Git Bash')
            shell = str(Path(git).resolve().parent.parent / 'usr/bin/sh.exe')
        self.assertTrue(shell is not None and Path(shell).is_file(), '恢复实测需要本地 sh')
        环境 = dict(os.environ, PATH=str(Path(shell).parent) + os.pathsep + os.environ.get('PATH', ''),
                    MSYS='winsymlinks:nativestrict')
        步骤 = 同步.recovery_steps(self.目标.home, 结果['backup'], 结果['backed'])
        for 项, 说明 in zip(结果['backed'], 步骤):
            当前 = 目标家 / 项['path']
            if 当前.is_dir() and not 当前.is_symlink():
                shutil.rmtree(当前)
            else:
                当前.unlink()
            命令 = 说明.split('运行 ', 1)[1]
            执行 = subprocess.run([shell, '-s'], input=(命令 + '\n').encode('utf-8'),
                                  capture_output=True, env=环境, timeout=15, check=False)
            self.assertEqual(执行.returncode, 0, '生成的恢复命令必须真实执行成功')
        self.assertEqual((目标家 / '.claude/settings.json').read_text(encoding='utf-8'), '{"language":"旧设置"}')
        self.assertEqual((目标家 / '.claude/agents/旧.md').read_text(encoding='utf-8'), '旧代理')
        self.assertFalse((目标家 / '.claude/agents/新.md').exists())
        还原指向 = os.readlink(目标家 / '.claude/CLAUDE.md')
        if os.name == 'nt':
            # Git Bash 创建的原生链接会带 Windows 扩展路径前缀，指向仍须相同。
            还原指向 = 还原指向.removeprefix('\\\\?\\')
        self.assertEqual(还原指向, 原指向)

    def test_恢复说明分文件目录链接(self):
        文本 = '\n'.join(同步.recovery_steps('/home/test', '/home/test/.dispatch-同步备份/1',
                         [{'path': 'file', 'kind': '文件'}, {'path': 'dir', 'kind': '目录'},
                          {'path': 'link', 'kind': '链接'}]))
        self.assertIn('mv --', 文本)
        self.assertIn('ln -s', 文本)
        self.assertIn('link.链接.txt', 文本)

    def test_覆盖家目录的安装预览忽略外部既有程序(self):
        清单 = self.写('执行机.toml', 'version=1\n[executors.test]\ntransport="ssh"\nssh_host="test"\nplatform="linux"\n')
        self.目标.home = '/tmp/覆盖家'
        for 方式, 版本 in (('binary', '0.1.0'), ('npm', '0.1.0'), ('binary', '0.166.0')):
            with self.subTest(方式=方式, 版本=版本):
                self.目标.cli = {'claude': {'version': '', 'method': 'missing', 'path': '', 'realpath': '',
                    'prefix': '', 'writable': False, 'arch': 'x86_64'}, 'codex': {
                    'version': 版本, 'method': 方式, 'path': '/usr/local/bin/codex',
                    'realpath': '/usr/local/bin/codex', 'prefix': '/usr/local' if 方式 == 'npm' else '',
                    'writable': False, 'arch': 'x86_64'}}
                输出 = io.StringIO()
                with patch.object(同步.Transport, 'probe', return_value=self.目标), contextlib.redirect_stdout(输出), contextlib.redirect_stderr(输出):
                    码 = 同步.main(['test', '--执行机清单', str(清单), '--目标家目录', self.目标.home,
                                  '--装CLI', '--claude版本', '2.1.316', '--codex版本', '0.166.0'])
                self.assertEqual(码, 0)
                self.assertIn('忽略目标家目录外的既有安装', 输出.getvalue())
                self.assertIn('/tmp/覆盖家/.local/bin/codex', 输出.getvalue())
                self.assertNotIn('/usr/local', 输出.getvalue())

    def test_安装计划固定版本保留安装方式(self):
        self.目标.cli = {
            'claude': {'version': '2.1.1', 'method': 'native', 'path': '/home/test/.local/bin/claude',
                       'realpath': '/home/test/.local/share/claude/versions/2.1.1', 'prefix': '', 'writable': True, 'arch': 'x86_64'},
            'codex': {'version': '0.1.0', 'method': 'npm', 'path': '/home/test/.local/bin/codex',
                      'realpath': '/home/test/.local/lib/node_modules/@openai/codex/bin/codex.js',
                      'prefix': '/home/test/.local', 'writable': True, 'arch': 'x86_64'}}
        计划 = 同步.plan_install(self.目标, {'claude': '2.1.2', 'codex': '0.2.0'})
        self.assertEqual(计划[0]['method'], 'native')
        self.assertIn('2.1.2', 计划[0]['command'])
        self.assertIn('@openai/codex@0.2.0', 计划[1]['command'])
        self.assertIn('--prefix', 计划[1]['command'])
        self.目标.cli['codex']['version'] = '0.2.0'
        self.assertEqual(同步.plan_install(self.目标, {'claude': '2.1.1', 'codex': '0.2.0'})[1]['action'], '不变')
        self.目标.cli['codex']['writable'] = False
        with self.assertRaises(同步.SyncError) as 异常:
            同步.plan_install(self.目标, {'claude': '2.1.1', 'codex': '0.3.0'})
        self.assertIn('sudo', str(异常.exception))

    def test_备份独立且写入脚本无中文标识符(self):
        self.写('.dispatch/config.toml', 'version=1\n')
        计划 = self.计划()
        self.assertIn('.dispatch', {项.path for 项 in 计划.items})
        self.assertNotIn('.dispatch-同步备份', {项.path for 项 in 计划.items})
        self.assertIn("home / '.dispatch-同步备份'", 同步.WRITE_CODE)
        self.assertIn('real_parents(backup)', 同步.WRITE_CODE)
        compile(同步.REMOTE_COMMON + '\npayload={}\narchive_data=""\n' + 同步.WRITE_CODE, '<测试>', 'exec')

    def test_旧入口直接写入但不调用验证且可推断家目录(self):
        规格 = importlib.util.spec_from_file_location('旧入口', Path(__file__).resolve().parents[2] / 'WSL/同步配置到WSL.py')
        旧 = importlib.util.module_from_spec(规格)
        规格.loader.exec_module(旧)
        self.写('.claude/CLAUDE.md', '旧入口源规则')
        with patch.object(旧, '推断Windows家目录', return_value=str(self.家)), patch.object(旧, 'local_main', return_value=0) as 调用:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(旧.main([]), 0)
            调用.assert_called_once_with(str(self.家))
        with patch.object(同步, 'collect_host', return_value=self.主机()), patch.object(同步, 'synchronize', return_value=0) as 调用:
            self.assertEqual(同步.local_main(str(self.家)), 0)
            self.assertTrue(调用.call_args.kwargs['execute'])
            self.assertFalse(调用.call_args.kwargs['validate'])

    def test_技能内本机配置独立同步且不重复归档(self):
        self.写('.claude/skills/dispatch/SKILL.md', '技能说明')
        个人 = self.写('.claude/skills/dispatch/本机/config.toml', 'version=1\n').parent
        主机 = 同步.collect_host(self.家, personal=个人, environ={})
        计划 = 同步.build_plan(主机, self.目标, self.执行机)
        self.assertFalse(计划.errors)
        self.assertEqual(self.项(计划, '.dispatch').kind, 'dir')
        技能项 = self.项(计划, '.claude/skills')
        self.assertFalse(any('本机' in 条目.name for 条目 in 技能项.entries))
        with tarfile.open(fileobj=io.BytesIO(同步.make_archive(计划)), mode='r:gz') as 归档:
            名称 = [项.name for 项 in 归档]
        self.assertEqual(sum(名称项.endswith('/config.toml') for 名称项 in 名称), 1)
        self.assertTrue(any('个人配置单独同步' in 通知 for 通知 in 计划.notices))
        WSL计划 = 同步.build_plan(主机, self.目标, {'transport': 'wsl'})
        self.assertFalse(WSL计划.errors)
        self.assertEqual(self.项(WSL计划, '.dispatch').kind, 'link')

    def test_本地模式个人配置不读WSL环境(self):
        self.写('.dispatch/config.toml', 'version=1\n')
        self.写('.claude/派活配置/文件.txt', '旧')
        with patch.dict(os.environ, {'DISPATCH_CONFIG_DIR': '/不该读取'}):
            主机 = 同步.collect_host(self.家, local=True)
        self.assertIn('.dispatch', {源.path for 源 in 主机.sources})
        self.assertNotIn('.claude/派活配置', {源.path for 源 in 主机.sources})
        (self.家 / '.dispatch/config.toml').unlink()
        主机 = 同步.collect_host(self.家, local=True)
        self.assertIn('.claude/派活配置', {源.path for 源 in 主机.sources})

    def test_受保护链接不进归档且家外普通链接允许(self):
        技能 = self.家 / '.claude/skills'
        技能.mkdir(parents=True)
        外部 = tempfile.TemporaryDirectory()
        self.addCleanup(外部.cleanup)
        外部文件 = Path(外部.name) / '普通说明.txt'
        外部文件.write_text('外部内容', encoding='utf-8')
        for 名, 路径 in (('历史链接', '.claude/history.jsonl'),
                         ('浏览器链接', '.config/chromium/Default/Cookies'),
                         ('缓存链接', '.claude/cache/普通文件.bin'),
                         ('其他缓存链接', '.claude/.pytest_cache/README.md')):
            (技能 / 名).symlink_to(self.写(路径, '禁止内容'))
        (技能 / '外部链接').symlink_to(外部文件)
        计划 = self.计划()
        with tarfile.open(fileobj=io.BytesIO(同步.make_archive(计划)), mode='r:gz') as 归档:
            名称 = [项.name for 项 in 归档]
        for 名 in ('历史链接', '浏览器链接', '缓存链接', '其他缓存链接'):
            self.assertFalse(any(名 in 路径 for 路径 in 名称), '受保护链接不能进入归档')
            self.assertIn('跳过：' + 名 + '（指向受保护内容）', 计划.notices)
        self.assertTrue(any(路径.endswith('/外部链接') for 路径 in 名称), '家外普通技能文件应复制进归档')
        另一个家 = self.家 / '另一个家'
        另一个家.mkdir()
        外部Claude = Path(外部.name) / '外部Claude'
        (外部Claude / 'skills').mkdir(parents=True)
        (外部Claude / 'skills/普通说明.txt').write_text('外部内容', encoding='utf-8')
        (另一个家 / '.claude').symlink_to(外部Claude, target_is_directory=True)
        另一个主机 = 同步.collect_host(另一个家, personal=另一个家 / '.dispatch', environ={})
        另一个计划 = 同步.build_plan(另一个主机, self.目标, self.执行机)
        self.assertEqual(self.项(另一个计划, '.claude/skills').action, '新建')
        with tarfile.open(fileobj=io.BytesIO(同步.make_archive(另一个计划)), mode='r:gz') as 归档:
            self.assertTrue(any(成员.name.endswith('/普通说明.txt') and 成员.isfile() for 成员 in 归档))

    def test_受保护路径特征在家内家外一致(self):
        外部 = tempfile.TemporaryDirectory()
        self.addCleanup(外部.cleanup)
        特征 = (
            ('历史', '.claude/history.jsonl', False),
            ('项目', '.claude/projects', True),
            ('会话', '.claude/sessions', True),
            ('Claude凭据', '.claude/.credentials.json', False),
            ('Claude设置', '.claude/settings.json', False),
            ('Codex配置', '.codex/config.toml', False),
            ('Codex会话', '.codex/sessions', True),
            ('Mozilla资料', '.mozilla/firefox/profile', True),
            ('Chromium资料', '.config/chromium/Default', True),
            ('Chrome资料', '.config/google-chrome/Default', True),
            ('Brave资料', '.config/BraveSoftware/Brave-Browser/Default', True),
            ('Edge资料', '.config/microsoft-edge/Default', True),
            ('Cookies文件', '浏览器资料/Default/Cookies', False),
            ('登录数据库', '浏览器资料/Default/Login Data', False),
            ('浏览器状态', '浏览器资料/Local State', False),
            ('浏览器数据', '浏览器资料/Default/Web Data', False),
            ('缓存目录', 'node_modules/.cache', True),
            ('认证文件', '普通目录/auth.json', False),
            ('私钥文件', '普通目录/id_ed25519_test', False))
        技能 = self.家 / '.claude/skills'
        技能.mkdir(parents=True)
        for 位置, 根 in (('家内', self.家), ('家外', Path(外部.name))):
            for 名称, 相对路径, 是目录 in 特征:
                with self.subTest(位置=位置, 特征=名称):
                    目标 = 根 / 相对路径
                    if 是目录:
                        目标.mkdir(parents=True, exist_ok=True)
                        (目标 / '内容.txt').write_text('{}', encoding='utf-8')
                    else:
                        目标.parent.mkdir(parents=True, exist_ok=True)
                        目标.write_text('model="m"\n' if 目标.suffix == '.toml' else '{}', encoding='utf-8')
                    链接名 = 位置 + '-' + 名称
                    (技能 / 链接名).symlink_to(目标, target_is_directory=是目录)
                    计划 = self.计划()
                    self.assertFalse(计划.errors)
                    with tarfile.open(fileobj=io.BytesIO(同步.make_archive(计划)), mode='r:gz') as 归档:
                        self.assertFalse(any(链接名 in 成员.name for 成员 in 归档), '受保护路径不能进入归档')
                    self.assertIn('跳过：' + 链接名 + '（指向受保护内容）', 计划.notices)

    def test_外部Codex家的规则和代理能够复制(self):
        外部 = tempfile.TemporaryDirectory()
        self.addCleanup(外部.cleanup)
        Codex家 = Path(外部.name) / '.codex'
        Codex家.mkdir()
        (Codex家 / 'AGENTS.md').write_text('外部 Codex 规则', encoding='utf-8')
        (Codex家 / 'agents').mkdir()
        (Codex家 / 'agents/代理.md').write_text('外部代理', encoding='utf-8')
        主机 = 同步.collect_host(self.家, personal=self.家 / '.dispatch', environ={'CODEX_HOME': str(Codex家)})
        计划 = 同步.build_plan(主机, self.目标, self.执行机)
        self.assertEqual(self.项(计划, '.codex/AGENTS.md').action, '新建')
        self.assertEqual(self.项(计划, '.codex/agents').action, '新建')
        with tarfile.open(fileobj=io.BytesIO(同步.make_archive(计划)), mode='r:gz') as 归档:
            内容 = [归档.extractfile(成员).read().decode('utf-8') for 成员 in 归档 if 成员.isfile()]
        self.assertIn('外部 Codex 规则', 内容)
        self.assertIn('外部代理', 内容)

    def test_缓存只匹配真实目录名不误删普通文件(self):
        普通文件 = ('cache.md', 'caching.py', 'cache_rules.md', 'code_cache.txt', 'cache', '.cache', '__pycache__')
        for 名称 in 普通文件:
            self.写('.claude/skills/普通文件/' + 名称, '普通内容')
        self.写('.claude/skills/cache-examples/说明.md', '普通目录')
        self.写('.claude/skills/资料/cache.md/说明.md', '普通目录')
        for 号, 名称 in enumerate(('__pycache__', '.cache', 'cache', 'Cache', '.pytest_cache')):
            self.写('.claude/skills/缓存容器/' + str(号) + '/' + 名称 + '/内部.txt', '不应同步')
        外部 = tempfile.TemporaryDirectory()
        self.addCleanup(外部.cleanup)
        外部缓存 = Path(外部.name) / 'node_modules/.cache'
        外部缓存.mkdir(parents=True)
        (外部缓存 / '内部.txt').write_text('不应同步', encoding='utf-8')
        (self.家 / '.claude/skills/外部缓存别名').symlink_to(外部缓存 / '内部.txt')
        计划 = self.计划()
        with tarfile.open(fileobj=io.BytesIO(同步.make_archive(计划)), mode='r:gz') as 归档:
            文件名 = [成员.name for 成员 in 归档 if 成员.isfile()]
        for 名称 in 普通文件:
            self.assertTrue(any(路径.endswith('/普通文件/' + 名称) for 路径 in 文件名),
                            '普通文件不能被缓存目录规则误删：' + 名称)
        self.assertTrue(any(路径.endswith('/cache-examples/说明.md') for 路径 in 文件名))
        self.assertTrue(any(路径.endswith('/资料/cache.md/说明.md') for 路径 in 文件名))
        self.assertFalse(any('/缓存容器/' in 路径 or '外部缓存别名' in 路径 for 路径 in 文件名))
        self.assertIn('跳过：外部缓存别名（指向受保护内容）', 计划.notices)

    def test_目录归档解引用但排除认证私钥和循环(self):
        技能 = self.家 / '.claude/skills'
        技能.mkdir(parents=True)
        外部 = self.写('普通素材/说明.md', '允许来自家目录内的普通技能素材')
        认证 = self.写('.codex/auth.json', '独特禁止认证')
        私钥 = self.写('.ssh/id_ed25519', '独特禁止私钥')
        会话 = self.写('.codex/sessions/x', '独特禁止会话')
        self.写('.claude/skills/__pycache__/x.pyc', '独特禁止缓存')
        self.写('.claude/skills/nested/auth.json', '禁止同名')
        for 名, 目标, 目录 in (('正常', 外部.parent, True), ('认证链接', 认证, False),
                                ('私钥链接', 私钥, False), ('会话链接', 会话.parent, True),
                                ('循环', 技能, True), ('失效', self.家 / '不存在', False)):
            (技能 / 名).symlink_to(目标, target_is_directory=目录)
        计划 = self.计划()
        with tarfile.open(fileobj=io.BytesIO(同步.make_archive(计划)), mode='r:gz') as 归档:
            成员 = 归档.getmembers()
            self.assertTrue(any('说明.md' in 项.name for 项 in 成员))
            for 项 in 成员:
                self.assertFalse(项.issym())
                for 禁止 in ('认证链接', '私钥链接', '会话链接', '循环', '失效', '__pycache__', 'auth.json'):
                    self.assertNotIn(禁止, 项.name)
        for 名 in ('认证链接', '私钥链接', '会话链接', '循环', '失效'):
            self.assertTrue(any(名 in 通知 for 通知 in 计划.notices), 名)

    def test_内容相同无写入无备份(self):
        self.写('.claude/settings.json', {'language': '中文'})
        初始 = self.计划()
        for 项 in 初始.writes:
            self.目标.items[项.path] = {'kind': '文件' if 项.kind == 'file' else '目录', 'digest': 项.digest}
            if 项.content:
                self.目标.documents[项.path] = 项.content.decode()
        第二次 = self.计划()
        self.assertEqual(第二次.writes, [])
        self.assertEqual(self.项(第二次, '.claude/settings.json').action, '不变')
        def 禁止运行(*参数, **选项):
            self.fail('全部不变不应生成写入脚本')
        结果 = 同步.Transport(self.执行机, 禁止运行).apply(第二次)
        self.assertEqual(结果['backup'], '')

    def test_示例清单是有效TOML(self):
        示例 = Path(__file__).resolve().parents[2] / '模板/执行机示例.toml'
        self.assertEqual(len(tomllib.loads(示例.read_text(encoding='utf-8'))['executors']), 2)

    def test_密钥与普通名称同词时保留完整MCP名字(self):
        self.写('.claude/settings.json', {'env': {'ANTHROPIC_AUTH_TOKEN': 'magpie'}})
        self.写('.claude.json', {'mcpServers': {'magpie-web-search': {'command': 'npx'}}})
        计划 = self.计划(gateway_key='目标测试值')
        输出 = io.StringIO()
        with contextlib.redirect_stdout(输出):
            同步.print_plan(计划)
        self.assertIn('MCP 跳过：magpie-web-search', 输出.getvalue())
        self.assertNotIn('【已隐藏】-web-search', 输出.getvalue())
        self.assertFalse('magpie' in 同步.redact('Authorization: magpie', 计划.secrets),
                         '实际凭据值仍须脱敏')

    def test_汇总跳过数包含目录条目和MCP(self):
        计划 = 同步.Plan('/home/test', [同步.Item('缺少文件', '跳过', '主机上没有')],
                       ['跳过：缓存链接（指向受保护内容）', 'MCP 跳过：测试服务（不支持）'])
        输出 = io.StringIO()
        with contextlib.redirect_stdout(输出):
            同步.print_plan(计划)
        self.assertIn('跳过 3', 输出.getvalue().splitlines()[-1])

    def test_非敏感env值不抹掉预览的数字和名称(self):
        self.写('.claude/settings.json', {'env': {'FEATURE_ENABLED': '1'}})
        计划 = self.计划()
        输出 = io.StringIO()
        with contextlib.redirect_stdout(输出):
            同步.print_plan(计划)
        self.assertIn('新建 1', 输出.getvalue())

    def 脚本运行器(self, command, **参数):
        脚本 = 参数['input'].decode('utf-8')
        程序 = 脚本.split("python3 -I - <<'DISPATCH_PY'\n", 1)[1].rsplit('\nDISPATCH_PY', 1)[0]
        输出 = io.StringIO()
        with contextlib.redirect_stdout(输出):
            exec(compile(程序, '<目标端脚本>', 'exec'), {})
        return subprocess.CompletedProcess(command, 0, 输出.getvalue().encode(), b'')

    def test_备份根为链接时拒绝并保留旧内容(self):
        self.写('.claude/CLAUDE.md', '新内容')
        目标家 = self.家 / '目标'
        旧文件 = self.写('目标/.claude/CLAUDE.md', '旧内容')
        外部 = self.家 / '外部备份'
        外部.mkdir()
        (目标家 / '.dispatch-同步备份').symlink_to(外部, target_is_directory=True)
        self.目标.home = str(目标家)
        self.目标.items['.claude/CLAUDE.md'] = {'kind': '文件', 'digest': hashlib.sha256('旧内容'.encode()).hexdigest()}
        结果 = 同步.Transport(self.执行机, self.脚本运行器).apply(self.计划(override_home=True))
        self.assertFalse(结果['ok'])
        self.assertEqual(旧文件.read_text(encoding='utf-8'), '旧内容')
        self.assertEqual(list(外部.iterdir()), [])
        self.assertEqual(结果['completed'], [])

    def test_生成文件以私有权限独占创建暂存文件(self):
        self.写('.claude/settings.json', {'language': '中文'})
        目标家 = self.家 / '权限目标'
        目标家.mkdir()
        self.目标.home = str(目标家)
        创建 = []
        原始打开 = os.open
        def 打开(路径, 标志, 模式=0o777, **参数):
            if Path(路径).name.startswith('settings.json.new-'):
                创建.append((标志, 模式))
            return 原始打开(路径, 标志, 模式, **参数)
        with patch.object(os, 'open', side_effect=打开):
            结果 = 同步.Transport(self.执行机, self.脚本运行器).apply(self.计划(override_home=True))
        self.assertTrue(结果['ok'])
        self.assertTrue(创建, '生成文件必须显式以私有权限创建，不能先普通复制再 chmod')
        for 标志, 模式 in 创建:
            self.assertEqual(模式, 0o600)
            self.assertEqual(标志 & (os.O_CREAT | os.O_EXCL | os.O_WRONLY), os.O_CREAT | os.O_EXCL | os.O_WRONLY)

    def test_真实写入脚本的文件目录链接备份(self):
        self.写('.claude/CLAUDE.md', '新规则')
        self.写('.claude/agents/新代理.md', '新代理')
        self.写('.claude/settings.json', {'language': '中文'})
        目标家 = self.家 / '目标'
        self.写('目标/.claude/agents/旧代理.md', '旧代理')
        self.写('目标/.claude/settings.json', '{"language":"旧设置"}')
        (目标家 / '.claude/CLAUDE.md').symlink_to('/原指向')
        self.目标.home = str(目标家)
        namespace = {}
        exec(同步.REMOTE_COMMON, namespace)
        for 路径 in ('.claude/CLAUDE.md', '.claude/agents', '.claude/settings.json'):
            self.目标.items[路径] = namespace['describe'](目标家 / 路径)
        self.目标.documents['.claude/settings.json'] = '{"language":"旧设置"}'
        计划 = self.计划(override_home=True)
        结果 = 同步.Transport(self.执行机, self.脚本运行器).apply(计划)
        self.assertTrue(结果['ok'])
        备份 = Path(结果['backup'])
        self.assertNotIn('/.dispatch/', 备份.as_posix())
        self.assertEqual((备份 / '.claude/CLAUDE.md.链接.txt').read_text(encoding='utf-8'), '/原指向')
        self.assertEqual((备份 / '.claude/agents/旧代理.md').read_text(encoding='utf-8'), '旧代理')
        self.assertEqual((备份 / '.claude/settings.json').read_text(encoding='utf-8'), '{"language":"旧设置"}')
        self.assertTrue((目标家 / '.claude/agents/新代理.md').is_file())

    def test_验证不继承Windows程序路径(self):
        namespace = {}
        exec(同步.REMOTE_COMMON, namespace)
        with patch.dict(os.environ, {'PATH': '/usr/bin:/mnt/c/Windows:/mnt/c/Users/test/bin:/opt/bin'}):
            环境 = namespace['runtime_env']('/tmp/test')
        self.assertEqual(环境['PATH'], '/tmp/test/.local/bin:/usr/bin:/opt/bin')

    def test_本地模式没有配置标记就跳过个人目录(self):
        self.写('.dispatch/无关文件.txt', '不要同步')
        主机 = 同步.collect_host(self.家, local=True)
        计划 = 同步.build_plan(主机, self.目标, {'transport': 'local'})
        self.assertEqual(self.项(计划, '.dispatch').action, '跳过')

    def test_实机脱敏探测夹具(self):
        记录 = 实机脱敏夹具['entry_lines']
        文档行 = ''.join('DISPATCH1\tDOCUMENT\t' + json.dumps({'path': 路径, 'content':
                           None if 内容 is None else base64.b64encode(内容.encode()).decode()}, separators=(',', ':')) + '\n'
                         for 路径, 内容 in 实机脱敏夹具['documents'].items())
        文本 = 记录.replace('DISPATCH1\tEND\t{}\n', 文档行 + 'DISPATCH1\tEND\t{}\n')
        探测 = 同步.parse_probe(文本, expected_paths=['.claude/CLAUDE.md', '.claude/agents', '.claude/skills',
                '.claude/派活配置', '.codex/AGENTS.md', '.codex/agents', '.agents/skills', *同步.GENERATED])
        self.assertEqual(探测.system, 'Linux')
        self.assertEqual(探测.items['.codex/agents']['kind'], '无')
        self.assertEqual(探测.items['.claude/agents']['kind'], '链接')
        tomllib.loads(探测.documents['.codex/config.toml'])
        with self.assertRaises(同步.SyncError):
            同步.parse_probe(文本 + '意外的输出\n')

    def test_MCP回环地址缺网关时整体拒绝(self):
        self.写('.claude.json', {'mcpServers': {'x': {'type': 'http', 'url': 'http://127.0.0.2:18080/mcp'}}})
        执行机 = dict(self.执行机)
        执行机.pop('magpie_url')
        计划 = 同步.build_plan(self.主机(), self.目标, 执行机)
        self.assertTrue(计划.errors)
        self.assertEqual(计划.writes, [])

    def test_空白目标密钥不盖过环境值(self):
        self.写('.claude/settings.json', {'env': {'ANTHROPIC_API_KEY': '主机值'}})
        self.目标.documents['.claude/settings.json'] = json.dumps({'env': {'ANTHROPIC_API_KEY': '  '}})
        计划 = self.计划(gateway_key='环境值')
        self.assertFalse(计划.errors)
        self.assertEqual(json.loads(self.项(计划, '.claude/settings.json').content)['env']['ANTHROPIC_API_KEY'], '环境值')

    def test_显式零端口不按缺端口处理(self):
        self.assertEqual(同步.rewrite_url('http://localhost:0/a', 'https://192.0.2.10:0', 'URL'),
                         'https://192.0.2.10:0/a')
        with self.assertRaises(同步.SyncError):
            同步.rewrite_url('http://localhost:0/a', 'http://192.0.2.10:80', 'URL')

    def test_覆盖家目录时上传临时目录也在目标家内(self):
        self.assertIn("TemporaryDirectory(prefix='.dispatch-上传-', dir=home)", 同步.WRITE_CODE)

    def test_安装成功但目标原有PATH找不到时提示(self):
        目标家 = self.家 / '安装目标'
        目标家.mkdir()
        self.目标.home = str(目标家)
        self.目标.cli = {'claude': {'name': 'claude', 'method': 'missing', 'version': '', 'path': '',
                'realpath': '', 'prefix': '', 'writable': False, 'arch': 'x86_64'}}
        class 假进程:
            returncode = 0
            def __init__(self, 输出):
                self.输出 = 输出
            def communicate(self, timeout=None):
                return self.输出, None
        def 启动(cmd, **选项):
            if cmd[0] == 'curl':
                Path(cmd[-1]).write_bytes(b'#!/bin/bash\n')
            return 假进程('2.1.316' if cmd[-1] == '--version' else '')
        输出 = io.StringIO()
        with patch.object(同步.subprocess, 'Popen', side_effect=启动), patch.object(同步.shutil, 'which', return_value=None), contextlib.redirect_stdout(输出):
            码 = 同步.install_cli(self.目标, {'claude': '2.1.316'},
                                 同步.Transport(self.执行机, self.脚本运行器), execute=True)
        self.assertEqual(码, 0)
        self.assertIn('目标非交互 PATH 还找不到 claude', 输出.getvalue())
        self.assertIn(str(目标家 / '.local/bin'), 输出.getvalue())

    def test_安装下载到HTML时不执行且说明网络原因(self):
        目标家 = self.家 / '安装目标'
        目标家.mkdir()
        self.目标.home = str(目标家)
        self.目标.cli = {名字: {'name': 名字, 'method': 'missing', 'version': '', 'path': '',
                'realpath': '', 'prefix': '', 'writable': False, 'arch': 'x86_64'} for 名字 in ('claude', 'codex')}
        调用 = []
        class 假进程:
            returncode = 0
            def communicate(self, timeout=None):
                return '', None
        def 启动(cmd, **选项):
            调用.append(cmd)
            if cmd[0] == 'curl':
                Path(cmd[-1]).write_text('<!DOCTYPE html><html>地域限制</html>', encoding='utf-8')
            return 假进程()
        输出 = io.StringIO()
        with patch.object(同步.subprocess, 'Popen', side_effect=启动), contextlib.redirect_stdout(输出):
            码 = 同步.install_cli(self.目标, {'claude': '2.1.316', 'codex': '0.166.0'},
                                 同步.Transport(self.执行机, self.脚本运行器), execute=True)
        self.assertEqual(码, 2)
        self.assertFalse(any(cmd[0] == 'bash' for cmd in 调用))
        self.assertIn('下载到的不是安装脚本，检查目标机的网络或代理', 输出.getvalue())
        self.assertNotIn('<html>', 输出.getvalue())

    def test_首项安装失败后剩余项回执和输出标为未尝试(self):
        目标家 = self.家 / '安装目标'
        目标家.mkdir()
        self.目标.home = str(目标家)
        self.目标.cli = {名字: {'method': 'missing', 'arch': 'x86_64'} for 名字 in ('claude', 'codex')}
        for 抛异常 in (False, True):
            with self.subTest(抛异常=抛异常):
                回执 = []
                def 运行器(command, **参数):
                    结果 = self.脚本运行器(command, **参数)
                    回执.extend(同步.protocol_lines(结果.stdout.decode())[0][1])
                    return 结果
                class 假进程:
                    returncode = 1
                    def communicate(self, timeout=None):
                        return '下载失败', None
                输出 = io.StringIO()
                with patch.object(同步.subprocess, 'Popen', side_effect=OSError if 抛异常 else None,
                                  return_value=假进程()) as 启动, contextlib.redirect_stdout(输出):
                    码 = 同步.install_cli(self.目标, {'claude': '2.1.316', 'codex': '0.166.0'},
                                         同步.Transport(self.执行机, 运行器), execute=True)
                self.assertEqual(码, 2)
                self.assertEqual(启动.call_count, 1)
                self.assertEqual([项['name'] for 项 in 回执], ['claude', 'codex'])
                self.assertFalse(回执[1]['ok'])
                self.assertIsNone(回执[1]['code'])
                self.assertEqual(回执[1]['output'], '未尝试：前一项安装失败')
                self.assertIn('安装 codex：未尝试；退出码 未运行', 输出.getvalue())

    def test_旧入口保留源Claude目录检查(self):
        规格 = importlib.util.spec_from_file_location('旧入口检查', Path(__file__).resolve().parents[2] / 'WSL/同步配置到WSL.py')
        旧 = importlib.util.module_from_spec(规格)
        规格.loader.exec_module(旧)
        with patch.object(旧, 'local_main', return_value=0) as 调用, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(旧.main([str(self.家)]), 2)
        调用.assert_not_called()

    def test_个人配置不得覆盖备份根(self):
        with self.assertRaises(同步.SyncError):
            同步.collect_host(self.家, personal=self.家 / '.dispatch-同步备份', environ={})
        with self.assertRaises(同步.SyncError):
            同步.collect_host(self.家, personal=self.家 / '.dispatch-同步备份/内部', environ={})

    def test_个人配置路径含上级跳转时不能逃出目标家(self):
        源 = self.家 / '..' / '本次测试不存在的外部个人配置'
        主机 = 同步.collect_host(self.家, personal=源, environ={})
        self.assertIn('.dispatch', {项.path for 项 in 主机.sources})
        self.assertFalse(any('..' in Path(项.path).parts for 项 in 主机.sources))

    def test_安装错误输出也隐藏目标机已有密钥及编码(self):
        目标家 = self.家 / '安装目标'
        目标家.mkdir()
        self.目标.home = str(目标家)
        self.目标.cli = {名字: {'name': 名字, 'method': 'missing', 'version': '', 'path': '',
                'realpath': '', 'prefix': '', 'writable': False, 'arch': 'x86_64'} for 名字 in ('claude', 'codex')}
        假密钥 = '安装阶段独特假密钥_825645'
        self.目标.documents['.claude/settings.json'] = json.dumps({'env': {'ANTHROPIC_AUTH_TOKEN': 假密钥}})
        class 假进程:
            def __init__(self, code, output):
                self.returncode, self.output = code, output
            def communicate(self, timeout=None):
                return self.output, None
        def 启动(cmd, **选项):
            if cmd[0] == 'curl':
                Path(cmd[-1]).write_bytes(b'#!/bin/bash\n')
                return 假进程(0, '')
            return 假进程(1, 假密钥 + ' ' + base64.b64encode(假密钥.encode()).decode())
        输出 = io.StringIO()
        with patch.object(同步.subprocess, 'Popen', side_effect=启动), contextlib.redirect_stdout(输出):
            同步.install_cli(self.目标, {'claude': '2.1.316', 'codex': '0.166.0'},
                           同步.Transport(self.执行机, self.脚本运行器), execute=True)
        self.assertFalse(假密钥 in 输出.getvalue(), '安装输出不应包含目标密钥明文')
        self.assertFalse(base64.b64encode(假密钥.encode()).decode() in 输出.getvalue(), '安装输出不应包含目标密钥编码')

    def test_脱敏TOML保留日期和表数组的合法类型(self):
        self.目标.documents['.codex/config.toml'] = 'when = 2026-01-02T03:04:05Z\n[[plugin]]\nname = "保留"\napi_key = "需要隐藏"\n'
        脱敏 = 同步.parse_probe(同步.sanitize_probe(同步.encode_probe_fixture(self.目标)))
        结果 = tomllib.loads(脱敏.documents['.codex/config.toml'])
        self.assertEqual(结果['when'].year, 2026)
        self.assertEqual(结果['plugin'][0]['name'], '保留')
        self.assertEqual(结果['plugin'][0]['api_key'], '已脱敏')

    def test_慢速安装提示半小时上限及明确的超时说明(self):
        目标家 = self.家 / '安装目标'
        目标家.mkdir()
        self.目标.home = str(目标家)
        self.目标.cli = {名字: {'name': 名字, 'method': 'missing', 'version': '', 'path': '',
                'realpath': '', 'prefix': '', 'writable': False, 'arch': 'x86_64'} for 名字 in ('claude', 'codex')}
        时限 = []
        class 假进程:
            returncode, pid = 0, 87654321
            def __init__(self, 超时):
                self.超时 = 超时
            def communicate(self, timeout=None):
                时限.append(timeout)
                if self.超时:
                    self.超时 = False
                    raise subprocess.TimeoutExpired('bash', timeout)
                return '原始下载输出', None
        def 启动(cmd, **选项):
            if cmd[0] == 'curl':
                Path(cmd[-1]).write_bytes(b'#!/bin/bash\n')
            return 假进程(cmd[0] == 'bash')
        输出 = io.StringIO()
        with patch.object(同步.subprocess, 'Popen', side_effect=启动), patch('os.killpg', create=True), patch('signal.SIGKILL', 9, create=True), contextlib.redirect_stdout(输出):
            码 = 同步.install_cli(self.目标, {'claude': '2.1.316', 'codex': '0.166.0'},
                                 同步.Transport(self.执行机, self.脚本运行器), execute=True)
        self.assertEqual(码, 2)
        self.assertIn('Claude Code 安装包约 350MB，每项最多等 30 分钟', 输出.getvalue())
        self.assertIn('claude 安装超时', 输出.getvalue())
        self.assertIn('1800 秒', 输出.getvalue())
        self.assertIn('检查目标机网络或代理', 输出.getvalue())
        self.assertIn('原始下载输出', 输出.getvalue())
        self.assertTrue(any(数 is not None and 数 > 1700 for 数 in 时限))

    def test_安装层允许独立验证一个CLI(self):
        self.目标.cli = {'codex': {'version': '0.166.0', 'method': 'binary', 'path': '/home/test/.local/bin/codex'}}
        输出 = io.StringIO()
        with contextlib.redirect_stdout(输出):
            码 = 同步.install_cli(self.目标, {'codex': '0.166.0'}, None, execute=True)
        self.assertEqual(码, 0)
        self.assertIn('版本一致，跳过', 输出.getvalue())
        self.assertNotIn('claude：', 输出.getvalue())

    def test_安装命令完成后给出单独的同步命令(self):
        清单 = self.写('执行机.toml', 'version=1\n[executors.test]\ntransport="ssh"\nssh_host="test"\nplatform="linux"\n')
        self.目标.cli = {名: {'version': 版本, 'method': 'native' if 名 == 'claude' else 'binary'}
                          for 名, 版本 in (('claude', '2.1.316'), ('codex', '0.166.0'))}
        class 假传输:
            def probe(内, *参数):
                return self.目标
        输出 = io.StringIO()
        with patch.object(同步, 'Transport', return_value=假传输()), contextlib.redirect_stdout(输出):
            码 = 同步.main(['test', '--执行机清单', str(清单), '--装CLI', '--执行',
                           '--claude版本', '2.1.316', '--codex版本', '0.166.0'])
        self.assertEqual(码, 0)
        下一步 = next((行 for 行 in 输出.getvalue().splitlines() if 行.startswith('下一步：')), '')
        self.assertEqual(shlex.split(下一步.removeprefix('下一步：'))[:4], ['python', '同步执行机.py', 'test', '--执行'])
        self.assertNotIn('--装CLI', 下一步)

    def test_计划生成只使用已收集的exe存在性(self):
        程序 = self.写('mcp.exe', '假程序')
        self.写('.claude.json', {'mcpServers': {'exe': {'command': str(程序)}}})
        主机 = self.主机()
        with patch.object(Path, 'is_file', side_effect=AssertionError('计划生成不应再次读取主机')):
            计划 = 同步.build_plan(主机, self.目标, {'transport': 'wsl'})
        self.assertFalse(计划.errors)
        self.assertIn('exe', json.loads(self.项(计划, '.claude.json').content)['mcpServers'])


# 结构取自一次实机只读探测（WSL 发行版，链接指向 Windows 家目录）；文档内容已换成最小示例。
实机脱敏夹具 = json.loads('{"entry_lines": "DISPATCH1\\tMETA\\t{\\"home\\":\\"/home/dev\\",\\"system\\":\\"Linux\\",\\"codex_home\\":\\"\\",\\"claude_config_dir\\":\\"\\"}\\nDISPATCH1\\tITEM\\t{\\"path\\":\\".claude/CLAUDE.md\\",\\"kind\\":\\"\\\\u94fe\\\\u63a5\\",\\"link\\":\\"/mnt/c/Users/user/.claude/CLAUDE.md\\"}\\nDISPATCH1\\tITEM\\t{\\"path\\":\\".claude/agents\\",\\"kind\\":\\"\\\\u94fe\\\\u63a5\\",\\"link\\":\\"/mnt/c/Users/user/.claude/agents\\"}\\nDISPATCH1\\tITEM\\t{\\"path\\":\\".claude/skills\\",\\"kind\\":\\"\\\\u94fe\\\\u63a5\\",\\"link\\":\\"/mnt/c/Users/user/.claude/skills\\"}\\nDISPATCH1\\tITEM\\t{\\"path\\":\\".claude/\\\\u6d3e\\\\u6d3b\\\\u914d\\\\u7f6e\\",\\"kind\\":\\"\\\\u94fe\\\\u63a5\\",\\"link\\":\\"/mnt/c/Users/user/.claude/\\\\u6d3e\\\\u6d3b\\\\u914d\\\\u7f6e\\"}\\nDISPATCH1\\tITEM\\t{\\"path\\":\\".codex/AGENTS.md\\",\\"kind\\":\\"\\\\u94fe\\\\u63a5\\",\\"link\\":\\"/mnt/c/Users/user/.codex/AGENTS.md\\"}\\nDISPATCH1\\tITEM\\t{\\"path\\":\\".codex/agents\\",\\"kind\\":\\"\\\\u65e0\\"}\\nDISPATCH1\\tITEM\\t{\\"path\\":\\".agents/skills\\",\\"kind\\":\\"\\\\u94fe\\\\u63a5\\",\\"link\\":\\"/mnt/c/Users/user/.agents/skills\\"}\\nDISPATCH1\\tITEM\\t{\\"path\\":\\".claude/settings.json\\",\\"kind\\":\\"\\\\u6587\\\\u4ef6\\",\\"digest\\":\\"ed44eaf12638997fab7b75235d6706187271a7ee1993a195e0c959559ea28dac\\"}\\nDISPATCH1\\tITEM\\t{\\"path\\":\\".codex/config.toml\\",\\"kind\\":\\"\\\\u6587\\\\u4ef6\\",\\"digest\\":\\"e516fef9ced1d348765a81a77d784412b4575e5151ad148d82ecfa414b0e8123\\"}\\nDISPATCH1\\tITEM\\t{\\"path\\":\\".claude.json\\",\\"kind\\":\\"\\\\u6587\\\\u4ef6\\",\\"digest\\":\\"a37fcc288705d25efeb94e9143c04e78514847a6c0c788cb6df748a266e4ac93\\"}\\nDISPATCH1\\tCLI\\t{\\"name\\":\\"claude\\",\\"path\\":\\"/home/dev/.local/bin/claude\\",\\"realpath\\":\\"/home/dev/.local/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe\\",\\"version\\":\\"2.1.316\\",\\"method\\":\\"npm\\",\\"prefix\\":\\"/home/dev/.local\\",\\"writable\\":true,\\"arch\\":\\"x86_64\\"}\\nDISPATCH1\\tCLI\\t{\\"name\\":\\"codex\\",\\"path\\":\\"/home/dev/.local/bin/codex\\",\\"realpath\\":\\"/home/dev/.local/lib/node_modules/@openai/codex/bin/codex.js\\",\\"version\\":\\"0.166.0\\",\\"method\\":\\"npm\\",\\"prefix\\":\\"/home/dev/.local\\",\\"writable\\":true,\\"arch\\":\\"x86_64\\"}\\nDISPATCH1\\tEND\\t{}\\n", "documents": {".claude/settings.json": "{\\n  \\"model\\": \\"opus\\",\\n  \\"env\\": {\\n    \\"ANTHROPIC_BASE_URL\\": \\"http://127.0.0.1:18080\\",\\n    \\"ANTHROPIC_AUTH_TOKEN\\": \\"已脱敏\\"\\n  },\\n  \\"permissions\\": {\\n    \\"defaultMode\\": \\"default\\"\\n  }\\n}", ".codex/config.toml": "\\"model\\" = \\"gpt-6.1-terra\\"\\n\\"model_reasoning_effort\\" = \\"high\\"\\n\\"service_tier\\" = \\"priority\\"\\n\\"model_reasoning_summary\\" = \\"detailed\\"\\n[\\"hooks\\"]\\n[\\"hooks\\".\\"state\\"]\\n[\\"hooks\\".\\"state\\".\\"/home/dev/.codex/hooks.json:session_start:0:0\\"]\\n\\"enabled\\" = true\\n\\"trusted_hash\\" = \\"sha256:0000000000000000000000000000000000000000000000000000000000000000\\"\\n", ".claude.json": "{\\n  \\"numStartups\\": 1,\\n  \\"hasCompletedOnboarding\\": true,\\n  \\"mcpServers\\": {},\\n  \\"projects\\": {\\n    \\"/home/dev/work/示例项目\\": {\\n      \\"hasTrustDialogAccepted\\": true\\n    }\\n  }\\n}"}}')


if __name__ == '__main__':
    unittest.main()
