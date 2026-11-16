#!/usr/bin/env python3
"""旧 WSL 入口：保留一跑就写的行为，不新增模型验证调用。

用法：python3 同步配置到WSL.py [Windows家目录]
不传参数时用 cmd.exe 和 wslpath 推断 Windows 家目录。
"""
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '脚本'))
from 同步执行机 import SyncError, local_main, run_cli


def 推断Windows家目录():
    """先向 Windows 询问家目录，再转换为 WSL 路径。"""
    try:
        result = subprocess.run(['cmd.exe', '/c', 'echo', '%USERPROFILE%'],
                                capture_output=True, text=True, timeout=10, check=False)
        windows = result.stdout.strip()
        if result.returncode != 0 or not windows or '%USERPROFILE%' in windows:
            return None
        result = subprocess.run(['wslpath', '-u', windows], capture_output=True,
                                text=True, timeout=10, check=False)
        return result.stdout.strip() or None if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def main(argv=None):
    print('以后可以在主机上运行 同步执行机.py <名称>，默认先预览')
    argv = sys.argv[1:] if argv is None else argv
    source = argv[0] if argv else 推断Windows家目录()
    if not source:
        print('推断不出 Windows 家目录，请把路径作为第一个参数传进来，'
              '例如：python3 同步配置到WSL.py /mnt/c/Users/<用户名>', file=sys.stderr)
        return 2
    if not (Path(source) / '.claude').is_dir():
        print('找不到 Windows 的 .claude 目录：' + str(Path(source) / '.claude'), file=sys.stderr)
        return 2
    try:
        return local_main(source)
    except SyncError as exc:
        print('错误：' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(run_cli(main))
