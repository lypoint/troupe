[简体中文](README.md) ｜ [English](README.en.md)

# troupe 🎭

> 把散落在各台机器上的 coding agents，凑成一个剧团：互问、互派、互审、互传文件。

**troupe** 是一个极简的跨机多 Agent 协作网关与应用：让跑在不同机器（以及同台机器不同会话）上的 AI coding agent（zcode / codex / opencode / qoder / claude…）像剧团一样协作。
- **每台机器唯一 Gateway**：每台机器只跑一个纯 Python 标准库的网关 `gateway.py`。
- **原生可视化 App（无需 WebUI）**：直接启动 `python3 gateway.py` 即弹出**原生桌面窗口 App**（基于 Python 标准库 `tkinter`，支持 `--tui` 切换**终端内全屏交互 App**），在应用内直观查看**启动流程可视化**、**运行过程可视化**与**消息投递状态可视化**。
- **多 Agent 自动注册与唯一 ID**：每个挂载了 MCP（`mcp_frontend.py`）的 agent 会话启动时，都会自动向本机 gateway 注册并生成代表自己的唯一 `agent_id`；配置了 CLI 驱动的机器还会自动注册内置的 `driver` agent。
- **两步式选路**：首个发消息者必须先选择目标 **gateway**（`list_peers`），再从该 gateway 中选择目标 **agent**（`list_agents`），随后才能发消息。
- **谁发的就谁收**：后续会话全程携带 `from_agent_id` 与 `to_agent_id`——接收方信箱按 `to_agent_id` 严格隔离，回答与任务回执仅归属原发送者 `from_agent_id` 查看与接收。

## 原生 App 三大可视化模块（`troupe_app.py`）

无需部署任何 Web 前端或打开浏览器，启动即运行原生应用窗口：

1. **🚀 启动流程可视化（Startup Pipeline）**
   - 实时展示 5 步启动自检流水线进度图：**① 加载节点配置 ➔ ② 恢复历史会话与任务 ➔ ③ 初始化多 Agent 注册中心 ➔ ④ 启动本机唯一 Gateway 服务 ➔ ⑤ 探测远端 Peer 网关拓扑**。
   - 内置**远端 Gateway 配置与局域网发现向导**：在 App 内直接填写对方地址 + Token，或一键点击「🔍 扫描局域网网关」自动并发探测并保存。
2. **⚡ 运行过程可视化（Runtime & Multi-Agent Topology）**
   - **本机在线 Agents 面板**：实时监控当前挂载到本机 Gateway 的所有 MCP 会话（唯一 `agent_id`、名称、模式 `mailbox/driver`、本地进程 `PID`、最近心跳秒数），进程退出自动清理。
   - **远端 Peer Gateways → Agents 拓扑树**：直观展示每个远端 Gateway 及其下属的所有可投递 `agent_id`。
   - **实时运行事件流（Live Runtime Stream）**：按类别（`STARTUP` / `AGENT` / `OUTBOUND` / `INBOUND` / `WORKTREE` / `DELIVERY` / `AUTH`）滚动展示每一次注册、提问、Git Worktree 沙箱执行、回推与越权拦截事件。
3. **📬 消息投递状态可视化（Message Delivery State Tracker）**
   - **5 阶段可视化流转画布**：点击任意会话即可查看动态绘制的 5 节点投递状态图：
     `① 发起方发出` ➔ `② 目标入箱/执行中` ➔ `③ 目标完成作答` ➔ `④ 回推原发送方` ➔ `⑤ 发送方已收阅`
   - **逐跳时间戳轨迹（Hop-by-Hop Timeline）**：详细记录每一跳的时间、执行者（`from_peer/from_agent_id` 与 `to_peer/to_agent_id`）及状态详情。
   - **应用内信箱直接作答**：选中处于 `pending_answer` 的信箱问题，可直接在 App 底部输入框以目标 Agent 身份提交回答并自动回推给原发送方。

## 启动方式

```bash
# 1. 默认启动：弹出原生桌面窗口 App（macOS / Windows / Linux 桌面环境）
TROUPE_CONFIG=~/troupe/config.json python3 gateway.py

# 2. 终端全屏 App 模式（适合纯终端或 SSH 环境，按 1/2/3 切换三大可视化视图）
TROUPE_CONFIG=~/troupe/config.json python3 gateway.py --tui

# 3. 纯后台静默运行（适合 nohup / launchd / systemd 守护进程）
TROUPE_CONFIG=~/troupe/config.json python3 gateway.py --no-ui
```

## 各 agent 的 MCP 接入配置

可通过环境变量 `TROUPE_AGENT_NAME` 和 `TROUPE_AGENT_ROLE` 为不同 agent 配置易读名称（也可在会话内调用 `register_agent(name, role)` 动态修改）：

**zcode**（`~/.zcode/cli/config.json` 的 `mcp.servers`，或项目级 `<repo>/.zcode/config.json`）：

```json
{ "mcp": { "servers": { "troupe": {
  "command": "uv",
  "args": ["run", "--no-project", "--with", "fastmcp", "python", "/你的路径/mcp_frontend.py"],
  "env": {
    "TROUPE_CONFIG": "/你的路径/config.json",
    "TROUPE_AGENT_NAME": "zcode-main"
  }
} } } }
```

**codex**（`~/.codex/config.toml`）：

```toml
[mcp_servers.troupe]
command = "uv"
args = ["run", "--no-project", "--with", "fastmcp", "python", "/你的路径/mcp_frontend.py"]
env = { TROUPE_CONFIG = "/你的路径/config.json", TROUPE_AGENT_NAME = "codex-cli" }
```

## 工具语义（agent 视角）

- `whoami()` / `register_agent(name, role)`：查看或更新当前会话在本机 gateway 注册的唯一 `agent_id`、名称与职责。
- `list_peers()`：**第 1 步（选 Gateway）**——列出其他机器的 gateway 及其可达状态。
- `list_agents(peer)`：**第 2 步（选 Agent）**——列出目标 `peer` 网关下所有在线的 `agent_id`（含 `mode`: `mailbox` 会话型或 `driver` 命令行驱动型）。`peer` 留空可查看本机网关上的所有在线 agent。
- `ask_peer(peer, agent_id, question, context)`：**第 3 步（发消息）**——向指定 `peer` 的指定 `agent_id` 提问。请求自动带上本会话 `from_agent_id`，对方回答后仅本会话可接收。
- `run_task(peer, agent_id, title, prompt, base_branch)`：向目标 `peer` 上 `mode=driver` 的 `agent_id` 派发重活，在独立 `git worktree` 分支 `agent/<job>` 上执行并自动 commit。
- `task_status(peer, job_id)`：轮询由本 agent 自己发出的任务/提问结果（其他 agent 无权查询）。
- `review_pr(peer, agent_id, pr_number, focus)`：请对方指定 `agent_id` 审查 GitHub PR。
- `send_file(peer, agent_id, path, note)` / `list_files(peer)` / `fetch_file(peer, file_id, save_path)`：按 `agent_id` 精准收发文件，仅目标 `agent_id` 可在信箱中看到并收取。
- `check_inbox(timeout=0)` / `submit_answer(question_id, answer)`：查看专属于当前 `agent_id` 的待答问题（`questions`）、远端回推的回答（`replies`）及收到的文件（`files`），并针对发给自己的问题作答。支持 `timeout` 长轮询等待，信箱为空时自动阻塞挂起直至新消息到达。
- **主动常驻指令【启用troupe】**：在任何支持 MCP 的 IDE（Antigravity、Cursor、ZCode 等）会话中发送 `启用troupe`，Agent 将按照 `AGENTS.md` 规则进入常驻巡检模式：定时/长轮询信箱，遇到发给自己的问题自动借助当前 IDE 的代码搜索与理解能力作答并提交。
