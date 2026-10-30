"""把重複驗證問題集中為一次性整理與少量剩餘位置。"""
from collections import defaultdict
import tkinter as tk
from tkinter import ttk

from score_recognizer.contract import ContractError
from .document import SOUND_PROFILES
from .review_workflow import REVIEW_MODES, correct_name, organize_instruments, review_summary, inferred_profile
from .segments import segment_bars


def open_review_assistant(app):
    doc = app.require_doc()
    app.commit_editor()
    previous = getattr(app, "review_assistant_window", None)
    if previous is not None and previous.winfo_exists():
        if previous.review_document is doc:
            previous.form["refresh"]()
            previous.deiconify()
            previous.lift()
            return previous
        previous.destroy()

    # 助手和原譜編輯器需要同時操作。不要建立 transient 的視窗從屬關係，
    # 讓 Windows 可以獨立啟用主視窗及其 Combobox 彈出選單。
    window = tk.Toplevel(app.root)
    window.review_document = doc
    app.review_assistant_window = window
    window.title("核對助手｜整譜整理與剩餘問題")
    window.geometry("1030x720")
    panel = ttk.Frame(window, padding=14)
    panel.pack(fill="both", expand=True)

    def guard():
        if app.document is not doc:
            raise ContractError("已切換專案，請關閉後從目前專案重新開啟核對助手")

    ttk.Label(panel, text="① 選核對方式", font=("Microsoft JhengHei UI", 11, "bold")).pack(anchor="w")
    mode = tk.StringVar(value=next(k for k, v in REVIEW_MODES.items() if v == doc.data["review_mode"]))
    modes = ttk.Combobox(panel, textvariable=mode, values=list(REVIEW_MODES), state="readonly", width=40)
    modes.pack(anchor="w", pady=4)
    ttk.Label(panel, text="節奏優先：樂器與敲擊時間仍須明確；未知技法、實際音高、重音等可留空，匯出會標示該欄位不可作為確定答案。", wraplength=960).pack(anchor="w", pady=(0, 10))
    ttk.Label(panel, text="② 一次修正整份譜的樂器名稱", font=("Microsoft JhengHei UI", 11, "bold")).pack(anchor="w")
    correction = ttk.Frame(panel)
    correction.pack(fill="x", pady=5)
    raw, target, profile = tk.StringVar(), tk.StringVar(), tk.StringVar(value="鼓")
    ttk.Label(correction, text="原名稱").pack(side="left")
    raw_combo = ttk.Combobox(correction, textvariable=raw, state="readonly", width=27)
    raw_combo.pack(side="left", padx=6)
    ttk.Label(correction, text="改為").pack(side="left")
    target_combo = ttk.Combobox(correction, textvariable=target, width=18)
    target_combo.pack(side="left", padx=6)
    ttk.Combobox(correction, textvariable=profile, values=list(SOUND_PROFILES.values()), state="readonly", width=12).pack(side="left", padx=6)
    ttk.Label(panel, text="例如：原名稱選「軍鼓」，改為「軍鈸」。會連同共用列中的名稱修正，原辨識紀錄另行保留。", foreground="#52647a", wraplength=960).pack(anchor="w")

    def target_changed(*args):
        sound = inferred_profile(target.get().strip())
        if sound:
            profile.set(SOUND_PROFILES[sound])

    target.trace_add("write", target_changed)
    operation_status = ttk.Label(panel, text="", wraplength=960)

    def refresh_app():
        app.refresh_catalog()
        app.refresh_bar_list()
        app.refresh_current(app.editor_id)

    def rename():
        guard()
        app.commit_editor()
        sound = next(k for k, v in SOUND_PROFILES.items() if v == profile.get())
        count = correct_name(doc, raw.get(), target.get(), sound)
        refresh_app()
        operation_status.configure(text=f"已整譜修正 {count} 筆名稱；原始辨識保留，需重新核對的項目維持待核對。")
        refresh()

    ttk.Button(correction, text="整譜套用", command=lambda: app.action(rename)).pack(side="left", padx=4)
    arrange = ttk.Frame(panel)
    arrange.pack(fill="x", pady=(8, 4))

    def organize():
        guard()
        app.commit_editor()
        result = organize_instruments(doc)
        refresh_app()
        operation_status.configure(text=f"已依單一樂器名稱整理 {result['mapped']} 筆，建立 {result['created']} 種樂器。共用聲部不猜測、未知技法保留。")
        refresh()

    ttk.Button(arrange, text="依名稱整理樂器", command=lambda: app.action(organize)).pack(side="left")
    ttk.Label(arrange, text="整理小堂鼓、鈴鼓等明確名稱；不需要逐筆建立對應。", foreground="#52647a").pack(side="left", padx=8)
    basic_controls = ttk.Frame(panel)
    basic_controls.pack(fill="x", pady=4)

    def apply_basic():
        guard()
        app.commit_editor()
        app.stop()
        result = doc.apply_basic_techniques()
        refresh_app()
        operation_status.configure(text=f"已套用 {result['mapped']} 筆基本擊法（一般敲擊）；另有 {result['missing_instrument']} 筆需先確認樂器。其他核對問題仍需處理。")
        refresh()

    ttk.Button(basic_controls, text="全部套用基本擊法", command=lambda: app.action(apply_basic)).pack(side="left")
    ttk.Label(basic_controls, text="全譜：只補尚未對應的擊法，保留輪奏、重音及裝飾音。", foreground="#52647a").pack(side="left", padx=8)
    operation_status.pack(anchor="w", pady=4)
    ttk.Separator(panel).pack(fill="x", pady=8)
    ttk.Label(panel, text="③ 批次接受可用資料，集中處理剩餘位置", font=("Microsoft JhengHei UI", 11, "bold")).pack(anchor="w")
    scope_controls = ttk.Frame(panel)
    scope_controls.pack(fill="x", pady=5)
    scope = tk.StringVar(value="已建立段落" if doc.data["segments"] else "全譜")
    scope_combo = ttk.Combobox(scope_controls, textvariable=scope, values=["已建立段落", "全譜", "目前小節"], state="readonly", width=18)
    scope_combo.pack(side="left")
    summary_label = ttk.Label(scope_controls, text="")
    summary_label.pack(side="left", padx=12)
    soft_label = ttk.Label(panel, text="", foreground="#52647a", wraplength=960)
    soft_label.pack(anchor="w", pady=3)
    table_host = ttk.Frame(panel)
    table_host.pack(fill="both", expand=True, pady=6)
    table = ttk.Treeview(table_host, columns=("bar", "page", "issues"), show="headings", height=7)
    for key, label, width in (("bar", "小節", 70), ("page", "頁碼", 60), ("issues", "需處理原因（雙擊回到原譜）", 780)):
        table.heading(key, text=label)
        table.column(key, width=width, minwidth=40)
    scroll = ttk.Scrollbar(table_host, orient="vertical", command=table.yview)
    table.configure(yscrollcommand=scroll.set)
    table.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")

    def bars():
        if scope.get() == "目前小節":
            return {app.current_bar} if app.current_bar is not None else set()
        if scope.get() == "已建立段落":
            return {b for s in doc.data["segments"] for b in segment_bars(s)}
        return set(map(int, doc.data["bars"]))

    acknowledged = tk.BooleanVar()

    def refresh(event=None):
        guard()
        acknowledged.set(False)
        names = sorted({e["value"].get("instrument_text") or e["value"]["staff_label"] for e in doc.entries()})
        raw_combo.configure(values=names)
        target_combo.configure(values=[i["name"] for i in doc.data["instruments"]])
        report = review_summary(doc, bars())
        grouped = defaultdict(set)
        for row in report["problems"]:
            grouped[row["bar"]].update(row["issues"])
        table.delete(*table.get_children())
        for bar, problems in sorted(grouped.items()):
            table.insert("", "end", iid=str(bar), values=(bar, doc.data["bars"][str(bar)]["page"], "、".join(sorted(problems))))
        summary_label.configure(text=f"可批次接受 {len(report['ready'])} 筆；需處理 {len(grouped)} 個小節")
        soft_label.configure(text="可保留的未知細節：" + ("；".join(f"{k} {v} 筆" for k, v in report["optional"].items()) or "目前沒有"))
        return report

    def set_mode(event=None):
        guard()
        app.commit_editor()
        doc.set_review_mode(REVIEW_MODES[mode.get()])
        refresh_app()
        refresh()

    def accept_ready():
        guard()
        if app.dirty:
            app.commit_editor()
            refresh()
        if not acknowledged.get():
            raise ContractError("請先勾選已核對此範圍節奏；不需要替未知細節猜答案")
        report = review_summary(doc, bars())
        doc.accept(report["ready"], acknowledged=True)
        refresh_app()
        operation_status.configure(text=f"已接受 {len(report['ready'])} 筆可用資料，其餘問題留待處理。")
        refresh()

    def locate(event=None):
        guard()
        if table.selection():
            bar = table.selection()[0]
            app.bars.selection_set(bar)
            app.bars.see(bar)
            app.select_bar()
            app.tabs.select(app.review_tab)
            window.destroy()

    table.bind("<Double-1>", lambda e: app.action(locate))
    modes.bind("<<ComboboxSelected>>", lambda e: app.action(set_mode))
    scope_combo.bind("<<ComboboxSelected>>", lambda e: app.action(refresh))
    ttk.Checkbutton(panel, text="我已試聽／核對此範圍節奏，接受其中可用的資料（未知細節照實保留）", variable=acknowledged).pack(anchor="w", pady=5)
    controls = ttk.Frame(panel)
    controls.pack(fill="x")
    ttk.Button(controls, text="重新檢查", command=lambda: app.action(refresh)).pack(side="left", padx=(0, 8))
    ttk.Button(controls, text="接受可用資料", command=lambda: app.action(accept_ready)).pack(side="left")
    ttk.Button(controls, text="關閉", command=window.destroy).pack(side="right")
    refresh()
    window.form = {"mode": mode, "set_mode": set_mode, "raw": raw, "target": target, "profile": profile,
                   "rename": rename, "organize": organize, "apply_basic": apply_basic, "scope": scope, "refresh": refresh,
                   "acknowledged": acknowledged, "accept": accept_ready, "table": table}
    return window
