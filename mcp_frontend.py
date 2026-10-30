#!/usr/bin/env python3
"""
troupe MCP 前端 —— 把跨机协作能力以 MCP 工具暴露给本机任意 agent
（zcode / codex / opencode / qoder / claude… 都支持 MCP 客户端，所以都能用）。

多 Agent 机制：
  1. 每个挂载 MCP 的 agent 启动本前端进程时，会自动向本机唯一 gateway 注册并生成唯一 agent_id。
  2. 后续所有会话请求均自动携带本会话的 from_agent_id，严格实现「谁发的就谁收」。
  3. 首个发消息者必须：(1) 调 list_peers() 选择目标 gateway；(2) 调 list_agents(peer) 选择该 gateway 下的目标 agent_id；
     (3) 携带 peer 与 agent_id 发送消息/任务/文件。

依赖 fastmcp。启动方式（由各 agent 的 MCP 配置拉起）：
  uv run --no-project --with fastmcp python mcp_frontend.py
环境变量：
  TROUPE_CONFIG      指向 gateway 的 config.json（默认 ~/.troupe/config.json）
  TROUPE_AGENT_NAME  可选，指定本 agent 会话的易读名称（默认取当前工作目录名）
  TROUPE_AGENT_ROLE  可选，指定本 agent 会话的角色说明
"""
import json
import os
import atexit
import signal
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from fastmcp import FastMCP
except ImportError:
    class FastMCP:  # 兼容未装 fastmcp 的纯标准库测试环境直接导入工具函数
        def __init__(self, name):
            self.name = name

        def tool(self, fn):
            return fn

        def run(self):
            raise RuntimeError("请使用 uv run --no-project --with fastmcp python mcp_frontend.py 启动")

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

# 本 MCP 会话绑定的唯一身份状态
_SESSION = {
    "agent_id": os.environ.get("TROUPE_AGENT_ID", "").strip(),
    "name": os.environ.get("TROUPE_AGENT_NAME", "").strip() or (Path.cwd().name or "mcp-agent"),
    "role": os.environ.get("TROUPE_AGENT_ROLE", "").strip() or "MCP 协作会话 Agent",
    "cwd": str(Path.cwd()),
    "pid": os.getpid(),
    "registered": False,
}


def _raw_http(path, payload=None, method="POST", timeout=900):
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


def ensure_registered(name="", role="") -> dict:
    """确保当前 MCP 会话已在本机 gateway 注册并拿到唯一 agent_id。默认 enabled=False，只有调用 enable_troupe 后才对外可见。"""
    if name:
        _SESSION["name"] = name.strip()
    if role:
        _SESSION["role"] = role.strip()
    if _SESSION["registered"] and _SESSION["agent_id"] and not name and not role:
        return _SESSION
    res = _raw_http("/api/agents/register", {
        "agent_id": _SESSION["agent_id"],
        "name": _SESSION["name"],
        "role": _SESSION["role"],
        "cwd": _SESSION["cwd"],
        "pid": _SESSION["pid"],
        "mode": "mailbox",
        "enabled": False,  # 默认不启用，对外部主机隐藏
    }, "POST", timeout=10)
    ag = (res or {}).get("agent")
    if isinstance(ag, dict) and ag.get("agent_id"):
        _SESSION["agent_id"] = ag["agent_id"]
        _SESSION["name"] = ag.get("name", _SESSION["name"])
        _SESSION["role"] = ag.get("role", _SESSION["role"])
        _SESSION["registered"] = True
    return res if "error" in (res or {}) else _SESSION


def _cleanup_session():
    """进程退出或会话终止时，主动向网关注销当前 agent，立即对外部主机结束发现。"""
    aid = _SESSION.get("agent_id")
    if aid and _SESSION.get("registered"):
        try:
            _raw_http("/api/agents/unregister", {"agent_id": aid}, "POST", timeout=2)
        except Exception:
            pass

atexit.register(_cleanup_session)

def _sig_handler(signum, frame):
    _cleanup_session()
    sys.exit(0)

try:
    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)
except Exception:
    pass


def call(path, payload=None, method="POST", timeout=900):
    reg = ensure_registered()
    if "error" in reg and not _SESSION["agent_id"]:
        return reg
    aid = _SESSION["agent_id"]
    if method == "GET":
        sep = "&" if "?" in path else "?"
        path = f"{path}{sep}agent_id={urllib.parse.quote(aid)}"
    elif isinstance(payload, dict):
        payload = dict(payload)
        payload.setdefault("from_agent_id", aid)
        payload.setdefault("agent_id", aid)
    return _raw_http(path, payload, method, timeout)


# 模块加载时尝试自动注册（若本机 gateway 尚未启动，后续首次调用工具时会自动补注册）
ensure_registered()

mcp = FastMCP("troupe")


@mcp.tool
def whoami() -> str:
    """查看当前 agent 在本机 gateway 上注册的唯一 agent_id、名称与角色信息。"""
    reg = ensure_registered()
    if "error" in reg:
        return json.dumps(reg, ensure_ascii=False)
    return json.dumps({
        "gateway": _cfg.get("name"),
        "agent_id": _SESSION["agent_id"],
        "name": _SESSION["name"],
        "role": _SESSION["role"],
        "cwd": _SESSION["cwd"],
        "pid": _SESSION["pid"],
    }, ensure_ascii=False)


@mcp.tool
def register_agent(name: str, role: str = "") -> str:
    """更新当前 agent 在本机 gateway 上的显示名称（name）和职责说明（role），保持同一唯一 agent_id。"""
    reg = ensure_registered(name=name, role=role)
    if "error" in reg:
        return json.dumps(reg, ensure_ascii=False)
    return whoami()


@mcp.tool
def list_peers() -> str:
    """【第 1 步：选择 Gateway】列出 troupe 中其他机器的 gateway 及其可达状态。
    选定目标 peer 后，必须再调用 list_agents(peer) 选择该 gateway 下的具体 agent_id，才能发消息。"""
    return json.dumps(call("/api/out/peers", {}), ensure_ascii=False)


@mcp.tool
def list_agents(peer: str = "") -> str:
    """【第 2 步：从 Gateway 中选择 Agent】列出目标 peer 网关上当前注册在线的所有 agent（含唯一 agent_id、name、mode、skills）。
    peer 留空或填 'self' 可查看本机 gateway 上的所有在线 agent。"""
    return json.dumps(call("/api/out/agents", {"peer": peer}), ensure_ascii=False)


@mcp.tool
def ask_peer(peer: str, agent_id: str, question: str, context: str = "") -> str:
    """【第 3 步：向指定 Gateway 的指定 Agent 提问】
    必须先通过 list_peers() 选择 peer（网关），再通过 list_agents(peer) 选择目标 agent_id。
    请求自动附带本会话唯一 from_agent_id，回答仅归属本 agent 接收。
    问题必须自包含：对方看不到你的会话上下文，请把相关文件路径、报错信息、约束条件写全。"""
    return json.dumps(call("/api/out/ask", {
        "peer": peer,
        "to_agent_id": agent_id,
        "question": question,
        "context": context,
    }), ensure_ascii=False)


@mcp.tool
def run_task(peer: str, agent_id: str, title: str, prompt: str, base_branch: str = "") -> str:
    """把重活派发给 peer 网关上的指定 agent_id（需选择 mode='driver' 的目标 agent）：
    它在独立 git worktree 分支上执行，不动它自己的工作区。
    返回 job_id，仅发起任务的本 agent 可通过 task_status(peer, job_id) 或 check_inbox() 获取结果。"""
    return json.dumps(call("/api/out/task", {
        "peer": peer,
        "to_agent_id": agent_id,
        "title": title,
        "prompt": prompt,
        "base": base_branch,
    }), ensure_ascii=False)


@mcp.tool
def task_status(peer: str, job_id: str) -> str:
    """查询本 agent 发给 peer 的任务/提问的进度与结果（status: running/done/error/pending_answer）。
    严格校验会话归属：只能查询本 agent 自己发出的 job_id。"""
    return json.dumps(call("/api/out/status", {"peer": peer, "job_id": job_id}), ensure_ascii=False)


@mcp.tool
def review_pr(peer: str, agent_id: str, pr_number: int, focus: str = "") -> str:
    """请 peer 网关上的指定 agent_id 审查某个 GitHub PR，返回审查意见文本（它不会发言论、不改代码）。"""
    return json.dumps(call("/api/out/review", {
        "peer": peer,
        "to_agent_id": agent_id,
        "pr": pr_number,
        "focus": focus,
    }), ensure_ascii=False)


# ---------- 跨机传文件（按 agent_id 精准投递，谁发的/发给谁的就谁收）----------

@mcp.tool
def send_file(peer: str, agent_id: str, path: str, note: str = "") -> str:
    """把本机文件发给 peer 网关上的指定 agent_id（存到对方网关，仅该目标 agent_id 可收取）。
    path 是本机文件路径（绝对路径，或相对本机 repo 的路径）。默认单文件上限 100MB。"""
    return json.dumps(call("/api/out/file/send", {
        "peer": peer,
        "to_agent_id": agent_id,
        "path": path,
        "note": note,
    }, timeout=1800), ensure_ascii=False)


@mcp.tool
def list_files(peer: str) -> str:
    """列出 peer 机器上网关代收、与本 agent 相关的待收取文件（file_id、文件名、大小、说明）。"""
    return json.dumps(call("/api/out/files", {"peer": peer}), ensure_ascii=False)


@mcp.tool
def fetch_file(peer: str = "", file_id: str = "", save_path: str = "") -> str:
    """取文件到本机（校验本 agent 归属权限）。两种用法：
    peer 填对方机器名 = 从对方网关拉文件；
    peer 留空 = 收取别人发到本机、且指定给本 agent_id 的文件（check_inbox 里看到的 file_id）。"""
    if peer:
        return json.dumps(call("/api/out/file/get", {
            "peer": peer,
            "file_id": file_id,
            "save_path": save_path,
        }, timeout=1800), ensure_ascii=False)
    return json.dumps(call(f"/api/file/{file_id}/collect", {"save_path": save_path}), ensure_ascii=False)


# ---------- 信箱模式（按本会话唯一 agent_id 严格隔离）----------

@mcp.tool
def enable_troupe() -> str:
    """【指令响应：启用troupe】当用户在会话中发送"启用troupe"或"启用 troupe"时调用。
    调用后，将在网关中正式激活本 Agent 为对外公开可发现状态，并进入常驻剧团巡检：
    1. 立即调用 check_inbox(timeout=10) 查看是否有分配给本 Agent 的待答问题或文件；
    2. 若 check_inbox 返回的 questions 列表中有待答问题：
       - 请针对该问题，充分调用当前本地代码搜索/阅读工具查阅项目代码，进行深度推理思考；
       - 组织完整、清晰的答复，调用 submit_answer(question_id, answer) 提交回答回推给提问方；
    3. 若 files 列表中有收到的新文件，按需调用 fetch_file(file_id=...) 接收；
    4. 保持周期性/长轮询调用 check_inbox(timeout=30) 静默待命，直至用户告知取消。"""
    reg = ensure_registered()
    aid = reg.get("agent_id") or _SESSION.get("agent_id", "unknown")
    # 向网关汇报：激活本 agent 为对外可见状态
    _raw_http("/api/agents/enable", {"agent_id": aid, "enabled": True}, "POST", timeout=5)
    return json.dumps({
        "status": "enabled",
        "agent_id": aid,
        "name": _SESSION.get("name", "mcp-agent"),
        "instruction": (
            "【已成功启用 troupe 剧团常驻巡检模式（已对其他主机公开可发现）】\n"
            "下一步行动准则：\n"
            "1. 请立即调用 check_inbox(timeout=10) 查阅信箱。\n"
            "2. 若发现待答问题，请查阅本地项目代码进行思考，并使用 submit_answer(question_id, answer) 提交回答。\n"
            "3. 随后请保持周期性或长轮询循环调用 check_inbox() 巡检待命。"
        )
    }, ensure_ascii=False)


@mcp.tool
def disable_troupe() -> str:
    """【指令响应：停用troupe】当用户在会话中发送"停用troupe"、"停止巡检"、"关闭troupe"时调用。
    调用后，将当前 Agent 在网关中恢复为隐身状态（不可被其他机器发现），并停止巡检。"""
    reg = ensure_registered()
    aid = reg.get("agent_id") or _SESSION.get("agent_id", "unknown")
    _raw_http("/api/agents/enable", {"agent_id": aid, "enabled": False}, "POST", timeout=5)
    return json.dumps({
        "status": "disabled",
        "agent_id": aid,
        "name": _SESSION.get("name", "mcp-agent"),
        "message": "【已成功停用 troupe 剧团巡检】本 Agent 已恢复为隐身状态，其他主机将不再可见。"
    }, ensure_ascii=False)


@mcp.tool
def check_inbox(timeout: int = 0) -> str:
    """查看发给本 agent（当前唯一 agent_id）的待答问题（questions）、远端回推的回答（replies）和传来的文件（files）。
    timeout: 长轮询等待秒数（0~60秒）。若>0且当前信箱为空，将保持阻塞等待直到有新消息到达或超时，避免无谓频繁空转。
    同机器上其他 agent 的消息不会出现在本信箱中。
    重要：若返回结果中的 questions 列表中有待答问题，请必须查阅本地代码思考后，调用 submit_answer(question_id, answer) 提交回答！"""
    path = f"/api/inbox?timeout={timeout}" if timeout > 0 else "/api/inbox"
    return json.dumps(call(path, None, "GET", timeout=max(timeout + 5, 30)), ensure_ascii=False)


@mcp.tool
def submit_answer(question_id: str, answer: str) -> str:
    """回答发给本 agent 信箱里的问题（先 check_inbox 拿到 question_id，作答后会自动回推给原发送方 agent）。
    仅该问题的目标接收 agent_id 本人可提交回答。"""
    return json.dumps(call(f"/api/jobs/{question_id}/answer", {"answer": answer}), ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
