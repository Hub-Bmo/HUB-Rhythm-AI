"""MusicAI 程式 B 的獨立介面；辨識候選、人工決定、試聽與匯出清楚分開。"""
from __future__ import annotations

from copy import deepcopy
from fractions import Fraction
import json
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from guoyue_annotator.photo_assets import asset_file_path
from score_recognizer.contract import ContractError
from score_recognizer.project_setup import project_destination
from .audio import Player, render_wav
from .canvas import EvidenceView, RhythmGrid
from .document import (ReviewDocument, MANIFEST, SOUND_PROFILES, display_time, midi_from_name,
                       pitch_name, pitched_values, rat, read_source, invalidate_navigation)
from .timeline import build_plan
from .navigation import expand_playback_order, route_summary, marker_range
from .segments import format_sequence, segment_statistics
from .segment_ui import open_sequence_editor
from .review_assistant import open_review_assistant
from .deletion_ui import open_exclusion_dialog

STATUSES = {"pending": "待核對", "accepted": "已接受", "rejected": "已排除"}
KINDS = {"note": "敲擊", "rest": "休止", "full_rest": "整小節休止", "unreadable": "待辨明"}
TIES = {"none": "無", "start": "開始", "continue": "延續", "stop": "結束", "unknown": "未知"}
FLAGS = {True: "是", False: "否", None: "未知"}
NAV_KINDS = {"反覆起點": "repeat_start", "反覆終點": "repeat_end", "結尾": "volta"}
JUMP_LABELS = {"D.C. 返回開頭": "dc", "D.C. 返回後接 Coda": "dc_coda"}
RETURN_POLICIES = {"略過一般反覆、採最後結尾": "skip", "重做一般反覆": "play"}
ORIGINAL_SOUND = "沿用原有設定"


def create_from_source(source, parent=None):
    _, data = read_source(source)
    parent = Path(parent) if parent is not None else Path(__file__).resolve().parents[1] / "projects"
    target = project_destination(parent, data["title"]).with_suffix(".score-review")
    if target.exists():
        if (target / MANIFEST).exists():
            doc = ReviewDocument.load(target)
            if doc.data["source_package_id"] in (None, data["package_id"]):
                return doc, doc.import_source(source)
        target = target.with_name(target.stem + "-" + data["package_id"][-8:] + ".score-review")
        if target.exists():
            raise ContractError("已有同名校對資料夾，請使用『開啟校對專案』")
    doc = ReviewDocument.create(target, data["title"])
    return doc, doc.import_source(source)


def scrollable(parent):
    container = ttk.Frame(parent)
    canvas = tk.Canvas(container, highlightthickness=0)
    scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
    body = ttk.Frame(canvas, padding=10)
    item = canvas.create_window(0, 0, window=body, anchor="nw")
    body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>", lambda e: canvas.itemconfigure(item, width=e.width))
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")
    # 局部綁定，不攔截其他視窗或頁籤的滾輪。
    def wheel(event):
        canvas.yview_scroll(-int(event.delta / 120) * 3, "units")
    canvas.bind("<MouseWheel>", wheel)
    container.scroll_canvas = canvas
    container.scroll_wheel = wheel
    return container, body


class ReviewerApp:
    def __init__(self, root, document=None):
        self.root, self.document = root, document
        self.current_bar = None
        self.editor_id = None
        self.loading = False
        self.dirty = False
        self.player = Player()
        self.messages = queue.Queue()
        self.cancel = threading.Event()
        self.render_generation = 0
        self.rendering = False
        self.play_plan = None
        self.started = None
        self.preview_warnings = []
        self.preview_path = None
        self.nav_draft = []
        self.status = tk.StringVar(value="先匯入第一個程式的辨識資料；本程式的校對與試聽都在本機進行。")
        root.title("MusicAI｜校對與試聽")
        root.geometry("1480x920")
        root.minsize(1100, 720)
        root.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style(root)
        style.configure("Treeview", rowheight=26)
        style.configure("TButton", padding=(8, 5))
        self.build()
        if document:
            self.load_document(document)
        self.poll_id = root.after(100, self.poll)

    def button(self, master, text, command, **pack):
        widget = ttk.Button(master, text=text, command=lambda: self.action(command))
        widget.pack(**({"side": "left", "padx": 3} | pack))
        return widget

    def action(self, command):
        try:
            return command()
        except Exception as error:
            self.status.set(str(error).splitlines()[0])
            messagebox.showerror("尚未完成", str(error), parent=self.root)
            return None

    def require_doc(self):
        if not self.document:
            raise ContractError("請先匯入辨識資料或開啟校對專案")
        return self.document

    def build(self):
        toolbar = ttk.Frame(self.root, padding=(8, 6))
        toolbar.pack(fill="x")
        self.button(toolbar, "匯入辨識資料…", self.import_new)
        self.button(toolbar, "開啟校對專案…", self.open_review)
        self.button(toolbar, "匯入後續結果", self.refresh_source)
        self.button(toolbar, "保存", self.save_all)
        self.button(toolbar, "匯出已核對段落…", self.export_training)
        self.button(toolbar, "匯出 Excel／CSV…", self.export_tables)
        self.project_label = ttk.Label(self.root, text="每首樂曲一份校對專案；可先處理部分完成的頁面。", padding=(12, 3))
        self.project_label.pack(fill="x")
        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill="both", expand=True, padx=8, pady=5)
        self.review_tab, self.sound_tab, self.segment_tab = (ttk.Frame(self.tabs) for _ in range(3))
        for frame, title in ((self.review_tab, "核對與播放"), (self.sound_tab, "樂器與音效"), (self.segment_tab, "段落與反覆")):
            self.tabs.add(frame, text=title)
        self.build_review()
        self.build_sounds()
        self.build_segments()
        ttk.Label(self.root, textvariable=self.status, padding=(10, 6), anchor="w").pack(fill="x")

    def build_review(self):
        defaults = ttk.Frame(self.review_tab, padding=6)
        defaults.pack(fill="x")
        self.meter, self.tempo, self.tonic = (tk.StringVar() for _ in range(3))
        for label, variable, width in (("共通拍號", self.meter, 6), ("四分音符／分鐘", self.tempo, 6), ("簡譜 1 的實際音高", self.tonic, 8)):
            ttk.Label(defaults, text=label).pack(side="left", padx=(5, 3))
            ttk.Entry(defaults, textvariable=variable, width=width).pack(side="left")
        self.button(defaults, "保存共通設定", self.save_defaults)
        self.button(defaults, "依基準換算待填音高", self.resolve_pitch)
        self.review_policy_label = ttk.Label(self.review_tab, text="音高需含八度，例如 G2；未知可先留白。時間以四分音符為一拍，支援分數。", foreground="#52647a", padding=(12, 1))
        self.review_policy_label.pack(fill="x")
        player = ttk.Frame(self.review_tab, padding=6)
        player.pack(fill="x")
        self.play_start, self.play_end = tk.StringVar(value="1"), tk.StringVar(value="4")
        self.button(player, "聽這小節", self.play_current)
        ttk.Label(player, text="範圍").pack(side="left", padx=3)
        for variable in (self.play_start, self.play_end):
            ttk.Entry(player, textvariable=variable, width=5).pack(side="left", padx=2)
        self.button(player, "播放範圍", self.play_range)
        self.button(player, "停止", self.stop)
        self.include_pending = tk.BooleanVar(value=True)
        self.use_repeats, self.metronome = tk.BooleanVar(), tk.BooleanVar()
        for text, variable in (("含待核對試聽", self.include_pending), ("依確認反覆", self.use_repeats), ("節拍提示", self.metronome)):
            ttk.Checkbutton(player, text=text, variable=variable).pack(side="left", padx=4)
        self.button(player, "試聽問題", self.show_warnings)
        self.button(player, "匯出 WAV…", self.export_wav)
        options = ttk.Frame(self.review_tab, padding=(10, 1))
        options.pack(fill="x")
        ttk.Label(options, text="只聽樂器").pack(side="left")
        self.solo = tk.StringVar(value="全部樂器")
        self.solo_combo = ttk.Combobox(options, textvariable=self.solo, state="readonly", values=["全部樂器"], width=18)
        self.solo_combo.pack(side="left", padx=6)
        self.follow_playback = tk.BooleanVar(value=True)
        self.follow_select = False
        ttk.Checkbutton(options, text="跟隨播放小節", variable=self.follow_playback).pack(side="left", padx=8)
        self.relative_pitch = tk.BooleanVar(value=True)
        ttk.Checkbutton(options, text="未知實際音高時，以 1=C3 試聽相對音高", variable=self.relative_pitch).pack(side="left", padx=8)
        body = ttk.Panedwindow(self.review_tab, orient="horizontal")
        body.pack(fill="both", expand=True, padx=6, pady=5)
        left = ttk.Frame(body, width=170)
        center = ttk.Frame(body)
        editor_container, editor = scrollable(body)
        self.editor_container, self.editor_body = editor_container, editor
        body.add(left, weight=0)
        body.add(center, weight=1)
        body.add(editor_container, weight=0)
        editor_container.configure(width=300)
        self.bars = ttk.Treeview(left, columns=("bar", "page", "pending"), show="headings", selectmode="browse", height=18)
        for key, label, width in (("bar", "小節", 48), ("page", "頁", 38), ("pending", "待核", 60)):
            self.bars.heading(key, text=label)
            self.bars.column(key, width=width, stretch=False)
        bar_scroll = ttk.Scrollbar(left, orient="vertical", command=self.bars.yview)
        self.bars.configure(yscrollcommand=bar_scroll.set)
        self.bars.pack(side="left", fill="both", expand=True)
        bar_scroll.pack(side="right", fill="y")
        self.bars.bind("<<TreeviewSelect>>", lambda e: self.action(self.select_bar))
        source_header = ttk.Frame(center)
        source_header.pack(fill="x")
        self.source_label = ttk.Label(source_header, text="原譜對照；橘框是目前小節，綠框是選取事件。")
        self.source_label.pack(side="left")
        self.button(source_header, "整頁原譜", self.show_full_page, side="right")
        self.button(source_header, "本小節設定", self.edit_bar_settings, side="right")
        panes = ttk.Panedwindow(center, orient="vertical")
        panes.pack(fill="both", expand=True)
        self.viewer = EvidenceView(panes)
        pane_bottom = ttk.Frame(panes)
        panes.add(self.viewer, weight=1)
        panes.add(pane_bottom, weight=2)
        actions = ttk.Frame(pane_bottom, padding=(0, 5))
        actions.pack(fill="x")
        self.button(actions, "補入敲擊", lambda: self.add_event(None, None))
        self.ack = tk.BooleanVar()
        ttk.Checkbutton(actions, text="已核對原譜與疑問", variable=self.ack).pack(side="left", padx=5)
        decisions = ttk.Frame(pane_bottom, padding=(0, 2))
        decisions.pack(fill="x")
        self.button(decisions, "接受選取", self.accept_selected)
        self.button(decisions, "接受本小節", self.accept_bar)
        self.button(decisions, "核對助手／批次整理…", lambda: open_review_assistant(self))
        delete_controls = ttk.Frame(pane_bottom, padding=(0, 2))
        delete_controls.pack(fill="x")
        self.button(delete_controls, "刪除選取", self.reject_selected)
        self.button(delete_controls, "刪除整列／樂器…", lambda: open_exclusion_dialog(self))
        self.button(delete_controls, "復原上次刪除", self.undo_last_exclusion)
        self.grid_add_mode = tk.BooleanVar(value=False)
        ttk.Checkbutton(delete_controls, text="啟用雙擊補音", variable=self.grid_add_mode, command=self.set_grid_add_mode).pack(side="left", padx=5)
        views = ttk.Notebook(pane_bottom)
        self.review_views = views
        views.pack(fill="both", expand=True)
        self.grid = RhythmGrid(views, self.choose_entry, lambda ins, onset: self.action(lambda: self.add_event(ins, onset)),
                               lambda eid: self.action(lambda: self.reject_selected([eid])),
                               on_map_row=lambda doc, bar, staff, name: self.action(
                                   lambda: self.map_row_instrument(doc, bar, staff, name)))
        self.grid.canvas.bind("<Delete>", lambda e: self.action(self.reject_selected))
        list_frame = ttk.Frame(views)
        self.event_list_frame = list_frame
        views.add(self.grid, text="節奏格（點選事件可校對）")
        views.add(list_frame, text="事件與休止清單")
        history_controls = ttk.Frame(list_frame)
        history_controls.pack(side="top", fill="x")
        self.show_rejected = tk.BooleanVar(value=False)
        ttk.Checkbutton(history_controls, text="顯示已刪除", variable=self.show_rejected, command=lambda: self.refresh_current(self.editor_id)).pack(side="left", padx=5)
        self.button(history_controls, "還原選取刪除", self.restore_selected_exclusions)
        ttk.Label(history_controls, text="刪除只影響試聽／匯出，原始紀錄保留。", foreground="#52647a").pack(side="left", padx=6)
        self.events = ttk.Treeview(list_frame, columns=("status", "instrument", "kind", "onset", "duration", "symbol"), show="headings", selectmode="extended")
        for key, label, width in (("status", "狀態", 68), ("instrument", "樂器", 100), ("kind", "種類", 90), ("onset", "第幾拍", 60), ("duration", "時值", 60), ("symbol", "原符號", 180)):
            self.events.heading(key, text=label)
            self.events.column(key, width=width)
        event_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.events.yview)
        self.events.configure(yscrollcommand=event_scroll.set)
        self.events.pack(side="left", fill="both", expand=True)
        event_scroll.pack(side="right", fill="y")
        for name, color in (("pending", "#986017"), ("accepted", "#167356"), ("rejected", "#7b8693")):
            self.events.tag_configure(name, foreground=color)
        self.events.bind("<<TreeviewSelect>>", lambda e: self.action(self.select_event))
        self.events.bind("<Delete>", lambda e: self.action(self.reject_selected))
        self.build_editor(editor)
        def bind_wheel(widget):
            widget.bind("<MouseWheel>", lambda e: (editor_container.scroll_wheel(e), "break")[1])
            for child in widget.winfo_children():
                bind_wheel(child)
        bind_wheel(editor)

    def build_editor(self, frame):
        ttk.Label(frame, text="選取位置的校對", font=("Microsoft JhengHei UI", 12, "bold")).pack(anchor="w", pady=(0, 6))
        self.editor_summary = ttk.Label(frame, text="先選一個音或休止。", wraplength=270)
        self.editor_summary.pack(anchor="w", pady=3)
        self.fields = {}
        self.editor_widgets = {}
        def field(key, label, choices=None):
            ttk.Label(frame, text=label).pack(anchor="w", pady=(7, 2))
            var = tk.StringVar()
            self.fields[key] = var
            widget = ttk.Combobox(frame, textvariable=var, values=choices, state="readonly") if choices is not None else ttk.Entry(frame, textvariable=var)
            self.editor_widgets[key] = widget
            widget.pack(fill="x")
            var.trace_add("write", self.mark_dirty)
            return widget
        field("kind", "種類", list(KINDS.values()))
        self.instrument_combo = field("instrument", "確認樂器", [])
        self.technique_combo = field("technique", "確認擊法", [])
        self.instrument_combo.bind("<<ComboboxSelected>>", lambda e: self.update_editor_techniques())
        field("onset", "第幾拍（從 1 起；可填 3/2）")
        field("duration", "時值（四分音符 = 1；八分 = 1/2）")
        field("pitch", "實際音高（G2 或 MIDI；和弦用逗號）")
        ttk.Label(frame, text="? 保留未知音高；清空表示不含音高。", foreground="#52647a", wraplength=270).pack(anchor="w")
        for key, label in (("is_roll", "輪奏／滾奏"), ("is_accent", "重音"), ("is_ornament", "裝飾音")):
            field(key, label, ["是", "否", "未知"])
        field("tie", "延音線", list(TIES.values()))
        field("slur", "連奏線／其他演奏指示")
        field("dynamic", "力度（p、mf、f 等）")
        field("note", "人工備註")
        self.button(frame, "保存修改", self.save_event, side="top", fill="x", pady=10)
        self.button(frame, "恢復原始辨識", self.restore_event, side="top", fill="x")
        ttk.Label(frame, text="辨識疑問（接受前請對照原譜）", wraplength=270).pack(anchor="w", pady=(10, 3))
        self.issues_label = ttk.Label(frame, text="", foreground="#986017", wraplength=270, justify="left")
        self.issues_label.pack(anchor="w")

    def mark_dirty(self, *_):
        if not self.loading and self.editor_id:
            self.dirty = True
            self.ack.set(False)
            if self.started is not None or self.rendering:
                self.stop()

    def commit_editor(self):
        if self.dirty:
            self.save_event(refresh=False)

    def save_all(self):
        self.require_doc()
        self.commit_editor()
        self.document.save()
        self.status.set("校對已保存。")

    def load_document(self, doc):
        self.stop()
        self.document, self.editor_id, self.current_bar = doc, None, None
        self.dirty = False
        defaults = doc.data["defaults"]
        self.meter.set(defaults["time_signature"] or "")
        self.tempo.set(str(defaults["tempo_quarter"] or ""))
        self.tonic.set(pitch_name(defaults["tonic_midi"]))
        self.project_label.configure(text=f"{doc.data['title']}　｜　校對資料夾：{doc.root}")
        self.nav_draft = deepcopy(doc.data.get("navigation_draft", doc.data["navigation"]))
        scope = doc.data.get("navigation_draft_scope") or {"start_bar": min(map(int, doc.data["bars"]), default=1), "end_bar": max(map(int, doc.data["bars"]), default=1)}
        self.nav_scope_start.set(str(scope["start_bar"]))
        self.nav_scope_end.set(str(scope["end_bar"]))
        self.nav_ack.set(doc.navigation_covered(scope["start_bar"], scope["end_bar"]))
        self.refresh_bar_list()
        self.refresh_catalog()
        self.refresh_segments()
        self.refresh_navigation()
        self.navigation_preview.configure(text=f"小節 {scope['start_bar']}–{scope['end_bar']} " +
            ("已確認；清單為保存的反覆設定。" if self.nav_ack.get() else "尚未確認反覆；可載入此範圍候選後核對。"))
        bars = sorted(map(int, doc.data["bars"]))
        if bars:
            self.play_start.set(str(bars[0]))
            self.play_end.set(str(min(bars[0] + 3, bars[-1])))
            self.bars.selection_set(str(bars[0]))
            self.select_bar()
        self.status.set("已開啟校對專案；橘色是待核對候選。可先試聽，再確認名稱、音高與節奏。")

    def import_new(self):
        self.commit_editor()
        selected = filedialog.askopenfilename(parent=self.root, title="選擇第一個程式的辨識資料", filetypes=[("辨識資料", "recognition-package.json *.recognition.zip *.zip"), ("JSON", "*.json")])
        if not selected:
            return
        doc, report = create_from_source(selected)
        self.load_document(doc)
        self.status.set(f"匯入 {report['added']} 筆；{report['ready_regions']}/{report['total_regions']} 個範圍已有完成結果。之後按『匯入後續結果』即可接續。")

    def open_review(self):
        self.commit_editor()
        selected = filedialog.askopenfilename(parent=self.root, title="開啟校對專案", initialdir=str(Path(__file__).resolve().parents[1] / "projects"), filetypes=[("校對專案", MANIFEST), ("JSON", "*.json")])
        if selected:
            self.load_document(ReviewDocument.load(selected))

    def refresh_source(self):
        doc = self.require_doc()
        self.commit_editor()
        source = doc.data["source_path"]
        if not source or not Path(source).exists():
            source = filedialog.askopenfilename(parent=self.root, title="重新選擇同一首樂曲的辨識資料", filetypes=[("辨識資料", "*.json *.zip")])
        if not source:
            return
        self.stop()
        report = doc.import_source(source)
        bar = self.current_bar
        self.load_document(doc)
        if bar and str(bar) in doc.data["bars"]:
            self.bars.selection_set(str(bar))
            self.select_bar()
        self.status.set(f"新增 {report['added']} 筆；已完成範圍 {report['ready_regions']}/{report['total_regions']}。同次辨識的人工修改已保留；重辨的舊修正另行封存。")

    def refresh_bar_list(self):
        doc = self.require_doc()
        selected = self.current_bar
        self.bars.delete(*self.bars.get_children())
        for number in sorted(map(int, doc.data["bars"])):
            bar = doc.data["bars"][str(number)]
            region_status = doc.data["regions"][bar["region_id"]]["status"]
            pending = sum(e["status"] == "pending" for e in doc.entries(number))
            text = str(pending) if region_status == "succeeded" else {"running": "辨識中", "pending": "待辨識", "failed": "失敗", "draft": "未送出", "empty": "空結果"}.get(region_status, "待處理")
            self.bars.insert("", "end", iid=str(number), values=(number, bar["page"], text))
        if selected and self.bars.exists(str(selected)):
            self.bars.selection_set(str(selected))
        if hasattr(self, "segments"):
            self.refresh_segments()

    def select_bar(self):
        selected = self.bars.selection()
        if not selected or not self.document:
            return
        bar = int(selected[0])
        if bar == self.current_bar:
            return
        if self.started is not None and not self.follow_select:
            self.follow_playback.set(False)
        try:
            self.commit_editor()
        except Exception:
            if self.current_bar:
                self.bars.selection_set(str(self.current_bar))
            raise
        self.current_bar, self.editor_id = bar, None
        self.dirty = False
        self.ack.set(False)
        self.refresh_current()

    def refresh_current(self, selected=None):
        if not self.document or self.current_bar is None:
            return
        doc, bar = self.document, self.document.data["bars"][str(self.current_bar)]
        self.review_policy_label.configure(text="節奏優先：未知技法、實際音高及演奏細節可保留；仍需確認樂器與敲擊時間。" if doc.data["review_mode"] == "rhythm" else "完整標註：需確認樂器、時間、擊法、實際音高與演奏記號；可在『核對助手』切換方式。")
        asset = next(a for a in doc.data["assets"] if a["asset_id"] == bar["asset_id"])
        meter, tempo = doc.bar_setting(self.current_bar)
        self.source_label.configure(text=f"第 {bar['page']} 頁／小節 {self.current_bar}　{meter or '拍號待填'}　♩={tempo or '?'}")
        self.viewer.load(asset_file_path(doc.root, asset, must_exist=True), bar["region_bbox"], bar["bar_bbox"])
        self.events.delete(*self.events.get_children())
        for entry in doc.entries(self.current_bar):
            if entry["status"] == "rejected" and not self.show_rejected.get():
                continue
            value = entry["value"]
            ins = doc.instrument(value.get("instrument_id"))
            onset = str(fraction_ui(value["onset"]) + 1) if value.get("onset") is not None else "?"
            self.events.insert("", "end", iid=entry["id"], values=(STATUSES[entry["status"]], (ins or {}).get("name") or value.get("instrument_text") or value["staff_label"],
                KINDS[value["kind"]], onset if value["kind"] != "full_rest" else "整節", display_time(value.get("duration")), value.get("raw_symbol", "")), tags=(entry["status"],))
        self.grid.set_bar(doc, self.current_bar, selected)
        ids = self.events.get_children()
        if selected and self.events.exists(selected):
            self.events.selection_set(selected)
            self.populate_editor(selected)
        elif ids:
            self.events.selection_set(ids[0])
            self.populate_editor(ids[0])
        else:
            self.clear_editor()

    def clear_editor(self):
        self.loading = True
        self.editor_id = None
        for variable in self.fields.values():
            variable.set("")
        self.editor_summary.configure(text="此小節尚無可用辨識；可檢視原譜或等候後續結果。")
        self.issues_label.configure(text="")
        self.loading = self.dirty = False

    def choose_entry(self, entry_id):
        try:
            self.commit_editor()
            self.events.selection_set(entry_id)
            self.events.see(entry_id)
            self.populate_editor(entry_id)
            if self.document.issues(self.document.entry(entry_id)):
                self.scroll_to_issue(self.document.entry(entry_id), focus=False)
        except Exception as error:
            self.action(lambda: raise_error(error))

    def select_event(self):
        selected = self.events.selection()
        if not selected or selected[0] == self.editor_id:
            return
        try:
            self.commit_editor()
        except Exception:
            if self.editor_id and self.events.exists(self.editor_id):
                self.events.selection_set(self.editor_id)
            raise
        self.populate_editor(selected[0])

    def populate_editor(self, entry_id):
        doc, entry = self.document, self.document.entry(entry_id)
        value = entry["value"]
        self.loading = True
        self.editor_id = entry_id
        self.instrument_combo.configure(values=[i["name"] for i in doc.data["instruments"]])
        ins = doc.instrument(value.get("instrument_id"))
        self.fields["kind"].set(KINDS[value["kind"]])
        self.fields["instrument"].set((ins or {}).get("name", ""))
        self.update_editor_techniques(value.get("technique_id"))
        self.fields["onset"].set(str(fraction_ui(value["onset"]) + 1) if value.get("onset") is not None else "")
        self.fields["duration"].set(display_time(value.get("duration")))
        self.fields["pitch"].set(",".join(pitch_name(p.get("midi")) if p.get("midi") is not None else "?" for p in value.get("pitches", [])))
        for flag in ("is_roll", "is_accent", "is_ornament"):
            self.fields[flag].set(FLAGS[value.get(flag)])
        self.fields["tie"].set(TIES[value.get("tie", "unknown")])
        for field in ("slur", "dynamic"):
            self.fields[field].set(value.get(field) or "")
        self.fields["note"].set(entry["review_note"])
        degree = ", ".join(p.get("written_text") or str(p.get("degree")) for p in value.get("pitches", []))
        current_name = value.get('instrument_text') or value['staff_label']
        original = entry["original"] or value
        original_name = original.get("instrument_text") or original["staff_label"]
        name_text = f"名稱：{current_name}" + (f"（原辨識：{original_name}）" if original_name != current_name else "")
        status_text = "已接受（節奏）" if entry["status"] == "accepted" and entry.get("acceptance_profile") == "rhythm" else STATUSES[entry["status"]]
        self.editor_summary.configure(text=f"{status_text}　{name_text}\n原符號：{value.get('raw_symbol', '—')}" + (f"\n譜面音高：{degree}" if degree else ""))
        issues = doc.issue_messages(entry)
        original_issues = (entry["original"] or {}).get("uncertainties", [])
        self.issues_label.configure(text="\n".join([*("需處理：" + i for i in issues),
            *("可保留未知：" + i for i in doc.optional_issues(entry)), *("原辨識：" + i for i in original_issues)]))
        self.viewer.set_evidence(value.get("source_bbox"))
        self.grid.selected = entry_id
        self.grid.redraw()
        self.dirty = self.loading = False
        self.ack.set(False)

    def update_editor_techniques(self, selected=None):
        if not self.document:
            return
        ins = next((i for i in self.document.data["instruments"] if i["name"] == self.fields["instrument"].get()), None)
        strokes = (ins or {}).get("strokes", [])
        self.technique_combo.configure(values=[s["name"] for s in strokes])
        stroke = next((s for s in strokes if s["technique_id"] == selected), None)
        self.fields["technique"].set(stroke["name"] if stroke else "")

    def save_event(self, refresh=True):
        doc = self.require_doc()
        if not self.editor_id:
            return
        fields = {k: v.get().strip() for k, v in self.fields.items()}
        if fields["kind"] not in KINDS.values():
            raise ContractError("請選事件種類")
        kind = next(k for k, v in KINDS.items() if v == fields["kind"])
        ins = next((i for i in doc.data["instruments"] if i["name"] == fields["instrument"]), None)
        stroke = next((s for s in (ins or {}).get("strokes", []) if s["name"] == fields["technique"]), None)
        onset = rat(str(Fraction(fields["onset"]) - 1)) if fields["onset"] else None
        changes = {"kind": kind, "instrument_id": (ins or {}).get("instrument_id"), "technique_id": (stroke or {}).get("technique_id"),
                   "onset": onset, "duration": rat(fields["duration"]) if fields["duration"] else None,
                   "pitches": pitched_values(fields["pitch"], doc.entry(self.editor_id)["value"].get("pitches", [])),
                   "tie": next((k for k, v in TIES.items() if v == fields["tie"]), "unknown"),
                   "slur": fields["slur"] or None, "dynamic": fields["dynamic"] or None}
        for flag in ("is_roll", "is_accent", "is_ornament"):
            changes[flag] = next((k for k, v in FLAGS.items() if v == fields[flag]), None)
        self.stop()
        doc.edit(self.editor_id, changes, fields["note"])
        self.dirty = False
        self.ack.set(False)
        if refresh:
            selected = self.editor_id
            self.refresh_bar_list()
            self.refresh_current(selected)
            self.status.set("修改已保存，這筆重新標為待核對。")

    def restore_event(self):
        doc = self.require_doc()
        if not self.editor_id:
            return
        entry = doc.entry(self.editor_id)
        if entry["original"] is None:
            raise ContractError("這是手動補入的事件；如不需要，請排除")
        self.stop()
        restored = deepcopy(entry["original"])
        # 原始證據保留送辨識當時的號碼；改號後復原音符不能把事件搬回舊小節。
        if doc.data.get("numbering_history"):
            restored["bar"] = entry["value"]["bar"]
        entry.update(value=restored, status="pending", acknowledged=False, review_note="恢復原始辨識")
        self.dirty = False
        doc.save()
        self.refresh_bar_list()
        self.refresh_current(entry["id"])

    def add_event(self, instrument_id, onset):
        doc = self.require_doc()
        self.commit_editor()
        if self.current_bar is None:
            return
        self.stop()
        entry = doc.add_event(self.current_bar, instrument_id, onset)
        self.refresh_bar_list()
        self.refresh_current(entry["id"])

    def selected_entries(self):
        selected = list(self.events.selection())
        if not selected:
            raise ContractError("先選取一個或多個事件")
        return selected

    def map_row_instrument(self, document, bar, staff, name):
        if self.document is not document or self.current_bar != bar:
            raise ContractError("已切換樂譜或小節，請在目前節奏格重新選擇樂器")
        instrument = next((i for i in document.data["instruments"] if i["name"] == name), None)
        if instrument is None:
            raise ContractError("請從下拉選單選擇已有樂器；輸入幾個字可篩選。新樂器可在『樂器與音效』新增。")
        self.commit_editor()
        self.stop()
        scope_label = self.grid.mapping_scope.get()
        report = document.map_staff_instrument(bar, staff, instrument["instrument_id"], self.grid.mapping_scopes[scope_label])
        self.refresh_catalog()
        self.refresh_bar_list()
        self.refresh_current(self.editor_id)
        assistant = getattr(self, "review_assistant_window", None)
        if assistant is not None and assistant.winfo_exists() and assistant.review_document is document:
            assistant.form["refresh"]()
        self.status.set(f"{scope_label}：{staff} → {name}，已更新 {report['mapped']} 筆，範圍涵蓋 {report['bars']} 個小節。修改項目維持待核對。")
        return report

    def accept_selected(self):
        acknowledged = self.ack.get()
        self.commit_editor()
        ids = self.selected_entries()
        if acknowledged:
            self.focus_invalid_entry(ids)
        self.require_doc().accept(ids, acknowledged=acknowledged)
        self.refresh_bar_list()
        self.refresh_current(self.editor_id)
        self.status.set("選取資料已接受；仍會在匯出時檢查整段是否完整。")

    def accept_bar(self):
        acknowledged = self.ack.get()
        self.commit_editor()
        doc = self.require_doc()
        ids = [e["id"] for e in doc.entries(self.current_bar) if e["status"] != "rejected"]
        if not ids:
            raise ContractError("這個小節沒有可接受的觀察")
        if acknowledged:
            self.focus_invalid_entry(ids)
        doc.accept(ids, acknowledged=acknowledged)
        self.refresh_bar_list()
        self.refresh_current(self.editor_id)

    def focus_invalid_entry(self, entry_ids):
        doc = self.require_doc()
        first = next((doc.entry(eid) for eid in entry_ids if doc.issues(doc.entry(eid))), None)
        if first:
            self.events.selection_set(first["id"])
            self.events.see(first["id"])
            self.populate_editor(first["id"])
            self.review_views.select(self.event_list_frame)
            self.scroll_to_issue(first)

    def scroll_to_issue(self, entry, focus=True):
        value = entry["value"]
        key = "onset" if value.get("onset") is None else ("duration" if value.get("duration") is None else "instrument")
        widget = self.editor_widgets[key]
        self.root.update_idletasks()
        height = max(1, self.editor_body.winfo_reqheight())
        self.editor_container.scroll_canvas.yview_moveto(max(0, (widget.winfo_y() - 24) / height))
        if focus:
            widget.focus_set()

    def reject_selected(self, entry_ids=None, reason="人工刪除／排除"):
        doc = self.require_doc()
        ids = list(entry_ids) if entry_ids is not None else self.selected_entries()
        # 刪除中的無效草稿不必先通過驗證；未選取的編輯仍保存。
        if self.editor_id not in ids:
            self.commit_editor()
        else:
            self.dirty = False
        self.stop()
        doc.reject(ids, reason=reason)
        self.refresh_bar_list()
        self.refresh_current(self.editor_id)
        self.refresh_catalog()
        self.status.set(f"已刪除／排除 {len(ids)} 筆；不參與試聽或匯出，可按『復原上次刪除』。")

    def undo_last_exclusion(self):
        self.commit_editor()
        doc = self.require_doc()
        self.stop()
        ids = doc.undo_exclusion()
        self.refresh_bar_list()
        self.refresh_catalog()
        self.refresh_current(ids[0] if doc.entry(ids[0])["value"]["bar"] == self.current_bar else self.editor_id)
        self.status.set(f"已復原 {len(ids)} 筆，恢復為待核對。")

    def restore_selected_exclusions(self):
        self.commit_editor()
        doc = self.require_doc()
        self.stop()
        ids = doc.restore_excluded(self.selected_entries())
        self.refresh_bar_list()
        self.refresh_catalog()
        self.refresh_current(ids[0] if ids else self.editor_id)

    def set_grid_add_mode(self):
        self.grid.enable_add = self.grid_add_mode.get()

    def save_defaults(self):
        doc = self.require_doc()
        self.commit_editor()
        self.stop()
        doc.update_defaults(self.meter.get().strip(), self.tempo.get().strip(), self.tonic.get().strip())
        self.refresh_current(self.editor_id)
        self.status.set("共通設定已保存；各小節已有的拍號與速度優先使用，可按原譜旁『本小節設定』修改。")

    def resolve_pitch(self):
        self.commit_editor()
        doc = self.require_doc()
        self.stop()
        count = doc.resolve_pitch([e for e in doc.entries() if e["status"] == "pending"])
        self.refresh_current(self.editor_id)
        self.status.set(f"已換算 {count} 筆音高；仍需對照譜面核對。缺音級或八度的項目保留未知。")

    def show_full_page(self):
        doc = self.require_doc()
        if self.current_bar is None:
            return
        bar = doc.data["bars"][str(self.current_bar)]
        asset = next(a for a in doc.data["assets"] if a["asset_id"] == bar["asset_id"])
        window = tk.Toplevel(self.root)
        window.title(f"原譜整頁｜{asset['original_filename']}（Ctrl＋滾輪縮放）")
        window.geometry("850x950")
        view = EvidenceView(window)
        view.pack(fill="both", expand=True)
        view.load(asset_file_path(doc.root, asset, must_exist=True), bar_box=bar["bar_bbox"])

    def edit_bar_settings(self):
        doc = self.require_doc()
        self.commit_editor()
        if self.current_bar is None:
            return
        meter, tempo = doc.bar_setting(self.current_bar)
        answer = simpledialog.askstring("本小節設定", "拍號、四分音符／分鐘（以逗號分隔，例如 3/4,144）", initialvalue=f"{meter or ''},{tempo or ''}", parent=self.root)
        if answer is None:
            return
        from score_recognizer.contract import meter_length
        import math
        new_meter, new_tempo = (item.strip() for item in answer.replace("，", ",").split(","))
        meter_length(new_meter)
        speed = float(new_tempo)
        if not new_meter or not math.isfinite(speed) or not 1 <= speed <= 600:
            raise ContractError("請填有效拍號與 1–600 的速度")
        self.stop()
        doc.data["bars"][str(self.current_bar)].update(time_signature=new_meter, tempo_quarter=speed)
        doc.save()
        self.refresh_current(self.editor_id)

    def play_current(self):
        self.require_doc()
        if self.current_bar is None:
            return
        self.start_render(self.current_bar, self.current_bar)

    def play_range(self):
        self.start_render(int(self.play_start.get()), int(self.play_end.get()))

    def export_wav(self):
        self.require_doc()
        selected = filedialog.asksaveasfilename(parent=self.root, title="匯出目前播放範圍的試聽 WAV", defaultextension=".wav", filetypes=[("試聽音訊", "*.wav")])
        if selected:
            self.start_render(int(self.play_start.get()), int(self.play_end.get()), target=selected)

    def start_render(self, start, end, target=None, *, navigation_draft=None, source_order=None):
        doc = self.require_doc()
        self.commit_editor()
        if navigation_draft is not None:
            # 草稿只在記憶體副本上試聽；不假冒人工確認或寫回正式導航。
            doc = ReviewDocument(doc.root, deepcopy(doc.data))
            doc.data["navigation"] = deepcopy(navigation_draft)
            doc.data["navigation_scopes"] = [{"start_bar": start, "end_bar": end}]
        solo = next((i["instrument_id"] for i in doc.data["instruments"] if i["name"] == self.solo.get()), None)
        extra = {"source_order": source_order} if source_order is not None else {}
        plan = build_plan(doc, start, end, include_pending=self.include_pending.get(), repeats=self.use_repeats.get() or navigation_draft is not None, metronome=self.metronome.get(), instrument_id=solo, relative_pitch=self.relative_pitch.get(), **extra)
        if navigation_draft is not None:
            plan["warnings"].insert(0, "本次使用未確認的跳轉草稿，僅供對照演奏；不代表訓練路線已接受")
        self.stop()
        self.preview_warnings = plan["warnings"]
        self.play_plan = plan
        self.rendering = True
        self.cancel = threading.Event()
        generation, cancel = self.render_generation, self.cancel
        destination = Path(target) if target else doc.root / ".preview" / f"preview-{generation}.wav"
        if destination.suffix.lower() != ".wav":
            raise ContractError("試聽音訊請另存為 .wav")
        if destination.resolve() == doc.root / MANIFEST or destination.resolve().is_relative_to(doc.root / "assets"):
            raise ContractError("試聽 WAV 不可覆蓋原圖或校對檔")
        self.status.set(f"正在準備小節 {start}–{end} 的音訊；{len(plan['warnings'])} 項試聽提醒，詳見『試聽問題』。")
        def worker():
            try:
                result = render_wav(plan, doc.root, destination, cancel=cancel)
                self.messages.put((generation, result, plan, bool(target), None))
            except Exception as error:
                self.messages.put((generation, None, plan, bool(target), str(error)))
        threading.Thread(target=worker, daemon=True).start()

    def stop(self):
        self.cancel.set()
        self.render_generation += 1
        self.rendering = False
        self.started = None
        self.player.stop()
        if hasattr(self, "grid"):
            self.grid.canvas.delete("playhead")
        if self.preview_path:
            try:
                self.preview_path.unlink(missing_ok=True)
            except OSError:
                pass
            self.preview_path = None

    def poll(self):
        try:
            while True:
                generation, path, plan, exported, error = self.messages.get_nowait()
                if generation != self.render_generation:
                    if path and not exported:
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            pass
                    continue
                self.rendering = False
                if error:
                    self.status.set("音訊準備失敗：" + error)
                elif path:
                    if exported:
                        self.status.set(f"試聽 WAV 已匯出：{path}；{len(plan['warnings'])} 項提醒。")
                    else:
                        try:
                            self.preview_path = path
                            self.player.play(path)
                            self.started = time.monotonic()
                            self.play_plan = plan
                        except Exception as playback_error:
                            self.status.set("無法播放：" + str(playback_error))
        except queue.Empty:
            pass
        if self.started is not None and self.play_plan:
            elapsed = time.monotonic() - self.started
            position = next((b for b in self.play_plan["bars"] if b["start"] <= elapsed < b["start"] + b["duration"]), None)
            if position:
                if self.follow_playback.get() and not self.dirty and position["bar"] != self.current_bar:
                    self.follow_select = True
                    try:
                        self.bars.selection_set(str(position["bar"]))
                        self.bars.see(str(position["bar"]))
                        self.select_bar()
                    finally:
                        self.follow_select = False
                if position["bar"] == self.current_bar:
                    self.grid.playhead((elapsed - position["start"]) / position["duration"])
                self.status.set(f"{'候選試聽' if self.play_plan['includes_pending'] else '已接受資料試聽'}｜小節 {position['bar']}｜{elapsed:.1f} 秒｜{len(self.play_plan['warnings'])} 項提醒（試聽問題）")
            elif elapsed > self.play_plan["duration"] + 2.6:
                self.started = None
                self.status.set(f"試聽結束；{len(self.play_plan['warnings'])} 項提醒，請查看『試聽問題』。")
        self.poll_id = self.root.after(100, self.poll)

    def show_warnings(self):
        window = tk.Toplevel(self.root)
        window.title("試聽問題與略過原因")
        window.geometry("820x480")
        text = tk.Text(window, wrap="word", padx=12, pady=12)
        text.pack(fill="both", expand=True)
        text.insert("1.0", "試聽不等同訓練資料。\n\n" + ("\n\n".join(self.preview_warnings) or "目前沒有試聽問題；這不代表所有辨識均正確，仍需對照原譜。"))
        text.configure(state="disabled")

    def build_sounds(self):
        self.sound_pages = ttk.Notebook(self.sound_tab)
        self.sound_pages.pack(fill="both", expand=True, padx=8, pady=8)
        self.simple_sound_tab, self.advanced_sound_tab = (ttk.Frame(self.sound_pages) for _ in range(2))
        self.sound_pages.add(self.simple_sound_tab, text="本譜樂器音色")
        self.sound_pages.add(self.advanced_sound_tab, text="進階：樂器名稱、擊法與自訂音效")
        panel = ttk.Frame(self.simple_sound_tab, padding=14)
        panel.pack(fill="both", expand=True)
        self.score_sound_summary = ttk.Label(panel, text="匯入樂譜後，這裡會列出本譜出現的樂器。", font=("Microsoft JhengHei UI", 13, "bold"))
        self.score_sound_summary.pack(anchor="w", pady=(0, 8))
        ttk.Label(panel, text="每行選一種預設音色，選完自動保存，再按「試聽」。只調整試聽聲音，樂器名稱與音高照原資料保留。", wraplength=1000).pack(anchor="w")
        ttk.Label(panel, text="共用一列、尚未分清的樂器保留組合名稱；只有休止的列也會顯示。", foreground="#52647a", wraplength=1000).pack(anchor="w", pady=(4, 10))
        self.button(panel, "修正樂器名稱／批次整理…", lambda: open_review_assistant(self), side="top", anchor="w", pady=(0, 8))
        container, self.score_sound_host = scrollable(panel)
        container.pack(fill="both", expand=True)
        self.score_sound_widgets = {}
        self.tabs.bind("<<NotebookTabChanged>>", self.sound_page_selected, add="+")
        self.build_advanced_sounds(self.advanced_sound_tab)

    def sound_page_selected(self, event=None):
        if self.tabs.select() == str(self.sound_tab) and self.document:
            self.refresh_score_sounds()

    def refresh_score_sounds(self):
        for child in self.score_sound_host.winfo_children():
            child.destroy()
        self.score_sound_widgets = {}
        if not self.document:
            return
        rows = self.document.score_sound_rows()
        self.score_sound_summary.configure(text=f"這份樂譜的樂器音色（{len(rows)} 列）")
        host = self.score_sound_host
        for column, text in enumerate(("樂譜中的樂器", "預設音色", "", "說明")):
            ttk.Label(host, text=text, font=("Microsoft JhengHei UI", 10, "bold")).grid(row=0, column=column, sticky="w", padx=8, pady=(0, 8))
        host.columnconfigure(0, weight=1, minsize=210)
        host.columnconfigure(3, weight=1, minsize=180)
        for index, row in enumerate(rows, 1):
            current = row["override"]
            if current:
                choice = SOUND_PROFILES[current]
                detail = "已選擇並保存" + ("；原自訂音效暫不使用" if row["samples"] else "")
            elif row["samples"]:
                choice, detail = ORIGINAL_SOUND, "目前包含自訂 WAV；可改選內建音色"
            elif len(row["profiles"]) == 1 and not row["unmapped"]:
                choice = SOUND_PROFILES[next(iter(row["profiles"]))]
                detail = "目前沿用原有音色"
            else:
                choice = ORIGINAL_SOUND
                detail = "尚未選音色" if not row["profiles"] else "目前有多種或未對應設定"
            if row["notes"] == 0:
                detail += "；目前只有休止／待辨明符號"
            ttk.Label(host, text=row["name"], wraplength=330).grid(row=index, column=0, sticky="w", padx=8, pady=9)
            variable = tk.StringVar(value=choice)
            combo = ttk.Combobox(host, textvariable=variable, values=[*SOUND_PROFILES.values(), ORIGINAL_SOUND], state="readonly", width=21)
            combo.grid(row=index, column=1, sticky="w", padx=8, pady=9)
            combo.bind("<<ComboboxSelected>>", lambda event, key=row["key"], var=variable: self.action(lambda: self.choose_score_sound(key, var.get())))
            button = ttk.Button(host, text="▶ 試聽", command=lambda key=row["key"]: self.action(lambda: self.audition_score_sound(key)))
            button.grid(row=index, column=2, padx=8, pady=9)
            ttk.Label(host, text=detail, foreground="#52647a", wraplength=330).grid(row=index, column=3, sticky="w", padx=8, pady=9)
            self.score_sound_widgets[row["key"]] = {"choice": variable, "combo": combo, "button": button}
        if not rows:
            ttk.Label(host, text="目前沒有可列出的辨識結果。匯入後續結果後會自動更新。").grid(row=1, column=0, columnspan=4, pady=18, sticky="w")

    def choose_score_sound(self, key, label):
        doc = self.require_doc()
        self.commit_editor()
        profile = None if label == ORIGINAL_SOUND else next((k for k, v in SOUND_PROFILES.items() if v == label), None)
        if profile is None and label != ORIGINAL_SOUND:
            raise ContractError("請選擇清單中的預設音色")
        self.stop()
        doc.set_score_sound(key, profile)
        self.refresh_score_sounds()
        self.status.set(f"{key}：已保存音色「{label}」，可直接試聽或返回播放樂譜。")

    def audition_score_sound(self, key):
        doc = self.require_doc()
        row = next((row for row in doc.score_sound_rows() if row["key"] == key), None)
        if not row:
            raise ContractError("請重新整理本譜音色清單")
        profile, sample = row["override"], None
        if not profile:
            if len(row["profiles"]) != 1 or row["unmapped"] or len(row["samples"]) > 1:
                raise ContractError("請先在這一行選一種預設音色，再按試聽")
            profile = next(iter(row["profiles"]))
            sample = next(iter(row["samples"]), None)
        self.stop()
        path = doc.root / ".preview" / "score-sound.wav"
        plan = {"duration": .7, "notes": [{"start": 0, "duration": .7, "profile": profile, "sample": sample,
            "pitches": [48] if profile in {"timpani", "bell"} else [], "is_roll": False, "is_accent": False,
            "is_ornament": False, "dynamic": "mf", "beat_seconds": .5}]}
        render_wav(plan, doc.root, path)
        self.player.play(path)
        self.preview_path = path
        self.status.set(f"試聽 {key}：{SOUND_PROFILES[profile]}" + ("（自訂音效）" if sample else ""))

    def build_advanced_sounds(self, parent):
        panel = ttk.Frame(parent, padding=12)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text="先建立或選擇樂器，再對應原譜名稱／新符號。相同名稱可一次套用到尚未接受的項目。", wraplength=1000).pack(anchor="w", pady=(0, 10))
        upper = ttk.Frame(panel)
        upper.pack(fill="both", expand=True)
        self.catalog = ttk.Treeview(upper, columns=("name", "profile", "pitched"), show="headings", height=10, selectmode="browse")
        for key, label, width in (("name", "樂器", 140), ("profile", "試聽音效", 120), ("pitched", "保留音高", 100)):
            self.catalog.heading(key, text=label)
            self.catalog.column(key, width=width)
        self.catalog.pack(side="left", fill="both", expand=True, padx=(0, 15))
        self.catalog.bind("<<TreeviewSelect>>", lambda e: self.select_instrument())
        form = ttk.Frame(upper, width=420)
        form.pack(side="left", fill="both", expand=True)
        self.sound_name, self.sound_profile = tk.StringVar(), tk.StringVar(value="鼓")
        self.sound_pitched = tk.BooleanVar()
        ttk.Label(form, text="樂器名稱").pack(anchor="w")
        ttk.Entry(form, textvariable=self.sound_name).pack(fill="x", pady=4)
        ttk.Label(form, text="內建校對音效").pack(anchor="w")
        ttk.Combobox(form, textvariable=self.sound_profile, values=list(SOUND_PROFILES.values()), state="readonly").pack(fill="x", pady=4)
        ttk.Checkbutton(form, text="這個樂器需要實際音高", variable=self.sound_pitched).pack(anchor="w", pady=3)
        buttons = ttk.Frame(form)
        buttons.pack(fill="x", pady=7)
        self.button(buttons, "新增樂器", self.new_instrument)
        self.button(buttons, "保存音效設定", self.save_instrument)
        ttk.Label(form, text="擊法（音級與擊法分開；輪奏另以記號保存）").pack(anchor="w", pady=(6, 3))
        self.sound_technique = tk.StringVar()
        self.sound_technique_combo = ttk.Combobox(form, textvariable=self.sound_technique, state="readonly")
        self.sound_technique_combo.pack(fill="x", pady=3)
        self.sound_technique_combo.bind("<<ComboboxSelected>>", lambda e: self.update_sample_label())
        technique_buttons = ttk.Frame(form)
        technique_buttons.pack(fill="x", pady=6)
        self.button(technique_buttons, "新增擊法", self.new_technique)
        self.button(technique_buttons, "匯入 WAV 音效…", self.import_sample)
        self.button(technique_buttons, "使用內建音效", self.clear_sample)
        self.sample_label = ttk.Label(form, text="", wraplength=420)
        self.sample_label.pack(anchor="w")
        self.button(form, "試聽此音效", self.audition_sound, side="top", anchor="w", pady=7)
        ttk.Label(panel, text="內建聲音用於分辨軌道與核對節奏。自訂音效支援 16-bit PCM WAV；樣本不會依音高自動改調。", foreground="#52647a", wraplength=1050).pack(anchor="w", pady=10)
        mapping = ttk.LabelFrame(panel, text="批次確認名稱／技法", padding=12)
        mapping.pack(fill="x", pady=8)
        self.raw_name, self.raw_symbol = tk.StringVar(), tk.StringVar()
        ttk.Label(mapping, text="原譜名稱").grid(row=0, column=0, sticky="w")
        self.raw_name_combo = ttk.Combobox(mapping, textvariable=self.raw_name, state="readonly", width=28)
        self.raw_name_combo.grid(row=0, column=1, padx=8, sticky="ew")
        ttk.Label(mapping, text="只限原符號（留白＝同名全部）").grid(row=1, column=0, sticky="w", pady=8)
        ttk.Entry(mapping, textvariable=self.raw_symbol).grid(row=1, column=1, padx=8, sticky="ew")
        mapping.columnconfigure(1, weight=1)
        ttk.Button(mapping, text="對應到上方選取的樂器與擊法", command=lambda: self.action(self.apply_mapping)).grid(row=2, column=1, sticky="e", pady=8)

    def refresh_catalog(self):
        if not self.document:
            return
        selected = self.catalog.selection()
        self.catalog.delete(*self.catalog.get_children())
        for ins in self.document.data["instruments"]:
            self.catalog.insert("", "end", iid=ins["instrument_id"], values=(ins["name"], SOUND_PROFILES[ins["profile"]], "需要" if ins["pitched"] else "無"))
        names = sorted({e["value"].get("instrument_text") or e["value"]["staff_label"] for e in self.document.entries()})
        self.raw_name_combo.configure(values=names)
        self.instrument_combo.configure(values=[i["name"] for i in self.document.data["instruments"]])
        self.solo_combo.configure(values=["全部樂器"] + [i["name"] for i in self.document.data["instruments"]])
        if self.solo.get() not in self.solo_combo.cget("values"):
            self.solo.set("全部樂器")
        if selected and self.catalog.exists(selected[0]):
            self.catalog.selection_set(selected[0])
        elif self.catalog.get_children():
            self.catalog.selection_set(self.catalog.get_children()[0])
        self.select_instrument()
        self.refresh_score_sounds()

    def selected_instrument(self):
        self.require_doc()
        selected = self.catalog.selection()
        if not selected:
            raise ContractError("先選取樂器")
        return self.document.instrument(selected[0])

    def selected_stroke(self):
        ins = self.selected_instrument()
        stroke = next((s for s in ins["strokes"] if s["name"] == self.sound_technique.get()), None)
        if not stroke:
            raise ContractError("先選取擊法")
        return ins, stroke

    def select_instrument(self):
        if not self.document or not self.catalog.selection():
            return
        ins = self.selected_instrument()
        self.sound_name.set(ins["name"])
        self.sound_profile.set(SOUND_PROFILES[ins["profile"]])
        self.sound_pitched.set(ins["pitched"])
        self.sound_technique_combo.configure(values=[s["name"] for s in ins["strokes"]])
        self.sound_technique.set(ins["strokes"][0]["name"])
        self.update_sample_label()

    def update_sample_label(self):
        if not self.document or not self.catalog.selection():
            return
        _, stroke = self.selected_stroke()
        self.sample_label.configure(text="自訂 WAV：" + stroke["sample"] if stroke.get("sample") else "使用內建校對音效")

    def new_instrument(self):
        doc = self.require_doc()
        self.commit_editor()
        profile = next(k for k, v in SOUND_PROFILES.items() if v == self.sound_profile.get())
        ins = doc.add_instrument(self.sound_name.get(), profile, self.sound_pitched.get())
        self.refresh_catalog()
        self.catalog.selection_set(ins["instrument_id"])
        self.select_instrument()

    def save_instrument(self):
        self.commit_editor()
        ins = self.selected_instrument()
        name = self.sound_name.get().strip()
        if not name or any(i["name"] == name and i is not ins for i in self.document.data["instruments"]):
            raise ContractError("樂器名稱不可空白或重複")
        self.stop()
        ins.update(name=name, profile=next(k for k, v in SOUND_PROFILES.items() if v == self.sound_profile.get()), pitched=self.sound_pitched.get())
        self.document.save()
        self.refresh_catalog()
        self.refresh_current(self.editor_id)

    def new_technique(self):
        ins = self.selected_instrument()
        name = simpledialog.askstring("新增擊法", "擊法名稱（例如鼓邊、悶音、擦奏）", parent=self.root)
        if name:
            stroke = self.document.add_technique(ins["instrument_id"], name)
            self.select_instrument()
            self.sound_technique.set(stroke["name"])

    def import_sample(self):
        ins, stroke = self.selected_stroke()
        path = filedialog.askopenfilename(parent=self.root, title="選擇單次敲擊的 WAV 音效", filetypes=[("16-bit PCM WAV", "*.wav")])
        if path:
            self.stop()
            self.document.set_sample(ins["instrument_id"], stroke["technique_id"], path)
            self.update_sample_label()

    def clear_sample(self):
        _, stroke = self.selected_stroke()
        self.stop()
        stroke["sample"] = None
        self.document.save()
        self.update_sample_label()

    def audition_sound(self):
        ins, stroke = self.selected_stroke()
        self.stop()
        path = self.document.root / ".preview" / "sound.wav"
        plan = {"duration": .7, "notes": [{"start": 0, "duration": .7, "profile": ins["profile"], "sample": stroke.get("sample"),
                "pitches": [60] if ins["pitched"] else [], "is_roll": False, "is_accent": False, "is_ornament": False, "dynamic": "mf", "beat_seconds": .5}]}
        render_wav(plan, self.document.root, path)
        self.player.play(path)

    def apply_mapping(self):
        doc = self.require_doc()
        self.commit_editor()
        ins, stroke = self.selected_stroke()
        if not self.raw_name.get():
            raise ContractError("先選擇原譜名稱")
        self.stop()
        count = doc.map_name(self.raw_name.get(), ins["instrument_id"], stroke["technique_id"], self.raw_symbol.get().strip())
        self.refresh_current(self.editor_id)
        self.status.set(f"已對應 {count} 筆到 {ins['name']}／{stroke['name']}；仍保留待核對狀態。")

    def build_segments(self):
        container, panel = scrollable(self.segment_tab)
        container.pack(fill="both", expand=True)
        ttk.Label(panel, text="一般段落填紙面起訖；跨 D.C.／Coda 或多次返回時，可新增一個按演奏順序排列的跳轉段落。", wraplength=1050).pack(anchor="w")
        self.segments = ttk.Treeview(panel, columns=("name", "start", "end", "events", "state", "order"), show="headings", height=5, selectmode="browse")
        for key, label in (("name", "段落名稱"), ("start", "開始小節"), ("end", "最後小節"), ("events", "擊打事件數"), ("state", "資料狀態"), ("order", "自訂演奏順序")):
            self.segments.heading(key, text=label)
            self.segments.column(key, width={"name": 140, "start": 75, "end": 75, "events": 100, "state": 220, "order": 300}[key], minwidth=60)
        self.segments.pack(fill="x", pady=8)
        self.segments.bind("<<TreeviewSelect>>", lambda e: self.select_segment())
        controls = ttk.Frame(panel)
        controls.pack(fill="x", pady=5)
        self.segment_name, self.segment_start, self.segment_end = tk.StringVar(), tk.StringVar(value="1"), tk.StringVar(value="4")
        for label, var, width in (("名稱", self.segment_name, 20), ("起點", self.segment_start, 6), ("終點", self.segment_end, 6)):
            ttk.Label(controls, text=label).pack(side="left", padx=3)
            ttk.Entry(controls, textvariable=var, width=width).pack(side="left", padx=3)
        self.button(controls, "加入段落", self.add_segment)
        self.button(controls, "保存段落修改", self.update_segment)
        self.button(controls, "移除選取段落", self.remove_segment)
        self.button(controls, "聽選取段落", self.play_segment)
        ttk.Label(panel, text="事件數按目前演奏順序計算；輪奏記一次。0 旁若有缺資料／待核對，不能當成整段無聲。移除段落後不匯出該段；其他保留段落引用的小節仍保留。", wraplength=1020, foreground="#52647a").pack(anchor="w", pady=4)
        sequence_controls = ttk.Frame(panel)
        sequence_controls.pack(fill="x", pady=5)
        self.button(sequence_controls, "新增跳轉段落…", lambda: open_sequence_editor(self))
        self.button(sequence_controls, "修改選取跳轉段落…", self.edit_sequence_segment)
        ttk.Separator(panel).pack(fill="x", pady=10)
        scope_controls = ttk.Frame(panel)
        scope_controls.pack(fill="x", pady=4)
        self.nav_scope_start, self.nav_scope_end = tk.StringVar(value="1"), tk.StringVar(value="9")
        for label, variable in (("反覆核對範圍：小節", self.nav_scope_start), ("至", self.nav_scope_end)):
            ttk.Label(scope_controls, text=label).pack(side="left", padx=3)
            ttk.Entry(scope_controls, textvariable=variable, width=6).pack(side="left", padx=3)
        ttk.Label(scope_controls, text="範圍須涵蓋完整反覆及所有結尾；可分批核對。", foreground="#52647a").pack(side="left", padx=8)
        nav_controls = ttk.Frame(panel)
        nav_controls.pack(fill="x", pady=4)
        self.button(nav_controls, "載入此範圍候選", self.load_nav_candidates)
        self.button(nav_controls, "清空（無反覆）", self.clear_navigation)
        self.nav_ack = tk.BooleanVar()
        ttk.Checkbutton(nav_controls, text="已核對此範圍反覆記號", variable=self.nav_ack).pack(side="left", padx=8)
        for variable in (self.nav_scope_start, self.nav_scope_end):
            variable.trace_add("write", lambda *_: self.nav_ack.set(False))
        self.button(nav_controls, "預覽並確認反覆", self.confirm_navigation)
        self.button(nav_controls, "預覽草稿路線", self.preview_navigation)
        self.button(nav_controls, "依草稿試聽", self.play_navigation_draft)
        self.navigation = ttk.Treeview(panel, columns=("kind", "start", "end", "passes", "times", "route"), show="headings", height=5, selectmode="browse")
        for key, label in (("kind", "種類"), ("start", "起點"), ("end", "終點／D.C.位置"), ("passes", "第幾次結尾"), ("times", "反覆總次數"), ("route", "跳轉路線與返回處理")):
            self.navigation.heading(key, text=label)
            self.navigation.column(key, width=360 if key == "route" else 125, minwidth=70)
        self.navigation.pack(fill="both", expand=True, pady=6)
        inputs = ttk.Frame(panel)
        inputs.pack(fill="x", pady=6)
        self.nav_kind = tk.StringVar(value="反覆起點")
        ttk.Combobox(inputs, textvariable=self.nav_kind, values=list(NAV_KINDS), state="readonly", width=12).pack(side="left", padx=3)
        self.nav_start, self.nav_end, self.nav_passes, self.nav_times = (tk.StringVar(value=v) for v in ("1", "1", "1", "2"))
        for label, var in (("小節", self.nav_start), ("結尾終點", self.nav_end), ("結尾次數", self.nav_passes), ("反覆次數", self.nav_times)):
            ttk.Label(inputs, text=label).pack(side="left", padx=3)
            ttk.Entry(inputs, textvariable=var, width=6).pack(side="left")
        self.button(inputs, "加入記號", self.add_navigation)
        self.button(inputs, "移除記號", self.remove_navigation)
        ttk.Label(panel, text="文字跳轉：D.C. 在所填小節結束後返回；Coda 從尾奏起點開始。配對與返回後反覆須人工核對。", wraplength=1050).pack(anchor="w", pady=(8, 2))
        jump_controls = ttk.Frame(panel)
        jump_controls.pack(fill="x", pady=4)
        self.jump_kind = tk.StringVar(value="D.C. 返回後接 Coda")
        self.jump_bar, self.jump_until, self.jump_target = (tk.StringVar() for _ in range(3))
        ttk.Combobox(jump_controls, textvariable=self.jump_kind, values=list(JUMP_LABELS), state="readonly", width=23).pack(side="left")
        for label, var in (("D.C. 小節末", self.jump_bar), ("回奏至小節末", self.jump_until), ("尾奏起點", self.jump_target)):
            ttk.Label(jump_controls, text=label).pack(side="left", padx=4)
            ttk.Entry(jump_controls, textvariable=var, width=6).pack(side="left")
        jump_policy = ttk.Frame(panel)
        jump_policy.pack(fill="x", pady=4)
        ttk.Label(jump_policy, text="返回後：").pack(side="left")
        self.jump_policy = tk.StringVar(value="請選擇（尚未確認）")
        ttk.Combobox(jump_policy, textvariable=self.jump_policy, values=list(RETURN_POLICIES), state="readonly", width=32).pack(side="left")
        self.button(jump_policy, "加入跳轉草稿", self.add_jump)
        self.button(jump_policy, "載入選取跳轉修改", self.select_jump)
        self.nav_evidence = ttk.Label(panel, text="", wraplength=1050, foreground="#52647a")
        self.nav_evidence.pack(anchor="w", pady=4)
        self.navigation_preview = ttk.Label(panel, text="尚未確認反覆。沒有反覆也請確認空清單。", wraplength=1100, foreground="#52647a")
        self.navigation_preview.pack(anchor="w", pady=8)

    def refresh_segments(self):
        selected = self.segments.selection()
        self.segments.delete(*self.segments.get_children())
        for segment in self.document.data["segments"]:
            stats = segment_statistics(self.document, segment)
            detail = format_sequence(segment["ranges"]) + ("（待核對）" if not segment.get("sequence_acknowledged") else "") if segment.get("kind") == "sequence" else "依紙面範圍"
            self.segments.insert("", "end", iid=segment["segment_id"], values=(segment["name"], segment["start_bar"], segment["end_bar"], stats["event_count"], stats["status"], detail))
        if selected and self.segments.exists(selected[0]):
            self.segments.selection_set(selected[0])

    def edit_sequence_segment(self):
        doc = self.require_doc()
        selected = self.segments.selection()
        segment = next((s for s in doc.data["segments"] if selected and s["segment_id"] == selected[0] and s.get("kind") == "sequence"), None)
        return open_sequence_editor(self, segment)

    def select_segment(self):
        if self.segments.selection():
            s = next(s for s in self.document.data["segments"] if s["segment_id"] == self.segments.selection()[0])
            self.segment_name.set(s["name"])
            self.segment_start.set(str(s["start_bar"]))
            self.segment_end.set(str(s["end_bar"]))

    def add_segment(self):
        self.commit_editor()
        self.require_doc().add_segment(self.segment_name.get(), int(self.segment_start.get()), int(self.segment_end.get()))
        self.refresh_segments()

    def remove_segment(self):
        doc = self.require_doc()
        if self.segments.selection():
            selected = self.segments.selection()[0]
            doc.data["segments"] = [s for s in doc.data["segments"] if s["segment_id"] != selected]
            doc.save()
            self.refresh_segments()

    def update_segment(self):
        doc = self.require_doc()
        if not self.segments.selection():
            raise ContractError("先選取要修改的段落")
        selected = self.segments.selection()[0]
        doc.update_segment(selected, self.segment_name.get(), int(self.segment_start.get()), int(self.segment_end.get()))
        self.refresh_segments()
        self.segments.selection_set(selected)

    def play_segment(self):
        doc = self.require_doc()
        if self.segments.selection():
            segment = next(s for s in doc.data["segments"] if s["segment_id"] == self.segments.selection()[0])
            if segment.get("kind") == "sequence":
                order = [item["source_bar"] for item in doc.segment_order(segment)]
                return self.start_render(min(order), max(order), source_order=order)
        self.play_start.set(self.segment_start.get())
        self.play_end.set(self.segment_end.get())
        self.play_range()

    def refresh_navigation(self):
        self.navigation.delete(*self.navigation.get_children())
        for index, m in enumerate(self.nav_draft):
            kind = next((k for k, v in {**NAV_KINDS, **JUMP_LABELS}.items() if v == m["type"]), m["type"])
            start = m.get("start_bar", "") if m["type"] == "volta" else (m["bar"] if m["type"] == "repeat_start" else "")
            end = m.get("end_bar", "") if m["type"] == "volta" else (m["bar"] if m["type"] in {"repeat_end", "dc", "dc_coda"} else "")
            self.navigation.insert("", "end", iid=str(index), values=(kind, start, end, ",".join(map(str, m.get("passes", []))), m.get("times", ""), route_summary([m])))
        active = {r["run_id"] for r in self.document.data["regions"].values() if r["status"] in {"succeeded", "empty"}}
        other = [f"小節 {c['marker']['start_bar']}：{c['marker']['raw_text']}" for c in self.document.data["navigation_candidates"]
                 if c["run_id"] in active and c["marker"]["kind"] == "other"]
        self.nav_evidence.configure(text="文字／其他跳轉原始候選：" + ("；".join(other) if other else "沒有回報；請另看整頁原譜，裁圖外的記號可能漏掉。"))

    def add_jump(self):
        self.require_doc()
        if self.jump_policy.get() not in RETURN_POLICIES:
            raise ContractError("請先核對並選擇返回後是否重做一般反覆")
        marker = {"type": JUMP_LABELS[self.jump_kind.get()], "bar": int(self.jump_bar.get()),
                  "repeat_policy": RETURN_POLICIES[self.jump_policy.get()]}
        if marker["type"] == "dc_coda":
            marker.update(until_bar=int(self.jump_until.get()), target_bar=int(self.jump_target.get()))
        # 修改同一組返回；仍保存為未確認草稿，不自動套用或接受推定路線。
        proposed = [deepcopy(m) for m in self.nav_draft if m["type"] not in {"dc", "dc_coda"}] + [marker]
        self.validate_navigation_draft(proposed)
        self.nav_draft = proposed
        self.navigation_changed()

    def select_jump(self):
        self.require_doc()
        if not self.navigation.selection():
            raise ContractError("請選取清單中的 D.C. 跳轉")
        marker = self.nav_draft[int(self.navigation.selection()[0])]
        if marker["type"] not in {"dc", "dc_coda"}:
            raise ContractError("請選取 D.C. 跳轉")
        self.jump_kind.set(next(k for k, v in JUMP_LABELS.items() if v == marker["type"]))
        self.jump_bar.set(str(marker["bar"]))
        self.jump_until.set(str(marker.get("until_bar", "")))
        self.jump_target.set(str(marker.get("target_bar", "")))
        self.jump_policy.set(next((k for k, v in RETURN_POLICIES.items() if v == marker.get("repeat_policy")), "請選擇（尚未確認）"))

    def validate_navigation_draft(self, markers):
        start, end = self.navigation_scope()
        for m in markers:
            first, last = marker_range(m)
            if first < start or last > end:
                raise ContractError("請擴大核對範圍，涵蓋開頭、D.C.、回奏與尾奏起點")
        return [item for item in expand_playback_order(max(map(int, self.document.data["bars"])), markers)
                if start <= item["source_bar"] <= end]

    def preview_navigation(self):
        self.require_doc()
        order = self.validate_navigation_draft(self.nav_draft)
        window = tk.Toplevel(self.root)
        window.title("演奏路線草稿（尚未確認）")
        window.geometry("850x500")
        text = tk.Text(window, wrap="word", padx=12, pady=12)
        scroll = ttk.Scrollbar(window, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        lines = [route_summary(self.nav_draft), f"共 {len(order)} 個演奏小節；不修改原事件。"]
        for phase, label in ((0, "首次演奏"), (1, "D.C. 返回後"), (2, "Coda 尾奏")):
            items = [str(item["source_bar"]) for item in order if item.get("route_phase", 0) == phase]
            if items:
                lines += ["\n" + label, " → ".join(items)]
        text.insert("1.0", "\n".join(lines))
        text.configure(state="disabled")
        return order

    def play_navigation_draft(self):
        self.require_doc()
        self.validate_navigation_draft(self.nav_draft)
        start, end = self.navigation_scope()
        self.start_render(start, end, navigation_draft=self.nav_draft)

    def load_nav_candidates(self):
        self.require_doc()
        start, end = self.navigation_scope()
        self.nav_draft = self.document.suggested_navigation(start, end)
        self.navigation_changed()
        self.navigation_preview.configure(text=f"已載入小節 {start}–{end} 的候選；檢查起點、終點與第一／第二結尾，再預覽確認。")

    def clear_navigation(self):
        self.require_doc()
        self.nav_draft = []
        self.navigation_changed()

    def add_navigation(self):
        self.require_doc()
        kind = NAV_KINDS[self.nav_kind.get()]
        if kind == "volta":
            marker = {"type": kind, "start_bar": int(self.nav_start.get()), "end_bar": int(self.nav_end.get()),
                      "passes": [int(p.strip()) for p in self.nav_passes.get().replace("，", ",").split(",")]}
        else:
            marker = {"type": kind, "bar": int(self.nav_start.get())}
            if kind == "repeat_end":
                marker["times"] = int(self.nav_times.get())
        self.nav_draft.append(marker)
        self.navigation_changed()

    def remove_navigation(self):
        self.require_doc()
        if self.navigation.selection():
            del self.nav_draft[int(self.navigation.selection()[0])]
            self.navigation_changed()

    def navigation_changed(self):
        self.nav_ack.set(False)
        start, end = self.navigation_scope()
        invalidate_navigation(self.document.data, start, end)
        self.document.data.update(navigation_draft=deepcopy(self.nav_draft), navigation_draft_scope={"start_bar": start, "end_bar": end})
        self.document.save()
        self.refresh_navigation()
        self.navigation_preview.configure(text="反覆清單已修改，尚未確認。請核對後重新預覽演奏順序。")

    def confirm_navigation(self):
        doc = self.require_doc()
        if not self.nav_ack.get():
            raise ContractError("請先核對這個範圍的反覆記號，再勾選確認")
        start, end = self.navigation_scope()
        order = doc.set_navigation(self.nav_draft, start, end)
        self.navigation_preview.configure(text=f"小節 {start}–{end} 已確認，共 {len(order)} 個演奏小節。" + route_summary(self.nav_draft) + "\n" + " → ".join(str(item["source_bar"]) for item in order[:80]) + (" …（完整順序見『預覽草稿路線』）" if len(order) > 80 else "") + ("；其餘範圍仍待核對。" if not doc.data["navigation_confirmed"] else ""))

    def navigation_scope(self):
        start, end = int(self.nav_scope_start.get()), int(self.nav_scope_end.get())
        if start > end or any(str(b) not in self.document.data["bars"] for b in range(start, end + 1)):
            raise ContractError("請填已登記且連續的反覆核對範圍，並涵蓋完整反覆與結尾")
        return start, end

    def export_training(self):
        doc = self.require_doc()
        self.commit_editor()
        selected = filedialog.asksaveasfilename(parent=self.root, title="匯出人工核對完成的訓練段落", defaultextension=".json", initialfile="reviewed-training.json", filetypes=[("訓練資料", "*.json")])
        if selected:
            report = doc.export_training(selected)
            self.status.set(f"已匯出 {report['segments']} 段、{report['bars']} 小節、{report['events']} 筆敲擊；段落外資料不包含在本次匯出。")

    def export_tables(self):
        doc = self.require_doc()
        self.commit_editor()
        selected = filedialog.asksaveasfilename(
            parent=self.root, title="匯出已核對段落的檢查與統計表格",
            defaultextension=".xlsx", initialfile="reviewed-tables.xlsx",
            filetypes=[("Excel 活頁簿", "*.xlsx"), ("CSV 表格壓縮包（內含多張 CSV）", "*.zip")])
        if selected:
            report = doc.export_tables(selected)
            self.status.set(f"表格已匯出：{report['segments']} 段、{report['events']} 筆原譜事件、{report['performed_events']} 筆演奏事件。表格修改不會回寫校對專案。")

    def close(self):
        try:
            self.commit_editor()
            if self.document:
                self.document.save()
        except Exception as error:
            messagebox.showerror("修改尚未保存", str(error) + "\n請修正欄位後再關閉。", parent=self.root)
            return
        self.stop()
        self.root.after_cancel(self.poll_id)
        self.root.destroy()


def fraction_ui(value):
    return Fraction(value["n"], value["d"])


def raise_error(error):
    raise error


def run():
    import argparse
    parser = argparse.ArgumentParser(description="MusicAI 校對與試聽（程式 B）")
    parser.add_argument("--project", help="既有校對專案資料夾或 review-project.json")
    parser.add_argument("--source", help="從程式 A 的資料建立／接續校對專案")
    args = parser.parse_args()
    root = tk.Tk()
    app = ReviewerApp(root)
    if args.project:
        app.action(lambda: app.load_document(ReviewDocument.load(args.project)))
    elif args.source:
        app.action(lambda: app.load_document(create_from_source(args.source)[0]))
    else:
        existing = list((Path(__file__).resolve().parents[1] / "projects").glob("*.score-review/review-project.json"))
        if len(existing) == 1:
            app.action(lambda: app.load_document(ReviewDocument.load(existing[0])))
    root.mainloop()
