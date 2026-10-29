# troupe 协作守则（本工作区）

本机（zcode-box）运行 troupe 网关，driver=mailbox：远程 agent 发来的问题会进入信箱，由本机 zcode 会话作答。

## 主动职责
- 会话开始时若 troupe 的 MCP 工具可用，先 `check_inbox()` 查看是否有远程提问；有则回答并用 `submit_answer(question_id, answer)` 提交。

## 向远端提问 / 派活
- 问信息用 `ask_peer(peer, question, context)`（对方只读，不改文件）。
- 派重活用 `run_task(peer, title, prompt)`（对方在独立 git worktree 分支执行并 commit），配合 `task_status(peer, job_id)` 轮询。
- 问题必须自包含：对方看不到本会话的任何上下文，需写明文件路径、报错原文、期望的回答格式。
- 预计超过 5 分钟的事不要用 `ask_peer` 阻塞等待，改用 `run_task`。
- 审查用 `review_pr(peer, pr_number, focus)`。

## 安全
- `~/.troupe/config.json` 含 token，不要提交进任何 git 仓库，不要在回复里原样输出完整 token。
