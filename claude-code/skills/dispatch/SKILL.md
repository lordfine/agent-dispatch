---
name: dispatch
description: 派活手册（Opus 主脑模式）。主会话是 Opus 5.5、要把摸底、调研、审查、诊断、抽取、写文档、实现等工作派给便宜模型时使用：派给谁、走原生子代理还是 Orca worker、任务说明怎么写、怎么验收、报错怎么改派。说「派活」「派出去」「开 worker」「让 GLM/MiniMax/DeepSeek/Codex 去做」「切组合」时触发。
---

# 派活手册

主脑（Opus 5.5）只做四件事：拆解、决策、验收、和用户沟通。其余能派的都派出去。少量文件的小改动、看几张截图，主脑自己做反而更省。

## 1. 派给谁

每个角色对应一个 magpie 角色路由组，组里现在是谁由"组合"决定（见第 10 节）。`cat ~/.claude/skills/dispatch/组合/当前组合.txt` 看当前用的是哪个组合，`magpie groups` 看每个组实际指向谁。

### 原生子代理（只读或短活，结果直接回到主会话）

| agent | 干什么 | 路由组 | 示例 A（国产） | 示例 B（Codex） |
|---|---|---|---|---|
| `explorer` 探索员 | 摸代码、找定义和调用链、恢复项目现状 | `group/explore` | MiniMax · medium | Terra · medium |
| `researcher` 尽调员 | 网上调研、选型对比、核实说法 | `group/research` | glm-5.3-flash · high | Sol · high |
| `extractor` 转录抽取员 | 日志/周报/表格/批量截图的逐字转录和抽取 | `group/extract` | MiniMax · medium | Terra · medium |
| `reviewer` 审查员 | 代码或方案审查；安全轴、正确性轴可分两次派 | `group/review` | glm-5.3-flash · high | Sol · xhigh |
| `diagnoser` 诊断员 | 线上/本机/服务器排障取证，只读，给假设+验证命令+回滚 | `group/diagnose` | glm-5.3-flash · high | Sol · high |
| `writer` 文档员 | 把定好的内容写成交接文档、进度文档、员工物料、JD 初稿 | `group/write` | deepseek-flash · high | Terra · high |
| `verifier` 复核员 | 关键决策复核、高风险最终验收、冲突裁决（少用） | `group/verify` | Opus 5.5 · high | Astra · high |

上表两列是参考搭配，不是推荐方案。探索、抽取可考虑快且成本较低的模型，审查和复核可选能力更强的模型；结合任务质量、费用与可用性调整，并按需配置兜底。

### Orca worker（要改代码、耗时长、要并行、要别的 agent）

| 角色 | 怎么起 | 用途 |
|---|---|---|
| 实现者 | `--agent claude --model group/impl` | 需求明确、范围清楚的实现 |
| 高级实现者 | `--agent claude --model group/impl-strong` | 复杂、跨模块、需要判断的实现 |
| Codex 原生实现者 | `--agent codex --model gpt-6.1-sol`；难活 `gpt-6-astra`；机械活 `gpt-6.1-terra` | 想用 Codex 自己的工具链时；fast 可根据自身配置选择 |
| （手动） | `--model <Claude来源>/claude-sonnet-5-5` | 按任务需要选用 |
| 守望者 | `--model group/minimax` | 盯外部 agent 会话，只发"继续"，非上游原因的阻塞只报告不介入 |

impl / impl-strong 两个组不写死推理档位，由主脑每次用 `--effort` 决定（第 4 节）。

## 2. 硬规则

- 调用具名 agent **不传 `model`**，让 frontmatter 生效。
- **不用内置 Explore / Plan**：它们的模型由 Claude Code 按环境决定（官方文档：经 `ANTHROPIC_BASE_URL` 网关时 Explore 跑在 `opus` 别名指向的模型上，部分云平台上跟随主会话），可能落到 Opus，也不受 agent 文件控制；具名 explorer 更便宜、更可控。
- `general-purpose` 不得已要用时传 `model="sonnet"` 或 `"fable"`；它们实际落到哪个模型由档位组决定。Opus 主会话下 `"opus"` 就是 Opus 本身。
- MiniMax 只派结果能校验的活（有原文可对照、有路径可核），不派"判断外面有没有这条信息"的活——它不知道时爱编。
- 实现任务选用适合代码质量要求、且结果可验证的模型。

## 3. 报错改派

- **智谱内容审核 1301**（"系统检测到输入或生成内容可能包含不安全或敏感内容"）：GLM 系列会被拦。同一个 agent 重派一次，传 `model="haiku"`（MiniMax）；如果是长任务或对准确性要求高，改用 Orca worker 指定 `--model group/ds-flash`。遇到此类错误时先核实原因，再选择可用的替代模型。
- **503 No available accounts / 429**：号池满或限流，等一会儿重派；连续失败就降一档（例如把主力模型换成同来源更快的模型）。
- 其他报错先看报错原文再决定，不盲目重试。

## 4. Orca worker

先跑一次 `orca skills get orchestration` 读版本匹配的指南。

```text
# 单任务，用当前工作区
orca orchestration worker-start --spec-file <任务说明.md> --task-title "<标题>" --worktree current --agent claude --model group/impl --effort <档位> --keep-open

# 并行改代码：每个 worker 独立 worktree（仅 git 仓库），并发上限见下面
orca orchestration worker-start --spec-file <说明.md> --task-title "<标题>" --worktree new-child --name <短名> --agent claude --model group/impl-strong --effort <档位> --keep-open

# Codex 原生 worker：--model 写裸模型名，不带 codex/ 前缀（在一台测试机上实测通过）
orca orchestration worker-start --spec-file <说明.md> --task-title "<标题>" --worktree current --agent codex --model gpt-6.1-sol --effort <档位> --keep-open
```

Codex worker 不读 `~/.claude` 下的任何规则，它只认 `~/.codex/AGENTS.md`（暂不改）。所以第 5 节的"实现者固定附加规则"必须原样抄进每份给 Codex 的任务说明里。

启动后**结束本轮**，Orca 会唤醒你；按唤醒提示跑 `check`，用 `send` 追加指令，不轮询。

### 推理档位（每次派 worker 由主脑判断，必须带 `--effort`）

示例配置使用 `medium`、`high`、`xhigh` 三档；可按模型支持情况、任务质量和费用调整。`--effort` 要和 `--model` 一起用。按任务难度选：

- `medium`：机械活——按明确清单改文案、重命名、批量替换、跑一遍给定命令收集输出。
- `high`：常规实现——范围清楚的功能、补测试、修有复现步骤的 bug。拿不准时默认它。
- `xhigh`：难活——跨模块改动、并发/安全/数据迁移、没有复现步骤的 bug、上一次返工过的任务。

示例中的实现角色组使用 medium / high / xhigh。示例里 Codex 成员写成 `:medium+high+xhigh`，由 `--effort` 在三档里选；GLM 和 DeepSeek 自己只有 low / high / max，所以钉 `:high`；MiniMax 写成 `:medium+high+xhigh`。谁传了 low 或 max，magpie 会换成最接近的那一档：在一台测试机上，2026-11-06 给 impl-strong 传 max，落到 astra 时是 xhigh。改之前的教训是：没钉档位的 Codex 成员收到 max 请求会被 magpie 跳过，整轮落到兜底成员。派活回复里写一句"选了什么档位、为什么"。

原生子代理的推理档位由角色组成员配置，派的时候**不要传 `effort` 参数**：在一台测试机上给 explorer 传 max、给 reviewer 传 low，magpie 记录都变成了 high，既不按传的走，还把 explorer 原本的 medium 冲掉了。

### 并发与连续运行

按各上游限流、额度和机器资源设置并发上限，避免请求拥堵和构建争抢资源。例如总计 7 个 worker，其中 Codex 最多 4 个；超出 Codex 上限的任务可交给有余量的兜底模型。使用同一上游的原生子代理也要计入该上游并发。WSL 构建和测试的数量按机器资源确定。连续运行的时长和检查间隔也可按自己的情况设置。

### 让用户看得到、确认后再关

- 每个 worker 都加 `--keep-open`：它在 Orca 对应工作区里开一个可见的 Claude Code 终端，用户随时能切过去看、能直接插话；干完也不自动关。
- `--task-title` 写成用户一眼能认出的中文标题。它就是 Orca 会话管理里这条会话的标题，以后靠它找回。
- 启动后告诉用户：在哪个工作区、标题是什么。
- 收到 `worker_done` 后先按第 6 节验收；**验收通过后**再关终端：`orca orchestration worker-release --dispatch <dispatch_id>`。验收不通过就别关，用 `send` 把修正意见发回去。
- 关掉只是释放终端，会话记录还在：用户可以在 Orca 的会话管理里按标题找到再打开；命令行用 `orca search "<标题关键词>" --scope all --sort newest` 也能搜到。需要它接着干活，用 `send` 发消息会把已关闭的 worker 重新唤起。
- 汇报时写清：哪些 worker 已关闭、标题分别是什么。
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
- 每次验收在回复里记一笔：是否一次通过、返工几次（三期起由 hook 写入项目 `.claude/派活记录.jsonl`）。

## 7. 核对模型落点

```text
magpie usage --csv --last 1h   # 按 session × requested_model 看，确认没有意外落到 Opus
```

模型一律写 magpie 路由组名，不写供应商前缀。两类组：
- 角色组（agent 和 worker 用）：`explore`、`extract`、`research`、`review`、`diagnose`、`write`、`verify`、`impl`、`impl-strong`。组成员由组合文件决定（第 10 节）。
- 档位组（`~/.claude/settings.json` 里的 opus/sonnet/haiku/fable 档位指向它们）：`glm-main`、`glm-fast`、`minimax`、`ds-flash`。

组名不带版本号，换来源或升级模型只改组成员。例如 glm-5.5 出来后，把两个组合文件里的 `glm-5.3` 换成 `glm-5.5` 再重新切一次组合，同时 `magpie group set glm-main models=<来源>/glm-5.5`；agent、手册、Orca 命令都不用动。`magpie groups` 看每个组现在指向谁。

## 8. 预算

按自己的计费方式、额度和任务量设置预算，并定期核对实际使用情况。

## 9. 和 mattpocock 技能配合

这些技能里写的是"派一个子代理"，没指定类型；Opus 主会话下直接派会落到 Opus。一律换成：

- `code-review` 的 Standards / Spec 两个子 agent → 都派 `reviewer`（一次前台并行发两个）。
- `research` 技能、`wayfinder` 的调研票 → `researcher`。
- `improve-codebase-architecture` 的走查、`implement-spec` 的探索子代理 → `explorer`。
- `implement-spec` 的实现者 / 合并者 → `general-purpose`，传 `model="fable"`，复杂票传 `"sonnet"`，`isolation` 用 `worktree`。
- `chief-of-staff` 里的子代理 → 按第 1 节选具名 agent。
- `claude-handoff` 的后台 agent → `general-purpose`，传 `model="sonnet"`。
- `/implement`、`/tdd`、`/prototype` 不在 Opus 主会话里跑：建议用户开一个主模型指向 `group/glm-main` 的实现会话（或新会话里 `/model sonnet`），每张票之间 `/clear`；要并行就拆成 Orca worker。

分工：Opus 会话跑对话与决策类（`/grill-with-docs`、`/grilling`、`/domain-modeling`、`/to-spec`、`/to-tickets`、`/codebase-design`、`/wayfinder`、`/zoom-out`、`/to-questionnaire`、`/handoff`、`/retro`）和 `/code-review` 的结论验收；实现会话跑 `/implement`、`/tdd`、常规 `/diagnosing-bugs`。

git：`/implement`、`implement-spec` 可在功能分支上本地 commit（`code-review` 要拿提交点比对改动）；不直接提交 main/master；push、合并进主分支等用户同意。Orca worker 仍按第 5 节停在"改完未提交"。

## 10. 组合：换一整套干活模型

组合就是"每个角色组里放谁"的一份清单，放在 `~/.claude/skills/dispatch/组合/`：
- `国产.txt`：以 GLM / MiniMax / DeepSeek 为成员的参考示例，不是推荐方案。
- `Codex.txt`：以 Codex 模型为首选、国产模型为兜底的参考示例。模型、档位和顺序都可以改，也可以照格式新增自己的组合。

切换会改 magpie 路由组，执行前先向使用者说明：

```text
# Windows
python "$HOME\.claude\skills\dispatch\组合\切换组合.py" Codex
# WSL（自动改用 Windows 的 magpie.exe，切的是同一个 magpie）
python3 ~/.claude/skills/dispatch/组合/切换组合.py Codex
```

脚本逐行执行文件里的 `magpie group add`，任何一行失败就停，成功后写 `当前组合.txt` 并打印 `magpie groups`。旧的 `切换组合.ps1` 只是转调它。切完派一个 explorer 小活，用 `magpie usage --csv --last 1h` 核对落点。

`stays`（一段对话是否粘在上次答话的模型上）：示例的国产组合用 auto；示例的 Codex 组合用 turn，每轮新对话重新从第一顺位选。turn 的理由是：路由先主后备时，主模型一次不可用会让 auto 把对话粘在兜底模型上，后续请求可能大量落到兜底。其他组合按自己的路由需要选择 auto 或 turn。切换前已经开始的对话可能还留在旧模型上，核对时看切换后新开的对话。

额度或质量要求变化时调组合文件本身，不新建 agent。示例里额度紧可以把 Astra 换 Sol、Sol 换 Terra，或把 `:xhigh` 改 `:high`；额度松就反过来。改完重跑一次切换脚本。新增组合照着现有文件的格式写一份新的 txt。

按实际情况监控额度和并发，用可验证的任务结果评估模型表现，必要时调整组合。

## 11. WSL：两种用法

WSL 里可按需安装 claude、codex、uv、node。配置可以和 Windows 共用：`~/.claude/CLAUDE.md`、`agents`、`skills`、`~/.codex/AGENTS.md`、`auth.json` 是指向 Windows 的链接，改 Windows 那份两边生效；`settings.json`、Codex 的 `config.toml`、MCP 是从 Windows 版生成的，Windows 那边改了这三样要在 WSL 里重跑一次：

```text
python3 /mnt/c/Users/<用户名>/.claude/skills/dispatch/WSL/同步配置到WSL.py
```

如果 magpie 运行在 Windows 上，WSL 需要按自己的网络模式配置连接地址。有几个 MCP 依赖 Windows 专用路径，在 WSL 里带不过去，同步脚本会跳过并列出名字；要用这些就在 Windows 会话里做。

### 用法一：Windows 主会话派 worker 到 WSL

Orca 通过 SSH 远程服务器连进 WSL：sshd 只监听 127.0.0.1，只认密钥登录。在 Orca 的"远程 Orca 服务器"里能看到它。项目要先在 WSL 里 clone 一份放在 ext4 上（如 `~/work/<项目>`），再登记成这台服务器上的项目：

```text
orca project setup-existing-folder --host runtime:<envId> --path /home/<用户>/work/<项目>
```

登记后拿到 repoId，派 worker 时比 Windows 多 `--on <服务器名>`，`--worktree` 写 `id:<repoId>::<WSL路径>`：

```text
orca orchestration worker-start --on <服务器名> --worktree "id:<repoId>::/home/<用户>/work/<项目>" --spec-file <说明.md> --task-title "<标题>" --agent claude --model group/impl --effort <档位> --keep-open
```

- 远程主机上 Orca 起的是终端模式 worker（不是结构化会话），一样出现在 Orca 界面里，能切过去看、能插话；`check`、`send`、`worker-release` 用法不变。看终端内容：`orca terminal read --environment <服务器名> --terminal <终端句柄>`。
- WSL 使用独立的 ext4 检出，远程仓库按项目需要配置。
- 依赖（`.venv`、`node_modules`）在 WSL 里单独装，不链接 Windows 检出里的。
- worker 启动报 `agent_prompt_blocked`：多半是 claude 首次运行的提示卡住了。先 `orca terminal read` 看是什么提示，回答后把对应的"已看过"键加进同步脚本。已知一个：`hasSeenAutoDefaultNudge`（"要不要把 auto 设为默认权限模式"），脚本已处理。
- 同时在 WSL 里跑构建或测试的 worker 数应按机器资源确定，并计入总并发。

### 用法二：直接在 WSL 里开主会话

在 WSL 终端里 `cd ~/work/<项目>` 后运行 `claude` 或 `codex`。规则、agent、本手册都是同一份，原生子代理照常派。要让会话在 Windows 的 Orca 里看得到、管得到，不要在裸 WSL 终端里起，而是在 Orca 里打开这台服务器上的这个项目，从 Orca 的终端里起 `claude`；Orca 的 worker 也只能从 Orca 里的会话派。切组合用第 10 节的 WSL 命令。

不做：不重启 WSL、不改 `.wslconfig`（如果 WSL 里还跑着别的项目，重启前先确认影响）。
