"""Small cross-project instrument/stroke library editor for Tkinter."""

from __future__ import annotations

from copy import deepcopy
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from .instrument_library import (
    InstrumentLibraryError,
    InstrumentLibraryStore,
    add_instrument,
    add_stroke,
    get_instrument,
    update_instrument,
    update_stroke,
)


SOUND_PROFILES = ("drum", "large_gong", "small_gong", "cymbal", "bell", "timpani")


def _aliases_from_text(value: str | None) -> list[str]:
    return [item.strip() for item in str(value or "").replace("，", ",").split(",") if item.strip()]


class InstrumentLibraryDialog(tk.Toplevel):
    def __init__(self, master: tk.Misc, library: dict, store: InstrumentLibraryStore) -> None:
        super().__init__(master)
        self.title("跨專案樂器／擊法庫")
        self.geometry("850x600")
        self.minsize(720, 500)
        self.transient(master)
        self.library = deepcopy(library)
        self.store = store
        self.instrument_ids: list[str] = []
        self.stroke_ids: list[str] = []
        self.result_library: dict | None = None
        self.apply_instrument_id: str | None = None
        self.dirty = False
        self.info_var = tk.StringVar(value="名稱與別名可修改；穩定 ID 不會改變。")
        self._build()
        self._refresh_instruments()
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.grab_set()

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)
        ttk.Label(
            outer,
            text="AI 只能對應這裡已有的樂器與擊法；遇到其他名稱時會保留為待確認。",
        ).pack(anchor="w", pady=(0, 10))
        panes = ttk.Panedwindow(outer, orient="horizontal")
        panes.pack(fill="both", expand=True)

        instruments = ttk.Frame(panes, padding=8)
        strokes = ttk.Frame(panes, padding=8)
        panes.add(instruments, weight=1)
        panes.add(strokes, weight=1)

        ttk.Label(instruments, text="樂器", font=("", 11, "bold")).pack(anchor="w")
        self.instrument_list = tk.Listbox(instruments, activestyle="none", exportselection=False)
        self.instrument_list.pack(fill="both", expand=True, pady=6)
        self.instrument_list.bind("<<ListboxSelect>>", self._instrument_selected)
        row = ttk.Frame(instruments)
        row.pack(fill="x")
        ttk.Button(row, text="新增", command=self._add_instrument).pack(side="left", expand=True, fill="x")
        ttk.Button(row, text="編輯", command=self._edit_instrument).pack(side="left", expand=True, fill="x", padx=(5, 0))

        ttk.Label(strokes, text="目前樂器的擊法", font=("", 11, "bold")).pack(anchor="w")
        self.stroke_list = tk.Listbox(strokes, activestyle="none", exportselection=False)
        self.stroke_list.pack(fill="both", expand=True, pady=6)
        row = ttk.Frame(strokes)
        row.pack(fill="x")
        ttk.Button(row, text="新增", command=self._add_stroke).pack(side="left", expand=True, fill="x")
        ttk.Button(row, text="編輯", command=self._edit_stroke).pack(side="left", expand=True, fill="x", padx=(5, 0))

        ttk.Label(outer, textvariable=self.info_var, foreground="#6F736F").pack(anchor="w", pady=(10, 4))
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="取消", command=self._cancel).pack(side="right")
        ttk.Button(buttons, text="儲存", command=self._save).pack(side="right", padx=6)
        ttk.Button(
            buttons,
            text="儲存並套用到目前軌道",
            command=self._save_and_apply,
        ).pack(side="right")

    def _selected_instrument_id(self) -> str | None:
        selected = self.instrument_list.curselection()
        return self.instrument_ids[int(selected[0])] if selected else None

    def _selected_stroke_id(self) -> str | None:
        selected = self.stroke_list.curselection()
        return self.stroke_ids[int(selected[0])] if selected else None

    def _refresh_instruments(self, selected_id: str | None = None) -> None:
        self.instrument_list.delete(0, "end")
        self.instrument_ids = []
        for instrument in self.library.get("instruments", []):
            self.instrument_ids.append(instrument["instrument_id"])
            aliases = instrument.get("aliases", [])
            suffix = f"（別名 {len(aliases)}）" if aliases else ""
            self.instrument_list.insert("end", f"{instrument['name']} {suffix}")
        if self.instrument_ids:
            index = self.instrument_ids.index(selected_id) if selected_id in self.instrument_ids else 0
            self.instrument_list.selection_set(index)
            self.instrument_list.activate(index)
        self._refresh_strokes()

    def _instrument_selected(self, _event=None) -> None:
        self._refresh_strokes()

    def _refresh_strokes(self, selected_id: str | None = None) -> None:
        self.stroke_list.delete(0, "end")
        self.stroke_ids = []
        instrument_id = self._selected_instrument_id()
        instrument = get_instrument(self.library, instrument_id) if instrument_id else None
        if instrument is None:
            return
        self.info_var.set(
            f"{instrument['name']}｜{instrument['sound_profile']}｜ID {instrument['instrument_id']}"
        )
        for stroke in instrument.get("strokes", []):
            self.stroke_ids.append(stroke["stroke_id"])
            self.stroke_list.insert("end", f"{stroke['name']}  [{stroke['code']}]")
        if self.stroke_ids:
            index = self.stroke_ids.index(selected_id) if selected_id in self.stroke_ids else 0
            self.stroke_list.selection_set(index)
            self.stroke_list.activate(index)

    def _add_instrument(self) -> None:
        name = simpledialog.askstring("新增樂器", "顯示名稱：", parent=self)
        if not name:
            return
        aliases = simpledialog.askstring("新增樂器", "別名（以逗號分隔，可留空）：", parent=self)
        profile = simpledialog.askstring(
            "新增樂器",
            "概念音色：\n" + "、".join(SOUND_PROFILES),
            initialvalue="bell",
            parent=self,
        )
        try:
            instrument = add_instrument(
                self.library,
                name,
                aliases=_aliases_from_text(aliases),
                sound_profile=profile or "bell",
            )
        except InstrumentLibraryError as error:
            messagebox.showerror("無法新增", str(error), parent=self)
            return
        self.dirty = True
        self._refresh_instruments(instrument["instrument_id"])

    def _edit_instrument(self) -> None:
        instrument_id = self._selected_instrument_id()
        instrument = get_instrument(self.library, instrument_id) if instrument_id else None
        if instrument is None:
            return
        name = simpledialog.askstring(
            "編輯樂器", "顯示名稱：", initialvalue=instrument["name"], parent=self
        )
        if not name:
            return
        aliases = simpledialog.askstring(
            "編輯樂器",
            "別名（以逗號分隔）：",
            initialvalue=", ".join(instrument.get("aliases", [])),
            parent=self,
        )
        profile = simpledialog.askstring(
            "編輯樂器",
            "概念音色：",
            initialvalue=instrument.get("sound_profile", "bell"),
            parent=self,
        )
        try:
            update_instrument(
                self.library,
                instrument_id,
                name=name,
                aliases=_aliases_from_text(aliases),
                sound_profile=profile or "bell",
            )
        except InstrumentLibraryError as error:
            messagebox.showerror("無法更新", str(error), parent=self)
            return
        self.dirty = True
        self._refresh_instruments(instrument_id)

    def _add_stroke(self) -> None:
        instrument_id = self._selected_instrument_id()
        if not instrument_id:
            return
        code = simpledialog.askstring("新增擊法", "穩定代碼（例如 open）：", parent=self)
        if not code:
            return
        name = simpledialog.askstring("新增擊法", "顯示名稱：", parent=self)
        if not name:
            return
        aliases = simpledialog.askstring("新增擊法", "別名（以逗號分隔）：", parent=self)
        try:
            stroke = add_stroke(
                self.library,
                instrument_id,
                code,
                name,
                aliases=_aliases_from_text(aliases),
            )
        except InstrumentLibraryError as error:
            messagebox.showerror("無法新增", str(error), parent=self)
            return
        self.dirty = True
        self._refresh_strokes(stroke["stroke_id"])

    def _edit_stroke(self) -> None:
        instrument_id = self._selected_instrument_id()
        stroke_id = self._selected_stroke_id()
        instrument = get_instrument(self.library, instrument_id) if instrument_id else None
        if instrument is None or not stroke_id:
            return
        stroke = next(item for item in instrument.get("strokes", []) if item["stroke_id"] == stroke_id)
        code = simpledialog.askstring(
            "編輯擊法", "穩定代碼：", initialvalue=stroke["code"], parent=self
        )
        if not code:
            return
        name = simpledialog.askstring(
            "編輯擊法", "顯示名稱：", initialvalue=stroke["name"], parent=self
        )
        if not name:
            return
        aliases = simpledialog.askstring(
            "編輯擊法",
            "別名（以逗號分隔）：",
            initialvalue=", ".join(stroke.get("aliases", [])),
            parent=self,
        )
        try:
            update_stroke(
                self.library,
                instrument_id,
                stroke_id,
                code=code,
                name=name,
                aliases=_aliases_from_text(aliases),
            )
        except InstrumentLibraryError as error:
            messagebox.showerror("無法更新", str(error), parent=self)
            return
        self.dirty = True
        self._refresh_strokes(stroke_id)

    def _persist(self) -> bool:
        try:
            self.store.save(self.library)
        except (OSError, InstrumentLibraryError) as error:
            messagebox.showerror("樂器庫儲存失敗", str(error), parent=self)
            return False
        self.result_library = self.library
        self.dirty = False
        return True

    def _save(self) -> None:
        if self._persist():
            self.destroy()

    def _save_and_apply(self) -> None:
        instrument_id = self._selected_instrument_id()
        if instrument_id is None:
            messagebox.showinfo("尚未選擇", "請先選擇一件樂器。", parent=self)
            return
        if self._persist():
            self.apply_instrument_id = instrument_id
            self.destroy()

    def _cancel(self) -> None:
        if self.dirty and not messagebox.askyesno(
            "捨棄變更", "樂器庫尚未儲存，要捨棄變更嗎？", parent=self
        ):
            return
        self.destroy()


__all__ = ["InstrumentLibraryDialog"]
