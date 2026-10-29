"""MCP 前端冒烟测试：用 fastmcp 内存客户端走完 前端→gateway A→gateway B→假agent 全链路。
用法：TROUPE_CONFIG=test/config-a.json uv run --no-project --with fastmcp python test/smoke_mcp.py
（注意：config-a 的 peers.b 即 box-b，需先启动 gateway a 和 b）"""
import asyncio
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastmcp import Client
import mcp_frontend  # noqa: E402  读 TROUPE_CONFIG，须在 import 前设好


async def main():
    async with Client(mcp_frontend.mcp) as c:
        tools = await c.list_tools()
        print("MCP tools:", [t.name for t in tools])
        r = await c.call_tool("ask_peer", {"peer": "b", "question": "MCP 端到端测试"})
        print("ask_peer ->", r)
        r2 = await c.call_tool("list_peers", {})
        print("list_peers ->", r2)

        # 文件传输端到端：send → list → fetch → 内容比对
        src = pathlib.Path("/tmp/troupe-test/smoke-file.txt")
        src.write_text("file roundtrip 你好 123", encoding="utf-8")
        rf = await c.call_tool("send_file", {"peer": "b", "path": str(src), "note": "冒烟测试文件"})
        fid = json.loads(rf.data)["file_id"]
        rl = await c.call_tool("list_files", {"peer": "b"})
        assert any(f["id"] == fid for f in json.loads(rl.data)["files"]), "list_files 没看到刚发的文件"
        rg = await c.call_tool("fetch_file", {"peer": "b", "file_id": fid, "save_path": "/tmp/troupe-test/back/"})
        back = pathlib.Path(json.loads(rg.data)["saved_to"])
        assert back.read_text(encoding="utf-8") == src.read_text(encoding="utf-8"), "往返内容不一致"
        print("file roundtrip ->", back, "OK")


asyncio.run(main())
