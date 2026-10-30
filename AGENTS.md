# troupe 协作守则（本工作区）

本机运行唯一一个 troupe 网关（`gateway.py`），支持本机挂载多个不同或相同的 coding agent 会话：
- 每个挂载了 MCP（`mcp_frontend.py`）的 agent 启动时会自动向本机 gateway 注册并生成代表自己的唯一 `agent_id`。
- 后续所有提问、任务、审查、文件收发均自动绑定 `from_agent_id` 与 `to_agent_id`，严格遵守**谁发的就谁收、发给谁的就谁收**。

## 主动职责
- 会话开始时若 troupe 的 MCP 工具可用，可调用 `whoami()` 查看自己的唯一 `agent_id`（或用 `register_agent(name, role)` 设置易读名称），并先调用 `check_inbox()` 查看是否有发给自己的远程提问、回复或文件；若有待答问题则回答并用 `submit_answer(question_id, answer)` 提交。

## 向远端提问 / 派活（强制两步选择）
1. **第一步（选 Gateway）**：调用 `list_peers()` 查看所有可达的远端网关。
2. **第二步（选 Agent）**：调用 `list_agents(peer)` 查看目标网关下在线的 `agent_id` 列表及其模式（`mailbox` 会话模式或 `driver` 命令行模式）。
3. **第三步（发消息 / 派活）**：
   - 问信息用 `ask_peer(peer, agent_id, question, context)`（对方只读，不改文件）。
   - 派重活用 `run_task(peer, agent_id, title, prompt)`（需选 `mode=driver` 的目标 agent，对方在独立 git worktree 分支执行并 commit），配合 `task_status(peer, job_id)` 或 `check_inbox()` 获取结果。
   - 审查用 `review_pr(peer, agent_id, pr_number, focus)`。
   - 传文件用 `send_file(peer, agent_id, path, note)`。
- 问题必须自包含：对方看不到本会话的任何上下文，需写明文件路径、报错原文、期望的回答格式。
- 预计超过 5 分钟的事不要用 `ask_peer` 阻塞等待，改用 `run_task`。

## 安全
- `~/.troupe/config.json` 含 token，不要提交进任何 git 仓库，不要在回复里原样输出完整 token。
