"""Tkinter manager for reusable recognition-symbol mapping rules.

The dialog edits a private draft.  Toggling or deleting a row never writes to
disk on its own; the user must press ``儲存變更``.  This keeps global,
cross-project recognition behaviour behind an explicit human action.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Mapping

from .instrument_library import (
    InstrumentLibraryError,
    get_instrument,
    get_stroke,
)
from .symbol_mappings import (
    SymbolMappingError,
    SymbolMappingStore,
    remove_mapping_rule,
    update_mapping_rule,
    validate_symbol_mappings,
)


PROFILE_LABELS = {
    "staff": "五線譜",
    "numbered": "數字譜／簡譜",
    "gongjing": "鑼鼓經",
}


def create_mapping_rule_draft(
    mappings: Mapping[str, object],
    library: Mapping[str, object],
) -> dict:
    """Validate and copy mappings for explicit-save editing.

    Keeping the draft separate is important because the caller may already be
    using ``mappings`` to make suggestions elsewhere in the application.
    """

    validate_symbol_mappings(mappings, library)
    return deepcopy(dict(mappings))


def _display_symbol(value: object) -> str:
    """Keep meaningful punctuation while making control whitespace visible."""

    return (
        str(value or "")
        .replace("\r", "␍")
        .replace("\n", "⏎")
        .replace("\t", "⇥")
    )


def mapping_rule_rows(
    mappings: Mapping[str, object],
    library: Mapping[str, object],
) -> list[dict]:
    """Build stable, presentation-ready rows for the manager Treeview."""

    validate_symbol_mappings(mappings, library)
    rows: list[dict] = []
    for rule in mappings.get("rules", []):  # type: ignore[union-attr]
        instrument_id = str(rule["instrument_id"])
        stroke_id = str(rule["stroke_id"])
        instrument = get_instrument(library, instrument_id)
        stroke = get_stroke(library, instrument_id, stroke_id)
        # Validation above guarantees both references exist.  Defensive labels
        # still keep this helper readable if a future validator becomes more
        # tolerant of temporarily missing library entries.
        instrument_name = (
            str(instrument.get("name") or instrument_id)
            if instrument is not None
            else f"遺失的樂器（{instrument_id}）"
        )
        if stroke is None:
            stroke_name = f"遺失的擊法（{stroke_id}）"
        else:
            stroke_name = str(stroke.get("name") or stroke_id)
            stroke_code = str(stroke.get("code") or "").strip()
            if stroke_code:
                stroke_name = f"{stroke_name} [{stroke_code}]"
        enabled = rule.get("enabled", True) is True
        profile = str(rule["notation_profile"])
        rows.append(
            {
                "rule_id": str(rule["rule_id"]),
                "enabled": enabled,
                "enabled_text": "啟用" if enabled else "停用",
                "notation_profile": profile,
                "profile_text": PROFILE_LABELS.get(profile, profile),
                "instrument_id": instrument_id,
                "instrument_text": instrument_name,
                "observed_symbol": str(rule["observed_symbol"]),
                "symbol_text": _display_symbol(rule["observed_symbol"]),
                "stroke_id": stroke_id,
                "stroke_text": stroke_name,
            }
        )
    return rows


def toggle_mapping_rule_enabled(
    mappings: dict,
    library: Mapping[str, object],
    rule_id: str,
) -> dict:
    """Toggle a rule in an in-memory draft and return the updated rule."""

    match = next(
        (
            rule
            for rule in mappings.get("rules", [])
            if isinstance(rule, dict) and rule.get("rule_id") == rule_id
        ),
        None,
    )
    if match is None:
        raise SymbolMappingError(f"找不到符號對應規則：{rule_id}")
    return update_mapping_rule(
        mappings,
        library,
        rule_id,
        enabled=not (match.get("enabled", True) is True),
    )


def delete_mapping_rule_from_draft(
    mappings: dict,
    library: Mapping[str, object],
    rule_id: str,
) -> bool:
    """Delete one rule from an in-memory draft."""

    return remove_mapping_rule(mappings, library, rule_id)


def mapping_rule_summary(row: Mapping[str, object]) -> str:
    """Return concise Traditional Chinese text for delete confirmation."""

    return (
        f"{row.get('profile_text', '')}｜{row.get('instrument_text', '')}｜"
        f"「{row.get('symbol_text', '')}」→ {row.get('stroke_text', '')}"
    )


class SymbolMappingManagerDialog(tk.Toplevel):
    """Review, enable/disable, and delete reusable symbol mappings."""

    def __init__(
        self,
        master: tk.Misc,
        mappings: Mapping[str, object],
        library: Mapping[str, object],
        store: SymbolMappingStore,
    ) -> None:
        super().__init__(master)
        self.title("辨識符號對應規則")
        self.geometry("900x560")
        self.minsize(720, 430)
        self.transient(master)

        self.library = library
        self.store = store
        self.mappings = create_mapping_rule_draft(mappings, library)
        self.result_mappings: dict | None = None
        self.saved = False
        self.dirty = False
        self._rows_by_id: dict[str, dict] = {}

        self.status_var = tk.StringVar(
            value="停用的規則不會提供辨識建議；所有修改都要按「儲存變更」才會寫入。"
        )
        self.selection_var = tk.StringVar(value="請選擇一條規則。")

        self._build()
        self._refresh_tree()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.grab_set()

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text="跨專案辨識符號對應",
            font=("", 14, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            outer,
            text=(
                "這些規則只會在相同譜面種類、相同樂器與相同原始符號時提供建議；"
                "仍須由你在候選清單確認後才會寫入節奏資料。"
            ),
            wraplength=840,
            justify="left",
        ).pack(anchor="w", pady=(4, 12))

        tree_frame = ttk.Frame(outer)
        tree_frame.pack(fill="both", expand=True)
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        columns = ("enabled", "profile", "instrument", "symbol", "stroke")
        self.tree = ttk.Treeview(
            tree_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        headings = {
            "enabled": "狀態",
            "profile": "譜面 Profile",
            "instrument": "樂器",
            "symbol": "原始符號",
            "stroke": "對應擊法",
        }
        widths = {
            "enabled": 72,
            "profile": 125,
            "instrument": 150,
            "symbol": 180,
            "stroke": 190,
        }
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(
                column,
                width=widths[column],
                minwidth=60,
                stretch=column != "enabled",
                anchor="center" if column in {"enabled", "profile"} else "w",
            )
        vertical = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        horizontal = ttk.Scrollbar(
            tree_frame, orient="horizontal", command=self.tree.xview
        )
        self.tree.configure(
            yscrollcommand=vertical.set,
            xscrollcommand=horizontal.set,
        )
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.tag_configure("disabled", foreground="#858585")
        self.tree.bind("<<TreeviewSelect>>", self._selection_changed)
        self.tree.bind("<Double-1>", self._toggle_selected)

        ttk.Label(
            outer,
            textvariable=self.selection_var,
            foreground="#6F736F",
            wraplength=840,
            justify="left",
        ).pack(anchor="w", pady=(9, 3))
        ttk.Label(
            outer,
            textvariable=self.status_var,
            foreground="#6F736F",
            wraplength=840,
            justify="left",
        ).pack(anchor="w", pady=(0, 8))

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x")
        self.toggle_button = ttk.Button(
            buttons,
            text="啟用／停用",
            command=self._toggle_selected,
            state="disabled",
        )
        self.toggle_button.pack(side="left")
        self.delete_button = ttk.Button(
            buttons,
            text="刪除規則…",
            command=self._delete_selected,
            state="disabled",
        )
        self.delete_button.pack(side="left", padx=(7, 0))

        ttk.Button(buttons, text="關閉", command=self._close).pack(side="right")
        self.save_button = ttk.Button(
            buttons,
            text="儲存變更",
            command=self._save,
            state="disabled",
        )
        self.save_button.pack(side="right", padx=(0, 7))

    def _selected_rule_id(self) -> str | None:
        selected = self.tree.selection()
        return str(selected[0]) if selected else None

    def _refresh_tree(self, selected_id: str | None = None) -> None:
        for item_id in self.tree.get_children(""):
            self.tree.delete(item_id)
        self._rows_by_id = {
            row["rule_id"]: row for row in mapping_rule_rows(self.mappings, self.library)
        }
        for row in self._rows_by_id.values():
            tags = () if row["enabled"] else ("disabled",)
            self.tree.insert(
                "",
                "end",
                iid=row["rule_id"],
                values=(
                    row["enabled_text"],
                    row["profile_text"],
                    row["instrument_text"],
                    row["symbol_text"],
                    row["stroke_text"],
                ),
                tags=tags,
            )
        if selected_id in self._rows_by_id:
            self.tree.selection_set(selected_id)
            self.tree.focus(selected_id)
            self.tree.see(selected_id)
        elif self._rows_by_id:
            first_id = next(iter(self._rows_by_id))
            self.tree.selection_set(first_id)
            self.tree.focus(first_id)
        self._selection_changed()
        if not self._rows_by_id:
            self.status_var.set(
                "草稿中已沒有規則；尚未儲存。"
                if self.dirty
                else "目前沒有已保存的對應規則。可在候選更正時選擇保存新規則。"
            )

    def _selection_changed(self, _event=None) -> None:
        rule_id = self._selected_rule_id()
        row = self._rows_by_id.get(rule_id or "")
        state = "normal" if row is not None else "disabled"
        self.toggle_button.configure(state=state)
        self.delete_button.configure(state=state)
        if row is None:
            self.selection_var.set("請選擇一條規則。")
            return
        self.selection_var.set(
            f"{mapping_rule_summary(row)}｜規則 ID {row['rule_id']}"
        )

    def _mark_dirty(self, message: str) -> None:
        self.dirty = True
        self.save_button.configure(state="normal")
        self.status_var.set(f"{message}；尚未儲存。")

    def _toggle_selected(self, _event=None) -> None:
        rule_id = self._selected_rule_id()
        if rule_id is None:
            return
        try:
            updated = toggle_mapping_rule_enabled(
                self.mappings, self.library, rule_id
            )
        except (SymbolMappingError, InstrumentLibraryError) as error:
            messagebox.showerror(
                "無法變更規則",
                f"這條規則目前無法更新：\n{error}",
                parent=self,
            )
            return
        state_text = "啟用" if updated.get("enabled", True) else "停用"
        self._mark_dirty(f"已在草稿中{state_text}所選規則")
        self._refresh_tree(rule_id)

    def _delete_selected(self) -> None:
        rule_id = self._selected_rule_id()
        row = self._rows_by_id.get(rule_id or "")
        if rule_id is None or row is None:
            return
        if not messagebox.askyesno(
            "刪除對應規則",
            (
                f"確定要刪除這條規則嗎？\n\n{mapping_rule_summary(row)}\n\n"
                "刪除後仍須按「儲存變更」才會寫入。"
            ),
            parent=self,
        ):
            return
        try:
            removed = delete_mapping_rule_from_draft(
                self.mappings, self.library, rule_id
            )
        except (SymbolMappingError, InstrumentLibraryError) as error:
            messagebox.showerror(
                "無法刪除規則",
                f"這條規則目前無法刪除：\n{error}",
                parent=self,
            )
            return
        if not removed:
            messagebox.showinfo(
                "規則已不存在",
                "找不到所選規則，清單將重新整理。",
                parent=self,
            )
            self._refresh_tree()
            return
        self._mark_dirty("已從草稿移除所選規則")
        self._refresh_tree()

    def _save(self) -> None:
        if not self.dirty:
            self.status_var.set("目前沒有需要儲存的變更。")
            return
        try:
            saved_path = self.store.save(self.mappings, self.library)
        except (OSError, SymbolMappingError, InstrumentLibraryError) as error:
            messagebox.showerror(
                "對應規則儲存失敗",
                f"變更尚未寫入，請檢查設定檔位置或內容後再試一次：\n{error}",
                parent=self,
            )
            return
        self.saved = True
        self.dirty = False
        self.result_mappings = deepcopy(self.mappings)
        self.save_button.configure(state="disabled")
        self.status_var.set(f"已儲存變更：{Path(saved_path)}")

    def _close(self) -> None:
        if self.dirty and not messagebox.askyesno(
            "捨棄尚未儲存的變更",
            "還有尚未儲存的修改，確定要捨棄並關閉嗎？",
            parent=self,
        ):
            return
        self.destroy()


__all__ = [
    "PROFILE_LABELS",
    "SymbolMappingManagerDialog",
    "create_mapping_rule_draft",
    "delete_mapping_rule_from_draft",
    "mapping_rule_rows",
    "mapping_rule_summary",
    "toggle_mapping_rule_enabled",
]
