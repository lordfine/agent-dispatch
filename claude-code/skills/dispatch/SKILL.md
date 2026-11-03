---
name: dispatch
description: 派活手册（Opus 主脑模式）。主会话是 Opus 5.5、要把摸底、调研、审查、诊断、抽取、写文档、实现等工作派给便宜模型时使用：派给谁、走原生子代理还是 Orca worker、任务说明怎么写、怎么验收、报错怎么改派。说「派活」「派出去」「开 worker」「让 GLM/MiniMax/DeepSeek 去做」时触发。
---

# 派活手册

主脑（Opus 5.5）只做四件事：**拆解、决策、验收、和用户沟通**。其余能派的都派出去。少量文件的小改动、看几张截图，主脑自己做反而更省。

## 1. 派给谁

### 原生子代理（只读或短活，结果直接回到主会话）

| agent | 干什么 | 模型 |
|---|---|---|
| `explorer` 探索员 | 摸代码、找定义和调用链、恢复项目现状 | MiniMax · medium |
| `researcher` 尽调员 | 网上调研、选型对比、核实说法 | glm-5.3-flash |
| `extractor` 转录抽取员 | 日志/周报/表格/批量截图的逐字转录和抽取 | MiniMax · medium |
| `reviewer` 审查员 | 代码或方案审查；安全轴、正确性轴可分两次派 | glm-5.3-flash |
| `diagnoser` 诊断员 | 线上/本机/服务器排障取证，只读，给假设+验证命令+回滚 | glm-5.3-flash |
| `writer` 文档员 | 把定好的内容写成交接文档、进度文档、员工物料、JD 初稿 | deepseek-flash |
| `verifier` 复核员 | 关键决策复核、高风险最终验收、冲突裁决（少用） | Opus 5.5 |

### Orca worker（要改代码、耗时长、要并行、要别的 agent）

| 角色 | 模型 | 用途 |
|---|---|---|
| 实现者 | `group/glm-fast` | 需求明确、范围清楚的实现 |
| 高级实现者 | `group/glm-main` | 复杂、跨模块、需要判断的实现 |
| （手动） | `<来源>/claude-sonnet-5-5` | **仅用户点名时**；用得少，没建组，直接写成员 |
| 守望者 | `group/minimax` | 盯外部 agent 会话，只发"继续"，非上游原因的阻塞只报告不介入 |

## 2. 硬规则

- 调用具名 agent **不传 `model`**，让 frontmatter 生效。
- **不用内置 Explore / Plan**：它们的模型由 Claude Code 按环境决定（官方文档：经 `ANTHROPIC_BASE_URL` 网关时 Explore 跑在 `opus` 别名指向的模型上，部分云平台上跟随主会话），可能落到 Opus，也不受 agent 文件控制；具名 explorer 更便宜、更可控。
- `general-purpose` 不得已要用时传 `model="sonnet"`（glm-5.3）或 `"fable"`（glm-5.3-flash）；Opus 主会话下 `"opus"` 就是 Opus 本身。
- MiniMax 只派结果能校验的活（有原文可对照、有路径可核），不派"判断外面有没有这条信息"的活——它不知道时爱编。
- 实现类用户不信任 MiniMax，不要派给它。

## 3. 报错改派

- **智谱内容审核 1301**（"系统检测到输入或生成内容可能包含不安全或敏感内容"）：GLM 系列会被拦。同一个 agent 重派一次，传 `model="haiku"`（MiniMax）；如果是长任务或对准确性要求高，改用 Orca worker 指定 `--model group/ds-flash`。涉及代理、封号、风控的任务，一开始就这样派。
- **503 No available accounts / 429**：号池满或限流，等一会儿重派；连续失败就降一档（glm-5.3 → glm-5.3-flash）。
- 其他报错先看报错原文再决定，不盲目重试。

## 4. Orca worker

先跑一次 `orca skills get orchestration` 读版本匹配的指南。

```text
# 单任务，用当前工作区
orca orchestration worker-start --spec-file <任务说明.md> --task-title "<标题>" --worktree current --agent claude --model group/glm-fast

# 并行改代码：每个 worker 独立 worktree（仅 git 仓库），并行上限 3
orca orchestration worker-start --spec-file <说明.md> --worktree new-child --name <短名> --agent claude --model group/glm-main

# Codex 当 worker（模型 id 写法尚未验证，验证后再用）
orca orchestration worker-start --spec-file <说明.md> --worktree current --agent codex --model <待验证>
```

启动后**结束本轮**，Orca 会唤醒你；按唤醒提示跑 `check`，用 `send` 追加指令，不轮询。
非 git 的文件夹工作区用 CLI 登记不进 Orca，只能在当前工作区起 worker，在说明里写清目标目录。

## 5. 任务说明（spec）怎么写

每份说明自包含，worker 不需要回来问就能干：
1. **目标**：一句话。
2. **基线**：基准提交哈希（如有）、规格文件路径、必读文件清单（项目 CLAUDE.md / AGENTS.md 必列）。
3. **范围**：允许改哪些文件/目录；禁止碰什么（别人的改动、配置、线上）。
4. **已知现状**：主脑已经跑过的结果（比如"当前 3 个测试红"）。
5. **完成判据**：要跑的测试命令及预期。
6. **失败口径**：非零退出算失败，还是收集失败信息本身就是交付物。

实现者固定附加的规则：
- 先读规格和 AGENTS.md，不回退别人的改动。
- Bash 每次只跑一条直接命令，不加 `cd`、不用 `&&` 和管道。
- 测试先定向、后全量；真实捕获退出码，不能凭输出末尾判断通过。
- 停在"改完未提交"：不 commit、不 push、不自行回传最终结论。

## 6. 验收

实现者交付必须带**凭证**：改动范围（`git diff --stat`）、测试命令与通过数、实际使用的模型、未解决的风险。
- 主脑按凭证核对，只对缺口或矛盾复查，不重跑全部。
- **部分通过不算完成**。不通过就用 `orca orchestration send` 把修正意见发回原 worker，不自己动手改。
- Orca worker 的 commit，以及所有 push、合并进主分支、发版、对外发送，由主脑在用户同意后做（mattpocock 流程的本地 commit 例外见第 9 节）。
- 每次验收在回复里记一笔：是否一次通过、返工几次（以后可以用 hook 自动写入项目的 `.claude/派活记录.jsonl`）。

## 7. 核对模型落点

```text
magpie usage --csv --last 1h   # 按 session × requested_model 看，确认没有意外落到 Opus
```

模型一律写 magpie 路由组名，不写供应商前缀：`group/glm-main`（主力 GLM）、`group/glm-fast`（快速 GLM）、`group/minimax`、`group/ds-flash`、`group/opus`。组名不带版本号，换来源或升级模型只改组成员，例如 `magpie group set glm-main models=<来源>/glm-5.5`，agent、手册、Orca 命令都不用动。Opus 也走组：`group/opus`。`magpie groups` 看每个组现在指向谁。

## 8. 预算

Claude 系（Opus + Sonnet + Haiku）月实付设一个上限（按你的计费方式折算 magpie 标价）。Claude 系 : 其他模型套餐等值 ≈ 1:2，只作监控。

## 9. 和 mattpocock 技能配合

这些技能里写的是"派一个子代理"，没指定类型；Opus 主会话下直接派会落到 Opus。一律换成：

- `code-review` 的 Standards / Spec 两个子 agent → 都派 `reviewer`（一次前台并行发两个）。
- `research` 技能、`wayfinder` 的调研票 → `researcher`。
- `improve-codebase-architecture` 的走查、`implement-spec` 的探索子代理 → `explorer`。
- `implement-spec` 的实现者 / 合并者 → `general-purpose`，传 `model="fable"`（glm-5.3-flash），复杂票传 `"sonnet"`（glm-5.3），`isolation` 用 `worktree`。
- `chief-of-staff` 里的子代理 → 按第 1 节选具名 agent。
- `claude-handoff` 的后台 agent → `general-purpose`，传 `model="sonnet"`。
- `/implement`、`/tdd`、`/prototype` 不在 Opus 主会话里跑：建议用户开主模型为 glm-5.3 的实现会话（`claude --model group/glm-main`，或新会话里 `/model sonnet`），每张票之间 `/clear`；要并行就拆成 Orca worker。

分工：Opus 会话跑对话与决策类（`/grill-with-docs`、`/grilling`、`/domain-modeling`、`/to-spec`、`/to-tickets`、`/codebase-design`、`/wayfinder`、`/zoom-out`、`/to-questionnaire`、`/handoff`、`/retro`）和 `/code-review` 的结论验收；实现会话跑 `/implement`、`/tdd`、常规 `/diagnosing-bugs`。

git：`/implement`、`implement-spec` 可在功能分支上本地 commit（`code-review` 要拿提交点比对改动）；不直接提交 main/master；push、合并进主分支等用户同意。Orca worker 仍按第 5 节停在"改完未提交"。
