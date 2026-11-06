# 兼容旧用法：转调同目录的 切换组合.py（Windows 和 WSL 通用的正式版本）。
# 用法：pwsh -File 切换组合.ps1 Codex
param([Parameter(Mandatory = $true)][string]$组合名)
& python (Join-Path $PSScriptRoot '切换组合.py') $组合名
exit $LASTEXITCODE
