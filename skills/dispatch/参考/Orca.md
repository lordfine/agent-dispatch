# Orca worker

需要：底座层（Orca）。远程执行机上的写法另见 `远程执行机.md`。

先跑一次 `orca skills get orchestration` 读版本匹配的指南；这里只记录官方指南没有写到的 Orca 坑，历史 worker 启动、复用和提示卡问题是在 Orca 1.4.250 上实测的。

## 启动

```text
# 常规实现
orca orchestration worker-start --spec-file <任务说明.md> --task-title "<标题>" --worktree current --agent claude --model group/impl --effort <档位> --keep-open
# 复杂实现，独立 worktree（仅 git 仓库）
orca orchestration worker-start --spec-file <说明.md> --task-title "<标题>" --worktree new-child --name <短名> --agent claude --model group/impl-strong --effort <档位> --keep-open
```

- 由 `worker-start` 建 worker 的工作区；受监督的 worker 不要先自己 `worktree create`。改同一批文件的 worker 各用独立工作区。`--keep-open` 表示保留可见终端，用户可以查看和插话。远程 worker 只用 Dispatch ID 称呼。
- `--agent codex` 只在需要 Codex 自己的工具链或做对比时使用：`--model` 写裸模型名，从在用组合 impl 组成员里取，去掉 `codex/` 前缀和 `:档位`、`:fast` 后缀（Orca 1.4.250 实测）；fast 在 `~/.codex/config.toml` 全局开。直接写 `codex/模型名` 的写法作废，模型名只出现在组合文件里。Codex worker 不读 `~/.claude`，附加规则要抄进说明；它只认 `~/.codex/AGENTS.md`（暂不改）。Codex 原生 worker 遇到上游卡顿、请求被取消后不会自己重试，会一直挂着（1.4.251 实测）；超过 10 分钟没动静就 `worker-stop` 后重派。
- 非 Git 文件夹可以登记：`orca repo add --path <绝对路径> --kind folder --json`。

## 等待和追加

启动 worker 后结束本轮，等 Orca 唤醒；唤醒消息让你跑 `check` 时才跑。追加指令或修正用 `orca orchestration send --to dispatch:<id> --subject ... --body ...`。

worker 被上游 429 等报错打断后会停在提示符，Orca 不通知：向用户报“还在做”之前，先用 `orca terminal read` 看一眼终端尾部（远程加 `--environment`），看到 API 报错就 `send` 一条“继续”，worker 会先跑 check 再接着干（实战项目批14 实测）。

卡片进度用：
```text
orca worktree set --worktree <选择器> --comment "<进度>" --workspace-status in-progress|in-review|completed --json
```
需要用户拍板时用：
```text
orca orchestration gate-create --task <task_id> --question "<问题>" --options '<JSON 数组>'
```

## 让用户看得到、确认后再关

收到 `worker_done` 后先按手册第 6 节验收，验收通过后再关终端：`orca orchestration worker-release --dispatch <dispatch_id>`。验收不通过就别关，用 `send` 发回修正意见。批量释放时回执里的 retained（`ownership_transferred`、`identity_unproven`、`no_owned_resource`）可以忽略，只要 `worker-list` 里没有还在跑的 worker 就行（实战项目批13 实测，1.4.251）。

`--task-title` 写成用户一眼能认出的中文标题，启动后告诉用户在哪个工作区、标题是什么；汇报时写清已关闭的 worker 和标题。关掉的会话记录还在，可以用 `orca search "<标题关键词>" --scope all --sort newest` 找回。

## 派活历史（给派活记录和复盘用）

`orca orchestration task-list --json` 列任务；`orca orchestration dispatch-show --task <task_id> --json` 给出派出时间、完成时间、状态，以及 `follow_up_of_dispatch_id`（这次是哪次派活的返工或续派，用它串起同一张票的返工链）。`worker-show --dispatch <id> --full --json` 里有 model、effort 字段，旧记录可能为空，空的话用 magpie 用量按时间对。远程执行机上的派活加 `--environment <远程服务器名>`。
