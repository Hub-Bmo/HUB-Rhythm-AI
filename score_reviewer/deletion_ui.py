"""有明確範圍與數量預覽的整列／整譜名稱排除；紀錄可復原。"""
import tkinter as tk
from tkinter import ttk

from score_recognizer.contract import ContractError
from .document import score_sound_key


def open_exclusion_dialog(app):
    doc = app.require_doc()
    if not app.editor_id:
        raise ContractError("請先點選要刪除的事件或休止")
    selected = doc.entry(app.editor_id)
    bar, staff = selected["value"]["bar"], selected["value"]["staff_label"]
    key = score_sound_key(selected["value"])
    window = tk.Toplevel(app.root)
    window.transient(app.root)
    window.title("刪除一列／整份譜的樂器資料")
    panel = ttk.Frame(window, padding=16)
    panel.pack(fill="both", expand=True)
    ttk.Label(panel, text=f"目前選取：小節 {bar}，原譜聲部「{staff}」", wraplength=660).pack(anchor="w", pady=5)
    scope = tk.StringVar(value="bar")
    summary = ttk.Label(panel, text="", wraplength=660)

    def targets():
        if app.document is not doc:
            raise ContractError("已切換專案，請關閉後重新選取刪除範圍")
        return [e for e in doc.entries() if e["status"] != "rejected" and
                ((e["value"]["bar"] == bar and e["value"]["staff_label"] == staff) if scope.get() == "bar"
                 else score_sound_key(e["value"]) == key)]

    def refresh():
        entries = targets()
        notes = sum(e["value"]["kind"] == "note" for e in entries)
        summary.configure(text=f"將排除 {len(entries)} 筆資料，其中 {notes} 筆敲擊，涵蓋 {len({e['value']['bar'] for e in entries})} 個小節。\n排除後不參與試聽或訓練匯出，原始紀錄保留，可復原。")
        return entries

    ttk.Radiobutton(panel, text=f"只刪小節 {bar} 的整個「{staff}」聲部", variable=scope, value="bar", command=refresh).pack(anchor="w", pady=5)
    ttk.Radiobutton(panel, text=f"整份譜中，名稱為「{key}」的資料", variable=scope, value="score", command=refresh).pack(anchor="w", pady=5)
    summary.pack(anchor="w", pady=12)

    def apply():
        entries = refresh()
        if not entries:
            raise ContractError("此範圍沒有尚未排除的資料")
        app.reject_selected([e["id"] for e in entries], reason=f"人工刪除範圍：{scope.get()}／{staff}")
        window.destroy()

    buttons = ttk.Frame(panel)
    buttons.pack(fill="x")
    ttk.Button(buttons, text="刪除上述資料", command=lambda: app.action(apply)).pack(side="left", padx=(0, 12))
    ttk.Button(buttons, text="取消", command=window.destroy).pack(side="left")
    refresh()
    window.form = {"scope": scope, "targets": targets, "apply": apply, "refresh": refresh}
    return window
