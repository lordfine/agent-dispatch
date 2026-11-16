# 全局 AGENTS.md 片段：多模型派活（Codex 版）

把下面这段追加到你的 `~/.codex/AGENTS.md`（设了 `CODEX_HOME` 的，是 `$CODEX_HOME/AGENTS.md`）。初始化引导（`$dispatch-retro`）会给你看这段并问要不要加，也可以自己贴。它和安装器装的技能、角色配套使用；细节都在 `dispatch` 手册里，这里只放原则和指针，以后升级框架一般不用改这段。用 Claude Code 的看 `CLAUDE.md片段.md`。

```markdown
## 多模型派活

- 主脑负责拆解、决策、验收和沟通；探索、调研、审查、诊断、抽取、成文和实现按任务派出，优先派具名角色：`explorer` 探索 / `researcher` 尽调 / `extractor` 转录抽取 / `reviewer` 审查 / `diagnoser` 诊断 / `writer` 文档 / `verifier` 复核（贵，少用）。
- 派子代理时 `agent_type` 写角色名，`model` 和 `reasoning_effort` 照 dispatch 技能里 `脚本/生效配置.py` 列出的值传；不派不带角色的通用子代理。
- 只读角色没有工具隔离：派之前和交回之后各看一次 `git status --short`，多出改动就停下告诉我。
- 实现交给 Orca worker；没装 Orca 时主会话自己写。按风险和难度选择推理档位。
- 派活、验收和宿主差异见 `dispatch` 技能；个人配置在 `~/.dispatch/`。
- 批次结束用 `$dispatch-feedback` 写反馈，复盘和调整个人配置用 `$dispatch-retro`。

## git

- 不直接提交到 main/master；push、合并进主分支等我同意。Orca worker 停在“改完未提交”。
```
