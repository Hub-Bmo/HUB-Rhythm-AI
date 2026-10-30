"""Tk editor for paper-score repeat and volta markers."""

from __future__ import annotations

from copy import deepcopy
import tkinter as tk
from tkinter import messagebox, ttk

from .navigation import validate_navigation


class NavigationDialog(tk.Toplevel):
    """Edit navigation markers without exposing raw project JSON."""

    def __init__(self, parent: tk.Misc, bar_count: int, markers: list[dict]) -> None:
        super().__init__(parent)
        self.title("反覆記號")
        self.transient(parent)
        self.grab_set()
        self.resizable(False, False)
        self.bar_count = max(1, int(bar_count))
        self.markers = deepcopy(markers)
        self.result: list[dict] | None = None

        self.repeat_start_var = tk.StringVar(value="1")
        self.repeat_end_var = tk.StringVar(value=str(self.bar_count))
        self.repeat_times_var = tk.StringVar(value="2")
        self.volta_repeat_start_var = tk.StringVar(value="1")
        self.first_start_var = tk.StringVar()
        self.first_end_var = tk.StringVar()
        self.second_start_var = tk.StringVar()
        self.second_end_var = tk.StringVar()

        self._build()
        self._refresh()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.wait_visibility()
        self.focus_set()

    def _build(self) -> None:
        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text="事件仍留在原譜小節；以下記號只決定播放與 token 展開順序。",
        ).grid(row=0, column=0, columnspan=8, sticky="w", pady=(0, 10))

        self.tree = ttk.Treeview(
            body,
            columns=("kind", "bars", "detail"),
            show="headings",
            height=7,
        )
        self.tree.heading("kind", text="記號")
        self.tree.heading("bars", text="小節")
        self.tree.heading("detail", text="設定")
        self.tree.column("kind", width=100, anchor="center")
        self.tree.column("bars", width=110, anchor="center")
        self.tree.column("detail", width=230)
        self.tree.grid(row=1, column=0, columnspan=8, sticky="ew")

        ttk.Label(body, text="一般反覆", font=("TkDefaultFont", 10, "bold")).grid(
            row=2, column=0, columnspan=8, sticky="w", pady=(14, 5)
        )
        ttk.Label(body, text="起點").grid(row=3, column=0, sticky="e")
        ttk.Entry(body, textvariable=self.repeat_start_var, width=6).grid(row=3, column=1, padx=(3, 12))
        ttk.Label(body, text="終點").grid(row=3, column=2, sticky="e")
        ttk.Entry(body, textvariable=self.repeat_end_var, width=6).grid(row=3, column=3, padx=(3, 12))
        ttk.Label(body, text="總次數").grid(row=3, column=4, sticky="e")
        ttk.Entry(body, textvariable=self.repeat_times_var, width=6).grid(row=3, column=5, padx=(3, 12))
        ttk.Button(body, text="加入反覆", command=self._add_repeat).grid(row=3, column=6, columnspan=2, sticky="ew")

        ttk.Label(body, text="第一／第二結尾", font=("TkDefaultFont", 10, "bold")).grid(
            row=4, column=0, columnspan=8, sticky="w", pady=(14, 5)
        )
        ttk.Label(body, text="反覆起點").grid(row=5, column=0, sticky="e")
        ttk.Entry(body, textvariable=self.volta_repeat_start_var, width=6).grid(row=5, column=1, padx=(3, 12))
        ttk.Label(body, text="第一結尾").grid(row=5, column=2, sticky="e")
        ttk.Entry(body, textvariable=self.first_start_var, width=5).grid(row=5, column=3, padx=(3, 2))
        ttk.Label(body, text="～").grid(row=5, column=4)
        ttk.Entry(body, textvariable=self.first_end_var, width=5).grid(row=5, column=5, padx=(2, 10))
        ttk.Label(body, text="第二結尾").grid(row=6, column=2, sticky="e", pady=(4, 0))
        ttk.Entry(body, textvariable=self.second_start_var, width=5).grid(row=6, column=3, padx=(3, 2), pady=(4, 0))
        ttk.Label(body, text="～").grid(row=6, column=4, pady=(4, 0))
        ttk.Entry(body, textvariable=self.second_end_var, width=5).grid(row=6, column=5, padx=(2, 10), pady=(4, 0))
        ttk.Button(body, text="加入兩個結尾", command=self._add_voltas).grid(
            row=5, column=6, columnspan=2, rowspan=2, sticky="nsew"
        )
        ttk.Label(
            body,
            text="先加入一般反覆；若有不同結尾，一般反覆的終點通常是第一結尾末小節。",
            foreground="#6F736F",
        ).grid(row=7, column=0, columnspan=8, sticky="w", pady=(6, 0))

        actions = ttk.Frame(body)
        actions.grid(row=8, column=0, columnspan=8, sticky="ew", pady=(14, 0))
        ttk.Button(actions, text="移除選取", command=self._remove_selected).pack(side="left")
        ttk.Button(actions, text="清除全部", command=self._clear).pack(side="left", padx=6)
        ttk.Button(actions, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(actions, text="套用", command=self._apply).pack(side="right", padx=6)

    def _number(self, variable: tk.StringVar, label: str, *, minimum: int = 1) -> int:
        try:
            value = int(variable.get().strip())
        except ValueError:
            raise ValueError(f"{label} 必須是整數") from None
        if not minimum <= value <= self.bar_count and label != "總次數":
            raise ValueError(f"{label} 必須介於 1 與 {self.bar_count}")
        if label == "總次數" and value < 2:
            raise ValueError("總次數至少是 2")
        return value

    def _add_repeat(self) -> None:
        try:
            start = self._number(self.repeat_start_var, "反覆起點")
            end = self._number(self.repeat_end_var, "反覆終點")
            times = self._number(self.repeat_times_var, "總次數", minimum=2)
            if end < start:
                raise ValueError("反覆終點不可早於起點")
        except ValueError as error:
            messagebox.showerror("無法加入反覆", str(error), parent=self)
            return
        self.markers.extend(
            (
                {"type": "repeat_start", "bar": start},
                {"type": "repeat_end", "bar": end, "times": times},
            )
        )
        self._refresh()

    def _add_voltas(self) -> None:
        try:
            repeat_start = self._number(self.volta_repeat_start_var, "反覆起點")
            first_start = self._number(self.first_start_var, "第一結尾起點")
            first_end = self._number(self.first_end_var, "第一結尾終點")
            second_start = self._number(self.second_start_var, "第二結尾起點")
            second_end = self._number(self.second_end_var, "第二結尾終點")
            if first_end < first_start or second_end < second_start:
                raise ValueError("結尾的末小節不可早於起始小節")
        except ValueError as error:
            messagebox.showerror("無法加入結尾", str(error), parent=self)
            return
        self.markers.extend(
            (
                {
                    "type": "volta",
                    "start_bar": first_start,
                    "end_bar": first_end,
                    "passes": [1],
                    "repeat_start_bar": repeat_start,
                },
                {
                    "type": "volta",
                    "start_bar": second_start,
                    "end_bar": second_end,
                    "passes": [2],
                    "repeat_start_bar": repeat_start,
                },
            )
        )
        self._refresh()

    def _description(self, marker: dict) -> tuple[str, str, str]:
        kind = marker.get("type")
        if kind == "repeat_start":
            return "反覆起點", str(marker.get("bar", "")), ""
        if kind == "repeat_end":
            return "反覆終點", str(marker.get("bar", "")), f"共 {marker.get('times', 2)} 次"
        if kind == "volta":
            start = marker.get("start_bar", "")
            end = marker.get("end_bar", "")
            passes = "、".join(str(item) for item in marker.get("passes", []))
            return "不同結尾", f"{start}～{end}", f"第 {passes} 次"
        return str(kind or "未知"), "", ""

    def _refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for index, marker in enumerate(self.markers):
            self.tree.insert("", "end", iid=str(index), values=self._description(marker))

    def _remove_selected(self) -> None:
        selected = sorted((int(item) for item in self.tree.selection()), reverse=True)
        for index in selected:
            if 0 <= index < len(self.markers):
                self.markers.pop(index)
        self._refresh()

    def _clear(self) -> None:
        self.markers.clear()
        self._refresh()

    def _apply(self) -> None:
        issues = validate_navigation(self.bar_count, self.markers)
        if issues:
            messagebox.showerror(
                "反覆記號尚未完整",
                "\n".join(f"• {issue.message}" for issue in issues),
                parent=self,
            )
            return
        self.result = deepcopy(self.markers)
        self.destroy()


__all__ = ["NavigationDialog"]
