# troupe 协作守则（本工作区）

本机运行唯一一个 troupe 网关（`gateway.py`），支持本机挂载多个不同或相同的 coding agent 会话：
- 每个挂载了 MCP（`mcp_frontend.py`）的 agent 启动时会自动向本机 gateway 注册并生成代表自己的唯一 `agent_id`。
- 后续所有提问、任务、审查、文件收发均自动绑定 `from_agent_id` 与 `to_agent_id`，严格遵守**谁发的就谁收、发给谁的就谁收**。

## 主动职责与信箱常驻
- 会话开始时若 troupe 的 MCP 工具可用，可调用 `whoami()` 查看自己的唯一 `agent_id`（或用 `register_agent(name, role)` 设置易读名称）。
- **指令响应【启用troupe】**：当用户在会话中发送“`启用troupe`”或“`启用 troupe`”时，必须立即进入**常驻剧团信箱巡检模式**：
  1. 立即执行一次 `check_inbox()` 查阅分配给本 Agent 的信箱（`questions`、`replies`、`files`）。
  2. 若 `questions` 中存在待答问题：
     - 逐一分析问题需求，利用本地代码阅读/搜索工具查阅仓库代码与上下文，进行深度思考；
     - 组织完整、清晰的答复，调用 `submit_answer(question_id, answer)` 提交回答回推给提问方。
  3. 若 `files` 中有发来的新文件，记录其 `file_id` 并按需调用 `fetch_file(file_id=...)` 接收。
  4. 启动周期性定时轮询：
     - 若当前宿主环境支持长轮询，调用 `check_inbox(timeout=30)` 阻塞静默等待新消息；
     - 若当前宿主环境支持定时调度工具（如 `schedule` / 后台定时任务），设置周期性循环检查（例如每 10~30 秒或收到通知时检查）；
     - 保持持续巡检与作答，直至收到用户取消指令。
- **指令响应【停用troupe】**：当用户在会话中发送“`停用troupe`”或“`停止巡检`”时，必须调用 `disable_troupe()` 取消后台定时调度，立即恢复隐身状态。

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
