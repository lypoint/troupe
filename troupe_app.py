#!/usr/bin/env python3
"""
troupe 原生可视化应用 (troupe_app.py) —— 无需 WebUI，纯 Python 标准库实现：
1. 原生桌面窗口 App（基于 tkinter / ttk，默认在桌面环境自动弹出独立应用窗口）
2. 终端全屏交互 App（基于 curses，可通过 `--tui` 启动或在无桌面环境自动切换）

三大核心可视化模块：
- 【启动流程可视化】：5 步启动流水线进度动画、每步耗时与状态详情、应用内局域网扫描与 Peer 节点配置
- 【运行过程可视化】：本机唯一 Gateway 与多挂载 Agent 实时状态卡片（PID、心跳、模式）、远端 Peer 网关与远端 Agent 拓扑树、Git Worktree 沙箱监控与分类着色实时事件流
- 【消息投递状态可视化】：端到端 5 阶段可视化流转图（①已发出 ➔ ②目标入箱/执行中 ➔ ③已作答/完成 ➔ ④已回推原发送方 ➔ ⑤原发送方已收阅）、逐跳时间戳轨迹、完整报文查看及应用内直接作答
"""
import curses
import os
import sys
import threading
import time


# =====================================================================
# 1. 原生桌面窗口 App (tkinter)
# =====================================================================

def run_tkinter_app(gw):
    import tkinter as tk
    from tkinter import ttk, messagebox
    import subprocess
    import platform

    root = tk.Tk()
    default_name = f"troupe-{gw.secrets.randbelow(90000) + 10000}" if hasattr(gw, "secrets") else "troupe-00000"
    cfg_name = gw.CFG.get("name") or default_name
    host, port = gw.CFG.get("listen", ["127.0.0.1", 8765])
    root.title(f"troupe 🎭 多 Agent 协作网关 App — {cfg_name} ({host}:{port})")
    root.geometry("1220x800")
    root.minsize(1020, 680)
    root.configure(bg="#0f172a")

    # 设置 App 专属图标（窗口标题栏图标 + macOS Dock 栏图标）
    try:
        from troupe_icon_b64 import TROUPE_ICON_GIF_B64
        app_icon_img = tk.PhotoImage(data=TROUPE_ICON_GIF_B64)
        root.iconphoto(True, app_icon_img)
    except Exception:
        pass

    # macOS 桌面环境：设置 Dock 栏图标并激活前台窗口（防止闪退到后台）
    if platform.system() == "Darwin":
        try:
            icon_png_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "troupe_icon.png")
            set_icon_line = ""
            if os.path.exists(icon_png_path):
                set_icon_line = f'''set img to current application's NSImage's alloc()'s initWithContentsOfFile:"{icon_png_path}"
current application's NSApplication's sharedApplication()'s setApplicationIconImage:img'''
            scpt = f'''use framework "AppKit"
use scripting additions
{set_icon_line}
tell application "System Events" to set frontmost of every process whose unix id is {os.getpid()} to true
'''
            subprocess.run(["osascript", "-e", scpt], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    try:
        root.lift()
        root.focus_force()
    except Exception:
        pass

    # 配色常量
    BG_DARK = "#0f172a"
    BG_PANEL = "#1e293b"
    BG_CARD = "#26354d"
    FG_MAIN = "#f8fafc"
    FG_MUTED = "#94a3b8"
    ACCENT_BLUE = "#38bdf8"
    ACCENT_GREEN = "#22c55e"
    ACCENT_AMBER = "#f59e0b"
    ACCENT_RED = "#ef4444"
    ACCENT_PURPLE = "#a855f7"

    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:
        pass

    style.configure("TFrame", background=BG_DARK)
    style.configure("Panel.TFrame", background=BG_PANEL)
    style.configure("Card.TFrame", background=BG_CARD)
    style.configure("TLabel", background=BG_DARK, foreground=FG_MAIN, font=("Helvetica", 11))
    style.configure("Panel.TLabel", background=BG_PANEL, foreground=FG_MAIN, font=("Helvetica", 11))
    style.configure("Muted.TLabel", background=BG_PANEL, foreground=FG_MUTED, font=("Helvetica", 10))
    style.configure("Title.TLabel", background=BG_DARK, foreground=FG_MAIN, font=("Helvetica", 16, "bold"))
    style.configure("TNotebook", background=BG_DARK, borderwidth=0)
    style.configure("TNotebook.Tab", background=BG_PANEL, foreground=FG_MUTED,
                    padding=(18, 9), font=("Helvetica", 12, "bold"))
    style.map("TNotebook.Tab",
              background=[("selected", ACCENT_BLUE)],
              foreground=[("selected", "#0f172a")],
              expand=[("selected", [0, 0, 0, 0]), ("!selected", [0, 0, 0, 0])],
              padding=[("selected", (18, 9)), ("!selected", (18, 9))])
    style.configure("Treeview", background=BG_PANEL, fieldbackground=BG_PANEL,
                    foreground=FG_MAIN, rowheight=28, font=("Helvetica", 11), borderwidth=0)
    style.configure("Treeview.Heading", background=BG_CARD, foreground=ACCENT_BLUE,
                    font=("Helvetica", 11, "bold"))
    style.map("Treeview", background=[("selected", "#0284c7")], foreground=[("selected", "#ffffff")])

    # 按钮规范样式（解决 macOS 下原生 tk.Button 白色背景/字体看不清问题）
    style.configure("Primary.TButton", background=ACCENT_BLUE, foreground="#0f172a",
                    font=("Helvetica", 10, "bold"), borderwidth=0, padding=(10, 5))
    style.map("Primary.TButton",
              background=[("active", "#0284c7"), ("pressed", "#0369a1")],
              foreground=[("active", "#ffffff"), ("pressed", "#ffffff")])

    style.configure("Secondary.TButton", background=BG_CARD, foreground=FG_MAIN,
                    font=("Helvetica", 10), borderwidth=0, padding=(10, 5))
    style.map("Secondary.TButton",
              background=[("active", "#334155"), ("pressed", "#1e293b")],
              foreground=[("active", ACCENT_BLUE), ("pressed", ACCENT_BLUE)])

    style.configure("Success.TButton", background=ACCENT_GREEN, foreground="#0f172a",
                    font=("Helvetica", 10, "bold"), borderwidth=0, padding=(10, 5))
    style.map("Success.TButton",
              background=[("active", "#16a34a"), ("pressed", "#15803d")],
              foreground=[("active", "#ffffff"), ("pressed", "#ffffff")])

    # ---------------- 顶部全局状态栏 (App Header) ----------------
    header = tk.Frame(root, bg="#0b1120", height=64, padx=18, pady=10)
    header.pack(fill="x")

    title_lbl = tk.Label(
        header,
        text=f"🎭 troupe App   |   本机唯一网关: {cfg_name}",
        bg="#0b1120", fg=FG_MAIN, font=("Helvetica", 16, "bold")
    )
    title_lbl.pack(side="left")

    status_pill = tk.Label(
        header,
        text="● 正在启动...",
        bg="#0b1120", fg=ACCENT_GREEN, font=("Helvetica", 12, "bold"), padx=14
    )
    status_pill.pack(side="right")

    meta_lbl = tk.Label(
        header,
        text=f"监听端口: {port}   |   默认驱动: {gw.DRIVER.get('type')}   |   仓库: {gw.CFG.get('repo')}",
        bg="#0b1120", fg=FG_MUTED, font=("Helvetica", 11)
    )
    meta_lbl.pack(side="right", padx=12)

    # ---------------- 主导航页签 (3 大可视化模块) ----------------
    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True, padx=14, pady=(8, 14))

    tab_startup = ttk.Frame(notebook)
    tab_runtime = ttk.Frame(notebook)
    tab_delivery = ttk.Frame(notebook)

    notebook.add(tab_startup, text=" 🚀 1. 启动流程可视化 ")
    notebook.add(tab_runtime, text=" ⚡ 2. 运行过程与多Agent拓扑 ")
    notebook.add(tab_delivery, text=" 📬 3. 消息投递状态可视化 ")

    # =================================================================
    # 页签 1：启动流程可视化 + 应用内节点配置向导
    # =================================================================
    startup_top = tk.Frame(tab_startup, bg=BG_PANEL, padx=16, pady=14)
    startup_top.pack(fill="x", pady=(6, 10))

    tk.Label(
        startup_top,
        text="启动流水线 (Startup Pipeline)",
        bg=BG_PANEL, fg=ACCENT_BLUE, font=("Helvetica", 14, "bold")
    ).pack(anchor="w")

    startup_canvas = tk.Canvas(startup_top, bg=BG_PANEL, height=130, highlightthickness=0)
    startup_canvas.pack(fill="x", pady=(8, 4))

    startup_Mid = tk.Frame(tab_startup, bg=BG_DARK)
    startup_Mid.pack(fill="both", expand=True)

    # 左侧：启动步骤详情表格
    step_frame = tk.Frame(startup_Mid, bg=BG_PANEL, padx=14, pady=12)
    step_frame.pack(side="left", fill="both", expand=True, padx=(0, 6))
    tk.Label(step_frame, text="启动阶段明细与自检结果", bg=BG_PANEL, fg=FG_MAIN,
             font=("Helvetica", 13, "bold")).pack(anchor="w", pady=(0, 8))

    step_tree = ttk.Treeview(step_frame, columns=("step", "status", "time", "detail"), show="headings", height=7)
    step_tree.heading("step", text="启动阶段")
    step_tree.heading("status", text="状态")
    step_tree.heading("time", text="完成时间")
    step_tree.heading("detail", text="自检与执行详情")
    step_tree.column("step", width=180, anchor="w")
    step_tree.column("status", width=90, anchor="center")
    step_tree.column("time", width=90, anchor="center")
    step_tree.column("detail", width=360, anchor="w")
    step_tree.pack(fill="both", expand=True)

    # 右侧：应用内 Peer 网关配置与局域网扫描向导
    wizard_frame = tk.Frame(startup_Mid, bg=BG_PANEL, width=440, padx=14, pady=12)
    wizard_frame.pack(side="right", fill="both", padx=(6, 0))
    wizard_frame.pack_propagate(False)

    tk.Label(wizard_frame, text="🌐 远端 Gateway 配置与局域网发现", bg=BG_PANEL, fg=ACCENT_BLUE,
             font=("Helvetica", 13, "bold")).pack(anchor="w", pady=(0, 8))

    form = tk.Frame(wizard_frame, bg=BG_PANEL)
    form.pack(fill="x")

    tk.Label(form, text="对方网关别名 (如 codex-box):", bg=BG_PANEL, fg=FG_MUTED).grid(row=0, column=0, sticky="w", pady=3)
    ent_peer_name = tk.Entry(form, bg=BG_CARD, fg=FG_MAIN, insertbackground=FG_MAIN, relief="flat", width=24)
    ent_peer_name.grid(row=0, column=1, sticky="ew", pady=3, padx=6)

    tk.Label(form, text="对方 URL (如 192.168.1.20:8765):", bg=BG_PANEL, fg=FG_MUTED).grid(row=1, column=0, sticky="w", pady=3)
    ent_peer_url = tk.Entry(form, bg=BG_CARD, fg=FG_MAIN, insertbackground=FG_MAIN, relief="flat", width=24)
    ent_peer_url.grid(row=1, column=1, sticky="ew", pady=3, padx=6)

    tk.Label(form, text="对方 Bearer Token:", bg=BG_PANEL, fg=FG_MUTED).grid(row=2, column=0, sticky="w", pady=3)
    ent_peer_token = tk.Entry(form, bg=BG_CARD, fg=FG_MAIN, insertbackground=FG_MAIN, relief="flat", width=24)
    ent_peer_token.grid(row=2, column=1, sticky="ew", pady=3, padx=6)
    form.columnconfigure(1, weight=1)

    wiz_status_lbl = tk.Label(wizard_frame, text="提示：可直接添加对方 Gateway，或点击扫描局域网自动发现",
                              bg=BG_PANEL, fg=FG_MUTED, wraplength=410, justify="left")
    wiz_status_lbl.pack(anchor="w", pady=6)

    def on_add_peer():
        u = ent_peer_url.get().strip()
        t = ent_peer_token.get().strip()
        n = ent_peer_name.get().strip()
        if not u:
            wiz_status_lbl.configure(text="✗ 请填写对方 Gateway 的 URL 地址", fg=ACCENT_RED)
            return
        saved_name, saved_url = gw.save_peer_to_config(n, u, t)
        wiz_status_lbl.configure(text=f"✓ 已保存 peers['{saved_name}'] = {saved_url}，正在后台探测...", fg=ACCENT_GREEN)
        threading.Thread(target=gw.probe_peers_on_startup, daemon=True).start()

    # 扫描发现的候选网关列表（点击自动填入输入框）
    scan_results_frame = tk.Frame(wizard_frame, bg=BG_PANEL)
    scan_results_frame.pack(fill="both", expand=True, pady=(4, 6))

    scan_hdr_lbl = tk.Label(scan_results_frame, text="🔍 发现的网关列表 (单击项自动填入上方输入框):",
                            bg=BG_PANEL, fg=ACCENT_BLUE, font=("Helvetica", 10, "bold"))
    scan_hdr_lbl.pack(anchor="w", pady=(2, 4))

    scan_tree = ttk.Treeview(
        scan_results_frame, columns=("name", "url", "matched"), show="headings", height=4
    )
    scan_tree.heading("name", text="网关名称")
    scan_tree.heading("url", text="IP:端口 / URL")
    scan_tree.heading("matched", text="Token匹配")
    scan_tree.column("name", width=120, anchor="w")
    scan_tree.column("url", width=200, anchor="w")
    scan_tree.column("matched", width=80, anchor="center")
    scan_tree.pack(fill="both", expand=True)

    def on_select_scanned_peer(event=None):
        sel = scan_tree.selection()
        if not sel:
            return
        vals = scan_tree.item(sel[0], "values")
        if vals:
            name_val, url_val = vals[0], vals[1]
            ent_peer_name.delete(0, "end")
            ent_peer_name.insert(0, name_val)
            ent_peer_url.delete(0, "end")
            ent_peer_url.insert(0, url_val)
            wiz_status_lbl.configure(
                text=f"已选定 [{name_val}] ({url_val})，点击【保存并连接 Peer】即可保存",
                fg=ACCENT_BLUE
            )

    scan_tree.bind("<<TreeviewSelect>>", on_select_scanned_peer)

    def on_scan_lan():
        t = ent_peer_token.get().strip()
        wiz_status_lbl.configure(text=f"⏳ 正在并发扫描本机局域网网段 (端口 {port})...", fg=ACCENT_AMBER)
        scan_tree.delete(*scan_tree.get_children())

        def _scan():
            found = gw.scan_lan(port, t)
            if not found:
                root.after(0, lambda: wiz_status_lbl.configure(
                    text="（扫描完成：局域网未发现其他 troupe 网关）", fg=ACCENT_AMBER))
                return

            def _populate():
                scan_tree.delete(*scan_tree.get_children())
                for url, card, matched in found:
                    cname = card.get("name", "") or "unknown"
                    m_text = "已匹配" if matched else ("未填Token" if not t else "不匹配")
                    scan_tree.insert("", "end", values=(cname, url, m_text))
                    # 自动保存到 config
                    gw.save_peer_to_config(cname, url, t)
                gw.probe_peers_on_startup()
                wiz_status_lbl.configure(
                    text=f"✓ 扫描发现 {len(found)} 个网关（已列于下方，单击可回填）",
                    fg=ACCENT_GREEN
                )

            root.after(0, _populate)

        threading.Thread(target=_scan, daemon=True).start()

    btn_bar = tk.Frame(wizard_frame, bg=BG_PANEL)
    btn_bar.pack(fill="x", pady=4)
    ttk.Button(btn_bar, text="➕ 保存并连接 Peer", command=on_add_peer,
               style="Primary.TButton").pack(side="left", padx=(0, 8))
    ttk.Button(btn_bar, text="🔍 扫描局域网网关", command=on_scan_lan,
               style="Secondary.TButton").pack(side="left")
    ttk.Button(btn_bar, text="🔄 重新探测", command=lambda: threading.Thread(target=gw.probe_peers_on_startup, daemon=True).start(),
               style="Secondary.TButton").pack(side="right")

    # =================================================================
    # 页签 2：运行过程可视化（多 Agent 拓扑 + 实时事件流）
    # =================================================================
    runtime_pane = tk.PanedWindow(tab_runtime, orient="horizontal", bg=BG_DARK, sashwidth=6, bd=0)
    runtime_pane.pack(fill="both", expand=True, pady=6)

    left_topo = tk.Frame(runtime_pane, bg=BG_PANEL, padx=12, pady=10)
    right_stream = tk.Frame(runtime_pane, bg=BG_PANEL, padx=12, pady=10)
    runtime_pane.add(left_topo, width=540)
    runtime_pane.add(right_stream)

    # 左上：本机 Gateway 下挂载的多个 Agent 实时表
    tk.Label(left_topo, text=f"📍 本机网关 ({cfg_name}) 已注册的在线 Agents", bg=BG_PANEL, fg=ACCENT_BLUE,
             font=("Helvetica", 13, "bold")).pack(anchor="w", pady=(0, 6))

    local_ag_tree = ttk.Treeview(
        left_topo, columns=("agent_id", "name", "mode", "pid", "last_seen"), show="headings", height=7
    )
    for col, txt, w in (
        ("agent_id", "唯一 Agent ID", 170),
        ("name", "名称", 110),
        ("mode", "模式", 75),
        ("pid", "进程 PID", 75),
        ("last_seen", "最近心跳", 85),
    ):
        local_ag_tree.heading(col, text=txt)
        local_ag_tree.column(col, width=w, anchor="w" if col in ("agent_id", "name") else "center")
    local_ag_tree.pack(fill="x", pady=(0, 12))

    # 左下：远端 Peer Gateways 及各网关下的 Agent 拓扑树
    peer_hdr = tk.Frame(left_topo, bg=BG_PANEL)
    peer_hdr.pack(fill="x", pady=(4, 6))
    tk.Label(peer_hdr, text="🌐 远端 Peer Gateways → 远端在线 Agents (两步选路拓扑)", bg=BG_PANEL, fg=ACCENT_GREEN,
             font=("Helvetica", 13, "bold")).pack(side="left")

    remote_cache = {"peers": {}, "last_fetch": 0.0}

    def refresh_remote_topology_async():
        def _worker():
            info = gw.peers_info()
            remote_cache["peers"] = info
            remote_cache["last_fetch"] = time.time()
        threading.Thread(target=_worker, daemon=True).start()

    ttk.Button(peer_hdr, text="🔄 刷新远端拓扑", command=refresh_remote_topology_async,
               style="Secondary.TButton").pack(side="right")

    peer_topo_tree = ttk.Treeview(
        left_topo, columns=("node", "id_or_url", "mode", "status"), show="tree headings", height=9
    )
    peer_topo_tree.heading("#0", text="网关 / Agent 层级")
    peer_topo_tree.heading("node", text="类型")
    peer_topo_tree.heading("id_or_url", text="唯一 Agent ID / 网关地址")
    peer_topo_tree.heading("mode", text="模式")
    peer_topo_tree.heading("status", text="状态")
    peer_topo_tree.column("#0", width=150)
    peer_topo_tree.column("node", width=70, anchor="center")
    peer_topo_tree.column("id_or_url", width=180, anchor="w")
    peer_topo_tree.column("mode", width=70, anchor="center")
    peer_topo_tree.column("status", width=80, anchor="center")
    peer_topo_tree.pack(fill="both", expand=True)

    # 右侧：实时运行过程事件流 (Runtime Event Stream)
    ev_hdr = tk.Frame(right_stream, bg=BG_PANEL)
    ev_hdr.pack(fill="x", pady=(0, 6))
    tk.Label(ev_hdr, text="⚡ 实时运行过程事件流 (Live Runtime Stream)", bg=BG_PANEL, fg=ACCENT_AMBER,
             font=("Helvetica", 13, "bold")).pack(side="left")

    ev_tree = ttk.Treeview(
        right_stream, columns=("time", "cat", "actor", "target", "summary"), show="headings", height=20
    )
    for col, txt, w in (
        ("time", "时间", 75),
        ("cat", "阶段类别", 85),
        ("actor", "发起方", 130),
        ("target", "目标方", 130),
        ("summary", "运行过程详情", 320),
    ):
        ev_tree.heading(col, text=txt)
        ev_tree.column(col, width=w, anchor="w" if col in ("summary", "actor", "target") else "center")
    ev_tree.pack(fill="both", expand=True)

    # =================================================================
    # 页签 3：消息投递状态可视化（5 阶段可视化流转画布 + 逐跳轨迹 + 应用内作答）
    # =================================================================
    deliv_top = tk.Frame(tab_delivery, bg=BG_PANEL, padx=14, pady=10)
    deliv_top.pack(fill="x", pady=(6, 8))

    stats_lbl = tk.Label(
        deliv_top,
        text="消息投递总览：加载中...",
        bg=BG_PANEL, fg=FG_MAIN, font=("Helvetica", 12, "bold")
    )
    stats_lbl.pack(side="left")

    tk.Label(
        deliv_top,
        text="谁发的就谁收：每条会话严格绑定 [from_peer / from_agent_id] ➔ [to_peer / to_agent_id]",
        bg=BG_PANEL, fg=ACCENT_BLUE, font=("Helvetica", 11)
    ).pack(side="right")

    # 中部：5 阶段消息投递状态流转可视化画布
    stage_box = tk.Frame(tab_delivery, bg=BG_PANEL, padx=14, pady=10)
    stage_box.pack(fill="x", pady=(0, 8))

    selected_route_lbl = tk.Label(
        stage_box,
        text="👇 请在下方列表点击任意消息/任务，查看其端到端 5 阶段投递状态流转图：",
        bg=BG_PANEL, fg=ACCENT_BLUE, font=("Helvetica", 12, "bold")
    )
    selected_route_lbl.pack(anchor="w")

    delivery_canvas = tk.Canvas(stage_box, bg=BG_PANEL, height=105, highlightthickness=0)
    delivery_canvas.pack(fill="x", pady=(6, 2))

    # 下部左右分栏：左侧消息列表，右侧单条消息逐跳轨迹 + 内容详情 + 应用内作答
    deliv_pane = tk.PanedWindow(tab_delivery, orient="horizontal", bg=BG_DARK, sashwidth=6, bd=0)
    deliv_pane.pack(fill="both", expand=True)

    jobs_frame = tk.Frame(deliv_pane, bg=BG_PANEL, padx=10, pady=10)
    detail_frame = tk.Frame(deliv_pane, bg=BG_PANEL, padx=12, pady=10)
    deliv_pane.add(jobs_frame, width=640)
    deliv_pane.add(detail_frame)

    tk.Label(jobs_frame, text="会话与消息投递列表（点击行查看全链路轨迹）", bg=BG_PANEL, fg=FG_MAIN,
             font=("Helvetica", 12, "bold")).pack(anchor="w", pady=(0, 6))

    job_tree = ttk.Treeview(
        jobs_frame,
        columns=("id", "kind", "route", "stage", "status", "title"),
        show="headings", height=14
    )
    for col, txt, w in (
        ("id", "消息/任务 ID", 135),
        ("kind", "类型", 55),
        ("route", "会话路由 (发起方 ➔ 接收方)", 215),
        ("stage", "投递进度阶段", 125),
        ("status", "状态", 80),
        ("title", "内容摘要", 160),
    ):
        job_tree.heading(col, text=txt)
        job_tree.column(col, width=w, anchor="w" if col in ("id", "route", "title") else "center")
    job_tree.pack(fill="both", expand=True)

    # 右侧详情区：逐跳轨迹 + 报文正文 + 信箱直接作答
    tk.Label(detail_frame, text="🔍 投递逐跳轨迹与报文内容 (Hop-by-Hop Timeline & Payload)", bg=BG_PANEL,
             fg=ACCENT_BLUE, font=("Helvetica", 12, "bold")).pack(anchor="w", pady=(0, 6))

    detail_txt = tk.Text(
        detail_frame, bg=BG_CARD, fg=FG_MAIN, font=("Menlo", 11),
        height=13, wrap="word", relief="flat", padx=10, pady=8
    )
    detail_txt.pack(fill="both", expand=True, pady=(0, 8))

    # 应用内信箱作答栏（当选中 status == pending_answer 的问题时可直接作答）
    reply_bar = tk.Frame(detail_frame, bg=BG_PANEL)
    reply_bar.pack(fill="x")
    tk.Label(reply_bar, text="信箱作答:", bg=BG_PANEL, fg=FG_MUTED).pack(side="left")
    ent_answer = tk.Entry(reply_bar, bg=BG_CARD, fg=FG_MAIN, insertbackground=FG_MAIN, relief="flat")
    ent_answer.pack(side="left", fill="x", expand=True, padx=6, ipady=4)

    selected_job_id = {"id": None}

    def on_submit_in_app_answer():
        jid = selected_job_id["id"]
        ans = ent_answer.get().strip()
        if not jid:
            messagebox.showinfo("提示", "请先在左侧选择一条待作答 (pending_answer) 的消息")
            return
        if not ans:
            messagebox.showwarning("提示", "请输入回答内容")
            return
        j = gw.get_job(jid)
        if not j or j.get("status") != "pending_answer":
            messagebox.showwarning("提示", f"消息 {jid} 当前状态不是 pending_answer，无需作答")
            return
        _, code, err = gw.answer_job(jid, ans, responder_agent_id=j.get("to_agent_id", ""))
        if err:
            messagebox.showerror("作答失败", f"HTTP {code}: {err}")
        else:
            ent_answer.delete(0, "end")
            refresh_ui()

    ttk.Button(
        reply_bar, text="📤 以目标 Agent 身份作答并回推", command=on_submit_in_app_answer,
        style="Success.TButton"
    ).pack(side="right")

    # ---------------- 画布绘制函数 ----------------

    def draw_startup_pipeline(steps):
        startup_canvas.delete("all")
        w = max(startup_canvas.winfo_width(), 900)
        n = len(steps)
        step_w = w / n
        y_center = 52

        color_map = {
            "done": ACCENT_GREEN,
            "running": ACCENT_BLUE,
            "warn": ACCENT_AMBER,
            "error": ACCENT_RED,
            "pending": "#475569",
        }
        icon_map = {
            "done": "✓",
            "running": "↻",
            "warn": "!",
            "error": "✗",
            "pending": "·",
        }

        for i, s in enumerate(steps):
            cx = step_w * i + step_w / 2
            st = s.get("status", "pending")
            col = color_map.get(st, "#475569")
            if i < n - 1:
                nx = step_w * (i + 1) + step_w / 2
                line_col = ACCENT_GREEN if st == "done" else "#334155"
                startup_canvas.create_line(cx + 22, y_center, nx - 22, y_center, fill=line_col, width=4)

            startup_canvas.create_oval(cx - 22, y_center - 22, cx + 22, y_center + 22,
                                       fill=col, outline="#0f172a", width=2)
            startup_canvas.create_text(cx, y_center, text=icon_map.get(st, "·"),
                                       fill="#0f172a", font=("Helvetica", 15, "bold"))
            startup_canvas.create_text(cx, y_center + 38, text=s["title"],
                                       fill=FG_MAIN, font=("Helvetica", 11, "bold"))
            short_detail = (s.get("detail") or "")[:28]
            startup_canvas.create_text(cx, y_center + 58, text=short_detail,
                                       fill=FG_MUTED, font=("Helvetica", 9))

    def draw_delivery_pipeline(job):
        delivery_canvas.delete("all")
        w = max(delivery_canvas.winfo_width(), 900)
        stages = [
            (1, "① 发起方发出"),
            (2, "② 目标入箱/执行"),
            (3, "③ 目标完成作答"),
            (4, "④ 回推原发送方"),
            (5, "⑤ 发送方已收阅"),
        ]
        cur_stage = gw.compute_delivery_stage(job) if job else 0
        is_err = (job or {}).get("status") == "error"
        step_w = w / len(stages)
        y = 42

        for i, (num, label) in enumerate(stages):
            cx = step_w * i + step_w / 2
            if i < len(stages) - 1:
                nx = step_w * (i + 1) + step_w / 2
                lcol = ACCENT_GREEN if cur_stage > num else "#334155"
                delivery_canvas.create_line(cx + 20, y, nx - 20, y, fill=lcol, width=4)

            if not job:
                col = "#334155"
                txt = str(num)
            elif is_err and num == cur_stage:
                col = ACCENT_RED
                txt = "✗"
            elif num < cur_stage or (num == 5 and cur_stage == 5):
                col = ACCENT_GREEN
                txt = "✓"
            elif num == cur_stage:
                col = ACCENT_BLUE
                txt = "●"
            else:
                col = "#334155"
                txt = str(num)

            delivery_canvas.create_oval(cx - 18, y - 18, cx + 18, y + 18, fill=col, outline="#0f172a", width=2)
            delivery_canvas.create_text(cx, y, text=txt, fill="#0f172a" if col != "#334155" else FG_MUTED,
                                        font=("Helvetica", 13, "bold"))
            delivery_canvas.create_text(cx, y + 32, text=label,
                                        fill=FG_MAIN if num <= cur_stage else FG_MUTED,
                                        font=("Helvetica", 10, "bold" if num == cur_stage else "normal"))

    def render_selected_job(job):
        draw_delivery_pipeline(job)
        detail_txt.delete("1.0", "end")
        if not job:
            selected_route_lbl.configure(text="👇 请在下方列表点击任意消息/任务，查看其端到端 5 阶段投递状态流转图：")
            return
        fp = job.get("from_peer") or gw.CFG["name"]
        fa = job.get("from_agent_id") or "anonymous"
        tp = job.get("to_peer") or gw.CFG["name"]
        ta = job.get("to_agent_id") or "unspecified"
        stg = gw.compute_delivery_stage(job)
        stg_name = gw.DELIVERY_STAGE_NAMES.get(stg, "")
        selected_route_lbl.configure(
            text=f"当前选中 [{job['id']}]   路由: {fp}/{fa}  ➔  {tp}/{ta}   |   当前阶段: {stg_name} (status={job.get('status')})"
        )
        lines = [
            f"【消息 ID】    {job['id']}" + (f"  (远端关联单号: {job['remote_job_id']})" if job.get("remote_job_id") else ""),
            f"【消息类型】  {job.get('kind')}   |   方向: {job.get('direction', 'inbound')}   |   当前状态: {job.get('status')}",
            f"【发起方】    Gateway: {fp}   |   唯一 Agent ID: {fa}",
            f"【接收方】    Gateway: {tp}   |   唯一 Agent ID: {ta}",
            f"【投递阶段】  {stg_name} ({stg}/5)",
            "",
            "================ 逐跳投递状态轨迹 (Hop-by-Hop Delivery Timeline) ================",
        ]
        for hop in job.get("timeline", []):
            lines.append(f"  [{hop.get('time_str', '--:--:--')}] [阶段{hop.get('stage')}] {hop.get('title')}  (执行者: {hop.get('actor')})")
            if hop.get("detail"):
                lines.append(f"               ↳ {hop.get('detail')}")
        lines.append("")
        lines.append("============================== 报文正文与响应结果 ==============================")
        if job.get("question") or job.get("title"):
            lines.append(f"▸ 提问 / 标题:\n{job.get('question') or job.get('title')}")
        if job.get("context"):
            lines.append(f"\n▸ 上下文补充:\n{job.get('context')}")
        if job.get("answer"):
            lines.append(f"\n▸ 回答 / 执行输出:\n{job.get('answer')}")
        if job.get("error"):
            lines.append(f"\n▸ 错误信息:\n{job.get('error')}")
        if job.get("saved_to"):
            lines.append(f"\n▸ 文件落盘路径: {job.get('saved_to')}")
        detail_txt.insert("1.0", "\n".join(lines))

    def on_job_select(_event):
        sel = job_tree.selection()
        if not sel:
            return
        jid = sel[0]
        selected_job_id["id"] = jid
        render_selected_job(gw.get_job(jid))

    job_tree.bind("<<TreeviewSelect>>", on_job_select)

    # ---------------- 定时刷新循环 ----------------
    def refresh_ui():
        steps = gw.get_startup_steps()
        local_agents = gw.list_local_agents()
        uptime = int(time.time() - gw.STARTED_AT)
        status_pill.configure(
            text=f"● 运行中 (在线 Agent: {len(local_agents)} | 已运行 {uptime}s)",
            fg=ACCENT_GREEN
        )

        # 1. 刷新启动流水线
        draw_startup_pipeline(steps)
        step_tree.delete(*step_tree.get_children())
        for s in steps:
            t_str = time.strftime("%H:%M:%S", time.localtime(s["time"])) if s.get("time") else "--:--:--"
            step_tree.insert("", "end", values=(s["title"], s["status"].upper(), t_str, s.get("detail", "")))

        # 2. 刷新本机 Agents
        local_ag_tree.delete(*local_ag_tree.get_children())
        for a in local_agents:
            ls = f"{int(time.time() - a.get('last_seen', time.time()))}s 前"
            local_ag_tree.insert("", "end", values=(
                a["agent_id"], a.get("name", ""), a.get("mode", ""),
                a.get("pid") or "常驻", ls
            ))

        # 刷新远端拓扑树
        if time.time() - remote_cache["last_fetch"] > 12:
            refresh_remote_topology_async()
        peer_topo_tree.delete(*peer_topo_tree.get_children())
        for pname, pinfo in remote_cache["peers"].items():
            reachable = pinfo.get("reachable")
            pnode = peer_topo_tree.insert(
                "", "end", text=f"🖥 {pname} ({pinfo.get('gateway') or '未知'})",
                values=("Gateway", pinfo.get("url", ""), "-", "在线 ✓" if reachable else "离线 ✗"),
                open=True
            )
            for ag in pinfo.get("agents", []):
                peer_topo_tree.insert(
                    pnode, "end", text=f"🤖 {ag.get('name')}",
                    values=("Agent", ag.get("agent_id"), ag.get("mode"), "可投递")
                )

        # 刷新实时事件流
        ev_tree.delete(*ev_tree.get_children())
        for ev in reversed(gw.get_runtime_events(120)):
            ev_tree.insert("", "end", values=(
                ev["time_str"], ev["category"], ev["actor"], ev["target"], ev["summary"]
            ))

        # 3. 刷新消息投递状态列表
        with gw.JOBS_LOCK:
            all_jobs = sorted(gw.JOBS.values(), key=lambda j: j.get("created", 0), reverse=True)

        total = len(all_jobs)
        in_flight = sum(1 for j in all_jobs if gw.compute_delivery_stage(j) in (1, 2))
        pushed = sum(1 for j in all_jobs if gw.compute_delivery_stage(j) in (3, 4))
        completed = sum(1 for j in all_jobs if gw.compute_delivery_stage(j) == 5)
        errors = sum(1 for j in all_jobs if j.get("status") == "error")
        stats_lbl.configure(
            text=f"📊 消息投递总计: {total}   |   ⏳ 待答/执行中: {in_flight}   |   📨 已作答/已回推: {pushed}   |   ✅ 已收阅完结: {completed}   |   ❌ 异常: {errors}"
        )

        cur_sel = selected_job_id["id"]
        job_tree.delete(*job_tree.get_children())
        for j in all_jobs[:80]:
            jid = j["id"]
            fp = j.get("from_peer") or gw.CFG["name"]
            fa = j.get("from_agent_id") or "?"
            tp = j.get("to_peer") or gw.CFG["name"]
            ta = j.get("to_agent_id") or "?"
            route = f"{fp}:{fa[:12]} ➔ {tp}:{ta[:12]}"
            stg = gw.compute_delivery_stage(j)
            stg_txt = gw.DELIVERY_STAGE_NAMES.get(stg, "")
            summary = (j.get("question") or j.get("title") or j.get("filename") or "")[:45]
            job_tree.insert("", "end", iid=jid, values=(
                jid, j.get("kind", ""), route, stg_txt, j.get("status", ""), summary
            ))

        if cur_sel and gw.get_job(cur_sel):
            try:
                job_tree.selection_set(cur_sel)
            except Exception:
                pass
            render_selected_job(gw.get_job(cur_sel))
        elif all_jobs and not cur_sel:
            first_id = all_jobs[0]["id"]
            selected_job_id["id"] = first_id
            try:
                job_tree.selection_set(first_id)
            except Exception:
                pass
            render_selected_job(all_jobs[0])
        else:
            render_selected_job(None)

        root.after(1000, refresh_ui)

    refresh_remote_topology_async()
    root.after(200, refresh_ui)
    root.mainloop()


# =====================================================================
# 2. 终端内全屏交互 App (curses TUI，--tui 或无桌面环境自动启用)
# =====================================================================

def run_curses_app(gw):
    state = {"tab": 1, "selected_idx": 0}

    def _loop(stdscr):
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(500)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_GREEN, -1)
            curses.init_pair(3, curses.COLOR_YELLOW, -1)
            curses.init_pair(4, curses.COLOR_RED, -1)
            curses.init_pair(5, curses.COLOR_MAGENTA, -1)

        while True:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            default_name = f"troupe-{gw.secrets.randbelow(90000) + 10000}" if hasattr(gw, "secrets") else "troupe-00000"
            cfg_name = gw.CFG.get("name") or default_name
            host, port = gw.CFG.get("listen", ["127.0.0.1", 8765])
            agents = gw.list_local_agents()
            uptime = int(time.time() - gw.STARTED_AT)

            # 顶部状态栏
            hdr = f" 🎭 troupe App [{cfg_name}] | 端口:{port} | 在线Agent:{len(agents)} | 运行:{uptime}s "
            stdscr.addstr(0, 0, hdr[:w - 1], curses.A_BOLD | curses.color_pair(1))
            nav = " 快捷键: [1] 🚀启动流程可视化   [2] ⚡运行过程与拓扑   [3] 📬消息投递状态可视化   [↑/↓]选择消息   [R]刷新Peers   [Q]退出 "
            stdscr.addstr(1, 0, nav[:w - 1], curses.A_REVERSE)

            if state["tab"] == 1:
                stdscr.addstr(3, 2, "=== 🚀 启动流程可视化 (Startup Pipeline) ===", curses.A_BOLD | curses.color_pair(1))
                for idx, s in enumerate(gw.get_startup_steps()):
                    y = 5 + idx * 2
                    if y >= h - 2:
                        break
                    st = s["status"]
                    icon = {"done": "[✓ 已完成]", "running": "[↻ 进行中]", "warn": "[! 需注意]", "error": "[✗ 异常]"}.get(st, "[· 等待]")
                    col = curses.color_pair(2 if st == "done" else (3 if st == "warn" else (4 if st == "error" else 1)))
                    line = f"{s['title']:<26} {icon:<12} {s.get('detail', '')}"
                    stdscr.addstr(y, 4, line[:w - 6], col)

            elif state["tab"] == 2:
                stdscr.addstr(3, 2, f"=== ⚡ 本机在线 Agents ({len(agents)} 个) ===", curses.A_BOLD | curses.color_pair(2))
                row = 4
                for a in agents:
                    if row >= h // 2:
                        break
                    line = f"• {a['agent_id']:<24} 名称:{a.get('name',''):<14} 模式:{a.get('mode',''):<8} PID:{str(a.get('pid') or '常驻'):<8}"
                    stdscr.addstr(row, 4, line[:w - 6])
                    row += 1

                row += 1
                if row < h - 3:
                    stdscr.addstr(row, 2, "=== ⚡ 实时运行过程事件流 (Live Runtime Events) ===", curses.A_BOLD | curses.color_pair(3))
                    row += 1
                    for ev in reversed(gw.get_runtime_events(h - row - 2)):
                        if row >= h - 1:
                            break
                        line = f"[{ev['time_str']}] [{ev['category']:<8}] {ev['actor']} -> {ev['target']}: {ev['summary']}"
                        stdscr.addstr(row, 4, line[:w - 6])
                        row += 1

            elif state["tab"] == 3:
                with gw.JOBS_LOCK:
                    jobs = sorted(gw.JOBS.values(), key=lambda j: j.get("created", 0), reverse=True)
                stdscr.addstr(3, 2, f"=== 📬 消息投递状态可视化 (共 {len(jobs)} 条，按 ↑/↓ 查看逐跳投递状态) ===",
                              curses.A_BOLD | curses.color_pair(1))
                if jobs:
                    state["selected_idx"] = max(0, min(state["selected_idx"], len(jobs) - 1))
                    sel_job = jobs[state["selected_idx"]]
                    stg = gw.compute_delivery_stage(sel_job)
                    bar = " ➔ ".join(
                        f"[{'✓' if i <= stg else '·'}{gw.DELIVERY_STAGE_NAMES[i]}]"
                        for i in range(1, 6)
                    )
                    stdscr.addstr(4, 2, f"当前选中轨迹: {bar}"[:w - 4], curses.color_pair(2) | curses.A_BOLD)

                row = 6
                for idx, j in enumerate(jobs[:max(1, (h - 12))]):
                    if row >= h - 6:
                        break
                    fp = j.get("from_peer") or gw.CFG["name"]
                    fa = j.get("from_agent_id") or "?"
                    tp = j.get("to_peer") or gw.CFG["name"]
                    ta = j.get("to_agent_id") or "?"
                    stg_lbl = gw.DELIVERY_STAGE_NAMES.get(gw.compute_delivery_stage(j), "")
                    summary = (j.get("question") or j.get("title") or j.get("filename") or "")[:28]
                    line = f" {j['id']:<22} [{j.get('kind',''):<6}] {fp}:{fa[:10]} -> {tp}:{ta[:10]} | {stg_lbl:<12} | {summary}"
                    attr = curses.A_REVERSE if idx == state["selected_idx"] else curses.A_NORMAL
                    stdscr.addstr(row, 2, line[:w - 4], attr)
                    row += 1

                if jobs and row < h - 2:
                    sel_job = jobs[state["selected_idx"]]
                    stdscr.addstr(h - 5, 2, f"▸ 提问/内容: {(sel_job.get('question') or '')[:w - 16]}", curses.color_pair(3))
                    stdscr.addstr(h - 4, 2, f"▸ 回答/结果: {(sel_job.get('answer') or sel_job.get('error') or '等待中...')[:w - 16]}", curses.color_pair(2))
                    hops = " | ".join(f"{hp.get('time_str')} {hp.get('title')}" for hp in sel_job.get("timeline", []))
                    stdscr.addstr(h - 3, 2, f"▸ 逐跳时间戳: {hops[:w - 18]}", curses.color_pair(1))

            stdscr.refresh()
            ch = stdscr.getch()
            if ch in (ord("q"), ord("Q")):
                break
            elif ch == ord("1"):
                state["tab"] = 1
            elif ch == ord("2"):
                state["tab"] = 2
            elif ch == ord("3"):
                state["tab"] = 3
            elif ch in (ord("r"), ord("R")):
                threading.Thread(target=gw.probe_peers_on_startup, daemon=True).start()
            elif ch == curses.KEY_UP:
                state["selected_idx"] = max(0, state["selected_idx"] - 1)
            elif ch == curses.KEY_DOWN:
                state["selected_idx"] += 1

    curses.wrapper(_loop)


def run_app(gw, prefer_tui=False):
    if not prefer_tui:
        try:
            run_tkinter_app(gw)
            return
        except Exception as e:
            sys.stderr.write(f"[troupe] 桌面窗口启动回退至终端 App ({e})\n")
    run_curses_app(gw)
