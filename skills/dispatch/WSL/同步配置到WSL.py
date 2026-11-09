#!/usr/bin/env python3
"""把 Windows 上的 Claude Code / Codex 配置同步到 WSL。

在 WSL 里运行：python3 /mnt/c/Users/<用户名>/.claude/skills/dispatch/WSL/同步配置到WSL.py [Windows家目录]
不传参数时自动用 cmd.exe 读 %USERPROFILE%，再经 wslpath 转成 /mnt/c/...；
推断不出来就停下来，要求把 Windows 家目录作为第一个参数传进来。

做三件事：
1. 能共享的直接建符号链接，指向 Windows 那份（改一处两边生效）：
   ~/.claude/CLAUDE.md、~/.claude/agents、~/.claude/skills、~/.claude/派活配置、
   ~/.codex/AGENTS.md、~/.codex/auth.json、~/.codex/magpie-desktop-models.json、~/.agents/skills
2. 含 Windows 专用内容的，从 Windows 版生成 WSL 版：
   ~/.claude/settings.json：照抄 env（模型路由、令牌、档位）和通用设置，去掉 hooks、状态栏、插件。
   ~/.codex/config.toml：只取模型、推理档位、fast、magpie 地址、模型清单几项。
3. MCP：把能在 Linux 下跑的合并进 ~/.claude.json 的 mcpServers。
   HTTP 类型原样带上；Windows 的 .exe 改成 /mnt/c/... 路径经互通调用；uvx/npx 类原样带上；
   其余（Windows 专用的 python.exe、cmd /c、本机脚本）跳过并列出名字。

只写 WSL 这边的文件；不读出、不打印任何令牌。已有的普通文件先改名备份再替换。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib


def 推断Windows家目录():
    """在 WSL 里问 Windows 要家目录，再转成 /mnt/<盘符>/...；任何一步失败都返回 None。"""
    try:
        r = subprocess.run(
            ["cmd.exe", "/c", "echo", "%USERPROFILE%"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return None
        win = r.stdout.strip()
        if not win or "%USERPROFILE%" in win:
            return None
        p = subprocess.run(
            ["wslpath", "-u", win], capture_output=True, text=True, timeout=10,
        )
        if p.returncode != 0:
            return None
        return p.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


if len(sys.argv) > 1:
    WIN = sys.argv[1]
else:
    WIN = 推断Windows家目录()
    if not WIN:
        sys.exit("推断不出 Windows 家目录，请把路径作为第一个参数传进来，"
                 "例如：python3 同步配置到WSL.py /mnt/c/Users/<用户名>")

HOME = os.path.expanduser("~")
STAMP = time.strftime("%Y%m%d-%H%M%S")

LINKS = [
    (".claude/CLAUDE.md", ".claude/CLAUDE.md"),
    (".claude/agents", ".claude/agents"),
    (".claude/skills", ".claude/skills"),
    (".claude/派活配置", ".claude/派活配置"),
    (".codex/AGENTS.md", ".codex/AGENTS.md"),
    (".codex/auth.json", ".codex/auth.json"),
    (".codex/magpie-desktop-models.json", ".codex/magpie-desktop-models.json"),
    (".agents/skills", ".agents/skills"),
]

SETTINGS_KEEP = [
    "env", "attribution", "model", "modelPicker", "language", "promptCacheTtl",
    "subagentPromptCacheTtl", "effortLevel", "modelSettings", "tui",
    "skipDangerousModePermissionPrompt", "autoMode",
]

CODEX_KEEP = [
    "model", "model_reasoning_effort", "service_tier", "openai_base_url",
    "model_catalog_json", "model_reasoning_summary",
]


def 备份(path):
    if os.path.lexists(path) and not os.path.islink(path):
        bak = f"{path}.bak-{STAMP}"
        shutil.move(path, bak)
        print(f"  已备份原文件：{bak}")


def 建链接(src_rel, dst_rel):
    src = os.path.join(WIN, src_rel)
    dst = os.path.join(HOME, dst_rel)
    if not os.path.exists(src):
        print(f"  跳过（Windows 上没有）：{src_rel}")
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.islink(dst):
        if os.readlink(dst) == src:
            print(f"  已是链接：~/{dst_rel}")
            return
        os.unlink(dst)
    备份(dst)
    os.symlink(src, dst)
    print(f"  链接：~/{dst_rel} -> {src}")


def 写文件(path, text, mode=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    备份(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def 读json(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def 转WSL路径(p):
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", p)
    if not m:
        return None
    return f"/mnt/{m.group(1).lower()}/" + m.group(2).replace("\\", "/")


def 同步settings():
    win = 读json(os.path.join(WIN, ".claude/settings.json"))
    out = {k: win[k] for k in SETTINGS_KEEP if k in win}
    perm = win.get("permissions") or {}
    if "defaultMode" in perm:
        out["permissions"] = {"defaultMode": perm["defaultMode"]}
    dst = os.path.join(HOME, ".claude/settings.json")
    if os.path.islink(dst):
        os.unlink(dst)
    写文件(dst, json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    print(f"  生成 ~/.claude/settings.json（带了：{', '.join(out.keys())}；"
          f"没带：{', '.join(k for k in win if k not in out)}）")


def 同步codex():
    with open(os.path.join(WIN, ".codex/config.toml"), "rb") as f:
        win = tomllib.load(f)
    lines = ["# 由 同步配置到WSL.py 从 Windows 版生成；要改请改 Windows 那份再重跑脚本"]
    for k in CODEX_KEEP:
        if k in win:
            lines.append(f"{k} = {json.dumps(win[k], ensure_ascii=False)}")
    写文件(os.path.join(HOME, ".codex/config.toml"), "\n".join(lines) + "\n")
    print(f"  生成 ~/.codex/config.toml（{', '.join(k for k in CODEX_KEEP if k in win)}）")


def 同步mcp():
    win = 读json(os.path.join(WIN, ".claude.json"))
    servers = win.get("mcpServers") or {}
    keep, skip = {}, []
    for name, cfg in servers.items():
        cfg = dict(cfg)
        typ = cfg.get("type", "stdio")
        if typ in ("http", "sse"):
            keep[name] = cfg
            continue
        cmd = cfg.get("command", "")
        args = [str(a) for a in cfg.get("args", [])]
        win_path_args = any(re.match(r"^[A-Za-z]:[\\/]", a) for a in args)
        if cmd.lower().endswith(".exe") and not win_path_args:
            wsl = 转WSL路径(cmd)
            if wsl and os.path.exists(wsl):
                cfg["command"] = wsl
                keep[name] = cfg
                continue
        if cmd in ("uvx", "npx", "uv") and not win_path_args:
            keep[name] = cfg
            continue
        skip.append(name)
    path = os.path.join(HOME, ".claude.json")
    cur = 读json(path) if os.path.exists(path) else {}
    cur["mcpServers"] = keep
    # 首次运行提示的"已回答/已看过"标记：不带的话，Orca 起的 worker 会卡在这些提示上（agent_prompt_blocked）
    for k, v in win.items():
        if isinstance(v, (bool, int, str)) and re.search(
            r"^(hasSeen|hasCompleted|hasReset|hasAcknowledged|lastOnboardingVersion$|lastReleaseNotesSeen$|migrationVersion$)"
            r"|MigrationComplete$|Acknowledged(At)?$|^officialMarketplaceAutoInstall", k):
            cur[k] = v
    # Orca 在 WSL 里用终端模式起 claude，会弹"要不要把 auto 设为默认权限模式"；Windows 走结构化会话从不弹，所以没有这个键。
    # 在一台测试机上，2026-11-06 实测：不设它，worker 启动报 agent_prompt_blocked。这里按 Windows 的选择（保持手动模式）记为已回答。
    cur["hasSeenAutoDefaultNudge"] = True
    写文件(path, json.dumps(cur, ensure_ascii=False, indent=2) + "\n")
    print(f"  MCP 带上：{', '.join(keep) or '无'}")
    print(f"  MCP 跳过（Windows 专用）：{', '.join(skip) or '无'}")


def main():
    if not os.path.isdir(os.path.join(WIN, ".claude")):
        sys.exit(f"找不到 Windows 的 .claude 目录：{WIN}/.claude")
    print(f"Windows 家目录：{WIN}")
    print("一、共享链接")
    for s, d in LINKS:
        建链接(s, d)
    print("二、生成配置")
    同步settings()
    同步codex()
    print("三、MCP")
    同步mcp()
    print("完成。Windows 那边改了 settings.json、Codex 配置或 MCP 后重跑本脚本。")


if __name__ == "__main__":
    main()
