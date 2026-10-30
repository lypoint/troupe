#!/usr/bin/env python3
"""
troupe gateway —— 让任意 coding agent（codex / zcode / opencode / qoder / claude…）互相提问与协作。

架构（每台机器部署唯一一个 gateway，支持本机挂载多个 agent，自带原生桌面/终端 App 界面）：
  1. 注册与会话层：每个挂载 MCP 的 agent 启动时自动向本机 gateway 注册并生成唯一 agent_id；
     本机配置的 CLI driver 也会注册为内置 driver agent。
  2. 选路层（两步选择）：发送方必须先选定目标 gateway（list_peers），再从该 gateway 中选择目标 agent（list_agents），
     随后才能向 (peer, agent_id) 发起提问、任务、审查或传文件。
  3. 会话归属（谁发的就谁收）：所有跨机请求均携带 (from_peer, from_agent_id -> to_agent_id)，
     接收方信箱按 to_agent_id 严格隔离；回答与任务结果仅归属原发送方 from_agent_id 查看与接收。
  4. 全程可视化 App（无需 WebUI）：默认启动原生桌面窗口 App（troupe_app.py，支持 --tui 切换终端全屏 App，
     后台或 --no-ui 自动静默运行），实时展示启动流程、运行过程与消息投递状态。

只用 Python 标准库。配置文件见 config.example.json；启动：TROUPE_CONFIG=path python3 gateway.py
"""
import errno
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONFIG_PATH = os.path.expanduser(os.environ.get("TROUPE_CONFIG", str(Path.home() / ".troupe" / "config.json")))
STARTED_AT = time.time()

# ---------------------------------------------------------------- 启动流程与运行过程可视化状态

STARTUP_LOCK = threading.Lock()
STARTUP_STEPS = [
    {"id": "config", "title": "1. 加载节点配置", "status": "pending", "detail": "等待读取配置文件...", "time": 0.0},
    {"id": "jobs",   "title": "2. 恢复历史会话与任务", "status": "pending", "detail": "等待扫描本地状态目录...", "time": 0.0},
    {"id": "agents", "title": "3. 初始化多 Agent 注册中心", "status": "pending", "detail": "等待校验本地 Agent 与内置 Driver...", "time": 0.0},
    {"id": "server", "title": "4. 启动本机唯一 Gateway 服务", "status": "pending", "detail": "等待绑定监听端口...", "time": 0.0},
    {"id": "peers",  "title": "5. 探测远端 Peer 网关拓扑", "status": "pending", "detail": "等待探测远端网关与在线 Agent...", "time": 0.0},
]

EVENTS_LOCK = threading.Lock()
RUNTIME_EVENTS = deque(maxlen=400)
_EVENT_SEQ = 0

def record_event(category, summary, actor="", target="", level="info", detail=""):
    """记录运行过程事件（供 App 实时可视化展示）。
    category: STARTUP | AGENT | OUTBOUND | INBOUND | WORKTREE | DELIVERY | AUTH | ERROR
    """
    global _EVENT_SEQ
    with EVENTS_LOCK:
        _EVENT_SEQ += 1
        ev = {
            "seq": _EVENT_SEQ,
            "time": time.time(),
            "time_str": time.strftime("%H:%M:%S"),
            "category": category,
            "actor": actor or "",
            "target": target or "",
            "summary": summary,
            "detail": detail or "",
            "level": level,
        }
        RUNTIME_EVENTS.append(ev)
        return ev

def update_startup_step(step_id, status, detail=""):
    with STARTUP_LOCK:
        for s in STARTUP_STEPS:
            if s["id"] == step_id:
                s["status"] = status
                if detail:
                    s["detail"] = detail
                s["time"] = time.time()
                break
    level = "error" if status == "error" else ("warn" if status == "warn" else "info")
    if status in ("done", "warn", "error"):
        record_event("STARTUP", f"[{step_id}] {detail}", actor="gateway", level=level)

def get_startup_steps():
    with STARTUP_LOCK:
        return [dict(s) for s in STARTUP_STEPS]

def get_runtime_events(limit=150):
    with EVENTS_LOCK:
        items = list(RUNTIME_EVENTS)
    return items[-limit:]

# ---------------------------------------------------------------- 配置

def load_config():
    update_startup_step("config", "running", f"正在读取 {CONFIG_PATH}")
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    cfg.setdefault("name", f"troupe-{secrets.randbelow(90000) + 10000}")
    cfg["repo"] = os.path.expanduser(cfg.get("repo", "."))
    cfg.setdefault("state_dir", "~/.troupe")
    cfg["state_dir"] = os.path.expanduser(cfg["state_dir"])
    cfg.setdefault("ask_timeout", 240)      # 问答类最长等待（秒）
    cfg.setdefault("task_timeout", 3600)    # 编码任务最长执行（秒）
    cfg.setdefault("file_max_mb", 100)      # 单文件传输上限（MB）
    cfg.setdefault("open_pr", False)        # 任务完成后是否自动 gh pr create
    cfg.setdefault("driver", {"type": "echo"})
    cfg.setdefault("peers", {})
    update_startup_step(
        "config", "done",
        f"节点={cfg.get('name')} | 驱动={cfg['driver'].get('type')} | 仓库={cfg['repo']}"
    )
    return cfg

CFG = load_config()
DRIVER = CFG["driver"]

# 常见 agent 的 driver 快捷方式：type 填名字即可；也可 type=command 并自定义 ask_cmd/task_cmd（argv 数组，提示词会追加为最后一个参数）
DRIVER_PRESETS = {
    "codex":    {"ask_cmd": ["codex", "exec", "--sandbox", "read-only"],
                 "task_cmd": ["codex", "exec", "--sandbox", "workspace-write"]},
    "opencode": {"ask_cmd": ["opencode", "run"],
                 "task_cmd": ["opencode", "run"]},
    "qoder":    {"ask_cmd": ["qoder", "--print"],
                 "task_cmd": ["qoder", "--print"]},
    "claude":   {"ask_cmd": ["claude", "-p"],
                 "task_cmd": ["claude", "-p", "--dangerously-skip-permissions"]},
    "echo":     {},       # 测试用：直接回显问题
    "mailbox":  {},       # 信箱模式：等待 GUI/MCP agent 用 check_inbox/submit_answer 作答
    "command":  {},       # 用户自定义 ask_cmd / task_cmd
}

def driver_cmd(kind):  # kind: "ask" | "task"
    preset = DRIVER_PRESETS.get(DRIVER.get("type", "command"), {})
    return DRIVER.get(kind + "_cmd", preset.get(kind + "_cmd"))

# ---------------------------------------------------------------- 多 Agent 注册表（内存 + 磁盘 + PID 存活检测）

AGENTS = {}
AGENTS_LOCK = threading.Lock()

def slugify(text):
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", (text or "").strip()).strip("-").lower()
    return s[:24] or "agent"

def new_id(prefix):
    return f"{prefix}-{time.strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}"

def new_agent_id(name):
    return f"ag-{slugify(name)}-{secrets.token_hex(3)}"

def pid_alive(pid):
    if not pid or not isinstance(pid, int) or pid <= 0:
        return True
    # Windows 的 os.kill(pid, 0) 不可用于存活探测：对存在的进程一律抛
    # OSError EINVAL（winerror 87），不存在的进程则可能静默成功，结果完全不可信。
    # 这里改用 Win32 API：OpenProcess + GetExitCodeProcess，STILL_ACTIVE(259) 视为存活。
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1F0FFF, False, int(pid))  # PROCESS_ALL_ACCESS
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError as e:
        return e.errno == errno.EPERM

def is_agent_alive(ag):
    if ag.get("mode") == "driver":
        return True
    pid = ag.get("pid")
    if pid:
        return pid_alive(pid)
    return True

def persist_agents():
    d = Path(CFG["state_dir"])
    d.mkdir(parents=True, exist_ok=True)
    with AGENTS_LOCK:
        data = {k: v for k, v in AGENTS.items() if v.get("mode") != "driver"}
    (d / "agents.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))

def ensure_builtin_driver_agent():
    """若本机配置了非 mailbox 的 CLI/echo driver，则自动注册一个内置 driver agent。"""
    dtype = DRIVER.get("type", "command")
    if dtype == "mailbox":
        return
    aid = f"ag-driver-{slugify(dtype)}"
    skills = ["ask", "review", "file"]
    if dtype == "echo" or driver_cmd("task") or driver_cmd("ask"):
        if dtype != "echo":
            skills.append("task")
    with AGENTS_LOCK:
        AGENTS[aid] = {
            "agent_id": aid,
            "name": f"{CFG['name']}-{dtype}",
            "role": f"内置 {dtype} 驱动 Agent",
            "mode": "driver",
            "driver_type": dtype,
            "cwd": CFG["repo"],
            "pid": None,
            "skills": skills,
            "registered_at": time.time(),
            "last_seen": time.time(),
        }

def load_agents():
    update_startup_step("agents", "running", "正在恢复并校验本机已注册 Agent...")
    p = Path(CFG["state_dir"]) / "agents.json"
    restored = 0
    pruned = 0
    if p.exists():
        try:
            raw = json.loads(p.read_text())
            if isinstance(raw, dict):
                with AGENTS_LOCK:
                    for aid, ag in raw.items():
                        if isinstance(ag, dict) and is_agent_alive(ag):
                            AGENTS[aid] = ag
                            restored += 1
                        else:
                            pruned += 1
        except Exception:
            pass
    ensure_builtin_driver_agent()
    persist_agents()
    active = list_local_agents()
    update_startup_step(
        "agents", "done",
        f"在线 Agent {len(active)} 个（恢复 MCP 会话 {restored} 个，清理失效 PID {pruned} 个）"
    )

def register_local_agent(name="", role="", cwd="", pid=None, agent_id="", mode="mailbox"):
    name = (name or "").strip() or "mcp-agent"
    cwd = os.path.expanduser((cwd or "").strip()) or CFG["repo"]
    is_new = False
    with AGENTS_LOCK:
        if agent_id and agent_id in AGENTS and AGENTS[agent_id].get("mode") != "driver":
            ag = AGENTS[agent_id]
            ag.update({
                "name": name or ag.get("name", "mcp-agent"),
                "role": role if role != "" else ag.get("role", ""),
                "cwd": cwd or ag.get("cwd", CFG["repo"]),
                "pid": pid if pid is not None else ag.get("pid"),
                "last_seen": time.time(),
            })
        else:
            is_new = True
            aid = agent_id.strip() if agent_id and not agent_id.startswith("ag-driver-") else new_agent_id(name)
            ag = {
                "agent_id": aid,
                "name": name,
                "role": role or "MCP 挂载会话 Agent",
                "mode": mode if mode in ("mailbox", "driver") else "mailbox",
                "cwd": cwd,
                "pid": pid,
                "skills": ["ask", "review", "file"],
                "registered_at": time.time(),
                "last_seen": time.time(),
            }
            AGENTS[aid] = ag
    persist_agents()
    if is_new:
        record_event("AGENT", f"新挂载 MCP Agent 注册：{ag['agent_id']} ({ag['name']}, PID={pid})",
                     actor=ag["agent_id"], target=CFG["name"])
    else:
        record_event("AGENT", f"Agent 更新/重连：{ag['agent_id']} ({ag['name']})",
                     actor=ag["agent_id"], target=CFG["name"])
    return ag

def touch_agent(agent_id):
    if not agent_id:
        return None
    with AGENTS_LOCK:
        ag = AGENTS.get(agent_id)
        if ag and is_agent_alive(ag):
            ag["last_seen"] = time.time()
            return dict(ag)
    return None

def list_local_agents():
    """返回当前网关所有存活的 Agent 列表，并自动清理已退出的本地 MCP 进程。"""
    alive_list = []
    dead_ids = []
    with AGENTS_LOCK:
        for aid, ag in list(AGENTS.items()):
            if is_agent_alive(ag):
                info = dict(ag)
                info["alive"] = True
                alive_list.append(info)
            else:
                dead_ids.append(aid)
        for aid in dead_ids:
            AGENTS.pop(aid, None)
    if dead_ids:
        persist_agents()
        for aid in dead_ids:
            record_event("AGENT", f"本地 MCP 进程已退出，自动注销 Agent：{aid}", actor=aid, level="warn")
    alive_list.sort(key=lambda x: (0 if x.get("mode") == "mailbox" else 1, x.get("registered_at", 0)))
    return alive_list

def get_active_agent(agent_id):
    if not agent_id:
        return None
    for ag in list_local_agents():
        if ag["agent_id"] == agent_id:
            return ag
    return None

# ---------------------------------------------------------------- 任务存档与消息投递状态机（5阶段全链路追踪）

JOBS = {}
JOBS_LOCK = threading.Lock()

DELIVERY_STAGE_NAMES = {
    1: "① 已发出",
    2: "② 目标入箱/执行中",
    3: "③ 已作答/已完成",
    4: "④ 已回推原发送方",
    5: "⑤ 原发送方已收阅",
}

def add_job_hop(j, stage, title, actor="", detail=""):
    """为消息/任务追加一条投递轨迹节点，并推进 delivery_stage。"""
    tl = j.setdefault("timeline", [])
    now = time.time()
    tl.append({
        "stage": stage,
        "title": title,
        "actor": actor or "",
        "detail": detail or "",
        "time": now,
        "time_str": time.strftime("%H:%M:%S", time.localtime(now)),
    })
    if stage > j.get("delivery_stage", 0):
        j["delivery_stage"] = stage
    j["delivery_stage_label"] = DELIVERY_STAGE_NAMES.get(j.get("delivery_stage", 1), "处理中")
    j["updated_at"] = now

def compute_delivery_stage(j):
    """兼容历史或未显式标记的 job 计算投递阶段 (1..5)。"""
    if j.get("delivery_stage"):
        return j["delivery_stage"]
    st = j.get("status", "")
    kind = j.get("kind", "")
    if kind == "reply":
        return 5 if st == "read" else 4
    if kind == "file":
        return 5 if st in ("collected", "received") else 2
    if st in ("pending_answer", "running", "pending"):
        return 2
    if st == "done":
        if j.get("delivered_to_sender"):
            return 5
        if j.get("reply_pushed"):
            return 4
        return 3
    return 1

def save_job(j):
    j["delivery_stage"] = compute_delivery_stage(j)
    j["delivery_stage_label"] = DELIVERY_STAGE_NAMES.get(j["delivery_stage"], "处理中")
    with JOBS_LOCK:
        JOBS[j["id"]] = j
    persist_job(j)

def persist_job(j):
    d = Path(CFG["state_dir"]) / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    (d / (j["id"] + ".json")).write_text(json.dumps(j, ensure_ascii=False, indent=1))

def update_outbound_shadow(remote_job_id, stage, title, actor="", detail="", answer="", status=""):
    """当本机作为发送方网关收到远端回推或轮询结果时，同步更新本机发起方的出站追踪记录。"""
    if not remote_job_id:
        return
    target_job = None
    with JOBS_LOCK:
        for j in JOBS.values():
            if j.get("remote_job_id") == remote_job_id and j.get("direction") == "outbound":
                target_job = j
                break
        if target_job:
            if answer:
                target_job["answer"] = answer
            if status:
                target_job["status"] = status
            add_job_hop(target_job, stage, title, actor=actor, detail=detail)
    if target_job:
        persist_job(target_job)

def answer_job(jid, answer, responder_agent_id=""):
    """信箱作答：锁内原子完成归属校验、状态校验与更新，避免并发作答或越权代答。"""
    with JOBS_LOCK:
        j = JOBS.get(jid)
        if not j:
            return None, 404, "job 不存在"
        target_aid = j.get("to_agent_id")
        if target_aid and responder_agent_id and target_aid != responder_agent_id:
            record_event("AUTH", f"拦截越权作答：{responder_agent_id} 试图回答归属 {target_aid} 的问题 {jid}",
                         actor=responder_agent_id, target=target_aid, level="warn")
            return None, 403, f"该问题归属于 agent {target_aid}，当前 agent ({responder_agent_id}) 无权作答"
        if j.get("status") != "pending_answer":
            return None, 409, f"该任务状态是 {j.get('status')}，不能作答"
        j.update({
            "status": "done",
            "answer": answer,
            "answered_by": responder_agent_id or target_aid,
            "answered_at": time.time(),
        })
        add_job_hop(j, 3, "目标 Agent 已作答", actor=responder_agent_id or target_aid,
                    detail=answer[:120])
    persist_job(j)
    record_event("DELIVERY", f"Agent {responder_agent_id or target_aid} 完成作答 {jid}，准备回推至 {j.get('from_peer')}/{j.get('from_agent_id')}",
                 actor=responder_agent_id or target_aid, target=j.get("from_agent_id", ""))
    # 回推给原发送方 agent 的信箱（谁发的就谁收）
    push_reply_to_sender(j)
    return j, None, None

def get_job(jid):
    with JOBS_LOCK:
        return JOBS.get(jid)

def load_jobs():
    update_startup_step("jobs", "running", f"正在扫描 {CFG['state_dir']}/jobs ...")
    d = Path(CFG["state_dir"]) / "jobs"
    count = 0
    if d.exists():
        for f in d.glob("*.json"):
            try:
                j = json.loads(f.read_text())
                j["delivery_stage"] = compute_delivery_stage(j)
                j["delivery_stage_label"] = DELIVERY_STAGE_NAMES.get(j["delivery_stage"], "处理中")
                JOBS[f.stem] = j
                count += 1
            except Exception:
                pass
    pending_cnt = sum(1 for j in JOBS.values() if j.get("status") == "pending_answer")
    update_startup_step("jobs", "done", f"已恢复历史消息/任务 {count} 条（其中待答信箱问题 {pending_cnt} 条）")

# ---------------------------------------------------------------- 提示词信封

ASK_ENVELOPE = """有一个远程协作 agent（来自网关 {from_peer} 的 {from_agent_id}）通过网络向你提问，请基于当前目录的代码库回答。
要求：只回答，不要修改任何文件；用中文；简洁但自包含；信息不足时直接说明缺什么。
{extra}问题：
{question}"""

TASK_ENVELOPE = """你收到一个来自远程协作 agent（网关 {from_peer} 的 {from_agent_id}）的开发任务，当前目录是一个独立的 git worktree 分支。
要求：直接动手完成；完成后 git add -A && git commit（不要 push）；最后输出小结：做了什么、关键决策、如何验证。
任务标题：{title}
任务内容：
{prompt}"""

REVIEW_ENVELOPE = """你收到一个来自远程协作 agent（网关 {from_peer} 的 {from_agent_id}）的代码审查请求。先用 `gh pr view {pr}` 和 `gh pr diff {pr}` 获取 PR 内容，再给出审查意见。
要求：只输出审查意见文本（问题按严重程度排列，引用文件路径和行号）；不要修改代码、不要在 PR 里发言论。{extra}"""

# ---------------------------------------------------------------- 本地执行与目标 Agent 路由

def run_cmd(argv, cwd, timeout):
    try:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"驱动命令超时({timeout}s)：{argv[0]}"
    except FileNotFoundError:
        return None, f"找不到命令 {argv[0]}，请检查 config 里 driver 的 *_cmd"
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    if p.returncode != 0:
        return None, f"驱动命令退出码 {p.returncode}\nstderr: {err[-1500:]}\nstdout: {out[-1500:]}"
    return out or err, None

def resolve_target_agent(to_agent_id):
    """校验入站请求的目标 agent_id 是否存在且在线。"""
    agents = list_local_agents()
    if not to_agent_id:
        record_event("ERROR", "入站请求缺少目标 to_agent_id，已拒绝", level="warn")
        return None, {
            "status": "error",
            "error": (f"未指定目标 agent_id。请先在网关 '{CFG['name']}' 上调用 list_agents 选择目标 agent。"
                      f"当前在线 agents: {[a['agent_id'] for a in agents]}"),
            "available_agents": agents,
        }
    for ag in agents:
        if ag["agent_id"] == to_agent_id:
            return ag, None
    record_event("ERROR", f"目标 Agent '{to_agent_id}' 不存在或已离线，已拒绝", target=to_agent_id, level="warn")
    return None, {
        "status": "error",
        "error": (f"目标 agent '{to_agent_id}' 在网关 '{CFG['name']}' 上不存在或已离线。"
                  f"当前在线 agents: {[a['agent_id'] for a in agents]}"),
        "available_agents": agents,
    }

def find_peer_entry(from_peer="", from_url=""):
    peers = CFG.get("peers", {})
    if from_peer in peers:
        return peers[from_peer]
    for k, p in peers.items():
        if from_url and p.get("url", "").rstrip("/") == from_url.rstrip("/"):
            return p
        if from_peer and (from_peer.endswith("-" + k) or k.endswith("-" + from_peer)):
            return p
    return None

def push_reply_to_sender(job):
    """当问题作答完成或异步任务结束时，若能连上原发送方网关，主动将结果回推至原发送方 from_agent_id 的专属信箱。"""
    from_peer = job.get("from_peer")
    from_agent_id = job.get("from_agent_id")
    if not from_peer or not from_agent_id:
        return
    p = find_peer_entry(from_peer, job.get("from_url", ""))
    if not p:
        return
    payload = {
        "remote_job_id": job["id"],
        "orig_kind": job.get("kind", "ask"),
        "from_peer": CFG["name"],
        "from_agent_id": job.get("to_agent_id", ""),
        "to_agent_id": from_agent_id,
        "question": job.get("question") or job.get("title") or "",
        "answer": job.get("answer") or "",
        "error": job.get("error") or "",
        "status": job.get("status", "done"),
        "branch": job.get("branch", ""),
        "pr": job.get("pr", ""),
    }
    try:
        res, err = http_json(p["url"].rstrip("/") + "/api/reply", p.get("token", ""), payload, "POST", timeout=10)
        if not err and res:
            job["reply_pushed"] = True
            add_job_hop(job, 4, f"已回推至原发送方 {from_peer}/{from_agent_id} 专属信箱",
                        actor=CFG["name"], detail=f"reply_id={res.get('reply_id', '')}")
            save_job(job)
            record_event("DELIVERY", f"结果已自动回推至 {from_peer}/{from_agent_id} 信箱",
                         actor=job.get("to_agent_id", ""), target=f"{from_peer}/{from_agent_id}")
    except Exception:
        pass

def handle_ask(question, context="", to_agent_id="", from_peer="", from_agent_id=""):
    """:returns {"status": done|pending|error, "answer"/"job_id"/"error": ...}"""
    target, err_resp = resolve_target_agent(to_agent_id)
    if err_resp:
        return err_resp

    meta = {
        "direction": "inbound",
        "from_peer": from_peer or "unknown",
        "from_agent_id": from_agent_id or "",
        "to_peer": CFG["name"],
        "to_agent_id": target["agent_id"],
        "to_agent_name": target.get("name", ""),
    }

    # 若目标 agent 是挂载 MCP 的交互式会话（信箱模式），问题进入该 agent 的专属信箱
    if target.get("mode") == "mailbox":
        j = {
            "id": new_id("q"),
            "kind": "ask",
            "status": "pending_answer",
            "question": question,
            "context": context,
            "created": time.time(),
            **meta,
        }
        add_job_hop(j, 1, f"收到来自 {meta['from_peer']}/{meta['from_agent_id']} 的提问",
                    actor=meta["from_agent_id"], detail=question[:100])
        add_job_hop(j, 2, f"已投递至 {target['agent_id']} 专属信箱（等待作答）",
                    actor=CFG["name"], detail=f"目标 Agent: {target.get('name')} (mode=mailbox)")
        save_job(j)
        record_event("INBOUND", f"收到提问 -> 投递至 {target['agent_id']} 信箱 ({j['id']})",
                     actor=f"{meta['from_peer']}/{meta['from_agent_id']}", target=target["agent_id"],
                     detail=question[:80])
        return {
            "status": "pending",
            "job_id": j["id"],
            "to_agent_id": target["agent_id"],
            "from_agent_id": from_agent_id,
            "note": f"已投递至 {target['agent_id']} 的专属信箱，稍后用 task_status(peer, job_id) 或 check_inbox() 获取答案",
        }

    # 目标是内置 driver agent
    t = DRIVER.get("type", "command")
    j = {
        "id": new_id("q"),
        "kind": "ask",
        "status": "running",
        "question": question,
        "created": time.time(),
        **meta,
    }
    add_job_hop(j, 1, f"收到来自 {meta['from_peer']}/{meta['from_agent_id']} 的提问",
                actor=meta["from_agent_id"], detail=question[:100])
    add_job_hop(j, 2, f"正在调用内置驱动 {target['agent_id']} ({t}) 执行",
                actor=target["agent_id"])

    if t == "echo":
        ans = f"[echo by {target['agent_id']}] {question}"
        j["status"] = "done"
        j["answer"] = ans
        add_job_hop(j, 3, f"{target['agent_id']} 已生成回答", actor=target["agent_id"], detail=ans[:100])
        add_job_hop(j, 5, f"已同步返回给发起方 {meta['from_peer']}/{meta['from_agent_id']}", actor=CFG["name"])
        j["delivered_to_sender"] = True
        save_job(j)
        record_event("INBOUND", f"Echo 驱动 {target['agent_id']} 已同步回答 {meta['from_agent_id']}",
                     actor=target["agent_id"], target=meta["from_agent_id"])
        return {"status": "done", "answer": ans, "job_id": j["id"],
                "to_agent_id": target["agent_id"], "from_agent_id": from_agent_id}

    cmd = driver_cmd("ask")
    if not cmd:
        return {"status": "error", "error": f"driver 类型 {t} 未配置 ask_cmd"}
    record_event("INBOUND", f"启动 CLI 驱动 {cmd[0]} 回答来自 {meta['from_agent_id']} 的提问",
                 actor=target["agent_id"], target=meta["from_agent_id"], detail=question[:80])
    prompt = ASK_ENVELOPE.format(
        from_peer=meta["from_peer"],
        from_agent_id=meta["from_agent_id"] or "anonymous",
        extra=f"背景补充：{context}\n" if context else "",
        question=question,
    )
    out, err = run_cmd(cmd + [prompt], target.get("cwd") or CFG["repo"], CFG["ask_timeout"])
    j["status"] = "done" if out else "error"
    if out:
        j["answer"] = out
        add_job_hop(j, 3, f"CLI 驱动 {target['agent_id']} 执行完成", actor=target["agent_id"], detail=out[:100])
        add_job_hop(j, 5, f"已同步返回给发起方 {meta['from_peer']}/{meta['from_agent_id']}", actor=CFG["name"])
        j["delivered_to_sender"] = True
        record_event("DELIVERY", f"CLI 驱动 {target['agent_id']} 回答完成并已返回给 {meta['from_agent_id']}",
                     actor=target["agent_id"], target=meta["from_agent_id"])
    else:
        j["error"] = err
        add_job_hop(j, 3, f"CLI 驱动执行失败", actor=target["agent_id"], detail=str(err)[:120])
        record_event("ERROR", f"CLI 驱动执行失败：{err}", actor=target["agent_id"], level="error")
    save_job(j)
    return {"status": j["status"], "answer": out, "error": err, "job_id": j["id"],
            "to_agent_id": target["agent_id"], "from_agent_id": from_agent_id}

def handle_review(pr, focus="", to_agent_id="", from_peer="", from_agent_id=""):
    target, err_resp = resolve_target_agent(to_agent_id)
    if err_resp:
        return err_resp

    meta = {
        "direction": "inbound",
        "from_peer": from_peer or "unknown",
        "from_agent_id": from_agent_id or "",
        "to_peer": CFG["name"],
        "to_agent_id": target["agent_id"],
        "to_agent_name": target.get("name", ""),
    }

    if target.get("mode") == "mailbox":
        j = {
            "id": new_id("r"),
            "kind": "review",
            "status": "pending_answer",
            "question": f"审查 PR #{pr}" + (f"（重点：{focus}）" if focus else ""),
            "pr": pr,
            "created": time.time(),
            **meta,
        }
        add_job_hop(j, 1, f"收到来自 {meta['from_peer']}/{meta['from_agent_id']} 的 PR #{pr} 审查请求",
                    actor=meta["from_agent_id"])
        add_job_hop(j, 2, f"已投递至 {target['agent_id']} 专属信箱（等待审查）", actor=CFG["name"])
        save_job(j)
        record_event("INBOUND", f"收到 PR #{pr} 审查请求 -> 投递至 {target['agent_id']} 信箱",
                     actor=meta["from_agent_id"], target=target["agent_id"])
        return {
            "status": "pending",
            "job_id": j["id"],
            "to_agent_id": target["agent_id"],
            "from_agent_id": from_agent_id,
            "note": f"已投递至 {target['agent_id']} 的专属信箱，稍后用 task_status(peer, job_id) 或 check_inbox() 轮询",
        }

    t = DRIVER.get("type", "command")
    j = {
        "id": new_id("r"),
        "kind": "review",
        "pr": pr,
        "status": "running",
        "question": f"审查 PR #{pr}",
        "created": time.time(),
        **meta,
    }
    add_job_hop(j, 1, f"收到 PR #{pr} 审查请求", actor=meta["from_agent_id"])
    add_job_hop(j, 2, f"正在由 {target['agent_id']} 执行审查", actor=target["agent_id"])
    if t == "echo":
        ans = f"[echo by {target['agent_id']}] review pr#{pr}"
        j["status"] = "done"
        j["answer"] = ans
        j["delivered_to_sender"] = True
        add_job_hop(j, 3, "审查完成", actor=target["agent_id"], detail=ans)
        add_job_hop(j, 5, "已同步返回审查意见", actor=CFG["name"])
        save_job(j)
        return {"status": "done", "answer": ans, "job_id": j["id"],
                "to_agent_id": target["agent_id"], "from_agent_id": from_agent_id}

    cmd = driver_cmd("ask")
    if not cmd:
        return {"status": "error", "error": f"driver 类型 {t} 未配置 ask_cmd"}
    prompt = REVIEW_ENVELOPE.format(
        from_peer=meta["from_peer"],
        from_agent_id=meta["from_agent_id"] or "anonymous",
        pr=pr,
        extra=f"\n重点关注：{focus}" if focus else "",
    )
    out, err = run_cmd(cmd + [prompt], target.get("cwd") or CFG["repo"], CFG["ask_timeout"])
    j["status"] = "done" if out else "error"
    if out:
        j["answer"] = out
        j["delivered_to_sender"] = True
        add_job_hop(j, 3, "PR 审查完成", actor=target["agent_id"], detail=out[:100])
        add_job_hop(j, 5, "已同步返回给发起方", actor=CFG["name"])
    else:
        j["error"] = err
        add_job_hop(j, 3, "PR 审查失败", actor=target["agent_id"], detail=str(err)[:120])
    save_job(j)
    return {"status": j["status"], "answer": out, "error": err, "job_id": j["id"],
            "to_agent_id": target["agent_id"], "from_agent_id": from_agent_id}

def git(args, cwd, ok_fail=False):
    p = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0 and not ok_fail:
        raise RuntimeError(f"git {' '.join(args)} 失败：{p.stderr.strip()[:500]}")
    return p

def default_base():
    p = git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], CFG["repo"], ok_fail=True)
    if p.returncode == 0:
        return p.stdout.strip().replace("origin/", "")
    for b in ("main", "master"):
        if git(["rev-parse", "--verify", f"origin/{b}"], CFG["repo"], ok_fail=True).returncode == 0:
            return b
    return "HEAD"

def cleanup_worktree(worktree):
    """任务成功后摘除 worktree；分支和 commit 都留在主仓库，内容不丢。"""
    git(["worktree", "remove", "--force", worktree], CFG["repo"], ok_fail=True)
    git(["worktree", "prune"], CFG["repo"], ok_fail=True)

def task_runner(job, worktree, branch):
    try:
        cmd = driver_cmd("task") or driver_cmd("ask")
        record_event("WORKTREE", f"开始在独立分支 {branch} 执行任务 {job['id']} ({job['title']})",
                     actor=job.get("to_agent_id", ""), target=branch)
        prompt = TASK_ENVELOPE.format(
            from_peer=job.get("from_peer", "unknown"),
            from_agent_id=job.get("from_agent_id", "anonymous"),
            title=job["title"],
            prompt=job["prompt"],
        )
        out, err = run_cmd(cmd + [prompt], worktree, CFG["task_timeout"])
        if err:
            raise RuntimeError(err)
        git(["add", "-A"], worktree, ok_fail=True)
        git(["commit", "-m", f"troupe {job['id']}: {job['title']}"], worktree, ok_fail=True)
        pushed = git(["push", "origin", f"{branch}:{branch}"], worktree, ok_fail=True).returncode == 0
        job.update({"status": "done", "answer": out, "pushed": pushed})
        add_job_hop(job, 3, f"Worktree 任务执行完成并已 Commit (branch={branch})",
                    actor=job.get("to_agent_id", ""), detail=out[:120])
        if CFG["open_pr"] and pushed:
            pr = subprocess.run(["gh", "pr", "create", "--head", branch,
                                 "--title", f"[troupe] {job['title']}", "--body", out[:60000]],
                                cwd=worktree, capture_output=True, text=True)
            if pr.returncode == 0:
                job["pr"] = pr.stdout.strip()
        record_event("WORKTREE", f"任务 {job['id']} 完成并提交至分支 {branch}",
                     actor=job.get("to_agent_id", ""), target=job.get("from_agent_id", ""))
    except Exception as e:
        job.update({"status": "error", "error": str(e)[-1500:]})
        add_job_hop(job, 3, f"Worktree 任务执行异常", actor=job.get("to_agent_id", ""), detail=str(e)[:120])
        record_event("ERROR", f"任务 {job['id']} 失败：{str(e)[:120]}",
                     actor=job.get("to_agent_id", ""), level="error")
    save_job(job)
    push_reply_to_sender(job)
    if job["status"] == "done":  # 失败时保留 worktree 现场供排查
        cleanup_worktree(worktree)

def handle_task(title, prompt, base="", to_agent_id="", from_peer="", from_agent_id=""):
    target, err_resp = resolve_target_agent(to_agent_id)
    if err_resp:
        return err_resp

    if target.get("mode") == "mailbox":
        return {"status": "error", "error": f"目标 agent ({to_agent_id}) 为信箱模式，暂不支持直接跑 worktree task（请改用 ask/review 或选择 mode=driver 的 agent）"}
    if not (driver_cmd("task") or driver_cmd("ask")):
        return {"status": "error", "error": f"driver 类型 {DRIVER.get('type')} 未配置 task_cmd"}
    tid = new_id("t")
    branch = f"agent/{tid}"
    worktree = str(Path(CFG["state_dir"]) / "worktrees" / f"agent-{tid}")
    Path(worktree).parent.mkdir(parents=True, exist_ok=True)
    try:
        git(["fetch", "origin"], CFG["repo"], ok_fail=True)
        git(["worktree", "add", "-b", branch, worktree, base or default_base()], CFG["repo"])
    except Exception as e:
        return {"status": "error", "error": f"创建 worktree 失败：{e}"}
    j = {
        "id": tid,
        "kind": "task",
        "direction": "inbound",
        "status": "running",
        "title": title,
        "prompt": prompt,
        "branch": branch,
        "worktree": worktree,
        "from_peer": from_peer or "unknown",
        "from_agent_id": from_agent_id or "",
        "to_peer": CFG["name"],
        "to_agent_id": target["agent_id"],
        "created": time.time(),
    }
    add_job_hop(j, 1, f"收到来自 {j['from_peer']}/{j['from_agent_id']} 的编码任务：{title}",
                actor=j["from_agent_id"])
    add_job_hop(j, 2, f"已创建隔离 Git Worktree ({branch}) 并启动后台执行",
                actor=target["agent_id"], detail=worktree)
    save_job(j)
    threading.Thread(target=task_runner, args=(j, worktree, branch), daemon=True).start()
    return {
        "status": "running",
        "job_id": tid,
        "branch": branch,
        "to_agent_id": target["agent_id"],
        "from_agent_id": from_agent_id,
        "note": "用 task_status(peer, job_id) 或 check_inbox() 轮询",
    }

# ---------------------------------------------------------------- 出站调用（→ peer 网关）

def http_json(url, token, payload=None, method="POST", timeout=300, raw=None):
    req = urllib.request.Request(url, method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    data = None
    if raw is not None:
        data = raw
        req.add_header("Content-Type", "application/octet-stream")
    elif payload is not None:
        data = json.dumps(payload).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}"), None
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode()[:300]
        except Exception:
            body = ""
        return None, f"HTTP {e.code}: {body}"
    except Exception as e:
        return None, f"无法连接 {url}：{e}"

def http_get_bytes(url, token, timeout=600):
    """:returns (body_bytes, headers, error)"""
    req = urllib.request.Request(url, method="GET")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), r.headers, None
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode()[:300]
        except Exception:
            body = ""
        return None, None, f"HTTP {e.code}: {body}"
    except Exception as e:
        return None, None, f"无法连接 {url}：{e}"

def peer_call(peer_name, path, payload=None, method="POST", timeout=None):
    peers = CFG.get("peers", {})
    if peer_name not in peers:
        return {"error": f"未配置的 peer：{peer_name}（已配置：{list(peers) or '无'}）"}
    p = peers[peer_name]
    res, err = http_json(p["url"].rstrip("/") + path, p.get("token", ""),
                         payload, method, timeout or CFG["ask_timeout"] + 60)
    return res if res is not None else {"error": err}

def peers_info():
    out = {}
    for name, p in CFG.get("peers", {}).items():
        card, err = http_json(p["url"].rstrip("/") + "/.well-known/agent-card.json",
                              p.get("token", ""), None, "GET", 8)
        if err:
            out[name] = {"url": p["url"], "reachable": False, "error": err}
        else:
            out[name] = {
                "url": p["url"],
                "reachable": True,
                "gateway": card.get("name"),
                "description": card.get("description"),
                "skills": [s.get("id") for s in card.get("skills", [])],
                "agents": card.get("agents", []),
                "agents_count": len(card.get("agents", [])),
                "next_step": f"请调用 list_agents(peer='{name}') 获取并选择该网关下的具体 agent_id",
            }
    return out

def probe_peers_on_startup():
    peers = CFG.get("peers", {})
    if not peers:
        update_startup_step("peers", "warn", "当前未配置远端 Peer 网关（可在 App 内扫描或手动添加）")
        return
    update_startup_step("peers", "running", f"正在探测 {len(peers)} 个远端 Peer 网关...")
    info = peers_info()
    ok_cnt = sum(1 for v in info.values() if v.get("reachable"))
    total_remote_agents = sum(v.get("agents_count", 0) for v in info.values() if v.get("reachable"))
    status = "done" if ok_cnt == len(peers) else ("warn" if ok_cnt > 0 else "warn")
    update_startup_step(
        "peers", status,
        f"已探测 {len(peers)} 个网关：在线 {ok_cnt} 个，共发现远端 Agent {total_remote_agents} 个"
    )

def peer_agents_info(peer_name):
    if not peer_name or peer_name in ("self", "local", CFG["name"]):
        return {"peer": CFG["name"], "gateway": CFG["name"], "agents": list_local_agents()}
    res = peer_call(peer_name, "/api/agents", None, "GET", timeout=15)
    if "error" in res:
        return res
    res["peer"] = peer_name
    record_event("OUTBOUND", f"查询网关 '{peer_name}' 下的在线 Agent 列表（共 {len(res.get('agents', []))} 个）",
                 actor=CFG["name"], target=peer_name)
    return res

def validate_outbound_request(body, require_target_agent=True):
    """校验出站请求的发送方身份 (from_agent_id) 与目标选择 (peer + to_agent_id)。"""
    from_agent_id = (body.get("from_agent_id") or "").strip()
    if not from_agent_id:
        return None, None, "缺少发送方身份 from_agent_id，请确保通过已注册的 MCP 会话发起调用"
    touch_agent(from_agent_id)
    if not require_target_agent:
        return from_agent_id, None, None
    to_agent_id = (body.get("to_agent_id") or "").strip()
    peer = (body.get("peer") or "").strip()
    if not peer:
        return None, None, "必须先选择目标 gateway（peer 参数）。请先调用 list_peers()"
    if not to_agent_id:
        return None, None, f"必须指定目标 gateway '{peer}' 下的 agent_id。请先调用 list_agents(peer='{peer}') 选择目标 agent"
    return from_agent_id, to_agent_id, None

def record_outbound_job(kind, peer, from_aid, to_aid, title_or_q, res):
    """在本机（发送方网关）记录出站消息投递状态，方便发送方在 App 内实时追踪投递进度。"""
    remote_jid = (res or {}).get("job_id") or (res or {}).get("file_id") or ""
    st = (res or {}).get("status") or ("error" if "error" in (res or {}) else "done")
    oj = {
        "id": f"out-{remote_jid}" if remote_jid else new_id("out"),
        "remote_job_id": remote_jid,
        "kind": kind,
        "direction": "outbound",
        "status": st,
        "from_peer": CFG["name"],
        "from_agent_id": from_aid,
        "to_peer": peer,
        "to_agent_id": to_aid,
        "question": title_or_q,
        "answer": (res or {}).get("answer", ""),
        "error": (res or {}).get("error", ""),
        "branch": (res or {}).get("branch", ""),
        "created": time.time(),
    }
    add_job_hop(oj, 1, f"本机 {from_aid} 向 {peer}/{to_aid} 发出 {kind} 请求",
                actor=from_aid, detail=title_or_q[:100])
    if "error" in (res or {}) or st == "error":
        add_job_hop(oj, 1, f"投递失败：{oj['error']}", actor=peer, detail=oj["error"][:120])
    elif st in ("pending", "running"):
        add_job_hop(oj, 2, f"已送达 {peer}/{to_aid}（等待对方作答/执行）",
                    actor=f"{peer}/{to_aid}", detail=f"远端单号: {remote_jid}")
    elif st == "done":
        add_job_hop(oj, 2, f"已送达 {peer}/{to_aid}", actor=f"{peer}/{to_aid}")
        add_job_hop(oj, 3, f"对方已完成处理", actor=f"{peer}/{to_aid}", detail=oj["answer"][:100])
        add_job_hop(oj, 5, f"发起方 {from_aid} 已同步收到返回结果", actor=from_aid)
        oj["delivered_to_sender"] = True
    save_job(oj)
    return oj

# ---------------------------------------------------------------- 文件传输（state_dir/incoming 落盘，按 agent_id 隔离）

def incoming_dir():
    d = Path(CFG["state_dir"]) / "incoming"
    d.mkdir(parents=True, exist_ok=True)
    return d

def save_dest(save_path, fname):
    """解析取回文件的落盘路径：空=项目目录用原名；以 / 结尾或指向已有目录=该目录下用原名；重名自动加序号。"""
    sp = (save_path or "").strip()
    base = Path(os.path.expanduser(sp)) if sp else Path(CFG["repo"])
    if not base.is_absolute():
        base = Path(CFG["repo"]) / base
    if sp.endswith("/") or base.is_dir():
        base.mkdir(parents=True, exist_ok=True)
        base = base / fname
    i = 1
    while base.exists():
        base = base.with_name(f"{base.stem}-{i}{base.suffix}")
        i += 1
    return base

def file_send(peer_name, to_agent_id, local_path, note="", from_agent_id=""):
    peers = CFG.get("peers", {})
    p = peers.get(peer_name)
    if not p:
        return {"error": f"未配置的 peer：{peer_name}（已配置：{list(peers) or '无'}）"}
    fp = Path(os.path.expanduser(local_path))
    if not fp.is_absolute():
        fp = Path(CFG["repo"]) / fp
    if not fp.is_file():
        return {"error": f"文件不存在：{local_path}"}
    max_bytes = CFG["file_max_mb"] * 1024 * 1024
    if fp.stat().st_size > max_bytes:
        return {"error": f"文件超过大小上限 {CFG['file_max_mb']}MB（config 的 file_max_mb 可调）"}
    params = {
        "name": fp.name,
        "to_agent_id": to_agent_id,
        "from_peer": CFG["name"],
        "from_agent_id": from_agent_id,
    }
    if note:
        params["note"] = note[:500]
    qs = urllib.parse.urlencode(params)
    res, err = http_json(p["url"].rstrip("/") + "/api/file/upload?" + qs,
                         p.get("token", ""), None, "POST", timeout=600, raw=fp.read_bytes())
    rfid = (res or {}).get("file_id")
    j = {
        "id": f"out-{rfid}" if rfid else new_id("f"),
        "remote_job_id": rfid,
        "kind": "file",
        "direction": "outbound",
        "status": "error" if err else "sent",
        "peer": peer_name,
        "from_peer": CFG["name"],
        "from_agent_id": from_agent_id,
        "to_peer": peer_name,
        "to_agent_id": to_agent_id,
        "filename": fp.name,
        "question": f"[文件] {fp.name} ({fp.stat().st_size} B)",
        "size": fp.stat().st_size,
        "note": note,
        "remote_file_id": rfid,
        "created": time.time(),
    }
    add_job_hop(j, 1, f"{from_agent_id} 发送文件 {fp.name} -> {peer_name}/{to_agent_id}",
                actor=from_agent_id, detail=note)
    if err:
        j["error"] = err
        add_job_hop(j, 1, f"文件上传失败：{err}", actor=peer_name)
        record_event("ERROR", f"文件 {fp.name} 发送失败：{err}", actor=from_agent_id, level="error")
    else:
        add_job_hop(j, 2, f"文件已存入 {peer_name} 代收区，等待 {to_agent_id} 收取",
                    actor=peer_name, detail=f"file_id={rfid}")
        record_event("OUTBOUND", f"{from_agent_id} 发送文件 {fp.name} 至 {peer_name}/{to_agent_id} ({rfid})",
                     actor=from_agent_id, target=f"{peer_name}/{to_agent_id}")
    save_job(j)
    if err:
        return {"error": err}
    return {
        "status": "done",
        "peer": peer_name,
        "to_agent_id": to_agent_id,
        "from_agent_id": from_agent_id,
        "file_id": rfid,
        "filename": fp.name,
        "size": j["size"],
        "note": f"已发给 {peer_name}/{to_agent_id}；仅目标 agent 可通过 check_inbox() / fetch_file() 收取",
    }

def file_fetch(peer_name, file_id, save_path="", requester_agent_id=""):
    peers = CFG.get("peers", {})
    p = peers.get(peer_name)
    if not p:
        return {"error": f"未配置的 peer：{peer_name}（已配置：{list(peers) or '无'}）"}
    qs = urllib.parse.urlencode({"agent_id": requester_agent_id}) if requester_agent_id else ""
    url = p["url"].rstrip("/") + f"/api/file/{file_id}" + (f"?{qs}" if qs else "")
    data, hdrs, err = http_get_bytes(url, p.get("token", ""))
    if err:
        return {"error": err}
    fname = urllib.parse.unquote(hdrs.get("X-File-Name") or "").strip() or "file"
    dest = save_dest(save_path, fname)
    dest.write_bytes(data)
    j = {
        "id": new_id("f"),
        "kind": "file",
        "direction": "inbound",
        "status": "received",
        "peer": peer_name,
        "from_peer": peer_name,
        "to_peer": CFG["name"],
        "to_agent_id": requester_agent_id,
        "filename": dest.name,
        "question": f"[拉取文件] {dest.name}",
        "size": len(data),
        "saved_to": str(dest),
        "remote_file_id": file_id,
        "delivered_to_sender": True,
        "created": time.time(),
    }
    add_job_hop(j, 5, f"{requester_agent_id} 已从 {peer_name} 拉取文件并落盘",
                actor=requester_agent_id, detail=str(dest))
    save_job(j)
    record_event("DELIVERY", f"{requester_agent_id} 已拉取文件 {dest.name} -> {dest}",
                 actor=requester_agent_id, target=peer_name)
    return {"status": "done", "saved_to": str(dest), "size": len(data)}

# ---------------------------------------------------------------- A2A 兼容子集 + Agent Card

def agent_card():
    host, port = CFG["listen"]
    base = CFG.get("public_url") or f"http://{host}:{port}"
    agents = list_local_agents()
    return {
        "name": CFG["name"],
        "description": f"troupe gateway（driver={DRIVER.get('type')}, online_agents={len(agents)}）",
        "url": base + "/a2a",
        "version": "0.2.0",
        "protocolVersion": "0.3.0",
        "capabilities": {"chat": True, "multiAgent": True},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "agents": [{"agent_id": a["agent_id"], "name": a["name"], "mode": a["mode"], "role": a.get("role", "")}
                   for a in agents],
        "skills": [
            {"id": "ask", "name": "ask", "description": "向指定 agent 提问（只读），返回回答"},
            {"id": "review", "name": "review", "description": "请指定 agent 审查 GitHub PR，返回意见文本"},
            {"id": "task", "name": "task", "description": "向指定 agent 派发编码任务，在独立 worktree 分支执行"},
            {"id": "file", "name": "file", "description": "向指定 agent 收发文件"},
        ],
        "authentication": {"schemes": ["Bearer"]},
        "preferredTransport": "JSONRPC",
    }

def a2a_handle(req):
    method, rid = req.get("method"), req.get("id")

    def ok(result):
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def errp(code, msg):
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}

    if method in ("message/send", "tasks/send", "message/sendStreaming"):
        params = req.get("params") or {}
        msg = params.get("message") or {}
        text = ""
        for part in msg.get("parts", []):
            if part.get("kind") == "text" or "text" in part:
                text += part.get("text", "")
        meta = msg.get("metadata") or {}
        to_aid = params.get("to_agent_id") or meta.get("to_agent_id") or ""
        from_aid = params.get("from_agent_id") or meta.get("from_agent_id") or "a2a-client"
        if not to_aid:
            agents = list_local_agents()
            if agents:
                to_aid = agents[0]["agent_id"]
        r = handle_ask(text, to_agent_id=to_aid, from_peer="a2a", from_agent_id=from_aid)
        ans = r.get("answer") or json.dumps(r, ensure_ascii=False)
        jid = r.get("job_id") or new_id("q")
        return ok({"kind": "message", "role": "agent", "messageId": new_id("m"),
                   "taskId": jid, "contextId": jid,
                   "parts": [{"kind": "text", "text": ans}]})
    if method in ("tasks/get", "message/get"):
        params = req.get("params") or {}
        jid = params.get("id") or params.get("taskId")
        j = get_job(jid)
        if not j:
            return errp(-32001, "task not found")
        state = {"done": "completed", "error": "failed",
                 "running": "working", "pending_answer": "input-required"}.get(j["status"], "working")
        return ok({"kind": "task", "id": jid, "status": {"state": state},
                   "artifacts": [{"parts": [{"kind": "text",
                                             "text": j.get("answer") or j.get("error") or ""}]}]})
    return errp(-32601, f"method not found: {method}")

# ---------------------------------------------------------------- HTTP 服务

class Handler(BaseHTTPRequestHandler):
    server_version = "troupe/0.2"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[{time.strftime('%m-%d %H:%M:%S')}] {fmt % args}\n")

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authed(self):
        if not CFG.get("token"):
            return True
        return self.headers.get("Authorization", "") == "Bearer " + CFG["token"]

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode() if n else ""
        return json.loads(raw) if raw.strip() else {}

    def query_params(self):
        return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items() if v}

    def serve_file(self, fid, requester_agent_id=""):
        j = get_job(fid)
        if not j or j.get("kind") != "file" or j.get("status") != "ready":
            return self.send_json(404, {"error": "文件不存在"})
        allowed = {x for x in (j.get("to_agent_id"), j.get("from_agent_id")) if x}
        if allowed and requester_agent_id and requester_agent_id not in allowed:
            record_event("AUTH", f"拦截越权拉取文件：{requester_agent_id} 尝试拉取 {fid}",
                         actor=requester_agent_id, level="warn")
            return self.send_json(403, {"error": f"文件归属于 {allowed}，当前 agent ({requester_agent_id}) 无权拉取"})
        fp = incoming_dir() / fid / j["filename"]
        if not fp.is_file():
            return self.send_json(404, {"error": "文件已不在磁盘上"})
        data = fp.read_bytes()
        quoted = urllib.parse.quote(j["filename"])
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quoted}")
        self.send_header("X-File-Name", quoted)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def recv_file(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        name = os.path.basename(qs.get("name", [""])[0].replace("\\", "/")).strip()[:200]
        note = (qs.get("note", [""])[0])[:500]
        to_agent_id = (qs.get("to_agent_id", [""])[0]).strip()
        from_peer = (qs.get("from_peer", [""])[0]).strip()
        from_agent_id = (qs.get("from_agent_id", [""])[0]).strip()
        if not name:
            return self.send_json(400, {"error": "缺少 name 查询参数（文件名）"})
        target, err_resp = resolve_target_agent(to_agent_id)
        if err_resp:
            return self.send_json(400, err_resp)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        max_bytes = CFG["file_max_mb"] * 1024 * 1024
        if n <= 0:
            return self.send_json(400, {"error": "请求体为空（应为原始文件字节流）"})
        if n > max_bytes:
            return self.send_json(413, {"error": f"文件超过大小上限 {CFG['file_max_mb']}MB"})
        fid = new_id("f")
        d = incoming_dir() / fid
        d.mkdir(parents=True, exist_ok=True)
        data = self.rfile.read(n)
        (d / name).write_bytes(data)
        j = {
            "id": fid,
            "kind": "file",
            "direction": "inbound",
            "status": "ready",
            "filename": name,
            "question": f"[收到文件] {name} ({n} B)",
            "size": n,
            "note": note,
            "to_peer": CFG["name"],
            "to_agent_id": target["agent_id"],
            "from_peer": from_peer,
            "from_agent_id": from_agent_id,
            "created": time.time(),
        }
        add_job_hop(j, 1, f"收到来自 {from_peer}/{from_agent_id} 的文件 {name}",
                    actor=from_agent_id, detail=note)
        add_job_hop(j, 2, f"已代收至本机 incoming 区，等待 {target['agent_id']} 收取",
                    actor=CFG["name"], detail=f"file_id={fid}")
        save_job(j)
        record_event("INBOUND", f"代收文件 {name} ({n}B) -> 归属 {target['agent_id']}",
                     actor=f"{from_peer}/{from_agent_id}", target=target["agent_id"])
        return self.send_json(200, {
            "status": "ready",
            "file_id": fid,
            "filename": name,
            "size": n,
            "to_agent_id": target["agent_id"],
        })

    def collect_file(self, fid, save_path, collector_agent_id=""):
        j = get_job(fid)
        if not j or j.get("kind") != "file" or j.get("status") != "ready":
            return self.send_json(404, {"error": "文件不存在或已收取"})
        target_aid = j.get("to_agent_id")
        if target_aid and collector_agent_id and target_aid != collector_agent_id:
            record_event("AUTH", f"拦截越权收取文件：{collector_agent_id} 尝试收取发给 {target_aid} 的文件 {fid}",
                         actor=collector_agent_id, target=target_aid, level="warn")
            return self.send_json(403, {"error": f"该文件发给 {target_aid}，当前 agent ({collector_agent_id}) 无权收取"})
        src = incoming_dir() / fid / j["filename"]
        if not src.is_file():
            return self.send_json(404, {"error": "文件已不在磁盘上"})
        dest = save_dest(save_path, j["filename"])
        data = src.read_bytes()
        dest.write_bytes(data)
        try:  # 收走后代收区不再保留
            src.unlink()
            src.parent.rmdir()
        except OSError:
            pass
        j.update({
            "status": "collected",
            "saved_to": str(dest),
            "collected_by": collector_agent_id or target_aid,
            "collected_at": time.time(),
            "delivered_to_sender": True,
        })
        add_job_hop(j, 5, f"目标 Agent {collector_agent_id or target_aid} 已收取文件并落盘",
                    actor=collector_agent_id or target_aid, detail=str(dest))
        save_job(j)
        record_event("DELIVERY", f"Agent {collector_agent_id or target_aid} 已收取文件 {j['filename']} -> {dest}",
                     actor=collector_agent_id or target_aid, target=str(dest))
        return self.send_json(200, {"status": "done", "saved_to": str(dest), "size": len(data)})

    def do_GET(self):
        path = self.path.split("?")[0]
        qp = self.query_params()
        if path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
            return self.send_json(200, agent_card())
        if not self.authed():
            return self.send_json(401, {"error": "unauthorized"})
        if path == "/api/status":
            return self.send_json(200, {
                "name": CFG["name"],
                "driver": DRIVER.get("type"),
                "repo": CFG["repo"],
                "peers": list(CFG.get("peers", {})),
                "agents": list_local_agents(),
                "startup_steps": get_startup_steps(),
                "time": time.strftime("%F %T"),
            })
        if path == "/api/agents":
            return self.send_json(200, {
                "gateway": CFG["name"],
                "agents": list_local_agents(),
            })
        if path == "/api/peers":
            return self.send_json(200, peers_info())
        if path == "/api/inbox":
            aid = (qp.get("agent_id") or "").strip()
            touch_agent(aid)
            # 严格按 agent_id 隔离：谁被提问/谁被发文件/谁收到回执，就谁收
            pending = [
                j for j in list(JOBS.values())
                if j.get("status") == "pending_answer" and (not aid or j.get("to_agent_id") == aid)
            ]
            new_files = [
                {
                    "id": j["id"],
                    "filename": j.get("filename"),
                    "size": j.get("size"),
                    "note": j.get("note", ""),
                    "from_peer": j.get("from_peer", ""),
                    "from_agent_id": j.get("from_agent_id", ""),
                    "to_agent_id": j.get("to_agent_id", ""),
                    "created": j.get("created", 0),
                }
                for j in list(JOBS.values())
                if j.get("kind") == "file" and j.get("status") == "ready"
                and (not aid or j.get("to_agent_id") == aid)
            ]
            replies = [
                j for j in list(JOBS.values())
                if j.get("kind") == "reply" and j.get("status") == "unread"
                and (not aid or j.get("to_agent_id") == aid)
            ]
            for r in replies:
                r["status"] = "read"
                r["delivered_to_sender"] = True
                add_job_hop(r, 5, f"原发送方 {aid or r.get('to_agent_id')} 已通过 check_inbox 收阅回复",
                            actor=aid or r.get("to_agent_id", ""))
                persist_job(r)
                update_outbound_shadow(
                    r.get("remote_job_id", ""), 5,
                    f"原发送方 {aid or r.get('to_agent_id')} 已收阅回复",
                    actor=aid or r.get("to_agent_id", ""),
                    answer=r.get("answer", ""), status="done"
                )
            return self.send_json(200, {
                "agent_id": aid,
                "questions": sorted(pending, key=lambda j: j.get("created", 0)),
                "replies": sorted(replies, key=lambda j: j.get("created", 0)),
                "files": sorted(new_files, key=lambda f: f["created"], reverse=True),
            })
        if path == "/api/files":
            aid = (qp.get("agent_id") or "").strip()
            files = [
                {
                    "id": j["id"],
                    "filename": j.get("filename"),
                    "size": j.get("size"),
                    "note": j.get("note", ""),
                    "from_peer": j.get("from_peer", ""),
                    "from_agent_id": j.get("from_agent_id", ""),
                    "to_agent_id": j.get("to_agent_id", ""),
                    "created": j.get("created", 0),
                }
                for j in list(JOBS.values())
                if j.get("kind") == "file" and j.get("status") == "ready"
                and (not aid or j.get("to_agent_id") == aid or j.get("from_agent_id") == aid)
            ]
            return self.send_json(200, {"files": sorted(files, key=lambda f: f["created"], reverse=True)})
        if path == "/api/jobs":
            aid = (qp.get("agent_id") or "").strip()
            js = [
                j for j in list(JOBS.values())
                if not aid or j.get("from_agent_id") == aid or j.get("to_agent_id") == aid
            ]
            js = sorted(js, key=lambda j: j.get("created", 0), reverse=True)[:50]
            return self.send_json(200, [{
                "id": j["id"],
                "kind": j.get("kind"),
                "direction": j.get("direction", "inbound"),
                "status": j.get("status"),
                "delivery_stage": compute_delivery_stage(j),
                "delivery_stage_label": DELIVERY_STAGE_NAMES.get(compute_delivery_stage(j), ""),
                "from_peer": j.get("from_peer"),
                "from_agent_id": j.get("from_agent_id"),
                "to_peer": j.get("to_peer", CFG["name"]),
                "to_agent_id": j.get("to_agent_id"),
                "title": (j.get("title") or j.get("question") or j.get("filename", ""))[:60],
                "branch": j.get("branch"),
            } for j in js])
        m = re.match(r"^/api/file/([\w.-]+)$", path)
        if m:
            return self.serve_file(m.group(1), qp.get("agent_id", ""))
        m = re.match(r"^/api/jobs/([\w.-]+)$", path)
        if m:
            j = get_job(m.group(1))
            if not j:
                return self.send_json(404, {"error": "job 不存在"})
            req_aid = (qp.get("agent_id") or "").strip()
            owner_aid = j.get("from_agent_id") or ""
            target_aid = j.get("to_agent_id") or ""
            # 校验“谁发的就谁收”：若任务绑定了发起方/接收方 agent_id，非相关 agent 禁止窥探
            if req_aid and owner_aid and req_aid not in (owner_aid, target_aid):
                record_event("AUTH", f"拦截越权查询：{req_aid} 尝试查询归属 {owner_aid} 的任务 {j['id']}",
                             actor=req_aid, target=owner_aid, level="warn")
                return self.send_json(403, {
                    "error": f"该会话由 agent '{owner_aid}' 发起并归属其接收，当前 agent '{req_aid}' 无权查询"
                })
            if req_aid and req_aid == owner_aid and j.get("status") == "done" and not j.get("delivered_to_sender"):
                j["delivered_to_sender"] = True
                add_job_hop(j, 5, f"原发送方 {req_aid} 已通过 task_status 获取结果", actor=req_aid)
                save_job(j)
            return self.send_json(200, j)
        self.send_json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if not self.authed():
            return self.send_json(401, {"error": "unauthorized"})
        if path == "/api/file/upload":  # 原始字节流，不走 JSON 解析
            return self.recv_file()
        try:
            body = self.read_body()
        except json.JSONDecodeError:
            return self.send_json(400, {"error": "请求体不是合法 JSON"})

        if path == "/a2a":
            try:
                return self.send_json(200, a2a_handle(body))
            except Exception as e:
                return self.send_json(200, {"jsonrpc": "2.0", "id": body.get("id"),
                                            "error": {"code": -32603, "message": str(e)[:300]}})

        # ---- 本机 MCP Agent 注册与管理 ----
        if path == "/api/agents/register":
            ag = register_local_agent(
                name=body.get("name", ""),
                role=body.get("role", ""),
                cwd=body.get("cwd", ""),
                pid=body.get("pid"),
                agent_id=body.get("agent_id", ""),
                mode=body.get("mode", "mailbox"),
            )
            return self.send_json(200, {"gateway": CFG["name"], "agent": ag})

        # ---- 入站：由 peer 网关调用，在本机目标 agent 上执行 ----
        if path == "/api/ask":
            return self.send_json(200, handle_ask(
                body.get("question", ""),
                body.get("context", ""),
                to_agent_id=body.get("to_agent_id", ""),
                from_peer=body.get("from_peer", ""),
                from_agent_id=body.get("from_agent_id", ""),
            ))
        if path == "/api/review":
            try:
                pr = int(body.get("pr", 0))
            except (TypeError, ValueError):
                return self.send_json(400, {"error": f"pr 必须是数字（收到 {body.get('pr')!r}）"})
            return self.send_json(200, handle_review(
                pr,
                body.get("focus", ""),
                to_agent_id=body.get("to_agent_id", ""),
                from_peer=body.get("from_peer", ""),
                from_agent_id=body.get("from_agent_id", ""),
            ))
        if path == "/api/task":
            return self.send_json(200, handle_task(
                body.get("title", "untitled"),
                body.get("prompt", ""),
                body.get("base", ""),
                to_agent_id=body.get("to_agent_id", ""),
                from_peer=body.get("from_peer", ""),
                from_agent_id=body.get("from_agent_id", ""),
            ))
        if path == "/api/reply":
            # 远端 agent 作答/完成任务后回推至原发送方 agent 的专属信箱
            rid = new_id("rep")
            remote_jid = body.get("remote_job_id", "")
            to_aid = body.get("to_agent_id", "")
            from_peer = body.get("from_peer", "")
            from_aid = body.get("from_agent_id", "")
            rep = {
                "id": rid,
                "kind": "reply",
                "direction": "inbound",
                "status": "unread",
                "remote_job_id": remote_jid,
                "orig_kind": body.get("orig_kind", "ask"),
                "from_peer": from_peer,
                "from_agent_id": from_aid,
                "to_peer": CFG["name"],
                "to_agent_id": to_aid,
                "question": body.get("question", ""),
                "answer": body.get("answer", ""),
                "error": body.get("error", ""),
                "branch": body.get("branch", ""),
                "pr": body.get("pr", ""),
                "created": time.time(),
            }
            add_job_hop(rep, 4, f"收到来自 {from_peer}/{from_aid} 的异步回推结果 -> 存入 {to_aid} 信箱",
                        actor=f"{from_peer}/{from_aid}", detail=(rep["answer"] or rep["error"])[:120])
            save_job(rep)
            update_outbound_shadow(
                remote_jid, 4,
                f"远端 {from_peer}/{from_aid} 已回推答案至本机 {to_aid} 信箱",
                actor=f"{from_peer}/{from_aid}",
                detail=(rep["answer"] or rep["error"])[:100],
                answer=rep["answer"],
                status="done",
            )
            record_event("DELIVERY", f"收到 {from_peer}/{from_aid} 回推的回答 -> 已落入原发送者 {to_aid} 信箱",
                         actor=f"{from_peer}/{from_aid}", target=to_aid)
            return self.send_json(200, {"ok": True, "reply_id": rid})

        m = re.match(r"^/api/jobs/([\w.-]+)/answer$", path)
        if m:  # 信箱模式：本机目标 agent 提交回答
            aid = (body.get("agent_id") or "").strip()
            touch_agent(aid)
            j, code, err = answer_job(m.group(1), body.get("answer", ""), responder_agent_id=aid)
            if err:
                return self.send_json(code, {"error": err})
            return self.send_json(200, {
                "ok": True,
                "job_id": j["id"],
                "from_peer": j.get("from_peer"),
                "from_agent_id": j.get("from_agent_id"),
                "answered_by": j.get("answered_by"),
            })
        m = re.match(r"^/api/file/([\w.-]+)/collect$", path)
        if m:  # 接收方收取：把别人发来、已代收在本机 incoming 的文件落到目标路径
            aid = (body.get("agent_id") or "").strip()
            touch_agent(aid)
            return self.collect_file(m.group(1), body.get("save_path", ""), collector_agent_id=aid)

        # ---- 出站：由本机 MCP frontend 调用，转发到 peer ----
        if path == "/api/out/peers":
            touch_agent(body.get("from_agent_id", ""))
            return self.send_json(200, peers_info())
        if path == "/api/out/agents":
            touch_agent(body.get("from_agent_id", ""))
            return self.send_json(200, peer_agents_info(body.get("peer", "")))
        if path == "/api/out/ask":
            from_aid, to_aid, err = validate_outbound_request(body, require_target_agent=True)
            if err:
                return self.send_json(400, {"error": err})
            peer = body.get("peer", "")
            q = body.get("question", "")
            record_event("OUTBOUND", f"{from_aid} -> {peer}/{to_aid} 发起提问",
                         actor=from_aid, target=f"{peer}/{to_aid}", detail=q[:80])
            res = peer_call(peer, "/api/ask", {
                "question": q,
                "context": body.get("context", ""),
                "to_agent_id": to_aid,
                "from_peer": CFG["name"],
                "from_agent_id": from_aid,
            })
            record_outbound_job("ask", peer, from_aid, to_aid, q, res)
            return self.send_json(200, res)
        if path == "/api/out/task":
            from_aid, to_aid, err = validate_outbound_request(body, require_target_agent=True)
            if err:
                return self.send_json(400, {"error": err})
            peer = body.get("peer", "")
            title = body.get("title", "untitled")
            record_event("OUTBOUND", f"{from_aid} -> {peer}/{to_aid} 派发编码任务：{title}",
                         actor=from_aid, target=f"{peer}/{to_aid}")
            res = peer_call(peer, "/api/task", {
                "title": title,
                "prompt": body.get("prompt", ""),
                "base": body.get("base", ""),
                "to_agent_id": to_aid,
                "from_peer": CFG["name"],
                "from_agent_id": from_aid,
            }, timeout=30)
            record_outbound_job("task", peer, from_aid, to_aid, title, res)
            return self.send_json(200, res)
        if path == "/api/out/status":
            from_aid, _, err = validate_outbound_request(body, require_target_agent=False)
            if err:
                return self.send_json(400, {"error": err})
            peer = body.get("peer", "")
            jid = body.get("job_id", "")
            qs = urllib.parse.urlencode({"agent_id": from_aid})
            res = peer_call(peer, f"/api/jobs/{jid}?{qs}", None, "GET", timeout=30)
            if isinstance(res, dict) and res.get("status") == "done":
                update_outbound_shadow(
                    jid, 5, f"发起方 {from_aid} 轮询获取到最终答案",
                    actor=from_aid, detail=(res.get("answer") or "")[:100],
                    answer=res.get("answer", ""), status="done"
                )
            return self.send_json(200, res)
        if path == "/api/out/review":
            from_aid, to_aid, err = validate_outbound_request(body, require_target_agent=True)
            if err:
                return self.send_json(400, {"error": err})
            peer = body.get("peer", "")
            pr = body.get("pr")
            record_event("OUTBOUND", f"{from_aid} -> {peer}/{to_aid} 请求审查 PR #{pr}",
                         actor=from_aid, target=f"{peer}/{to_aid}")
            res = peer_call(peer, "/api/review", {
                "pr": pr,
                "focus": body.get("focus", ""),
                "to_agent_id": to_aid,
                "from_peer": CFG["name"],
                "from_agent_id": from_aid,
            })
            record_outbound_job("review", peer, from_aid, to_aid, f"审查 PR #{pr}", res)
            return self.send_json(200, res)
        if path == "/api/out/file/send":
            from_aid, to_aid, err = validate_outbound_request(body, require_target_agent=True)
            if err:
                return self.send_json(400, {"error": err})
            return self.send_json(200, file_send(
                body.get("peer", ""),
                to_aid,
                body.get("path", ""),
                body.get("note", ""),
                from_agent_id=from_aid,
            ))
        if path == "/api/out/files":
            from_aid, _, err = validate_outbound_request(body, require_target_agent=False)
            if err:
                return self.send_json(400, {"error": err})
            qs = urllib.parse.urlencode({"agent_id": from_aid})
            return self.send_json(200, peer_call(
                body.get("peer", ""),
                f"/api/files?{qs}",
                None, "GET", timeout=30,
            ))
        if path == "/api/out/file/get":
            from_aid, _, err = validate_outbound_request(body, require_target_agent=False)
            if err:
                return self.send_json(400, {"error": err})
            return self.send_json(200, file_fetch(
                body.get("peer", ""),
                body.get("file_id", ""),
                body.get("save_path", ""),
                requester_agent_id=from_aid,
            ))
        self.send_json(404, {"error": "not found"})

# ---------------------------------------------------------------- 启动向导与局域网发现

def ip_to_int(s):
    a, b, c, d = (int(x) for x in s.split("."))
    return (a << 24) | (b << 16) | (c << 8) | d

def int_to_ip(n):
    return f"{n >> 24 & 255}.{n >> 16 & 255}.{n >> 8 & 255}.{n & 255}"

def parse_ipconfig_windows(out):
    """解析 Windows ipconfig 输出（中文/英文均可），提取带 IPv4+子网掩码的接口。"""
    iface, ip = "", ""
    out2 = []
    for line in out.splitlines():
        line = line.strip()
        if line.endswith(":") and ("适配器" in line or "adapter" in line.lower()):
            iface, ip = line.rstrip(":").strip(), ""
            continue
        if "IPv4" in line:
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
            if m:
                ip = m.group(1)
            continue
        if ip and ("子网掩码" in line or "Subnet Mask" in line):
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
            if m:
                out2.append((iface, ip, ip_to_int(m.group(1))))
                ip = ""
    return out2

def local_subnets():
    """本机所有局域网接口 → [(接口名, IPv4, 掩码int)]，Windows(ipconfig) / macOS(ifconfig) / Linux(ip) 通吃。"""
    out = []
    try:
        if sys.platform == "win32":
            p = subprocess.run(["ipconfig"], capture_output=True, timeout=5)
            raw = p.stdout or b""
            text = None
            for enc in ("gbk", "utf-8", "cp437"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            out = parse_ipconfig_windows(text if text is not None else raw.decode("utf-8", "replace"))
        elif sys.platform == "darwin":
            p = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5)
            iface = ""
            for line in p.stdout.splitlines():
                m = re.match(r"^(\S+?):", line)
                if m:
                    iface = m.group(1)
                    continue
                m = re.search(r"inet (\d+\.\d+\.\d+\.\d+) netmask (\S+)", line)
                if m:
                    nm = m.group(2)
                    mask = int(nm, 16) if nm.startswith("0x") else ip_to_int(nm)
                    out.append((iface, m.group(1), mask))
        else:
            p = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, timeout=5)
            for line in p.stdout.splitlines():
                m = re.search(r"^\d+: (\S+)\s+inet (\d+\.\d+\.\d+\.\d+)/(\d+)", line)
                if m:
                    out.append((m.group(1), m.group(2), (0xFFFFFFFF << (32 - int(m.group(3)))) & 0xFFFFFFFF))
    except Exception:
        pass
    return [(i, ip, mask) for i, ip, mask in out
            if not ip.startswith("127.") and not ip.startswith("169.254.")]

def subnet_hosts(ip, mask, cap=1024):
    base = ip_to_int(ip) & mask
    size = 1 << (32 - bin(mask).count("1"))
    stop = min(size - 1, cap + 1)  # 跳过网络/广播地址；超大网段截断保护
    return [int_to_ip(base + i) for i in range(1, stop)]

def tcp_open(ip, port, timeout=0.6):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False

def check_token(url, token):
    """用需鉴权的 /api/status 验证 token 是否为对方网关的。"""
    if not token:
        return False
    res, err = http_json(url.rstrip("/") + "/api/status", token, None, "GET", timeout=4)
    return err is None and isinstance(res, dict) and "name" in res

def scan_lan(port, token=""):
    """并发探测本机所有局域网网段 → [(url, agent_card, token是否匹配)]，自动排除本机自身 IP 与节点名。"""
    subnets = local_subnets()
    if not subnets:
        return []
    local_ips = {"127.0.0.1", "localhost", "0.0.0.0"}
    for _, ip, _ in subnets:
        local_ips.add(ip)

    hosts = []
    for iface, ip, mask in subnets:
        hs = subnet_hosts(ip, mask)
        hosts += hs

    my_name = CFG.get("name", "")

    def probe(ip):
        # 排除探测本机自身 IP
        if ip in local_ips:
            return None
        if not tcp_open(ip, port):
            return None
        url = f"http://{ip}:{port}"
        card, _ = http_json(url + "/.well-known/agent-card.json", "", None, "GET", timeout=2)
        if not isinstance(card, dict) or "troupe" not in (card.get("description") or ""):
            return None
        # 如果发现的节点名字与本机节点名完全一致，也排除（防止NAT/虚拟网卡环回）
        if my_name and card.get("name") == my_name:
            return None
        return url, card, check_token(url, token)

    found = []
    with ThreadPoolExecutor(max_workers=128) as ex:
        futs = [ex.submit(probe, h) for h in dict.fromkeys(hosts) if h not in local_ips]
        for f in as_completed(futs):
            r = f.result()
            if r:
                found.append(r)
    return found

def peer_card_name(url):
    card, _ = http_json(url.rstrip("/") + "/.well-known/agent-card.json", "", None, "GET", timeout=3)
    return card.get("name") if isinstance(card, dict) else None

def save_peer_to_config(name, url, token):
    url = url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    name = (name or "").strip() or peer_card_name(url) or f"peer-{len(CFG.get('peers', {})) + 1}"
    # 禁止将本机自己添加为远端 Peer
    if name == CFG.get("name"):
        return name, url
    CFG.setdefault("peers", {})[name] = {"url": url, "token": token.strip()}
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(CFG, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass
    record_event("STARTUP", f"已保存远端 Peer 网关：{name} -> {url}", actor="config", target=name)
    return name, url

def start_http_server():
    host, port = CFG["listen"]
    update_startup_step("server", "running", f"正在绑定 http://{host}:{port} ...")
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    update_startup_step("server", "done", f"HTTP 网关正在监听 http://{host}:{port}")
    return srv

def main():
    load_jobs()
    load_agents()

    # 判断启动模式：
    # --no-ui / --no-setup 或非交互后台进程 -> 纯后台运行
    # --tui -> 终端内全屏 App
    # 默认交互启动 -> 原生桌面窗口 App（若桌面环境不可用则自动回退到终端 TUI App）
    headless = ("--no-ui" in sys.argv) or ("--no-setup" in sys.argv) or (not sys.stdin.isatty() and not sys.stdout.isatty() and "--gui" not in sys.argv and "--tui" not in sys.argv)
    prefer_tui = "--tui" in sys.argv

    srv = start_http_server()

    if headless:
        threading.Thread(target=probe_peers_on_startup, daemon=True).start()
        host, port = CFG["listen"]
        print(f"troupe gateway '{CFG['name']}' -> http://{host}:{port} "
              f"(driver={DRIVER.get('type')}, repo={CFG['repo']})", flush=True)
        srv.serve_forever()
        return

    # 在后台线程运行 HTTP Server，主线程运行原生 App 界面
    srv_thread = threading.Thread(target=srv.serve_forever, daemon=True)
    srv_thread.start()
    threading.Thread(target=probe_peers_on_startup, daemon=True).start()

    import troupe_app
    troupe_app.run_app(sys.modules[__name__], prefer_tui=prefer_tui)


if __name__ == "__main__":
    main()
