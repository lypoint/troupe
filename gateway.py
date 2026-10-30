#!/usr/bin/env python3
"""
troupe gateway —— 让任意 coding agent（codex / zcode / opencode / qoder / claude…）互相提问与协作。

架构（每台机器部署一个 gateway）：
  1. 接入层：本机 agent 作为 MCP 客户端调用 mcp_frontend.py，frontend 转发到本 gateway（HTTP）
  2. 网络层：gateway 之间用「简单 REST + A2A 兼容子集(JSON-RPC)」通信，Bearer token 鉴权
  3. 执行层：driver 驱动本机 agent 应答 —— CLI 模板（codex exec / opencode run / qoder --print…）
     或 mailbox 信箱模式（GUI-only agent 如 zcode 桌面版，由其会话用 MCP 工具作答）

只用 Python 标准库。配置文件见 config.example.json；启动：TROUPE_CONFIG=path python3 gateway.py
"""
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONFIG_PATH = os.path.expanduser(os.environ.get("TROUPE_CONFIG", str(Path.home() / ".troupe" / "config.json")))

# ---------------------------------------------------------------- 配置

def load_config():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    cfg["repo"] = os.path.expanduser(cfg.get("repo", "."))
    cfg.setdefault("state_dir", "~/.troupe")
    cfg["state_dir"] = os.path.expanduser(cfg["state_dir"])
    cfg.setdefault("ask_timeout", 240)      # 问答类最长等待（秒）
    cfg.setdefault("task_timeout", 3600)    # 编码任务最长执行（秒）
    cfg.setdefault("file_max_mb", 100)      # 单文件传输上限（MB）
    cfg.setdefault("open_pr", False)        # 任务完成后是否自动 gh pr create
    cfg.setdefault("driver", {"type": "echo"})
    cfg.setdefault("peers", {})
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
    "mailbox":  {},       # 信箱模式：等待 GUI agent 用 check_inbox/submit_answer 作答
    "command":  {},       # 用户自定义 ask_cmd / task_cmd
}

def driver_cmd(kind):  # kind: "ask" | "task"
    preset = DRIVER_PRESETS.get(DRIVER.get("type", "command"), {})
    return DRIVER.get(kind + "_cmd", preset.get(kind + "_cmd"))

# ---------------------------------------------------------------- 任务存档（内存 + 磁盘）

JOBS = {}
JOBS_LOCK = threading.Lock()

def new_id(prefix):
    return f"{prefix}-{time.strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}"

def save_job(j):
    with JOBS_LOCK:
        JOBS[j["id"]] = j
    persist_job(j)

def persist_job(j):
    d = Path(CFG["state_dir"]) / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    (d / (j["id"] + ".json")).write_text(json.dumps(j, ensure_ascii=False, indent=1))

def answer_job(jid, answer):
    """信箱作答：锁内原子完成状态校验与更新，避免并发作答竞态。"""
    with JOBS_LOCK:
        j = JOBS.get(jid)
        if not j:
            return None, 404, "job 不存在"
        if j.get("status") != "pending_answer":
            return None, 409, f"该任务状态是 {j.get('status')}，不能作答"
        j.update({"status": "done", "answer": answer, "answered_at": time.time()})
    persist_job(j)
    return j, None, None

def get_job(jid):
    with JOBS_LOCK:
        return JOBS.get(jid)

def load_jobs():
    d = Path(CFG["state_dir"]) / "jobs"
    if d.exists():
        for f in d.glob("*.json"):
            try:
                JOBS[f.stem] = json.loads(f.read_text())
            except Exception:
                pass

# ---------------------------------------------------------------- 提示词信封

ASK_ENVELOPE = """有一个远程协作 agent 通过网络向你提问，请基于当前目录的代码库回答。
要求：只回答，不要修改任何文件；用中文；简洁但自包含；信息不足时直接说明缺什么。
{extra}问题：
{question}"""

TASK_ENVELOPE = """你收到一个来自远程协作 agent 的开发任务，当前目录是一个独立的 git worktree 分支。
要求：直接动手完成；完成后 git add -A && git commit（不要 push）；最后输出小结：做了什么、关键决策、如何验证。
任务标题：{title}
任务内容：
{prompt}"""

REVIEW_ENVELOPE = """你收到一个来自远程协作 agent 的代码审查请求。先用 `gh pr view {pr}` 和 `gh pr diff {pr}` 获取 PR 内容，再给出审查意见。
要求：只输出审查意见文本（问题按严重程度排列，引用文件路径和行号）；不要修改代码、不要在 PR 里发言论。{extra}"""

# ---------------------------------------------------------------- 本地执行

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

def handle_ask(question, context=""):
    """:returns {"status": done|pending|error, "answer"/"job_id"/"error": ...}"""
    t = DRIVER.get("type", "command")
    if t == "echo":
        return {"status": "done", "answer": f"[echo] {question}"}
    if t == "mailbox":
        j = {"id": new_id("q"), "kind": "ask", "status": "pending_answer",
             "question": question, "context": context, "created": time.time()}
        save_job(j)
        return {"status": "pending", "job_id": j["id"],
                "note": "对方为信箱模式，稍后用 task_status(peer, job_id) 轮询答案"}
    cmd = driver_cmd("ask")
    if not cmd:
        return {"status": "error", "error": f"driver 类型 {t} 未配置 ask_cmd"}
    prompt = ASK_ENVELOPE.format(extra=f"背景补充：{context}\n" if context else "", question=question)
    out, err = run_cmd(cmd + [prompt], CFG["repo"], CFG["ask_timeout"])
    j = {"id": new_id("q"), "kind": "ask", "status": "done" if out else "error",
         "question": question, "created": time.time()}
    if out:
        j["answer"] = out
    else:
        j["error"] = err
    save_job(j)
    return {"status": j["status"], "answer": out, "job_id": j["id"]}

def handle_review(pr, focus=""):
    t = DRIVER.get("type", "command")
    if t == "echo":
        return {"status": "done", "answer": f"[echo] review pr#{pr}"}
    if t == "mailbox":
        j = {"id": new_id("r"), "kind": "review", "status": "pending_answer",
             "question": f"审查 PR #{pr}" + (f"（重点：{focus}）" if focus else ""),
             "pr": pr, "created": time.time()}
        save_job(j)
        return {"status": "pending", "job_id": j["id"],
                "note": "对方为信箱模式，稍后用 task_status(peer, job_id) 轮询"}
    cmd = driver_cmd("ask")
    if not cmd:
        return {"status": "error", "error": f"driver 类型 {t} 未配置 ask_cmd"}
    prompt = REVIEW_ENVELOPE.format(pr=pr, extra=f"\n重点关注：{focus}" if focus else "")
    out, err = run_cmd(cmd + [prompt], CFG["repo"], CFG["ask_timeout"])
    j = {"id": new_id("r"), "kind": "review", "pr": pr,
         "status": "done" if out else "error", "created": time.time()}
    if out:
        j["answer"] = out
    else:
        j["error"] = err
    save_job(j)
    return {"status": j["status"], "answer": out, "job_id": j["id"]}

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
        prompt = TASK_ENVELOPE.format(title=job["title"], prompt=job["prompt"])
        out, err = run_cmd(cmd + [prompt], worktree, CFG["task_timeout"])
        if err:
            raise RuntimeError(err)
        git(["add", "-A"], worktree, ok_fail=True)
        git(["commit", "-m", f"troupe {job['id']}: {job['title']}"], worktree, ok_fail=True)
        pushed = git(["push", "origin", f"{branch}:{branch}"], worktree, ok_fail=True).returncode == 0
        job.update({"status": "done", "answer": out, "pushed": pushed})
        if CFG["open_pr"] and pushed:
            pr = subprocess.run(["gh", "pr", "create", "--head", branch,
                                 "--title", f"[troupe] {job['title']}", "--body", out[:60000]],
                                cwd=worktree, capture_output=True, text=True)
            if pr.returncode == 0:
                job["pr"] = pr.stdout.strip()
    except Exception as e:
        job.update({"status": "error", "error": str(e)[-1500:]})
    save_job(job)
    if job["status"] == "done":  # 失败时保留 worktree 现场供排查
        cleanup_worktree(worktree)

def handle_task(title, prompt, base=""):
    t = DRIVER.get("type", "command")
    if t == "mailbox":
        return {"status": "error", "error": "信箱模式暂不支持 task 派发（ask/review 可用）"}
    if not (driver_cmd("task") or driver_cmd("ask")):
        return {"status": "error", "error": f"driver 类型 {t} 未配置 task_cmd"}
    tid = new_id("t")
    branch = f"agent/{tid}"
    worktree = str(Path(CFG["state_dir"]) / "worktrees" / f"agent-{tid}")
    Path(worktree).parent.mkdir(parents=True, exist_ok=True)
    try:
        git(["fetch", "origin"], CFG["repo"], ok_fail=True)
        git(["worktree", "add", "-b", branch, worktree, base or default_base()], CFG["repo"])
    except Exception as e:
        return {"status": "error", "error": f"创建 worktree 失败：{e}"}
    j = {"id": tid, "kind": "task", "status": "running", "title": title, "prompt": prompt,
         "branch": branch, "worktree": worktree, "created": time.time()}
    save_job(j)
    threading.Thread(target=task_runner, args=(j, worktree, branch), daemon=True).start()
    return {"status": "running", "job_id": tid, "branch": branch, "note": "用 task_status(peer, job_id) 轮询"}

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
            out[name] = {"url": p["url"], "reachable": True,
                         "agent": card.get("name"), "description": card.get("description"),
                         "skills": [s.get("id") for s in card.get("skills", [])]}
    return out

# ---------------------------------------------------------------- 文件传输（state_dir/incoming 落盘，不依赖 driver）

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

def file_send(peer_name, local_path, note=""):
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
    qs = "name=" + urllib.parse.quote(fp.name)
    if note:
        qs += "&note=" + urllib.parse.quote(note[:500])
    res, err = http_json(p["url"].rstrip("/") + "/api/file/upload?" + qs,
                         p.get("token", ""), None, "POST", timeout=600, raw=fp.read_bytes())
    j = {"id": new_id("f"), "kind": "file", "status": "error" if err else "sent",
         "peer": peer_name, "filename": fp.name, "size": fp.stat().st_size,
         "note": note, "remote_file_id": (res or {}).get("file_id"), "created": time.time()}
    if err:
        j["error"] = err
    save_job(j)
    if err:
        return {"error": err}
    return {"status": "done", "peer": peer_name, "file_id": j["remote_file_id"],
            "filename": fp.name, "size": j["size"],
            "note": "对方 list_files(peer) 可见；fetch_file(peer, file_id) 收取"}

def file_fetch(peer_name, file_id, save_path=""):
    peers = CFG.get("peers", {})
    p = peers.get(peer_name)
    if not p:
        return {"error": f"未配置的 peer：{peer_name}（已配置：{list(peers) or '无'}）"}
    data, hdrs, err = http_get_bytes(p["url"].rstrip("/") + f"/api/file/{file_id}", p.get("token", ""))
    if err:
        return {"error": err}
    fname = urllib.parse.unquote(hdrs.get("X-File-Name") or "").strip() or "file"
    dest = save_dest(save_path, fname)
    dest.write_bytes(data)
    save_job({"id": new_id("f"), "kind": "file", "status": "received", "peer": peer_name,
              "filename": dest.name, "size": len(data), "saved_to": str(dest),
              "remote_file_id": file_id, "created": time.time()})
    return {"status": "done", "saved_to": str(dest), "size": len(data)}

# ---------------------------------------------------------------- A2A 兼容子集 + Agent Card

def agent_card():
    host, port = CFG["listen"]
    base = CFG.get("public_url") or f"http://{host}:{port}"
    return {
        "name": CFG["name"],
        "description": f"troupe gateway（driver={DRIVER.get('type')}）",
        "url": base + "/a2a",
        "version": "0.1.0",
        "protocolVersion": "0.3.0",
        "capabilities": {"chat": True},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {"id": "ask", "name": "ask", "description": "向本机 agent 提问（只读），返回回答"},
            {"id": "review", "name": "review", "description": "审查 GitHub PR，返回意见文本"},
            {"id": "task", "name": "task", "description": "派发编码任务，在独立 worktree 分支执行"},
            {"id": "file", "name": "file", "description": "收发文件：POST /api/file/upload 上传，GET /api/file/<id> 下载，GET /api/files 列出待收取"},
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
        msg = (req.get("params") or {}).get("message") or {}
        text = ""
        for part in msg.get("parts", []):
            if part.get("kind") == "text" or "text" in part:
                text += part.get("text", "")
        r = handle_ask(text)
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
    server_version = "troupe/0.1"

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

    def serve_file(self, fid):
        j = get_job(fid)
        if not j or j.get("kind") != "file" or j.get("status") != "ready":
            return self.send_json(404, {"error": "文件不存在"})
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
        if not name:
            return self.send_json(400, {"error": "缺少 name 查询参数（文件名）"})
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
        save_job({"id": fid, "kind": "file", "status": "ready", "filename": name,
                  "size": n, "note": note, "created": time.time()})
        return self.send_json(200, {"status": "ready", "file_id": fid, "filename": name, "size": n})

    def collect_file(self, fid, save_path):
        j = get_job(fid)
        if not j or j.get("kind") != "file" or j.get("status") != "ready":
            return self.send_json(404, {"error": "文件不存在或已收取"})
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
        j.update({"status": "collected", "saved_to": str(dest), "collected_at": time.time()})
        save_job(j)
        return self.send_json(200, {"status": "done", "saved_to": str(dest), "size": len(data)})

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
            return self.send_json(200, agent_card())
        if not self.authed():
            return self.send_json(401, {"error": "unauthorized"})
        if path == "/api/status":
            return self.send_json(200, {"name": CFG["name"], "driver": DRIVER.get("type"),
                                        "repo": CFG["repo"], "peers": list(CFG.get("peers", {})),
                                        "time": time.strftime("%F %T")})
        if path == "/api/peers":
            return self.send_json(200, peers_info())
        if path == "/api/inbox":
            pending = [j for j in list(JOBS.values()) if j.get("status") == "pending_answer"]
            new_files = [{"id": j["id"], "filename": j.get("filename"), "size": j.get("size"),
                          "note": j.get("note", ""), "created": j.get("created", 0)}
                         for j in JOBS.values()
                         if j.get("kind") == "file" and j.get("status") == "ready"]
            return self.send_json(200, {"questions": sorted(pending, key=lambda j: j.get("created", 0)),
                                        "files": sorted(new_files, key=lambda f: f["created"], reverse=True)})
        if path == "/api/files":
            files = [{"id": j["id"], "filename": j.get("filename"), "size": j.get("size"),
                      "note": j.get("note", ""), "created": j.get("created", 0)}
                     for j in JOBS.values()
                     if j.get("kind") == "file" and j.get("status") == "ready"]
            return self.send_json(200, {"files": sorted(files, key=lambda f: f["created"], reverse=True)})
        if path == "/api/jobs":
            js = sorted(JOBS.values(), key=lambda j: j.get("created", 0), reverse=True)[:20]
            return self.send_json(200, [{"id": j["id"], "kind": j.get("kind"), "status": j.get("status"),
                                         "title": (j.get("title") or j.get("question") or j.get("filename", ""))[:60],
                                         "branch": j.get("branch")} for j in js])
        m = re.match(r"^/api/file/([\w.-]+)$", path)
        if m:
            return self.serve_file(m.group(1))
        m = re.match(r"^/api/jobs/([\w.-]+)$", path)
        if m:
            return self.send_json(200, get_job(m.group(1)) or {"error": "job 不存在"})
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
        # ---- 入站：由 peer 网关/第三方 A2A 客户端调用，在本机执行 ----
        if path == "/api/ask":
            return self.send_json(200, handle_ask(body.get("question", ""), body.get("context", "")))
        if path == "/api/review":
            try:
                pr = int(body.get("pr", 0))
            except (TypeError, ValueError):
                return self.send_json(400, {"error": f"pr 必须是数字（收到 {body.get('pr')!r}）"})
            return self.send_json(200, handle_review(pr, body.get("focus", "")))
        if path == "/api/task":
            return self.send_json(200, handle_task(body.get("title", "untitled"),
                                                   body.get("prompt", ""), body.get("base", "")))
        m = re.match(r"^/api/jobs/([\w.-]+)/answer$", path)
        if m:  # 信箱模式：本机 agent 提交回答
            j, code, err = answer_job(m.group(1), body.get("answer", ""))
            if err:
                return self.send_json(code, {"error": err})
            return self.send_json(200, {"ok": True, "job_id": j["id"]})
        m = re.match(r"^/api/file/([\w.-]+)/collect$", path)
        if m:  # 接收方收取：把别人发来、已代收在本机 incoming 的文件落到目标路径
            return self.collect_file(m.group(1), body.get("save_path", ""))
        # ---- 出站：由本机 MCP frontend 调用，转发到 peer ----
        if path == "/api/out/peers":
            return self.send_json(200, peers_info())
        if path == "/api/out/ask":
            return self.send_json(200, peer_call(body.get("peer", ""), "/api/ask",
                                                 {"question": body.get("question", ""),
                                                  "context": body.get("context", "")}))
        if path == "/api/out/task":
            return self.send_json(200, peer_call(body.get("peer", ""), "/api/task",
                                                 {"title": body.get("title", "untitled"),
                                                  "prompt": body.get("prompt", ""),
                                                  "base": body.get("base", "")}, timeout=30))
        if path == "/api/out/status":
            return self.send_json(200, peer_call(body.get("peer", ""),
                                                 f"/api/jobs/{body.get('job_id', '')}",
                                                 None, "GET", timeout=30))
        if path == "/api/out/review":
            return self.send_json(200, peer_call(body.get("peer", ""), "/api/review",
                                                 {"pr": body.get("pr"), "focus": body.get("focus", "")}))
        if path == "/api/out/file/send":
            return self.send_json(200, file_send(body.get("peer", ""), body.get("path", ""),
                                                 body.get("note", "")))
        if path == "/api/out/files":
            return self.send_json(200, peer_call(body.get("peer", ""), "/api/files",
                                                 None, "GET", timeout=30))
        if path == "/api/out/file/get":
            return self.send_json(200, file_fetch(body.get("peer", ""), body.get("file_id", ""),
                                                  body.get("save_path", "")))
        self.send_json(404, {"error": "not found"})

# ---------------------------------------------------------------- 启动向导（交互式配 peers：手填 / 扫描局域网按 token 匹配）

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
    """并发探测本机所有局域网网段 → [(url, agent_card, token是否匹配)]。"""
    subnets = local_subnets()
    if not subnets:
        print("（没找到局域网接口）")
        return []
    hosts = []
    for iface, ip, mask in subnets:
        hs = subnet_hosts(ip, mask)
        print(f"扫描 {iface} {int_to_ip(ip_to_int(ip) & mask)}/{bin(mask).count('1')}（{len(hs)} 个地址）...")
        hosts += hs

    def probe(ip):
        if not tcp_open(ip, port):
            return None
        url = f"http://{ip}:{port}"
        card, _ = http_json(url + "/.well-known/agent-card.json", "", None, "GET", timeout=2)
        if not isinstance(card, dict) or "troupe" not in (card.get("description") or ""):
            return None  # 端口开着但不是 troupe 网关
        return url, card, check_token(url, token)

    found = []
    with ThreadPoolExecutor(max_workers=128) as ex:
        futs = [ex.submit(probe, h) for h in dict.fromkeys(hosts)]
        for f in as_completed(futs):
            r = f.result()
            if r:
                found.append(r)
                mark = "token ✓" if r[2] else ("token ✗" if token else "未验 token")
                print(f"  发现 {r[0]}  {r[1].get('name')}（{mark}）")
    if not found:
        print("（没发现 troupe 网关）")
    return found

def peer_card_name(url):
    card, _ = http_json(url.rstrip("/") + "/.well-known/agent-card.json", "", None, "GET", timeout=3)
    return card.get("name") if isinstance(card, dict) else None

def _save_peer(name, url, token):
    CFG.setdefault("peers", {})[name] = {"url": url, "token": token}
    print(f"✓ 已添加 peers[\"{name}\"] = {url}")

def manual_add():
    name = input("对方名称（如 codex-box，回车自动取）: ").strip()
    url = input("对方 url（如 http://192.168.1.20:8765）: ").strip()
    if not url:
        return False
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    url = url.rstrip("/")
    token = input("对方 token: ").strip()
    if token:
        ok = check_token(url, token)
        print("✓ 可达且 token 匹配" if ok else "✗ 连不上或 token 不匹配")
        if not ok and input("仍保存？（对方启动后自动生效）[y/N] ").strip().lower() != "y":
            return False
    elif input("未填 token，确定保存？[y/N] ").strip().lower() != "y":
        return False
    _save_peer(name or peer_card_name(url) or f"peer-{len(CFG.get('peers', {})) + 1}", url, token)
    return True

def scan_add():
    token = input("对方 token（用于匹配验证；回车=只发现不验证）: ").strip()
    found = scan_lan(CFG["listen"][1], token)
    if not found:
        return False
    pick = input("添加哪个（序号，回车取消）: ").strip()
    if not pick.isdigit() or not 1 <= int(pick) <= len(found):
        return False
    url, card, matched = found[int(pick) - 1]
    if not matched:
        token = input(f"{url} 的 token: ").strip()
        if token and not check_token(url, token):
            print("✗ token 不匹配，未保存（可重扫或改用手动填）")
            return False
        if not token and input("未填 token，确定保存？[y/N] ").strip().lower() != "y":
            return False
    _save_peer(input(f"保存名称（回车={card.get('name') or 'peer'}）: ").strip()
               or card.get("name") or "peer", url, token)
    return True

def setup_wizard():
    """交互式配置 peers，改动写回 config.json。仅交互终端调用（nohup 自动跳过）。"""
    changed = False
    while True:
        print(f"\n当前 peers: {list(CFG.get('peers', {})) or '（空）'}")
        c = input("[1]手动填对方地址  [2]扫描局域网添加  [3]完成并启动 > ").strip()
        if c == "1":
            changed |= manual_add()
        elif c == "2":
            changed |= scan_add()
        else:
            break
    if changed:
        with open(CONFIG_PATH, "w") as f:
            json.dump(CFG, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.chmod(CONFIG_PATH, 0o600)
        print(f"✓ peers 已写入 {CONFIG_PATH}")

def main():
    load_jobs()
    if sys.stdin.isatty() and sys.stdout.isatty() and "--no-setup" not in sys.argv:
        print("troupe peers 配置向导（后台 nohup 启动会自动跳过；--no-setup 强制跳过）")
        try:
            setup_wizard()
        except (EOFError, KeyboardInterrupt):
            print("\n（向导已跳过）")
    host, port = CFG["listen"]
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    print(f"troupe gateway '{CFG['name']}' -> http://{host}:{port} "
          f"(driver={DRIVER.get('type')}, repo={CFG['repo']})", flush=True)
    srv.serve_forever()

if __name__ == "__main__":
    main()
