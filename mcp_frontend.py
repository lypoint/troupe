#!/usr/bin/env python3
"""
troupe MCP 前端 —— 把跨机协作能力以 MCP 工具暴露给本机任意 agent
（zcode / codex / opencode / qoder / claude… 都支持 MCP 客户端，所以都能用）。

依赖 fastmcp。启动方式（由各 agent 的 MCP 配置拉起）：
  uv run --no-project --with fastmcp python mcp_frontend.py
环境变量 TROUPE_CONFIG 指向 gateway 的 config.json（默认 ~/.troupe/config.json）。
"""
import json
import os
import urllib.request
import urllib.error
from pathlib import Path

from fastmcp import FastMCP

# 本机 gateway 走回环地址，绝不能经过 HTTP(S) 代理（系统代理会把 127.0.0.1 拦下返回 502）
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_k, None)
_no_proxy = os.environ.get("NO_PROXY", "")
if "127.0.0.1" not in _no_proxy:
    os.environ["NO_PROXY"] = (_no_proxy + "," if _no_proxy else "") + "127.0.0.1,localhost,::1"

CONFIG_PATH = os.path.expanduser(os.environ.get("TROUPE_CONFIG", str(Path.home() / ".troupe" / "config.json")))
_cfg = json.loads(Path(CONFIG_PATH).read_text())
_host, _port = _cfg.get("listen", ["127.0.0.1", 8765])
if _host in ("0.0.0.0", "::"):  # 通配监听地址不能作为客户端连接目标（Windows 直接 10049 报错）
    _host = "127.0.0.1"
BASE = os.environ.get("MESH_GATEWAY", f"http://{_host}:{_port}").rstrip("/")


def call(path, payload=None, method="POST", timeout=900):
    req = urllib.request.Request(BASE + path, method=method)
    if _cfg.get("token"):
        req.add_header("Authorization", "Bearer " + _cfg["token"])
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode()[:300]}"}
    except Exception as e:
        return {"error": f"连不上本机 gateway（{BASE}）：{e}。请确认 gateway.py 在运行。"}


mcp = FastMCP("troupe")


@mcp.tool
def list_peers() -> str:
    """列出 troupe 中其他机器/agent 及其能力（agent card）。开始跨机协作前先调用这个。"""
    return json.dumps(call("/api/out/peers", {}), ensure_ascii=False)


@mcp.tool
def ask_peer(peer: str, question: str, context: str = "") -> str:
    """向 peer 机器上的 agent 提问并等待回答（对方只读代码库，不改文件）。
    问题必须自包含：对方看不到你的会话上下文，请把相关文件路径、报错信息、约束条件写全。
    若返回 status=pending（对方是信箱模式），用 task_status(peer, job_id) 轮询取答案。"""
    return json.dumps(call("/api/out/ask", {"peer": peer, "question": question, "context": context}),
                      ensure_ascii=False)


@mcp.tool
def run_task(peer: str, title: str, prompt: str, base_branch: str = "") -> str:
    """把重活派发给 peer 机器的 agent：它在独立 git worktree 分支上执行，不动它自己的工作区。
    prompt 要自包含且足够详细（相当于一份小型任务说明）。返回 job_id，用 task_status 轮询。"""
    return json.dumps(call("/api/out/task", {"peer": peer, "title": title,
                                             "prompt": prompt, "base": base_branch}),
                      ensure_ascii=False)


@mcp.tool
def task_status(peer: str, job_id: str) -> str:
    """查询发给 peer 的任务/提问的进度与结果（status: running/done/error/pending_answer）。"""
    return json.dumps(call("/api/out/status", {"peer": peer, "job_id": job_id}), ensure_ascii=False)


@mcp.tool
def review_pr(peer: str, pr_number: int, focus: str = "") -> str:
    """请 peer 机器的 agent 审查某个 GitHub PR，返回审查意见文本（它不会发言论、不改代码）。"""
    return json.dumps(call("/api/out/review", {"peer": peer, "pr": pr_number, "focus": focus}),
                      ensure_ascii=False)


# ---------- 跨机传文件（不依赖 driver，信箱机器也能收发）----------

@mcp.tool
def send_file(peer: str, path: str, note: str = "") -> str:
    """把本机文件发给 peer 机器（存到对方网关，等对方收取）。path 是本机文件路径（绝对路径，或相对本机 repo 的路径）。
    note 用一句话说明这是什么文件。默认单文件上限 100MB。"""
    return json.dumps(call("/api/out/file/send", {"peer": peer, "path": path, "note": note},
                           timeout=1800), ensure_ascii=False)


@mcp.tool
def list_files(peer: str) -> str:
    """列出 peer 机器上网关代收、等待本机收取的文件（file_id、文件名、大小、说明）。"""
    return json.dumps(call("/api/out/files", {"peer": peer}), ensure_ascii=False)


@mcp.tool
def fetch_file(peer: str = "", file_id: str = "", save_path: str = "") -> str:
    """取文件到本机。两种用法：peer 填对方机器名 = 从对方网关拉文件；
    peer 留空 = 收取别人发到本机的文件（check_inbox / list_files 里看到的 file_id）。
    save_path 为落盘路径：空=存到本机项目目录用原文件名；以 / 结尾或指向已有目录=存到该目录；同名自动加序号。"""
    if peer:
        return json.dumps(call("/api/out/file/get", {"peer": peer, "file_id": file_id,
                                                     "save_path": save_path}, timeout=1800),
                          ensure_ascii=False)
    return json.dumps(call(f"/api/file/{file_id}/collect", {"save_path": save_path}),
                      ensure_ascii=False)


# ---------- 信箱模式（本机 agent 是 GUI-only、无法被 CLI 驱动时，如 zcode 桌面版）----------

@mcp.tool
def check_inbox() -> str:
    """查看其他 agent 发来的、正在等待本机回答的问题和传来的文件（信箱模式）。建议经常调用；
    传来的文件记下 file_id，用 fetch_file(file_id=...)（peer 留空）收取。"""
    return json.dumps(call("/api/inbox", None, "GET"), ensure_ascii=False)


@mcp.tool
def submit_answer(question_id: str, answer: str) -> str:
    """回答信箱里的问题（先 check_inbox 拿到 question_id，作答后远端即可收到）。"""
    return json.dumps(call(f"/api/jobs/{question_id}/answer", {"answer": answer}), ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
