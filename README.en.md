[简体中文](README.md) ｜ [English](README.en.md)

# troupe 🎭

> Bring coding agents scattered across machines together into one troupe: ask, delegate, review, and transfer files.

**troupe** is a minimalist cross-machine multi-agent collaboration gateway and native application:
- **Single Gateway per Machine**: Each machine runs a single zero-dependency Python standard-library gateway (`gateway.py`).
- **Native Visual App (No WebUI Required)**: Running `python3 gateway.py` launches a **Native Desktop App Window** (`troupe_app.py`, built on standard-library `tkinter`, with `--tui` fallback to an interactive full-screen `curses` Terminal App) visualizing the **Startup Pipeline**, **Runtime Process & Topology**, and **5-Stage Message Delivery Status** directly inside the app.
- **Multi-Agent Auto-Registration & Unique IDs**: Every agent session that mounts MCP (`mcp_frontend.py`) automatically registers with its local gateway on startup and receives a unique `agent_id`.
- **Two-Step Target Selection**: A sender first selects a target **gateway** (`list_peers()`), then selects a target **agent** on that gateway (`list_agents(peer)`), before sending messages.
- **Strict Session Affinity**: Every session request carries `from_agent_id` and `to_agent_id`. Inboxes are strictly isolated per `to_agent_id`, and replies are bound exclusively to the original sender `from_agent_id`.

## Launch Modes

```bash
# 1. Default: Launch Native Desktop Window App (macOS / Windows / Linux Desktop)
TROUPE_CONFIG=~/troupe/config.json python3 gateway.py

# 2. Full-screen Terminal App (curses TUI, press 1/2/3 to switch views)
TROUPE_CONFIG=~/troupe/config.json python3 gateway.py --tui

# 3. Headless background mode (for nohup / launchd / systemd)
TROUPE_CONFIG=~/troupe/config.json python3 gateway.py --no-ui
```
