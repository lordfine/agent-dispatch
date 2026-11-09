# 全局 CLAUDE.md 片段：多模型派活

把下面这段追加到你的 `~/.claude/CLAUDE.md`。初始化引导（`/dispatch-retro`）会给你看这段并问要不要加，也可以自己贴。它和 `skills/`、`agents/` 配套使用；细节都在 `dispatch` 手册里，这里只放原则和指针，所以以后升级框架一般不用改这段。

```markdown
## 多模型派活

- 主脑负责拆解、决策、验收和沟通；探索、调研、审查、诊断、抽取、成文和实现按任务派出，优先使用具名角色：`explorer` 探索 / `researcher` 尽调 / `extractor` 转录抽取 / `reviewer` 审查 / `diagnoser` 诊断 / `writer` 文档 / `verifier` 复核（贵，少用）。调用具名 agent 不传 `model`。
- 不用内置 Explore、Plan 子代理（会跟着主会话的模型跑）。`general-purpose` 必须用时传 `model="sonnet"` 或 `"fable"`；Opus 主会话里不传 `"opus"`。
- 实现交给 Orca worker，或让我开实现会话运行 `/implement`、`/tdd`；按风险和难度选择流程与推理档位。
- 派活、验收和其他技能的角色映射见 `dispatch` skill；本机条件、在用组合和预算见个人配置 `~/.claude/派活配置/本机条件.md`。
- 批次结束用 `dispatch-feedback` 写反馈，复盘和调整个人配置用 `dispatch-retro`。
- 切组合、改 magpie 路由先跟我说清楚，等我同意。

## git

- `/implement`、`implement-spec` 这类流程可以在功能分支上本地 commit；不直接提交到 main/master；push、合并进主分支等我同意。Orca worker 停在“改完未提交”。
```
