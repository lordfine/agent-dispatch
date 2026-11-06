# 全局 CLAUDE.md 片段：多模型派活

把下面这段贴进你的 `~/.claude/CLAUDE.md`。它和同目录下的 `agents/`、`skills/dispatch/` 配套使用。

```markdown
## 多模型派活（主会话为 Opus 5.5 时）

- 主脑只做拆解、决策、验收、和我沟通；摸底、调研、审查、诊断、抽取、写文档、实现一律派出去，细节见 `dispatch` skill。
- 优先用具名 agent：`explorer` 探索 / `researcher` 尽调 / `extractor` 转录抽取 / `reviewer` 审查 / `diagnoser` 诊断 / `writer` 文档 / `verifier` 复核（贵，少用）。调用时**不传 `model`**。每个 agent 落在哪个模型由当前"组合"决定（示例里有国产和 Codex 两套），切组合会改 magpie，先跟我说；细节见 `dispatch` skill 第 10 节。
- 不用内置 Explore、Plan 子代理（会跑在 Opus 上）。`general-purpose` 必须用时传 `model="sonnet"` 或 `"fable"`，不传 `"opus"`。
- 实现类不在 Opus 会话里亲自写：走 Orca worker（常规 `group/impl`，复杂 `group/impl-strong`，推理档位由主脑按难度定；Codex 原生 worker 的规则写进每份任务说明），或让我开一个主模型指向 `group/glm-main` 的实现会话跑 `/implement`、`/tdd`。
- 技能（如 mattpocock 那套）里要派子代理时，同样映射到具名 agent，不让它默认跟着 Opus 跑；映射表见 `dispatch` skill 第 9 节。
- 子代理报智谱内容审核 1301 时，同一个 agent 重派并传 `model="haiku"`，或改用 Orca worker 指定 `group/ds-flash`。
- 派活后用 `magpie usage --csv --last 1h` 核对实际模型落点。Claude 系模型设一个月度实付上限，具体数额按自己的计费方式定。

## git

- `/implement`、`implement-spec` 这类流程可以在功能分支上本地 commit；不直接提交到 main/master；push、合并进主分支等我同意。Orca worker 仍停在"改完未提交"。
```
