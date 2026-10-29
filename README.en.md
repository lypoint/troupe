[简体中文](README.md) ｜ [English](README.en.md)

# troupe 🎭

> Gather the coding agents scattered across your machines into a troupe: they ask, delegate, review, and share files with each other.

**troupe** is a minimal cross-machine gateway that lets AI coding agents (zcode / codex / opencode / qoder / claude…) on different machines collaborate like a theater troupe. Each machine runs a single-file, Python-stdlib-only gateway; any agent joins via MCP with zero changes. Heavy work runs in an isolated git worktree branch, then is committed, pushed, and opened as a PR automatically. GUI-only agents join through a mailbox. troupe doesn't make agents — it gives yours a stage.

## Why "troupe"

Each machine's agent is a member of the troupe: one plays the lead (doing the work in a worktree), one feeds lines (answering questions), one directs (reviewing code). Everyone plays their part, and together they put on the show — a task. Add a machine, and the troupe gains a role.

## Features

- **Ask** `ask_peer`: synchronous cross-machine Q&A; the peer reads its codebase read-only
- **Delegate** `run_task`: dispatch heavy work; the peer executes it in an isolated git worktree branch, then commits / pushes / opens a PR automatically
- **Review** `review_pr`: have a peer agent review a GitHub PR and return written feedback
- **File transfer** `send_file` / `fetch_file`: send screenshots, logs, datasets — things outside the codebase — across machines, no driver required
- **Zero-change onboarding**: every mainstream coding agent is already an MCP client; configure and go
- **Mailbox mode**: GUI-only agents (e.g. the zcode desktop app) can join too — questions land in a mailbox, and their sessions answer via MCP tools
- **Single-file gateway**: `gateway.py` uses only the Python standard library, no frameworks; the MCP frontend depends only on fastmcp
- **A2A-compatible subset**: third-party standard A2A clients can exchange text with the gateway directly

## Why this architecture

- **MCP is the only common language across coding agents** (all agents listed above work as MCP clients), so the access layer is uniformly MCP: any agent becomes a "requester" without any modification.
- **A2A is the industry standard for agent-to-agent dialogue** (initiated by Google, hosted by the Linux Foundation), but no coding agent supports it natively — so it serves as the protocol *between gateways*, not a requirement on agents.
- **Execution relies on drivers**: the answering agent is driven from the command line (`codex exec` / `opencode run` / `qoder --print`…). GUI-only agents without a CLI (like the zcode desktop app) fall back to **mailbox mode** — questions land in the mailbox, and the agent's own sessions answer via MCP tools.

```
┌─────── any agent on machine A (zcode…) ───────┐        ┌─────── any agent on machine B (codex…) ───────┐
│  ask_peer / run_task / task_status / review_pr │        │  (driven by a driver, or runs the frontend)   │
└──────────────────────┬────────────────────────┘        └────────────────────────▲──────────────────────┘
                       │ MCP (stdio)                                              │ driver
                mcp_frontend.py                                   codex exec / opencode run / qoder --print
                       │ HTTP (localhost)                                         │
                  gateway.py  ────── REST + A2A subset (Bearer auth) ──────►  gateway.py
```

## Quick start (3 steps per machine)

1. Copy `gateway.py`, `mcp_frontend.py`, and `config.example.json` to any directory (e.g. `~/troupe/`), rename the config to `config.json`, and set: `name`, `token` (a long random string), `repo` (the path of your project on this machine), `driver.type`, and `peers` (the peer's address and token).
2. Start the gateway: run `TROUPE_CONFIG=~/troupe/config.json python3 gateway.py` in the foreground and it enters a **peers setup wizard** — `[1]` enter the peer's address + token manually, or `[2]` scan the LAN to auto-discover troupe gateways (scans all subnets across NICs, probes on the local listen port, verifies by token match); the chosen peers are written back to config.json before startup continues. Starting with `nohup … &` skips the wizard; `--no-setup` forces it skipped; if a peer changed its port the scan can't find it — use `[1]`. (On macOS you can turn this into a launchd service.)
3. Configure the MCP frontend into the local agent (see below).

## MCP client configuration per agent

**zcode** (in `~/.zcode/cli/config.json` under `mcp.servers`, or per-project `<repo>/.zcode/config.json`):

```json
{ "mcp": { "servers": { "troupe": {
  "command": "uv",
  "args": ["run", "--no-project", "--with", "fastmcp", "python", "/your/path/mcp_frontend.py"],
  "env": { "TROUPE_CONFIG": "/your/path/config.json" }
} } } }
```

**codex** (in `~/.codex/config.toml`):

```toml
[mcp_servers.troupe]
command = "uv"
args = ["run", "--no-project", "--with", "fastmcp", "python", "/your/path/mcp_frontend.py"]
env = { TROUPE_CONFIG = "/your/path/config.json" }
```

**opencode / qoder / claude etc.**: each tool keeps its MCP config elsewhere, but the content is the same (command + args + env) — follow its official docs.

## Drivers

| type | who uses it | notes |
|---|---|---|
| `codex` | machines with the Codex CLI | ask runs in a read-only sandbox, task in a workspace-write sandbox |
| `opencode` / `qoder` / `claude` | the respective CLIs | `qoder --print`, `claude -p`, etc. |
| `command` | any agent with a CLI | custom `ask_cmd`/`task_cmd` (argv array; the prompt is appended as the last argument) |
| `mailbox` | GUI-only agents (zcode desktop) | questions land in a mailbox; the agent's sessions answer via `check_inbox`/`submit_answer` |
| `echo` | testing | echoes the question back |

## Tool semantics (from the agent's point of view)

- `list_peers()`: list troupe members and their capabilities. Call this before collaborating.
- `ask_peer(peer, question, context)`: synchronous Q&A (240s timeout by default). The peer reads its codebase read-only. **Questions must be self-contained.**
- `run_task(peer, title, prompt, base_branch)`: dispatch heavy work. The peer executes it in an isolated `git worktree` branch `agent/<job>` without touching its own working tree, then commits (auto-push / auto-PR configurable). Returns a `job_id`. The worktree is cleaned up on success; on failure it is kept for inspection (path in the task status's `worktree` field).
- `task_status(peer, job_id)`: poll for task/question progress and results.
- `review_pr(peer, pr_number, focus)`: have the peer review a GitHub PR and return written feedback (it never posts comments).
- `send_file(peer, path, note)` / `list_files(peer)` / `fetch_file(peer, file_id, save_path)`: cross-machine file transfer (screenshots, logs, datasets — things outside the codebase). Default per-file cap is `file_max_mb` = 100MB; **no driver required, mailbox machines can send and receive too**; the peer sees new files via `check_inbox()`. Sent files are staged under `state_dir/incoming/<file_id>/` on the receiver. For `fetch_file`, **leaving `peer` empty collects files sent to this machine**, while setting `peer` actively pulls from that peer's gateway; an empty `save_path` saves into the local project directory with the original filename, auto-numbering on name collisions.
- `check_inbox()` / `submit_answer(id, answer)`: the answering side of mailbox mode.

Don't block on `ask_peer` for anything that takes more than a few minutes — use `run_task` instead. It's recommended to put collaboration rules into the repo's `AGENTS.md` (agents on both sides will read it).

## Security

- Every endpoint except the agent card requires `Authorization: Bearer <token>`; each machine has its own token, and `peers` holds the *other* machine's token.
- For the network layer prefer **Tailscale** (NAT traversal + MagicDNS + optional HTTPS), then LAN; never expose it raw to the public internet. Stopgap: `ssh -L 8765:127.0.0.1:8765 user@machine`, then point peers at `http://127.0.0.1:8765`.
- Dispatching a task lets the other machine's agent run code in a sandbox — only grant the `task` capability to machines you trust.

## A2A compatibility scope

Implements a usable subset of A2A: the Agent Card (`/.well-known/agent-card.json`, with the legacy `agent.json` path also served), `message/send` (with the legacy `tasks/send` accepted), and `tasks/get`. Not implemented: SSE streaming (`message/stream`), push notifications, and the full task lifecycle — the Q&A scenario doesn't need them, and third-party standard A2A clients can still exchange text with this gateway.

## Testing (self-contained loopback test)

Three configs under `test/` simulate three machines on one box (A=echo, B=fake agent, C=mailbox) and verify the full pipeline: Q&A, task worktrees, mailbox answering, cross-machine file transfer, direct A2A, auth rejection, and an end-to-end MCP frontend test. See the config files in `test/`. To run:

```bash
rm -rf /tmp/troupe-test && mkdir -p /tmp/troupe-test/repo
cd /tmp/troupe-test/repo && git init -q -b main && echo hi > f.txt && git add -A && git -c user.email=t@t -c user.name=t commit -qm init
cd /path/to/troupe
for c in a b c; do TROUPE_CONFIG=test/config-$c.json python3 gateway.py >/tmp/troupe-test/$c.log 2>&1 & done
curl -s -X POST localhost:18765/api/out/ask -H "Authorization: Bearer ta" -d '{"peer":"b","question":"hi"}'
TROUPE_CONFIG=$PWD/test/config-a.json uv run --no-project --with fastmcp python test/smoke_mcp.py
```

## Known limitations

- When the gateway process restarts, running task threads are lost (task state files survive; finish manually if needed). Keep it alive with launchd/systemd in production.
- Total ask latency equals one full CLI run on the peer (cold context), so slowness is normal; batch several small questions into one.
- Mailbox mode does not support `task` dispatch yet (ask/review work).
- If the MCP client's own tool timeout is shorter than the ask duration, the result is truncated — split the question, or switch to a task.
