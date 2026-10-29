[简体中文](README.md) ｜ [English](README.en.md)

# troupe 🎭

> 把散落在各台机器上的 coding agents，凑成一个剧团：互问、互派、互审、互传文件。

**troupe** 是一个极简的跨机协作网关：让跑在不同机器上的 AI coding agent（zcode / codex / opencode / qoder / claude…）像剧团一样协作。每台机器只跑一个纯 Python 标准库的单文件网关，任意 agent 通过 MCP 零改造接入；重活在独立的 git worktree 分支上执行，完成后自动 commit / push / 开 PR；只有 GUI 的 agent 走信箱模式兜底。troupe 不生产 agent，它只负责让已有的 agents 同台演出。

## 为什么叫 troupe

每台机器的 agent 是剧团里的演员：有人主演（在 worktree 里干活）、有人提词（答疑）、有人导演（审查），各司其职，合力演好一场戏（一个任务）。机器加一台，团里就多一个角色。

## 特色

- **互问** `ask_peer`：跨机同步问答，对方只读代码库
- **互派** `run_task`：派重活，对方在隔离的 git worktree 分支执行，自动 commit / push / 开 PR
- **互审** `review_pr`：请对方 agent 审查 GitHub PR，返回意见文本
- **传文件** `send_file` / `fetch_file`：跨机收发截图、日志、数据集等代码库之外的东西，不依赖 driver
- **零改造接入**：所有主流 coding agent 都是 MCP 客户端，配上就能用
- **信箱模式**：GUI-only 的 agent（如 zcode 桌面版）也能入团——问题落信箱，它的会话用 MCP 工具作答
- **单文件网关**：`gateway.py` 只用 Python 标准库，无框架依赖；MCP 前端仅依赖 fastmcp
- **A2A 兼容子集**：第三方标准 A2A 客户端可直接与网关互发文本

## 为什么是这个架构

- **MCP 是 coding agent 唯一的"公共语言"**（上面列的 agent 全都支持作为 MCP 客户端），所以接入层统一用 MCP：任何 agent 不需要任何改造就能当"提问方"。
- **A2A 是 agent 互相对话的行业标准**（Google 发起、Linux 基金会托管），但没有任何 coding agent 原生支持它，所以把它作为网关之间的通信协议，而不是对 agent 的要求。
- **执行侧靠 driver**：被提问方的 agent 用命令行驱动（`codex exec` / `opencode run` / `qoder --print`…）。只有 GUI、没有 CLI 的（如 zcode 桌面版）走**信箱模式**兜底——问题落进信箱，它的会话用 MCP 工具作答。

```
┌────────── 机器 A 的任意 agent（zcode…）──────────┐        ┌────────── 机器 B 的任意 agent（codex…）─────────┐
│  ask_peer / run_task / task_status / review_pr  │        │  （被 driver 驱动应答，或也装前端主动提问）      │
└───────────────────────┬─────────────────────────┘        └───────────────────────▲─────────────────────────┘
                        │ MCP (stdio)                                              │ driver
                 mcp_frontend.py                                   codex exec / opencode run / qoder --print
                        │ HTTP (本机)                                              │
                   gateway.py  ────── REST + A2A 子集（token 鉴权）──────►  gateway.py
```

## 快速开始（每台机器 3 步）

1. 拷贝 `gateway.py`、`mcp_frontend.py`、`config.example.json` 到任意目录（如 `~/troupe/`），配置改名为 `config.json`：改 `name`、`token`（随机长串）、`repo`（本项目在这台机器上的路径）、`driver.type`、`peers`（对方的地址和 token）。
2. 启动网关：前台 `TROUPE_CONFIG=~/troupe/config.json python3 gateway.py` 会先进入 **peers 配置向导**——`[1]` 手动填对方地址+token，`[2]` 扫描局域网自动发现 troupe 网关（多网卡网段全扫，按本机 listen 端口探测、以 token 匹配验证），选完写回 config.json 再继续启动。`nohup … &` 后台启动自动跳过向导；`--no-setup` 强制跳过；对方换过端口时扫描探不到，请用 `[1]` 手填。（macOS 可做成 launchd 开机自启）
3. 把 MCP 前端配置进本机的 agent（见下）。

## 各 agent 的 MCP 接入配置

**zcode**（`~/.zcode/cli/config.json` 的 `mcp.servers`，或项目级 `<repo>/.zcode/config.json`）：

```json
{ "mcp": { "servers": { "troupe": {
  "command": "uv",
  "args": ["run", "--no-project", "--with", "fastmcp", "python", "/你的路径/mcp_frontend.py"],
  "env": { "TROUPE_CONFIG": "/你的路径/config.json" }
} } } }
```

**codex**（`~/.codex/config.toml`）：

```toml
[mcp_servers.troupe]
command = "uv"
args = ["run", "--no-project", "--with", "fastmcp", "python", "/你的路径/mcp_frontend.py"]
env = { TROUPE_CONFIG = "/你的路径/config.json" }
```

**opencode / qoder / claude 等**：各家 MCP 配置位置不同，但内容同上（command + args + env），按其官方文档填即可。

## driver 配置

| type | 谁用 | 说明 |
|---|---|---|
| `codex` | 装了 Codex CLI 的机器 | ask 只读沙箱，task 可写沙箱 |
| `opencode` / `qoder` / `claude` | 对应 CLI | `qoder --print`、`claude -p` 等 |
| `command` | 任何有 CLI 的 agent | 自定义 `ask_cmd`/`task_cmd`（argv 数组，提示词追加为最后一个参数） |
| `mailbox` | 只有 GUI 的（zcode 桌面版） | 问题进信箱，由其会话 `check_inbox`/`submit_answer` 作答 |
| `echo` | 测试 | 原样回显 |

## 工具语义（agent 视角）

- `list_peers()`：列出 troupe 成员与能力，协作前先调它。
- `ask_peer(peer, question, context)`：同步问答（默认 240s 超时）。对方只读代码库。**问题必须自包含**。
- `run_task(peer, title, prompt, base_branch)`：派重活。对方在独立 `git worktree` 分支 `agent/<job>` 上执行，不动它的工作区；完成后 commit（可配置自动 push / 开 PR）。返回 `job_id`。任务成功后 worktree 自动清理；失败时保留现场（路径见任务状态里的 `worktree` 字段）供排查。
- `task_status(peer, job_id)`：轮询任务/提问结果。
- `review_pr(peer, pr_number, focus)`：请对方审查 GitHub PR，返回意见文本（不发言论）。
- `send_file(peer, path, note)` / `list_files(peer)` / `fetch_file(peer, file_id, save_path)`：跨机传文件（截图、日志、数据集等代码库之外的东西）。默认单文件上限 `file_max_mb`=100MB；**不依赖 driver，信箱机器也能收发**；对方 `check_inbox()` 能看到新文件。发送方代收在 `state_dir/incoming/<file_id>/`；`fetch_file` 的 **peer 留空 = 收取别人发到本机的文件**，填 peer = 主动从对方网关拉取；落盘路径空则用本机项目目录+原文件名，重名自动加序号。
- `check_inbox()` / `submit_answer(id, answer)`：信箱模式的应答侧。

超过几分钟的事不要用 `ask_peer` 阻塞，改 `run_task`。建议把协作守则写进仓库的 `AGENTS.md`（两边 agent 都会读）。

## 安全

- 所有接口（除 agent card）需要 `Authorization: Bearer <token>`；每台机器 token 独立，`peers` 里填对方的。
- 网络层优先 **Tailscale**（内网穿透 + MagicDNS + 可上 HTTPS），其次局域网，公网裸奔不要。临时方案：`ssh -L 8765:127.0.0.1:8765 user@机器` 然后 peers url 填 `http://127.0.0.1:8765`。
- task 派发等于让对方 agent 在沙箱里跑代码，只给你信任的机器配 `task` 能力。

## A2A 兼容范围

实现了 A2A 的可用子集：Agent Card（`/.well-known/agent-card.json`，兼容旧路径 `agent.json`）、`message/send`（兼容 `tasks/send`）、`tasks/get`。没有实现 SSE 流式（`message/stream`）、push notification 和完整任务生命周期——问答场景用不到，第三方标准 A2A 客户端仍可与此网关互发文本。

## 测试（本仓库自带回环测试）

`test/` 下三个配置模拟三台机器（A=echo、B=假 agent、C=信箱），在单机上起三个网关验证全链路：问答、任务 worktree、信箱应答、跨机传文件、A2A 直连、鉴权拒绝、MCP 前端端到端。详见 `test/` 内配置文件，跑法：

```bash
rm -rf /tmp/troupe-test && mkdir -p /tmp/troupe-test/repo
cd /tmp/troupe-test/repo && git init -q -b main && echo hi > f.txt && git add -A && git -c user.email=t@t -c user.name=t commit -qm init
cd /path/to/troupe
for c in a b c; do TROUPE_CONFIG=test/config-$c.json python3 gateway.py >/tmp/troupe-test/$c.log 2>&1 & done
curl -s -X POST localhost:18765/api/out/ask -H "Authorization: Bearer ta" -d '{"peer":"b","question":"hi"}'
TROUPE_CONFIG=$PWD/test/config-a.json uv run --no-project --with fastmcp python test/smoke_mcp.py
```

## 已知边界

- gateway 进程重启时，正在跑的 task 线程会丢（任务状态文件还在，可人工收尾）；生产用 launchd/systemd 守住。
- ask 的总耗时 = 对方 agent 一次完整 CLI 运行（上下文冷启动），偏慢是正常的；频繁小问题建议攒成一次问。
- 信箱模式的 `task` 派发暂未支持（ask/review 可用）。
- MCP 客户端自身的工具超时若小于 ask 耗时会截断——遇到就把问题拆小，或改走 task。
