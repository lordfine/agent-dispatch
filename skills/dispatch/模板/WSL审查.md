# WSL 审查说明

当审查需要在 WSL 里跑测试、原生 reviewer 无法运行时，改派给 WSL 上的 worker（claude 或 codex 都行）。

## 约束
- 只读审查：不改文件、不提交。主脑在说明里写清要审的分支、基线提交和对照的规格路径。
- 只跑复现问题需要的最小用例。
- 测试结果只用：未运行、失败、部分通过、通过；跳过或测试数为 0 不算通过。
- 规范轴和需求轴分开报告。
- 每个问题都写 `文件:行号` 和触发条件；没有问题也说明检查范围。

## 起 worker 示例

`--worktree` 指向要审的那个工作区的路径。review 组成员钉了 xhigh，传 high 也会按 xhigh 发，因此示例不传 `--effort`。

```text
orca orchestration worker-start --on <远程服务器名> --worktree "id:<repoId>::<要审的那个工作区的路径>" --spec-file <说明.md> --task-title "WSL 审查" --agent claude --model group/review
```

## 交付

按 `模板/交付.md` 的格式交付：先写交付文件，再发 `worker_done`。交付中分别列规范轴、需求轴、测试命令、真实退出码、通过数、改动统计（应为零）和未做的事。
