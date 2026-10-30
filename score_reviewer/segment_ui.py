"""以小節範圍建立單一跳轉段落的本機編輯視窗。"""
import tkinter as tk
from tkinter import ttk

from .segments import parse_sequence, expand_ranges, format_sequence
from score_recognizer.contract import ContractError


def open_sequence_editor(app, segment=None):
    doc = app.require_doc()
    app.commit_editor()
    window = tk.Toplevel(app.root)
    window.transient(app.root)
    window.title("修改跳轉段落" if segment else "新增跳轉段落")
    window.geometry("900x520")
    window.minsize(730, 470)
    panel = ttk.Frame(window, padding=14)
    panel.pack(fill="both", expand=True)
    name = tk.StringVar(value=segment["name"] if segment else "")
    ttk.Label(panel, text="段落名稱").pack(anchor="w")
    ttk.Entry(panel, textvariable=name).pack(fill="x", pady=(3, 10))
    ttk.Label(panel, text="依演奏順序填範圍，用 → 或換行分隔。每組奏一次，第一／第二結尾請直接寫成實際順序。", wraplength=800).pack(anchor="w")
    ttk.Label(panel, text="例如：153-154 → 1-8 → 1-7 → 9-17", foreground="#52647a").pack(anchor="w", pady=(4, 6))
    source = tk.Text(panel, height=3, wrap="word", undo=True)
    source.pack(fill="x")
    if segment:
        source.insert("1.0", format_sequence(segment["ranges"]))
    source.edit_modified(False)
    acknowledged = tk.BooleanVar(value=bool(segment and segment.get("sequence_acknowledged")))

    def changed(event=None):
        if source.edit_modified():
            acknowledged.set(False)
            source.edit_modified(False)

    source.bind("<<Modified>>", changed)
    preview = tk.Text(panel, height=5, wrap="word", state="disabled", padx=8, pady=8)
    preview.pack(fill="both", expand=True, pady=10)

    def values():
        if app.document is not doc:
            raise ContractError("已切換校對專案，請關閉這個視窗，從目前專案重新開啟跳轉段落")
        ranges = parse_sequence(source.get("1.0", "end"))
        order = [item["source_bar"] for item in expand_ranges(ranges, doc.data["bars"])]
        return ranges, order

    def show_preview():
        ranges, order = values()
        preview.configure(state="normal")
        preview.delete("1.0", "end")
        preview.insert("1.0", f"{format_sequence(ranges)}\n共 {len(order)} 個演奏小節，使用 {len(set(order))} 個紙面小節。\n\n" + " → ".join(map(str, order)))
        preview.configure(state="disabled")
        return order

    def audition():
        order = show_preview()
        app.start_render(min(order), max(order), source_order=order)

    def save():
        ranges, order = values()
        stored = doc.set_sequence_segment(name.get(), ranges, acknowledged=acknowledged.get(),
                                          segment_id=segment["segment_id"] if segment else None)
        app.refresh_segments()
        app.segments.selection_set(stored["segment_id"])
        app.select_segment()
        app.status.set(f"已保存段落「{stored['name']}」，共 {len(order)} 個演奏小節。" + ("" if acknowledged.get() else "演奏順序仍待核對，可先試聽。"))
        window.destroy()

    ttk.Checkbutton(panel, text="已核對這個演奏順序，包括反覆與第一／第二結尾", variable=acknowledged).pack(anchor="w", pady=6)
    controls = ttk.Frame(panel)
    controls.pack(fill="x")
    for text, command in (("預覽完整順序", show_preview), ("試聽這個順序", audition), ("保存為一個段落", save)):
        ttk.Button(controls, text=text, command=lambda fn=command: app.action(fn)).pack(side="left", padx=(0, 8))
    ttk.Button(controls, text="停止", command=app.stop).pack(side="left")
    ttk.Button(controls, text="取消", command=window.destroy).pack(side="right")
    # 僅讓本機元件測試存取表單，不操作外部視窗。
    window.form = {"name": name, "source": source, "acknowledged": acknowledged,
                   "preview": show_preview, "audition": audition, "save": save}
    if segment:
        show_preview()
    return window
