#!/usr/bin/env python3
"""按固定清单同步 Linux 执行机，默认只读预览。

Claude 的源配置固定为 ~/.claude，不支持 CLAUDE_CONFIG_DIR。
收集主机、生成计划、传输脚本和命令行分层；探测内容及密钥只留在内存中。
"""
from __future__ import annotations

import argparse
import base64
import copy
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import tomllib
from urllib.parse import urlsplit, urlunsplit

sys.dont_write_bytecode = True
from 组合解析 import config_root, run_cli

SETTINGS_KEEP = ('env', 'attribution', 'model', 'modelPicker', 'language', 'promptCacheTtl',
                 'subagentPromptCacheTtl', 'effortLevel', 'modelSettings', 'tui',
                 'skipDangerousModePermissionPrompt', 'autoMode')
CODEX_KEEP = ('model', 'model_reasoning_effort', 'service_tier', 'openai_base_url',
              'model_reasoning_summary', 'model_catalog_json')
GENERATED = ('.claude/settings.json', '.codex/config.toml', '.claude.json')
INSTALL_TIMEOUT = 1800
SEEN = re.compile(r'^(hasSeen|hasCompleted|hasReset|hasAcknowledged|lastOnboardingVersion$|'
                  r'lastReleaseNotesSeen$|migrationVersion$)|MigrationComplete$|'
                  r'Acknowledged(At)?$|^officialMarketplaceAutoInstall')
SECRET_KEY = re.compile(r'TOKEN|KEY|SECRET|PASSWORD', re.I)
HEADER_KEY = re.compile(r'^(Authorization|Cookie)$|TOKEN|KEY|SECRET', re.I)
GATEWAY_KEYS = ('ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY')
PROXY_KEYS = frozenset(('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY'))


class SyncError(ValueError):
    """可直接显示给使用者的错误；不得包含配置值。"""


@dataclass
class Entry:
    name: str
    kind: str
    digest: str = ''
    source: Path | None = field(default=None, repr=False)
    mode: int = 0o644


@dataclass
class Source:
    path: str
    source: Path = field(repr=False)
    kind: str = '无'
    entries: list[Entry] = field(default_factory=list, repr=False)
    digest: str = ''
    link: str = ''
    reason: str = ''
    excluded_backups: bool = False


@dataclass
class Host:
    home: Path
    codex: Path
    sources: list[Source]
    documents: dict[str, str | None] = field(repr=False)
    notices: list[str] = field(default_factory=list)
    available_exes: set[str] = field(default_factory=set, repr=False)


@dataclass
class Probe:
    home: str
    system: str
    codex_home: str
    claude_config_dir: str
    items: dict
    documents: dict = field(repr=False)
    cli: dict = field(default_factory=dict)


@dataclass
class Item:
    path: str
    action: str
    reason: str
    kind: str = 'file'
    content: bytes | None = field(default=None, repr=False)
    link: str = ''
    entries: list[Entry] = field(default_factory=list, repr=False)
    changed_keys: list[str] = field(default_factory=list)
    previous: dict = field(default_factory=lambda: {'kind': '无'})
    digest: str = ''


@dataclass
class Plan:
    home: str
    items: list[Item]
    notices: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list, repr=False)
    mcp_names: set[str] = field(default_factory=set, repr=False)

    @property
    def writes(self):
        return [] if self.errors else [item for item in self.items if item.action in ('新建', '替换')]


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def directory_digest(rows: list[list[str]]) -> str:
    return digest_bytes(json.dumps(sorted(rows), ensure_ascii=True, separators=(',', ':')).encode())


def parse_url(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in ('http', 'https') or not parsed.hostname:
            return None
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme.lower() == 'https' else 80)
        return parsed, port
    except ValueError:
        raise SyncError('URL 格式无效（仅报告键名，不显示值）') from None


def is_loopback(host: str) -> bool:
    host = host.rstrip('.').lower()
    if host == 'localhost':
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            address = ipaddress.ip_address(socket.inet_aton(host))
        except OSError:
            return False
    return address.is_loopback or address.is_unspecified


def parse_executors(text: str) -> dict:
    try:
        config = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise SyncError('执行机清单不是有效 TOML') from None
    errors = []
    if type(config.get('version')) is not int or config.get('version') != 1:
        errors.append('version 必须为 1')
    errors.extend(f'清单不认识的键：{key}' for key in config.keys() - {'version', 'executors'})
    executors = config.get('executors', {})
    if not isinstance(executors, dict) or not executors:
        errors.append('executors 必须包含至少一台执行机')
        executors = {}
    allowed = {'transport', 'distro', 'ssh_host', 'platform', 'workdir', 'orca_name', 'magpie_url'}
    for name, entry in executors.items():
        if not isinstance(entry, dict):
            errors.append(f'{name}：必须为表')
            continue
        errors.extend(f'{name}：不认识的键 {key}' for key in entry.keys() - allowed)
        for key, value in entry.items():
            if not isinstance(value, str) or not value.strip() or any(c in value for c in '\n\r\0'):
                errors.append(f'{name}：{key} 必须为非空单行字符串')
        if entry.get('transport') not in ('wsl', 'ssh'):
            errors.append(f'{name}：transport 只支持 wsl、ssh')
        if entry.get('platform') != 'linux':
            errors.append(f'{name}：platform 只支持 linux')
        required = 'distro' if entry.get('transport') == 'wsl' else 'ssh_host'
        if not isinstance(entry.get(required), str) or not entry[required].strip():
            errors.append(f'{name}：缺少 {required}')
        if isinstance(entry.get('ssh_host'), str) and entry['ssh_host'].startswith('-'):
            errors.append(f'{name}：ssh_host 不能以减号开头')
        if 'magpie_url' in entry:
            try:
                url = parse_url(entry['magpie_url'])
                if not url or url[0].username or url[0].password or url[0].query or url[0].fragment or url[0].path not in ('', '/'):
                    errors.append(f'{name}：magpie_url 必须为不带路径和认证的 HTTP 地址')
                elif entry.get('transport') == 'ssh' and is_loopback(url[0].hostname):
                    errors.append(f'{name}：magpie_url 不得是回环地址')
            except SyncError:
                errors.append(f'{name}：magpie_url 格式无效')
    if errors:
        raise SyncError('\n'.join(errors))
    return executors


def wsl_path(path: str) -> str | None:
    match = re.match(r'^([A-Za-z]):[\\/](.*)$', path)
    if match:
        return f'/mnt/{match[1].lower()}/' + match[2].replace('\\', '/')
    return path if path.startswith('/') else None


def source_specs(home: Path, codex: Path, personal: Path | None):
    specs = [(home / '.claude/CLAUDE.md', '.claude/CLAUDE.md'),
             (home / '.claude/agents', '.claude/agents'),
             (home / '.claude/skills', '.claude/skills')]
    if personal is not None:
        try:
            relative = Path(os.path.abspath(personal)).relative_to(Path(os.path.abspath(home))).as_posix()
        except ValueError:
            relative = '.dispatch'
        if relative in ('', '.'):
            raise SyncError('个人配置目录不能是主机家目录本身')
        if relative == '.dispatch-同步备份' or relative.startswith('.dispatch-同步备份/'):
            raise SyncError('个人配置目录不能与备份根重叠')
        if any(part in ('', '.', '..') for part in relative.split('/')) or any(c in relative for c in '\n\r\0'):
            raise SyncError('个人配置目录目标路径无效')
        if personal.name == '本机' and any(relative.startswith(root + '/') for root in ('.claude/skills', '.agents/skills')):
            # 独立落点避免嵌在技能链接中写回主机，也避免目录摘要在每次同步后再次变化。
            relative = '.dispatch'
        specs.append((personal, relative))
    else:
        specs.append((home / '.dispatch', '.dispatch'))
    specs.extend([(codex / 'AGENTS.md', '.codex/AGENTS.md'),
                  (codex / 'agents', '.codex/agents'),
                  (home / '.agents/skills', '.agents/skills')])
    destinations = [destination for _, destination in specs]
    for destination in destinations:
        if destination in GENERATED or any(destination == other or destination.startswith(other + '/')
                                           for other in destinations if other != destination):
            raise SyncError('个人配置目录与固定同步项重叠')
    if len(destinations) != len(set(destinations)):
        raise SyncError('个人配置目录与固定同步项重叠')
    return specs


def collect_host(home: Path, *, personal: Path | None = None, environ=None, local=False) -> Host:
    """只收集固定清单；目录链接解引用，受保护路径即使绕路也拒绝。"""
    environ = os.environ if environ is None else environ
    codex = Path(environ.get('CODEX_HOME') or home / '.codex').expanduser()
    if local:
        personal = home / '.dispatch' if (home / '.dispatch/config.toml').is_file() else (
            home / '.claude/派活配置' if (home / '.claude/派活配置').is_dir() else None)
        codex = home / '.codex'
    personal_real = personal.resolve() if personal is not None else None
    protected = [home / '.ssh', home / '.codex/auth.json', home / '.claude/.credentials.json',
                 home / '.claude/projects', home / '.codex/sessions', home / '.claude.json',
                 home / '.claude/settings.json', home / '.claude/history.jsonl',
                 home / '.mozilla', home / 'Library/Safari', home / 'Library/Cookies']
    for suffix in ('.config/google-chrome', '.config/chromium', '.config/microsoft-edge',
                   '.config/BraveSoftware/Brave-Browser', 'AppData/Local/Google/Chrome/User Data',
                   'AppData/Local/Chromium/User Data', 'AppData/Local/Microsoft/Edge/User Data',
                   'AppData/Local/BraveSoftware/Brave-Browser/User Data',
                   'AppData/Roaming/Mozilla/Firefox', 'Library/Application Support/Google/Chrome',
                   'Library/Application Support/Chromium', 'Library/Application Support/Firefox'):
        protected.append(home / suffix)
    protected = [p.resolve() for p in protected]
    codex_real = codex.resolve()
    permitted_codex = [(codex / 'AGENTS.md').resolve(), (codex / 'agents').resolve()]
    notices = []
    cache_directories = frozenset(('__pycache__', '.cache', 'cache', 'caches',
                                  '.pytest_cache', '.mypy_cache', '.ruff_cache', '_cacache'))
    claude_private = frozenset(('history.jsonl', 'projects', 'sessions', '.credentials.json', 'settings.json'))
    browser_configs = frozenset(('chromium', 'chromium-browser', 'google-chrome', 'google-chrome-beta',
                                 'google-chrome-unstable', 'bravesoftware', 'microsoft-edge',
                                 'microsoft-edge-beta', 'microsoft-edge-dev', 'vivaldi', 'opera'))
    browser_files = frozenset(('cookies', 'login data', 'local state', 'web data'))

    def blocked(path, real):
        if path.name in ('auth.json', '.credentials.json') or path.suffix == '.pyc':
            return True
        if path.name.startswith(('id_rsa', 'id_ed25519', 'id_ecdsa')):
            return True
        if any(real == root or root in real.parents for root in protected):
            return True
        directories = (real, *real.parents) if real.is_dir() else real.parents
        if any(directory.name.casefold() in cache_directories for directory in directories):
            return True
        # 受保护特征只看解引用后的路径段，与主机家目录的位置无关。
        parts = tuple(part.casefold() for part in real.parts)
        if '.mozilla' in parts or (real.is_file() and real.name.casefold() in browser_files):
            return True
        for parent, child in zip(parts, parts[1:]):
            if parent == '.claude' and child in claude_private:
                return True
            if parent == '.codex' and child not in ('agents.md', 'agents'):
                return True
            if parent == '.config' and child in browser_configs:
                return True
        if real == codex_real or codex_real in real.parents:
            return not any(real == root or root in real.parents for root in permitted_codex)
        return False

    def walk(path, relative, ancestors, entries, skip_backups=False):
        try:
            real = path.resolve(strict=True)
        except (OSError, RuntimeError):
            notices.append(f'跳过：{relative}（失效或循环链接）')
            return
        if relative and personal_real is not None and real == personal_real:
            notices.append(f'跳过：{relative}（个人配置单独同步）')
            return
        if blocked(path, real) or blocked(real, real):
            notices.append(f'跳过：{relative}（指向受保护内容）')
            return
        if real in ancestors:
            notices.append(f'跳过：{relative}（循环链接）')
            return
        if real.is_dir():
            entries.append(Entry(relative, '目录'))
            for child in sorted(real.iterdir()):
                if skip_backups and child.name == '备份':
                    continue
                walk(child, f'{relative}/{child.name}' if relative else child.name,
                     ancestors | {real}, entries)
        elif real.is_file():
            with real.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            mode = 0o755 if real.stat().st_mode & 0o111 else 0o644
            entries.append(Entry(relative, '文件', digest, real, mode))
        else:
            notices.append(f'跳过：{relative}（不支持的文件类型）')

    sources = []
    for path, relative in source_specs(home, codex, personal):
        source = Source(relative, path)
        if local and personal is None and relative == '.dispatch':
            source.reason = '主机上没有已识别的个人配置目录，跳过'
            sources.append(source)
            continue
        if not path.exists():
            source.reason = '失效或循环链接' if path.is_symlink() else '主机上没有'
            notices.append(f'跳过：{relative}（{source.reason}）')
        else:
            source.excluded_backups = (personal_real is not None and path.resolve() == personal_real
                                       and (path / '备份').is_dir())
            walk(path, '', set(), source.entries, skip_backups=source.excluded_backups)
            if source.entries:
                source.kind = source.entries[0].kind
                if source.kind == '目录':
                    source.digest = directory_digest([[e.name, e.kind, e.digest] for e in source.entries if e.name])
                else:
                    source.digest = source.entries[0].digest
                source.link = wsl_path(str(path.absolute())) or ''
            else:
                source.reason = '受保护或不可读取的路径'
        sources.append(source)
    documents = {}
    for relative, path in ((GENERATED[0], home / GENERATED[0]),
                           (GENERATED[1], codex / 'config.toml'),
                           (GENERATED[2], home / GENERATED[2])):
        try:
            documents[relative] = path.read_text(encoding='utf-8-sig') if path.is_file() else None
        except (OSError, UnicodeError):
            raise SyncError(f'主机源文件不能读取：{relative}') from None
    available_exes = set()
    try:
        servers = json.loads(documents.get('.claude.json') or '{}').get('mcpServers') or {}
        if isinstance(servers, dict):
            for config in servers.values():
                command = config.get('command') if isinstance(config, dict) else None
                if isinstance(command, str) and command.lower().endswith('.exe'):
                    converted = wsl_path(command)
                    if converted and (Path(command).is_file() or Path(converted).is_file()):
                        available_exes.add(converted)
    except (ValueError, AttributeError):
        # 配置格式错误由纯计划层统一报告，不在这里显示原文。
        pass
    return Host(home, codex, sources, documents, notices, available_exes)


def load_json(text: str | None, name: str) -> dict:
    if text is None:
        return {}
    try:
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise SyncError(f'{name} 不是有效 JSON 对象') from None


def rewrite_url(value, magpie_url, key):
    try:
        parsed = parse_url(value)
    except SyncError:
        raise SyncError(f'地址格式无效：{key}') from None
    if parsed is None or not is_loopback(parsed[0].hostname):
        return value
    if not magpie_url:
        raise SyncError(f'回环地址需要 magpie_url：{key}')
    gateway = parse_url(magpie_url)
    if gateway is None or parsed[1] != gateway[1]:
        raise SyncError(f'回环地址端口不同：{key}')
    return urlunsplit((gateway[0].scheme, gateway[0].netloc, parsed[0].path,
                       parsed[0].query, parsed[0].fragment))


def settings_content(host, target, executor, gateway_key, stdin_key, notices):
    result = {key: copy.deepcopy(host[key]) for key in SETTINGS_KEEP if key in host}
    permissions = host.get('permissions') or {}
    if isinstance(permissions, dict) and 'defaultMode' in permissions:
        result['permissions'] = {'defaultMode': permissions['defaultMode']}
    if 'env' in result and not isinstance(result['env'], dict):
        raise SyncError('settings.json：env 必须为对象')
    if executor['transport'] != 'ssh':
        return result
    target_env = target.get('env') or {}
    if not isinstance(target_env, dict):
        raise SyncError('目标 settings.json：env 必须为对象')
    errors = []
    for key in list(result.get('env', {})):
        if key.upper() in PROXY_KEYS:
            value = target_env.get(key)
            if isinstance(value, str) and value.strip():
                result['env'][key] = value
            else:
                result['env'].pop(key)
                notices.append(f'env.{key}：代理设置跟随目标机，目标没有同名值，已去掉')
        elif key in GATEWAY_KEYS:
            value = next((candidate for candidate in (target_env.get(key), gateway_key, stdin_key)
                          if isinstance(candidate, str) and candidate.strip()), None)
            if value is None:
                errors.append(f'{key}：请在目标 settings.json 中配置、设置 DISPATCH_GATEWAY_KEY，或用 --密钥从标准输入')
                continue
            result['env'][key] = value
        elif SECRET_KEY.search(key):
            if target_env.get(key):
                result['env'][key] = target_env[key]
            else:
                result['env'].pop(key)
                notices.append(f'env.{key}：目标没有同名值，已去掉，请在目标机配置')
        else:
            try:
                result['env'][key] = rewrite_url(result['env'][key], executor.get('magpie_url'), f'env.{key}')
            except SyncError as exc:
                errors.append(str(exc))
    if errors:
        raise SyncError('\n'.join(errors))
    return result


def mcp_content(host, target, executor, notices, available_exes=()):
    result = copy.deepcopy(target)
    servers = host.get('mcpServers') or {}
    if not isinstance(servers, dict):
        raise SyncError('mcpServers 必须为对象')
    old_servers = target.get('mcpServers') or {}
    keep = {}
    for name, raw in servers.items():
        if not isinstance(raw, dict):
            notices.append(f'MCP 跳过：{name}（配置不是对象）')
            continue
        config = copy.deepcopy(raw)
        kind = config.get('type', 'stdio')
        reason = ''
        if kind in ('http', 'sse'):
            if executor['transport'] == 'ssh':
                address = parse_url(config.get('url'))
                if address and is_loopback(address[0].hostname) and not executor.get('magpie_url'):
                    raise SyncError(f'MCP.{name}.url：回环地址需要 magpie_url')
                try:
                    config['url'] = rewrite_url(config.get('url'), executor.get('magpie_url'), 'MCP.url')
                except SyncError:
                    reason = '回环 URL 无法转换'
                headers = config.get('headers') or {}
                existing = old_servers.get(name, {}) if isinstance(old_servers, dict) else {}
                old_headers = existing.get('headers', {}) if isinstance(existing, dict) else {}
                if not isinstance(headers, dict) or not isinstance(old_headers, dict):
                    reason = 'headers 格式无效'
                else:
                    for key in headers:
                        if HEADER_KEY.search(key):
                            if key not in old_headers or not old_headers[key]:
                                reason = '目标没有同名敏感 header'
                            else:
                                config['headers'][key] = old_headers[key]
                environment = config.get('env', {})
                old_environment = existing.get('env', {}) if isinstance(existing, dict) else {}
                if not isinstance(environment, dict) or not isinstance(old_environment, dict):
                    reason = 'env 格式无效'
                else:
                    for key in list(environment):
                        if SECRET_KEY.search(key):
                            value = old_environment.get(key)
                            if isinstance(value, str) and value.strip():
                                environment[key] = value
                            else:
                                environment.pop(key)
                                notices.append(f'MCP.{name}.env.{key}：目标没有同名值，已去掉，请在目标机配置')
        elif executor['transport'] == 'ssh':
            reason = 'ssh 只带 HTTP/SSE'
        else:
            command = config.get('command', '')
            args = config.get('args') or []
            windows_args = any(re.match(r'^[A-Za-z]:[\\/]', str(arg)) for arg in args)
            if isinstance(command, str) and command.lower().endswith('.exe') and not windows_args:
                converted = wsl_path(command)
                if converted and converted in available_exes:
                    config['command'] = converted
                else:
                    reason = '主机上没有该 exe'
            elif command not in ('uvx', 'npx', 'uv') or windows_args:
                reason = 'Windows 专用命令或参数'
        if reason:
            notices.append(f'MCP 跳过：{name}（{reason}）')
        else:
            keep[name] = config
    result['mcpServers'] = keep
    for key, value in host.items():
        if isinstance(value, (bool, int, str)) and SEEN.search(key):
            result[key] = value
    result['hasSeenAutoDefaultNudge'] = True
    return result


def toml_value(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (datetime, date, datetime_time)):
        return value.isoformat()
    if isinstance(value, list):
        return '[' + ', '.join(toml_value(x) for x in value) + ']'
    if isinstance(value, dict):
        return '{' + ', '.join(json.dumps(key, ensure_ascii=False) + ' = ' + toml_value(item)
                               for key, item in value.items()) + '}'
    raise SyncError('Codex 托管键的类型不支持')


def merge_codex(source: str, target: str, executor: dict) -> str:
    """只编辑根表中的完整赋值语句，其他字节（含表与注释）保留。"""
    try:
        host = tomllib.loads(source)
        original = tomllib.loads(target)
    except tomllib.TOMLDecodeError:
        raise SyncError('Codex config.toml 不是有效 TOML') from None
    managed = {key: value for key, value in host.items() if key in CODEX_KEEP and key != 'model_catalog_json'}
    if executor['transport'] == 'ssh' and 'openai_base_url' in managed:
        managed['openai_base_url'] = rewrite_url(managed['openai_base_url'], executor.get('magpie_url'), 'openai_base_url')
    lines = target.splitlines(keepends=True)
    output = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.lstrip()
        if stripped.startswith('['):
            output.extend(lines[index:])
            break
        if not stripped.strip() or stripped.startswith('#'):
            output.append(line)
            index += 1
            continue
        statement = line
        index += 1
        while True:
            try:
                part = tomllib.loads(statement)
                break
            except tomllib.TOMLDecodeError:
                if index >= len(lines):
                    raise SyncError('Codex 根表赋值无法安全合并') from None
                statement += lines[index]
                index += 1
        if not (part.keys() & set(CODEX_KEEP)):
            output.append(statement)
    prefix = ''.join(f'{key} = {toml_value(value)}\n' for key, value in managed.items())
    result = prefix + ''.join(output)
    expected = {key: value for key, value in original.items() if key not in CODEX_KEEP}
    expected.update(managed)
    try:
        if tomllib.loads(result) != expected:
            raise ValueError()
    except (ValueError, tomllib.TOMLDecodeError):
        raise SyncError('Codex 合并结果不合法，该项不会写入') from None
    return result


def secret_values(value, *, all_values=False):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            sensitive = all_values or key == 'headers' or bool(SECRET_KEY.search(key))
            found.extend(secret_values(item, all_values=sensitive))
    elif isinstance(value, list):
        for item in value:
            found.extend(secret_values(item, all_values=all_values))
    elif all_values and isinstance(value, str) and value:
        found.append(value)
    return found


def redact(text: str, secrets=()) -> str:
    """日志只显示键名；同时清理已知明文、编码值和常见凭据格式。"""
    for value in sorted(set(secrets), key=len, reverse=True):
        if value:
            text = text.replace(base64.b64encode(value.encode()).decode(), '【已隐藏】').replace(value, '【已隐藏】')
    text = re.sub(r'(?i)(Bearer\s+)[^\s\"\',;]+', r'\1【已隐藏】', text)
    text = re.sub(r'\b(?:sk-|sk_ant_|sk-ant-)[A-Za-z0-9_-]+', '【已隐藏】', text)
    return text


def sanitize_document(text: str, *, toml=False) -> str:
    """探测夹具用的脱敏内容：env、headers 和像密钥的键全部占位。"""
    data = tomllib.loads(text) if toml else json.loads(text)
    def clean(value, force=False):
        if isinstance(value, dict):
            return {key: clean(item, force or key in ('env', 'headers') or bool(SECRET_KEY.search(key)))
                    for key, item in value.items()}
        if isinstance(value, list):
            return [clean(item, force) for item in value]
        return '已脱敏' if force else value
    cleaned = clean(data)
    if not toml:
        return json.dumps(cleaned, ensure_ascii=False, separators=(',', ':'))
    lines = []
    def write_table(table, prefix=()):
        if prefix:
            lines.append('[' + '.'.join(json.dumps(key, ensure_ascii=False) for key in prefix) + ']')
        for key, value in table.items():
            if not isinstance(value, dict):
                lines.append(json.dumps(key, ensure_ascii=False) + ' = ' + toml_value(value))
        for key, value in table.items():
            if isinstance(value, dict):
                write_table(value, (*prefix, key))
    write_table(cleaned)
    return '\n'.join(lines) + '\n'


def build_plan(host: Host, probe: Probe, executor: dict, *, gateway_key='', stdin_key='', override_home=False) -> Plan:
    plan = Plan(probe.home, [], list(host.notices))
    for path in GENERATED:
        target_text = probe.documents.get(path)
        if target_text is not None:
            try:
                target_data = tomllib.loads(target_text) if path.endswith('.toml') else load_json(target_text, '目标 ' + path)
                plan.secrets.extend(secret_values(target_data))
            except (ValueError, SyncError):
                plan.errors.append('目标配置无法解析：' + path)
    if probe.system != 'Linux':
        plan.errors.append('目标不是 Linux')
    if not override_home:
        if probe.codex_home and probe.codex_home != probe.home + '/.codex':
            plan.errors.append('目标使用自定义 CODEX_HOME，本脚本不支持')
        if probe.claude_config_dir and probe.claude_config_dir != probe.home + '/.claude':
            plan.errors.append('目标使用自定义 CLAUDE_CONFIG_DIR，本脚本不支持')
    for source in host.sources:
        previous = probe.items.get(source.path, {'kind': '无'})
        if source.kind == '无':
            plan.items.append(Item(source.path, '跳过', source.reason or '主机上没有'))
            continue
        link_mode = executor['transport'] in ('wsl', 'local')
        # WSL/本地模式整体链接个人目录，仍可见其中的备份；只有 SSH 复制时排除。
        if not link_mode and source.excluded_backups:
            plan.notices.append(f'跳过：{source.path}/备份（本机的安装和复盘备份，不同步）')
        if link_mode and not source.link:
            plan.errors.append(f'不能转换为 WSL 路径：{source.path}')
        same = (previous.get('kind') == '链接' and previous.get('link') == source.link) if link_mode else (
            previous.get('kind') == source.kind and previous.get('digest') == source.digest)
        plan.items.append(Item(source.path, '不变' if same else ('新建' if previous['kind'] == '无' else '替换'),
                               '链接指向相同' if same and link_mode else ('内容相同' if same else '按固定清单同步'),
                               'link' if link_mode else ('dir' if source.kind == '目录' else 'file'),
                               link=source.link if link_mode else '', entries=source.entries,
                               previous=previous, digest=source.digest))
    for path in GENERATED:
        text = host.documents.get(path)
        previous = probe.items.get(path, {'kind': '无'})
        if text is None:
            plan.items.append(Item(path, '跳过', '主机上没有，跳过'))
            continue
        try:
            target_text = probe.documents.get(path)
            if path == '.codex/config.toml':
                before = tomllib.loads(target_text or '')
                after_text = merge_codex(text, target_text or '', executor)
                after = tomllib.loads(after_text)
                plan.secrets.extend(secret_values(tomllib.loads(text)))
                plan.secrets.extend(secret_values(before))
            else:
                source = load_json(text, '主机 ' + path)
                before = load_json(target_text, '目标 ' + path)
                if path == '.claude.json' and isinstance(source.get('mcpServers'), dict):
                    plan.mcp_names.update(source['mcpServers'])
                plan.secrets.extend(secret_values(source))
                plan.secrets.extend(secret_values(before))
                if path == '.claude/settings.json':
                    after = settings_content(source, before, executor, gateway_key, stdin_key, plan.notices)
                else:
                    after = mcp_content(source, before, executor, plan.notices, host.available_exes)
                after_text = json.dumps(after, ensure_ascii=False, indent=2) + '\n'
            content = after_text.encode('utf-8')
            changed = []
            for key in sorted(before.keys() | after.keys()):
                if before.get(key) != after.get(key) or (key in before) != (key in after):
                    changed.append(key)
                    if key == 'env':
                        old_env, new_env = before.get('env') or {}, after.get('env') or {}
                        if isinstance(old_env, dict) and isinstance(new_env, dict):
                            changed.extend('env.' + k for k in sorted(old_env.keys() | new_env.keys())
                                           if old_env.get(k) != new_env.get(k) or (k in old_env) != (k in new_env))
            same = previous.get('kind') == '文件' and previous.get('digest') == digest_bytes(content)
            plan.items.append(Item(path, '不变' if same else ('新建' if previous['kind'] == '无' else '替换'),
                                   '内容相同' if same else '生成配置', content=content,
                                   changed_keys=changed, previous=previous, digest=digest_bytes(content)))
        except (SyncError, ValueError) as exc:
            message = str(exc) if isinstance(exc, SyncError) else f'{path} 无法解析或合并'
            plan.errors.append(message)
            plan.items.append(Item(path, '跳过', '生成失败，不写入'))
    plan.secrets.extend(value for value in (gateway_key, stdin_key) if value)
    return plan


def make_archive(plan: Plan) -> bytes:
    """只把计划中获准的普通文件和目录打包，逐文件流式读取。"""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for index, item in enumerate(plan.writes):
            if item.kind == 'link':
                continue
            if item.content is not None:
                info = tarfile.TarInfo(str(index))
                info.size = len(item.content)
                info.mode = 0o600
                archive.addfile(info, io.BytesIO(item.content))
                continue
            for entry in item.entries:
                info = tarfile.TarInfo(str(index) + ('/' + entry.name if entry.name else ''))
                info.mode = entry.mode
                if entry.kind == '目录':
                    info.type = tarfile.DIRTYPE
                    archive.addfile(info)
                else:
                    if entry.source is None:
                        raise SyncError('归档缺少源文件')
                    with entry.source.open('rb') as stream:
                        if hashlib.file_digest(stream, 'sha256').hexdigest() != entry.digest:
                            raise SyncError(f'主机源在预览后变化：{item.path}')
                        stream.seek(0)
                        info.size = entry.source.stat().st_size
                        archive.addfile(info, stream)
    return buffer.getvalue()


# 这些 Python 片段在目标 sh 的标准输入中运行；不在两端落脚本文件。
REMOTE_COMMON = r'''
import base64, hashlib, io, json, os, pathlib, re, shutil, signal, stat, subprocess, sys, tarfile, tempfile, time
P = pathlib.Path

def emit(kind, value):
    print('DISPATCH1\t' + kind + '\t' + json.dumps(value, ensure_ascii=True, separators=(',', ':')), flush=True)

def sha(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def describe(path):
    if path.is_symlink():
        return {'kind': '链接', 'link': os.readlink(path)}
    if not path.exists():
        return {'kind': '无'}
    if path.is_file():
        return {'kind': '文件', 'digest': sha(path)}
    if not path.is_dir():
        raise ValueError('unsupported type')
    rows = []
    def visit(folder, prefix):
        for child in sorted(folder.iterdir()):
            name = prefix + child.name
            if child.is_symlink():
                rows.append([name, '链接', os.readlink(child)])
            elif child.is_dir():
                rows.append([name, '目录', ''])
                visit(child, name + '/')
            elif child.is_file():
                rows.append([name, '文件', sha(child)])
            else:
                raise ValueError('unsupported type')
    visit(path, '')
    digest = hashlib.sha256(json.dumps(sorted(rows), ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
    return {'kind': '目录', 'digest': digest}

def real_parents(path, create=False):
    parts = list(reversed(path.parents)) + [path]
    for parent in parts:
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            raise ValueError('unsafe parent')
        if create and not parent.exists():
            parent.mkdir(mode=0o755)

def runtime_env(home):
    env = os.environ.copy()
    env.update(HOME=home, CODEX_HOME=home + '/.codex', CLAUDE_CONFIG_DIR=home + '/.claude',
               PATH=home + '/.local/bin:' + ':'.join(part for part in env.get('PATH', '').split(':')
                    if part and not re.match(r'^/mnt/[A-Za-z]/', part)), LC_ALL='C.UTF-8')
    return env

def run_process(command, env, cwd=None, limit=180):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding='utf-8', errors='replace', env=env,
                               cwd=cwd, start_new_session=True)
    try:
        output, _ = process.communicate(timeout=limit)
        return process.returncode, output
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        output, _ = process.communicate()
        return 124, output + '\n执行超时'
'''

PROBE_CODE = r'''
try:
    home = payload['home'] or os.environ['HOME']
    if not home.startswith('/') or '..' in P(home).parts:
        raise ValueError('home')
    emit('META', {'home': home.rstrip('/'), 'system': os.uname().sysname,
                  'codex_home': os.environ.get('CODEX_HOME', ''),
                  'claude_config_dir': os.environ.get('CLAUDE_CONFIG_DIR', '')})
    for name in payload['paths']:
        emit('ITEM', {'path': name, **describe(P(home) / name)})
    for name in payload['documents']:
        path = P(home) / name
        content = base64.b64encode(path.read_bytes()).decode() if path.is_file() else None
        emit('DOCUMENT', {'path': name, 'content': content})
    env = runtime_env(home)
    for name in ('claude', 'codex'):
        path = shutil.which(name, path=env['PATH'])
        data = {'name': name, 'path': path or '', 'realpath': '', 'version': '',
                'method': 'missing', 'prefix': '', 'writable': False, 'arch': os.uname().machine}
        if path:
            real = str(P(path).resolve())
            data['realpath'] = real
            code, output = run_process([path, '--version'], env, limit=20)
            match = re.search(r'\b(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)\b', output) if code == 0 else None
            data['version'] = match[1] if match else ''
            if '/node_modules/@anthropic-ai/claude-code/' in real or '/node_modules/@openai/codex/' in real:
                data['method'] = 'npm'
                before = real.split('/node_modules/', 1)[0]
                data['prefix'] = before[:-4] if before.endswith('/lib') else before
            elif name == 'claude' and '/.local/share/claude/versions/' in real:
                data['method'] = 'native'
            elif name == 'codex':
                with open(real, 'rb') as stream:
                    data['method'] = 'binary' if stream.read(4) == b'\x7fELF' else 'unknown'
            else:
                data['method'] = 'unknown'
            parent = P(data['prefix']) if data['method'] == 'npm' else P(real).parent
            data['writable'] = os.access(parent, os.W_OK)
        emit('CLI', data)
    emit('END', {})
except Exception:
    emit('ERROR', {'message': '目标探测失败；未显示配置内容'})
    sys.exit(2)
'''

WRITE_CODE = r'''
os.umask(0o022)
home = P(payload['home'])
backup = home / '.dispatch-同步备份' / payload['stamp']
completed = []
backed = []
current = ''
staged = None
try:
    real_parents(home)
    real_parents(backup)
    for item in payload['items']:
        real_parents((home / item['path']).parent)
    # 先验证所有目标仍与探测时一致，避免用旧快照覆盖另一会话的修改。
    for item in payload['items']:
        current = item['path']
        if describe(home / current) != item['previous']:
            raise ValueError('target changed')
    real_parents(home, create=True)
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix='.dispatch-上传-', dir=home) as scratch:
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(archive_data)), mode='r:gz') as archive:
            for member in archive:
                parts = P(member.name).parts
                if member.name.startswith('/') or '..' in parts or not (member.isfile() or member.isdir()):
                    raise ValueError('unsafe archive')
                target = P(scratch) / member.name
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    target.chmod(0o755)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as src, target.open('wb') as dst:
                        shutil.copyfileobj(src, dst)
                    target.chmod(member.mode & 0o777)
        os.umask(0o022)
        for index, item in enumerate(payload['items']):
            current = item['path']
            target = home / current
            real_parents(target.parent, create=True)
            staged = target.with_name(target.name + '.new-' + payload['stamp'])
            if os.path.lexists(staged):
                raise ValueError('staging exists')
            if item['kind'] == 'link':
                staged.symlink_to(item['link'])
            elif item['kind'] == 'dir':
                shutil.copytree(P(scratch) / str(index), staged)
                staged.chmod(0o755)
            else:
                mode = 0o600 if item['generated'] else item['mode']
                descriptor = os.open(staged, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
                with os.fdopen(descriptor, 'wb') as dst, (P(scratch) / str(index)).open('rb') as src:
                    shutil.copyfileobj(src, dst)
            if item['previous']['kind'] != '无':
                real_parents(backup, create=True)
                destination = backup / current
                real_parents(destination.parent, create=True)
                if target.is_symlink():
                    link_note = destination.with_name(destination.name + '.链接.txt')
                    if os.path.lexists(link_note):
                        raise ValueError('backup exists')
                    link_note.write_text(os.readlink(target), encoding='utf-8')
                    link_note.chmod(0o600)
                    backed.append({'path': current, 'kind': '链接'})
                    target.unlink()
                else:
                    if os.path.lexists(destination):
                        raise ValueError('backup exists')
                    target.rename(destination)
                    backed.append({'path': current, 'kind': item['previous']['kind']})
            staged.replace(target)
            staged = None
            completed.append(current)
    emit('RESULT', {'ok': True, 'completed': completed, 'failed': '',
                    'backup': str(backup) if backed else '', 'backed': backed, 'error': ''})
except Exception as exc:
    if staged is not None and os.path.lexists(staged):
        if staged.is_symlink() or staged.is_file():
            staged.unlink()
        else:
            shutil.rmtree(staged)
    emit('RESULT', {'ok': False, 'completed': completed, 'failed': current,
                    'backup': str(backup) if backed else '', 'backed': backed,
                    'error': '写入停止（' + type(exc).__name__ + '），可能是权限、目录链接或目标变化'})
'''

VALIDATE_CODE = r'''
results = []
home = payload['home']
env = runtime_env(home)
os.umask(0o077)
try:
    real_parents(P(home))
    with tempfile.TemporaryDirectory(prefix='.dispatch-验证-', dir=home) as scratch:
        for name, arguments in (('claude', ['-p', '只回复 OK', '--model', 'haiku']),
                                ('codex', ['exec', '--skip-git-repo-check', '只回复 OK'])):
            executable = shutil.which(name, path=env['PATH'])
            if not executable:
                results.append({'name': name, 'status': '没装', 'code': None, 'output': ''})
                continue
            try:
                code, output = run_process([executable, *arguments], env, scratch, 180)
            except OSError:
                code, output = 126, '无法启动 CLI'
            if code == 0 and 'OK' in output:
                status = '通过'
            elif name == 'codex' and re.search(r'not (?:logged|signed) in|(?:login|authentication) required|please (?:run .*login|log in|sign in)|missing (?:api key|authentication)|401 Unauthorized|未登录|未登入', output, re.I):
                status = '需要用户处理'
            else:
                status = '失败'
            results.append({'name': name, 'status': status, 'code': code,
                            'output': '\n'.join(output.splitlines()[-20:]) if status != '通过' else ''})
    emit('VALIDATION', results)
except Exception:
    emit('ERROR', {'message': '验证环境无法创建'})
    sys.exit(2)
'''


def shell_script(code: str, payload: dict, *, archive: bytes | None = None) -> str:
    header = "LC_ALL=C.UTF-8\nexport LC_ALL\numask 022\npython3 -I - <<'DISPATCH_PY'\n"
    encoded = base64.b64encode(json.dumps(payload, ensure_ascii=True).encode()).decode()
    body = REMOTE_COMMON + '\npayload = json.loads(base64.b64decode(' + repr(encoded) + '))\n'
    if archive is not None:
        body += 'archive_data = ' + repr(base64.b64encode(archive).decode()) + '\n'
    return header + body + code + '\nDISPATCH_PY\n'


class Transport:
    """只通过参数列表启动 sh -s；脚本经标准输入传入，不落主机磁盘。"""
    def __init__(self, executor: dict, runner=None):
        self.executor = executor
        self.runner = runner or subprocess.run

    def run(self, script: str, *, timeout=120) -> str:
        mode = self.executor['transport']
        if mode == 'wsl':
            command = ['wsl.exe', '-d', self.executor['distro'], '--exec', 'sh', '-s']
        elif mode == 'ssh':
            command = ['ssh', '-o', 'BatchMode=yes', self.executor['ssh_host'], 'sh', '-s']
        elif mode == 'local':
            command = ['sh', '-s']
        else:
            raise SyncError('不支持的传输方式')
        try:
            # Windows 的文本 stdin 会生成 CRLF；远端 sh 只接受原样的 UTF-8 字节。
            result = self.runner(command, input=script.encode('utf-8'), capture_output=True,
                                 timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise SyncError('目标命令超时；请检查目标机状态') from None
        except OSError:
            raise SyncError('传输命令无法启动；请检查 wsl.exe、ssh 或 sh') from None
        if result.returncode != 0:
            raise SyncError(f'目标命令失败，退出码 {result.returncode}；SSH 请检查现有免交互配置，目标需 python3；未显示配置输出')
        return result.stdout.decode('utf-8', errors='strict') if isinstance(result.stdout, bytes) else result.stdout

    def probe(self, paths, home=None) -> Probe:
        return parse_probe(self.run(shell_script(PROBE_CODE, {'home': home, 'paths': paths,
                                                              'documents': list(GENERATED)})), expected_paths=paths)

    def apply(self, plan: Plan) -> dict:
        if plan.errors:
            raise SyncError('计划有错误，拒绝写入')
        if not plan.writes:
            return {'ok': True, 'completed': [], 'failed': '', 'backup': '', 'backed': [], 'error': ''}
        payload = {'home': plan.home, 'stamp': time.strftime('%Y%m%d-%H%M%S') + '-' + str(time.time_ns() % 1000000000),
                   'items': [{'path': item.path, 'kind': item.kind, 'link': item.link,
                              'previous': item.previous, 'generated': item.content is not None,
                              'mode': item.entries[0].mode if item.entries else 0o644}
                             for item in plan.writes]}
        return parse_result(self.run(shell_script(WRITE_CODE, payload, archive=make_archive(plan)), timeout=600), 'RESULT')

    def validate(self, plan: Plan):
        return parse_result(self.run(validation_script(plan.home), timeout=390), 'VALIDATION')


def protocol_lines(text):
    records = []
    for line in text.splitlines():
        parts = line.split('\t', 2)
        if len(parts) != 3 or parts[0] != 'DISPATCH1':
            raise SyncError('目标输出包含无法识别的行；未显示原文')
        try:
            value = json.loads(parts[2])
        except (ValueError, TypeError):
            raise SyncError('目标输出不是有效协议 JSON；未显示原文') from None
        records.append((parts[1], value))
    return records


def valid_relative(path):
    return isinstance(path, str) and bool(path) and not path.startswith('/') and '\\' not in path and not any(
        part in ('', '.', '..') for part in path.split('/')) and not any(c in path for c in '\n\r\0')


def parse_probe(text: str, expected_paths=None) -> Probe:
    meta, items, documents, cli = None, {}, {}, {}
    ended = False
    try:
        for kind, value in protocol_lines(text):
            if ended:
                raise ValueError()
            if kind == 'META' and meta is None and not items:
                if not isinstance(value, dict) or set(value) != {'home', 'system', 'codex_home', 'claude_config_dir'}:
                    raise ValueError()
                if any(not isinstance(item, str) for item in value.values()) or not value['home'].startswith('/'):
                    raise ValueError()
                if '..' in PurePosixPath(value['home']).parts or any(c in value['home'] for c in '\n\r\0'):
                    raise ValueError()
                meta = value
            elif kind == 'ITEM' and meta is not None:
                path = value['path']
                if not valid_relative(path) or path in items:
                    raise ValueError()
                file_kind = value['kind']
                required = {'path', 'kind'} | ({'digest'} if file_kind in ('文件', '目录') else {'link'} if file_kind == '链接' else set())
                if file_kind not in ('无', '文件', '目录', '链接') or set(value) != required:
                    raise ValueError()
                if 'digest' in value and not re.fullmatch('[a-f0-9]{64}', value['digest']):
                    raise ValueError()
                if 'link' in value and not isinstance(value['link'], str):
                    raise ValueError()
                items[path] = {key: item for key, item in value.items() if key != 'path'}
            elif kind == 'DOCUMENT' and meta is not None:
                if not isinstance(value, dict) or set(value) != {'path', 'content'}:
                    raise ValueError()
                path = value['path']
                if path not in GENERATED or path in documents:
                    raise ValueError()
                documents[path] = None if value['content'] is None else base64.b64decode(value['content'], validate=True).decode('utf-8-sig')
            elif kind == 'CLI' and meta is not None:
                required = {'name', 'path', 'realpath', 'version', 'method', 'prefix', 'writable', 'arch'}
                if not isinstance(value, dict) or set(value) != required or value['name'] not in ('claude', 'codex') or value['name'] in cli:
                    raise ValueError()
                if any(not isinstance(value[key], str) for key in required - {'writable'}) or type(value['writable']) is not bool:
                    raise ValueError()
                cli[value['name']] = value
            elif kind == 'END' and value == {} and meta is not None:
                ended = True
            else:
                raise ValueError()
        if not ended or meta is None or set(documents) != set(GENERATED):
            raise ValueError()
        if expected_paths is not None and (set(items) != set(expected_paths) or set(cli) != {'claude', 'codex'}):
            raise ValueError()
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise SyncError('目标探测协议无效、不完整或有重复行；未显示原文') from None
    return Probe(**meta, items=items, documents=documents, cli=cli)


def encode_probe_fixture(probe: Probe) -> str:
    records = [('META', {'home': probe.home, 'system': probe.system, 'codex_home': probe.codex_home,
                          'claude_config_dir': probe.claude_config_dir})]
    records.extend(('ITEM', {'path': path, **value}) for path, value in probe.items.items())
    records.extend(('DOCUMENT', {'path': path, 'content': None if probe.documents.get(path) is None else
                                  base64.b64encode(probe.documents[path].encode()).decode()}) for path in GENERATED)
    records.extend(('CLI', value) for value in probe.cli.values())
    records.append(('END', {}))
    return ''.join('DISPATCH1\t' + kind + '\t' + json.dumps(value, ensure_ascii=True, separators=(',', ':')) + '\n'
                   for kind, value in records)


def sanitize_probe(text: str) -> str:
    probe = parse_probe(text)
    for path, text in probe.documents.items():
        if text is None:
            continue
        probe.documents[path] = sanitize_document(text, toml=path.endswith('.toml'))
    return encode_probe_fixture(probe)


def parse_result(text, kind):
    records = protocol_lines(text)
    if len(records) != 1 or records[0][0] != kind:
        raise SyncError('目标操作回执不完整；未显示原文')
    value = records[0][1]
    if kind == 'RESULT':
        if not isinstance(value, dict) or set(value) != {'ok', 'completed', 'failed', 'backup', 'backed', 'error'} or type(value['ok']) is not bool:
            raise SyncError('写入回执格式无效')
        if not isinstance(value['completed'], list) or not all(valid_relative(p) for p in value['completed']):
            raise SyncError('写入回执路径无效')
        if not all(isinstance(value[k], str) for k in ('failed', 'backup', 'error')) or not isinstance(value['backed'], list):
            raise SyncError('写入回执格式无效')
        for item in value['backed']:
            if not isinstance(item, dict) or set(item) != {'path', 'kind'} or not valid_relative(item['path']) or item['kind'] not in ('文件', '目录', '链接'):
                raise SyncError('备份回执无效')
    elif kind == 'VALIDATION':
        if not isinstance(value, list) or len(value) != 2 or {item.get('name') for item in value if isinstance(item, dict)} != {'claude', 'codex'}:
            raise SyncError('验证回执格式无效')
        for item in value:
            if set(item) != {'name', 'status', 'code', 'output'} or item['status'] not in ('通过', '失败', '没装', '需要用户处理') or not isinstance(item['output'], str):
                raise SyncError('验证回执格式无效')
    return value


def validation_script(home: str) -> str:
    return shell_script(VALIDATE_CODE, {'home': home})


def recovery_steps(home: str, backup: str, backed: list[dict]) -> list[str]:
    steps = []
    for item in backed:
        source = backup + '/' + item['path']
        target = home + '/' + item['path']
        if item['kind'] == '链接':
            command = 'ln -s -- "$(cat -- ' + shlex.quote(source + '.链接.txt') + ')" ' + shlex.quote(target)
            steps.append('链接 ' + item['path'] + '：先移开当前目标，再运行 ' + command)
        else:
            steps.append(item['kind'] + ' ' + item['path'] + '：先移开当前目标，再运行 mv -- ' + shlex.quote(source) + ' ' + shlex.quote(target))
    return steps


INSTALL_CODE = r'''
results = []
env = runtime_env(payload['home'])
os.umask(0o077)
try:
    real_parents(P(payload['home']), create=True)
    with tempfile.TemporaryDirectory(prefix='.dispatch-安装-', dir=payload['home']) as scratch:
        os.umask(0o022)
        for item in payload['items']:
            deadline = time.monotonic() + payload['install_timeout']
            def install_process(command):
                return run_process(command, env, payload['home'], max(0.001, deadline - time.monotonic()))
            if item['action'] == '不变':
                results.append({'name': item['name'], 'ok': True, 'code': 0, 'version': item['wanted'], 'output': '版本一致，跳过'})
                continue
            code, output = 0, ''
            if item['method'] == 'npm':
                code, output = install_process(['npm', 'install', '-g', '--prefix', item['prefix'], item['package']])
            elif item['name'] == 'claude':
                installer = str(P(scratch) / 'install.sh')
                code, output = install_process(['curl', '-fsSL', 'https://claude.ai/install.sh', '-o', installer])
                if code == 0:
                    with open(installer, 'rb') as stream:
                        first_line = stream.readline(4096).removeprefix(b'\xef\xbb\xbf').strip()
                    if not re.fullmatch(br'#![^\r\n]*(?:/|\s)(?:ba)?sh(?:\s.*)?', first_line):
                        code, output = 2, '下载到的不是安装脚本，检查目标机的网络或代理'
                    else:
                        code, output = install_process(['bash', installer, item['wanted']])
            else:
                archive = str(P(scratch) / 'codex.tar.gz')
                code, output = install_process(['curl', '-fSL', item['url'], '-o', archive])
                if code == 0:
                    destination = P(item['destination'])
                    real_parents(destination.parent, create=True)
                    staged = destination.with_name(destination.name + '.new-' + str(os.getpid()))
                    try:
                        with tarfile.open(archive, 'r:gz') as package:
                            members = package.getmembers()
                            # 官方发布包通常只有程序；只读取指定成员，不解压其他成员。
                            member = next(m for m in members if m.name == item['asset_binary'] and m.isfile())
                            with package.extractfile(member) as src, staged.open('xb') as dst:
                                shutil.copyfileobj(src, dst)
                        staged.chmod(0o755)
                        staged.replace(destination)
                    finally:
                        if staged.exists():
                            staged.unlink()
            if code == 0:
                command = item['check_path']
                code, output = run_process([command, '--version'], env, scratch, 30)
                match = re.search(r'\b(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)\b', output)
                if code == 0 and (not match or match[1] != item['wanted']):
                    code = 3
                    output += '\n装后版本与指定版本不一致'
                if code == 0 and not shutil.which(item['name'], path=os.environ.get('PATH', '')):
                    output += '\n目标非交互 PATH 还找不到 ' + item['name'] + '；请在目标机将 ' + str(P(command).parent) + ' 加入 PATH 后使用。'
            results.append({'name': item['name'], 'ok': code == 0, 'code': code,
                            'version': item['wanted'] if code == 0 else '', 'output': output})
            if code != 0:
                break
except Exception as exc:
    results.append({'name': item['name'] if 'item' in locals() else '安装', 'ok': False,
                    'code': 2, 'version': '', 'output': '安装停止（' + type(exc).__name__ + '）；需要 sudo 时请自行安装，不会切换安装方法'})
for item in payload['items'][len(results):]:
    results.append({'name': item['name'], 'ok': False, 'code': None, 'version': '',
                    'output': '未尝试：前一项安装失败'})
emit('INSTALL', results)
'''


def version_value(text):
    match = re.search(r'\b(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)\b', text)
    return match[1] if match else None


def host_versions(explicit: dict) -> dict:
    versions = {}
    errors = []
    for name in ('claude', 'codex'):
        value = explicit.get(name)
        if value is None:
            executable = shutil.which(name)
            if executable:
                try:
                    result = subprocess.run([executable, '--version'], capture_output=True, text=True,
                                            encoding='utf-8', errors='replace', timeout=20, check=False)
                    value = version_value(result.stdout) if result.returncode == 0 else None
                except (OSError, subprocess.SubprocessError):
                    value = None
        if not value or not re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][\w.-]+)?', value):
            errors.append(f'主机 {name} 版本查不到或无效，请用 --{name}版本 指定')
        else:
            versions[name] = value
    if errors:
        raise SyncError('\n'.join(errors))
    return versions


def plan_install(probe: Probe, versions: dict, *, override_home=False) -> list[dict]:
    if not versions or versions.keys() - {'claude', 'codex'}:
        raise SyncError('安装版本清单必须包含 claude 或 codex')
    plans = []
    for name in ('claude', 'codex'):
        if name not in versions:
            continue
        old = dict(probe.cli.get(name, {}))
        ignored = override_home and any(old.get(key) and not PurePosixPath(old[key]).is_relative_to(
            PurePosixPath(probe.home)) for key in ('path', 'realpath', 'prefix'))
        if ignored:
            old.update(method='missing', version='', path='', realpath='', prefix='', writable=False)
        wanted = versions[name]
        method = old.get('method', 'missing')
        item = {'name': name, 'wanted': wanted, 'current': old.get('version') or '没装或版本未知',
                'notice': '忽略目标家目录外的既有安装，将在指定目标家目录内安装' if ignored else '',
                'action': '不变' if old.get('version') == wanted else '安装', 'method': method,
                'command': '', 'prefix': '', 'package': '', 'url': '', 'asset_binary': '',
                'destination': '', 'check_path': old.get('path') or probe.home + '/.local/bin/' + name}
        if item['action'] == '不变':
            plans.append(item)
            continue
        if method == 'missing':
            method = 'native' if name == 'claude' else 'binary'
            item['method'] = method
        elif method not in ('native', 'npm', 'binary'):
            raise SyncError(f'{name}：无法确认原安装方式，请先自行处理原安装位置')
        if method == 'npm':
            prefix = old['prefix']
            package = ('@anthropic-ai/claude-code' if name == 'claude' else '@openai/codex') + '@' + wanted
            item.update(prefix=prefix, package=package, command=shlex.join(['npm', 'install', '-g', '--prefix', prefix, package]))
        elif name == 'claude' and method == 'native':
            item['command'] = 'curl -fsSL https://claude.ai/install.sh -o <临时目录>/install.sh；bash <临时目录>/install.sh ' + shlex.quote(wanted)
        elif name == 'codex' and method == 'binary':
            target_arch = {'x86_64': 'x86_64', 'aarch64': 'aarch64', 'arm64': 'aarch64'}.get(old.get('arch'))
            if not target_arch:
                raise SyncError('codex：不支持目标 CPU 架构')
            binary = f'codex-{target_arch}-unknown-linux-musl'
            url = f'https://github.com/openai/codex/releases/download/rust-v{wanted}/{binary}.tar.gz'
            destination = old.get('realpath') or probe.home + '/.local/bin/codex'
            item.update(url=url, asset_binary=binary, destination=destination,
                        command=shlex.join(['curl', '-fSL', url, '-o', '<临时目录>/codex.tar.gz']) + '；提取 ' + binary + ' 到 ' + destination)
        else:
            raise SyncError(f'{name}：不支持原安装方式')
        if old.get('method') != 'missing' and not old.get('writable'):
            raise SyncError(f'{name}：原安装目录不可写，需要 sudo；请用户自行运行：' + item['command'])
        plans.append(item)
    return plans


def print_plan(plan: Plan):
    # 这里只打印计划元数据；已知 MCP 标识不当成配置值扫描，验证原文仍完整脱敏。
    identifiers = {chr(0xF0000 + index): name for index, name in enumerate(
        sorted(plan.mcp_names, key=len, reverse=True)) if name}
    def output(text):
        for marker, name in identifiers.items():
            text = text.replace(name, marker)
        text = redact(text, plan.secrets)
        for marker, name in identifiers.items():
            text = text.replace(marker, name)
        print(text)
    for item in plan.items:
        keys = '；变化的键：' + '、'.join(item.changed_keys) if item.changed_keys else ''
        output(f'{item.action}：~/{item.path}（{item.reason}）{keys}')
    for notice in plan.notices:
        output(notice)
    for error in plan.errors:
        output('错误：' + error)
    counts = {action: sum(item.action == action for item in plan.items) for action in ('新建', '替换', '不变', '跳过')}
    counts['跳过'] += sum(notice.startswith(('跳过：', 'MCP 跳过：')) for notice in plan.notices)
    output('汇总：' + '，'.join(f'{key} {value}' for key, value in counts.items()))


def synchronize(host: Host, executor: dict, transport, *, target_home=None, execute=False,
                gateway_key='', stdin_key='', validate=True) -> int:
    paths = [source.path for source in host.sources] + list(GENERATED)
    probe = transport.probe(paths, target_home)
    print(f'目标家目录：{probe.home}；系统：{probe.system}')
    print('目标非交互环境：CODEX_HOME=' + (probe.codex_home or '未设置') +
          '；CLAUDE_CONFIG_DIR=' + (probe.claude_config_dir or '未设置'))
    plan = build_plan(host, probe, executor, gateway_key=gateway_key, stdin_key=stdin_key,
                      override_home=target_home is not None)
    print_plan(plan)
    if plan.errors:
        print('计划生成失败，未写入任何项。')
        return 2
    if not execute:
        print('加 --执行 才会写。')
        return 0
    result = transport.apply(plan)
    print(redact('已完成的项：' + ('、'.join(result['completed']) or '无'), plan.secrets))
    print('备份位置：' + (result['backup'] or '无（没有替换项）'))
    if not result['ok']:
        print(redact('失败项：' + (result['failed'] or '写前检查') + '；' + result['error'], plan.secrets))
        print('手动恢复办法（不会自动回滚）：')
        for step in recovery_steps(plan.home, result['backup'], result['backed']):
            print(redact(step, plan.secrets))
        if not result['backed']:
            print('没有已备份的替换项；已完成的新建项可按清单手动移开。')
        return 2
    print('配置同步完成。')
    if validate:
        for item in transport.validate(plan):
            print(f"验证 {item['name']}：{item['status']}；退出码 {item['code'] if item['code'] is not None else '未运行'}")
            if item['output']:
                print(redact(item['output'], plan.secrets))
            if item['name'] == 'codex' and item['status'] == '需要用户处理':
                print('请在目标机上运行 codex login。')
    return 0


def install_cli(probe: Probe, versions: dict, transport: Transport, *, execute=False, override_home=False) -> int:
    # 安装程序的失败输出也可能包含已有目标凭据；只在内存收集用于脱敏。
    secrets = [value for key, value in os.environ.items() if SECRET_KEY.search(key) and value]
    for path, text in probe.documents.items():
        if text is None:
            continue
        try:
            data = tomllib.loads(text) if path.endswith('.toml') else json.loads(text)
            secrets.extend(secret_values(data))
        except (ValueError, TypeError):
            pass
    plans = plan_install(probe, versions, override_home=override_home)
    for item in plans:
        if item['notice']:
            print(item['name'] + '：' + item['notice'])
        print(f"{item['name']}：现有 {item['current']}，指定 {item['wanted']}，安装方式 {item['method']}")
        print('版本一致，跳过' if item['action'] == '不变' else '将运行：' + item['command'])
    if not execute:
        print('加 --执行 才会写。')
        return 0
    if all(item['action'] == '不变' for item in plans):
        return 0
    if any(item['name'] == 'claude' and item['action'] != '不变' for item in plans):
        print('Claude Code 安装包约 350MB，每项最多等 30 分钟', flush=True)
    text = transport.run(shell_script(INSTALL_CODE, {'home': probe.home, 'items': plans,
                            'install_timeout': INSTALL_TIMEOUT}), timeout=(INSTALL_TIMEOUT + 60) * len(plans))
    records = protocol_lines(text)
    if len(records) != 1 or records[0][0] != 'INSTALL' or not isinstance(records[0][1], list):
        raise SyncError('安装回执无效；未显示原文')
    results = records[0][1]
    for item in results:
        if not isinstance(item, dict) or set(item) != {'name', 'ok', 'code', 'version', 'output'} or type(item['ok']) is not bool:
            raise SyncError('安装回执无效')
        status = '未尝试' if item['code'] is None else ('成功' if item['ok'] else '失败')
        code = item['code'] if item['code'] is not None else '未运行'
        print(f"安装 {item['name']}：{status}；退出码 {code}；版本 {item['version'] or '未确认'}")
        if item['output']:
            print(redact(item['output'], secrets))
        if item['code'] == 124:
            print(f"{item['name']} 安装超时，已等待 {INSTALL_TIMEOUT} 秒；建议检查目标机网络或代理，或先在目标机上自己装好再重跑。")
    return 0 if len(results) == len(plans) and all(item['ok'] for item in results) else 2


def local_main(source_home: str) -> int:
    host = collect_host(Path(source_home), local=True)
    return synchronize(host, {'transport': 'local', 'platform': 'linux'}, Transport({'transport': 'local'}),
                       execute=True, validate=False)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('名称', nargs='?')
    parser.add_argument('--列出', action='store_true')
    parser.add_argument('--执行', action='store_true')
    parser.add_argument('--装CLI', action='store_true', help='安装模式：只安装软件，不同步配置；加 --执行 真正安装')
    parser.add_argument('--claude版本')
    parser.add_argument('--codex版本')
    parser.add_argument('--密钥从标准输入', action='store_true')
    parser.add_argument('--执行机清单', type=Path)
    parser.add_argument('--目标家目录')
    args = parser.parse_args(argv)
    try:
        personal = config_root(Path(__file__).resolve().parents[1])
        path = args.执行机清单 or personal / '执行机.toml'
        try:
            executors = parse_executors(path.read_text(encoding='utf-8-sig'))
        except (OSError, UnicodeError):
            raise SyncError('执行机清单无法读取；请在个人配置目录准备 执行机.toml，或用 --执行机清单 指定') from None
        if args.列出:
            for name, executor in executors.items():
                print(f"{name}：{executor['transport']}，{executor['platform']}")
            return 0
        if not args.名称 or args.名称 not in executors:
            raise SyncError('请指定清单里的执行机名称；用 --列出 查看')
        if args.目标家目录 and (not args.目标家目录.startswith('/') or '..' in PurePosixPath(args.目标家目录).parts or any(c in args.目标家目录 for c in '\n\r\0')):
            raise SyncError('--目标家目录 必须为 Linux 绝对路径，且不能包含上级跳转')
        executor = executors[args.名称]
        transport = Transport(executor)
        if args.装CLI:
            versions = host_versions({'claude': args.claude版本, 'codex': args.codex版本})
            probe = transport.probe(list(GENERATED), args.目标家目录)
            safety = build_plan(Host(Path.home(), Path.home() / '.codex', [], {}), probe,
                                executor, override_home=args.目标家目录 is not None)
            if safety.errors:
                raise SyncError('\n'.join(safety.errors))
            result = install_cli(probe, versions, transport, execute=args.执行,
                                 override_home=args.目标家目录 is not None)
            if result == 0 and args.执行:
                command = ['python', '同步执行机.py', args.名称, '--执行']
                if args.执行机清单:
                    command.extend(['--执行机清单', str(args.执行机清单)])
                if args.目标家目录:
                    command.extend(['--目标家目录', args.目标家目录])
                print('下一步：' + shlex.join(command))
            return result
        stdin_key = sys.stdin.readline().rstrip('\r\n') if args.密钥从标准输入 else ''
        host = collect_host(Path.home(), personal=personal)
        return synchronize(host, executor, transport, target_home=args.目标家目录, execute=args.执行,
                           gateway_key=os.environ.get('DISPATCH_GATEWAY_KEY', ''), stdin_key=stdin_key)
    except SyncError as exc:
        # SyncError 的构造点只接受键名、路径和固定说明；外部输出另走脱敏。
        print('错误：' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(run_cli(main))
