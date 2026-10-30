"""MCP 多 Agent 端到端冒烟测试：
验证：
1. 每台机器唯一 gateway 下挂载多个 MCP agent 会话，各自自动注册并生成唯一 agent_id
2. 首个发消息者：先选 gateway (list_peers)，再选该 gateway 下的 agent (list_agents)，未选 agent 拒绝发送
3. 谁发的就谁收：
   - 接收侧按 to_agent_id 严格隔离信箱与回答权限（同机器其他 agent 看不到、不能代答）
   - 发送侧按 from_agent_id 严格隔离查询权限与回推信箱（同机器其他 agent 查不到、收不到回复）
   - 文件收发同样按 agent_id 隔离

用法：TROUPE_CONFIG=test/config-a.json uv run --no-project --with fastmcp python test/smoke_mcp.py
（需先启动 gateway a、b、c）"""
import asyncio
import json
import os
import pathlib
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastmcp import Client
import mcp_frontend  # noqa: E402  读 TROUPE_CONFIG，须在 import 前设好


def http_call(url, token, payload=None, method="POST"):
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", "Bearer " + token)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=15) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


async def main():
    async with Client(mcp_frontend.mcp) as c:
        tools = await c.list_tools()
        print("MCP tools:", [t.name for t in tools])

        # 1. 验证本机 MCP 会话自动注册并生成唯一 agent_id (Agent A1)
        me = json.loads((await c.call_tool("whoami", {})).data)
        ag_a1 = me["agent_id"]
        assert ag_a1.startswith("ag-"), f"未生成合法 agent_id: {me}"
        print("Agent A1 auto-registered:", ag_a1)

        # 在同一台 Gateway A 上模拟第二个 MCP Agent 会话 (Agent A2)
        _, reg_a2 = http_call("http://127.0.0.1:18765/api/agents/register", "ta",
                              {"name": "agent-a2", "role": "同机旁路Agent", "pid": os.getpid()})
        ag_a2 = reg_a2["agent"]["agent_id"]
        assert ag_a2 != ag_a1, "同机不同会话必须生成不同唯一 agent_id"
        print("Agent A2 registered on same Gateway A:", ag_a2)

        # 在 Gateway C (mailbox) 上模拟两个挂载 MCP 的 Agent 会话 (Agent C1, Agent C2)
        _, reg_c1 = http_call("http://127.0.0.1:18767/api/agents/register", "tc",
                              {"name": "zcode-c1", "role": "主力开发", "pid": os.getpid()})
        _, reg_c2 = http_call("http://127.0.0.1:18767/api/agents/register", "tc",
                              {"name": "zcode-c2", "role": "测试专员", "pid": os.getpid()})
        ag_c1 = reg_c1["agent"]["agent_id"]
        ag_c2 = reg_c2["agent"]["agent_id"]
        print("Gateway C registered 2 agents:", ag_c1, ag_c2)

        # 2. 第一步：选择 Gateway (list_peers)
        peers = json.loads((await c.call_tool("list_peers", {})).data)
        assert peers["b"]["reachable"] and peers["c"]["reachable"], f"peers 不可达: {peers}"
        print("Step 1 list_peers ->", list(peers.keys()))

        # 3. 第二步：从目标 Gateway 中选择 Agent (list_agents)
        agents_b = json.loads((await c.call_tool("list_agents", {"peer": "b"})).data)["agents"]
        ag_b_driver = agents_b[0]["agent_id"]
        print("Step 2 list_agents(b) ->", [a["agent_id"] for a in agents_b])

        agents_c = json.loads((await c.call_tool("list_agents", {"peer": "c"})).data)["agents"]
        c_ids = [a["agent_id"] for a in agents_c]
        assert ag_c1 in c_ids and ag_c2 in c_ids, f"Gateway C 应同时列出 C1 与 C2: {c_ids}"
        print("Step 2 list_agents(c) ->", c_ids)

        # 校验未选 agent_id 时直接发消息会被拦截
        err_no_ag = json.loads((await c.call_tool("ask_peer", {
            "peer": "b", "agent_id": "", "question": "无目标测试"
        })).data)
        assert "error" in err_no_ag, f"未传 agent_id 应当报错拒绝: {err_no_ag}"

        # 4. 第三步：向 Gateway B 的内置 Driver Agent 提问 & 跑任务
        r_ask_b = json.loads((await c.call_tool("ask_peer", {
            "peer": "b", "agent_id": ag_b_driver, "question": "MCP 多Agent端到端测试"
        })).data)
        assert r_ask_b["status"] == "done" and r_ask_b["from_agent_id"] == ag_a1
        print("ask_peer(b, ag_b_driver) ->", r_ask_b["answer"])

        # 5. 验证「谁发的就谁收」：Agent A1 向 Gateway C 的 Agent C1 提问
        r_ask_c = json.loads((await c.call_tool("ask_peer", {
            "peer": "c", "agent_id": ag_c1, "question": "请问 C1 数据库连接池配置在哪？"
        })).data)
        assert r_ask_c["status"] == "pending"
        qid = r_ask_c["job_id"]
        print("Agent A1 -> Agent C1 pending question:", qid)

        # 5.1 接收侧隔离验证：C2 查看信箱看不到发给 C1 的问题；C1 查看信箱能看到
        _, inbox_c2 = http_call(f"http://127.0.0.1:18767/api/inbox?agent_id={ag_c2}", "tc", method="GET")
        assert len(inbox_c2["questions"]) == 0, f"C2 不应看到发给 C1 的问题: {inbox_c2}"
        _, inbox_c1 = http_call(f"http://127.0.0.1:18767/api/inbox?agent_id={ag_c1}", "tc", method="GET")
        assert any(q["id"] == qid for q in inbox_c1["questions"]), "C1 应看到发给自己的问题"

        # 5.2 越权回答拦截：C2 尝试代替 C1 回答 -> 403
        code_c2, _ = http_call(f"http://127.0.0.1:18767/api/jobs/{qid}/answer", "tc",
                               {"agent_id": ag_c2, "answer": "C2 越权抢答"})
        assert code_c2 == 403, f"C2 越权作答必须返回 403，实际: {code_c2}"

        # 5.3 发送侧隔离验证：同在 Gateway A 的 Agent A2 尝试查询 A1 发起的 job_id -> 403
        _, st_a2 = http_call("http://127.0.0.1:18765/api/out/status", "ta",
                             {"peer": "c", "job_id": qid, "from_agent_id": ag_a2})
        assert "error" in st_a2 and "403" in st_a2["error"], f"A2 不应能查看 A1 的会话状态: {st_a2}"

        # 5.4 C1 正常回答问题 -> 自动回推至 Gateway A 的 Agent A1 信箱
        code_c1, ans_res = http_call(f"http://127.0.0.1:18767/api/jobs/{qid}/answer", "tc",
                                     {"agent_id": ag_c1, "answer": "在 config/db.py 第 42 行"})
        assert code_c1 == 200 and ans_res.get("ok"), f"C1 作答失败: {ans_res}"

        # 5.5 验证谁发的就谁收：A1 通过 task_status 和 check_inbox 均能收到，A2 的 check_inbox 收不到
        st_a1 = json.loads((await c.call_tool("task_status", {"peer": "c", "job_id": qid})).data)
        assert st_a1["status"] == "done" and st_a1["answer"] == "在 config/db.py 第 42 行"

        _, inbox_a2 = http_call(f"http://127.0.0.1:18765/api/inbox?agent_id={ag_a2}", "ta", method="GET")
        assert len(inbox_a2["replies"]) == 0, f"A2 信箱不应收到 A1 的回复: {inbox_a2}"

        inbox_a1 = json.loads((await c.call_tool("check_inbox", {})).data)
        assert any(r["remote_job_id"] == qid and r["answer"] == "在 config/db.py 第 42 行"
                   for r in inbox_a1["replies"]), f"A1 信箱应收到回推的答案: {inbox_a1}"
        print("Sender/Receiver strict affinity verified: A1 sent -> C1 answered -> A1 received!")

        # 6. 文件传输端到端（指定目标 agent_id）：send → list → fetch → 内容比对
        src = pathlib.Path("/tmp/troupe-test/smoke-file.txt")
        src.write_text("file roundtrip 你好 123", encoding="utf-8")
        rf = await c.call_tool("send_file", {
            "peer": "b", "agent_id": ag_b_driver, "path": str(src), "note": "冒烟测试文件"
        })
        fid = json.loads(rf.data)["file_id"]
        rl = await c.call_tool("list_files", {"peer": "b"})
        assert any(f["id"] == fid for f in json.loads(rl.data)["files"]), "list_files 没看到刚发的文件"
        rg = await c.call_tool("fetch_file", {"peer": "b", "file_id": fid, "save_path": "/tmp/troupe-test/back/"})
        back = pathlib.Path(json.loads(rg.data)["saved_to"])
        assert back.read_text(encoding="utf-8") == src.read_text(encoding="utf-8"), "往返内容不一致"
        print("file roundtrip ->", back, "OK")


asyncio.run(main())
