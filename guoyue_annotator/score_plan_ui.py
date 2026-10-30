"""Tk workflow for planning an entire photographed score before AI use."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import math
from pathlib import Path
import re
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from PIL import Image, ImageOps, ImageTk

from .manual_layout import (
    ManualLayoutError,
    adjust_measure_boundary,
    adjust_system_bbox,
    apply_manual_measure_counts,
    restore_automatic_layout,
)
from .photo_assets import PhotoAssetError, asset_file_path
from .score_layout import ScoreLayoutError, analyze_score_page
from .score_plan import (
    ScorePlanError,
    ordered_page_assets,
    reorder_page_assets,
    suggest_segment_ranges,
)
from .segment_plan_apply import apply_segment_proposals, flatten_measure_sequence


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _natural_key(value: object) -> tuple:
    parts = re.split(r"(\d+)", str(value or "").casefold())
    return tuple(int(part) if part.isdigit() else part for part in parts)


def _positive_bar_or_none(value: object) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


class ScorePlanDialog(tk.Toplevel):
    """Page-order, local-layout and cross-page Segment planning dialog."""

    def __init__(self, master: tk.Misc, app, project_directory: Path) -> None:
        super().__init__(master)
        self.app = app
        self.project_directory = Path(project_directory)
        self.title("整份樂譜規劃（本機分析，不使用 API）")
        self.geometry("1180x780")
        self.minsize(960, 650)
        self.transient(master.winfo_toplevel())
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        self.assets = ordered_page_assets(
            asset
            for asset in self.app.project.get("assets", [])
            if isinstance(asset, dict)
        )
        saved_plan = self.app.project.get("score_plan")
        self.analyses: dict[str, dict] = {}
        self.proposals: list[dict] = []
        self.excluded_asset_ids: set[str] = set()
        if isinstance(saved_plan, dict):
            analyses = saved_plan.get("analyses_by_asset")
            if isinstance(analyses, dict):
                self.analyses = deepcopy(analyses)
            proposals = saved_plan.get("segment_proposals")
            if isinstance(proposals, list):
                self.proposals = [
                    deepcopy(item) for item in proposals if isinstance(item, dict)
                ]
            excluded = saved_plan.get("excluded_asset_ids")
            if isinstance(excluded, list):
                self.excluded_asset_ids = {
                    str(value) for value in excluded if isinstance(value, str) and value
                }

        self.preview_photo: ImageTk.PhotoImage | None = None
        self._preview_transform: dict[str, object] | None = None
        self._boundary_handles: list[dict[str, object]] = []
        self._system_edge_handles: list[dict[str, object]] = []
        self._active_boundary_drag: dict[str, object] | None = None
        self._active_system_drag: dict[str, object] | None = None
        self.layout_edit_mode_var = tk.StringVar(value="measures")
        self.target_bars_var = tk.StringVar(value="4")
        self.status_var = tk.StringVar(
            value="先確認頁面順序；本機分析與段落規劃不會產生 OpenAI 費用。"
        )
        self.planning_issue_var = tk.StringVar()
        self.empty_state_var = tk.StringVar()
        self._build()
        self.bind("<Escape>", self._cancel_boundary_drag)
        self._refresh_pages(select_index=0)
        self._refresh_proposals()
        self.grab_set()

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)
        ttk.Label(
            outer,
            text="整份樂譜：確認頁序 → 本機找小節 → 拆分／合併建議段落",
            font=("Microsoft JhengHei UI", 15, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            outer,
            text=(
                "這個畫面只分析本機照片；建立並確認段落後，才會在照片辨識頁由你決定是否送給 OpenAI。"
            ),
            foreground="#6F736F",
        ).pack(anchor="w", pady=(3, 10))

        body = ttk.Panedwindow(outer, orient="horizontal")
        left = ttk.LabelFrame(body, text="1　頁面順序", padding=8)
        right = ttk.Frame(body)
        body.add(left, weight=1)
        body.add(right, weight=3)

        self.page_tree = ttk.Treeview(
            left,
            columns=("seq", "name", "measures", "confidence"),
            show="headings",
            height=18,
            selectmode="browse",
        )
        self.page_tree.heading("seq", text="順序")
        self.page_tree.heading("name", text="照片")
        self.page_tree.heading("measures", text="小節")
        self.page_tree.heading("confidence", text="信心")
        self.page_tree.column("seq", width=48, anchor="center", stretch=False)
        self.page_tree.column("name", width=190)
        self.page_tree.column("measures", width=82, anchor="center", stretch=False)
        self.page_tree.column("confidence", width=62, anchor="center", stretch=False)
        page_scroll = ttk.Scrollbar(left, orient="vertical", command=self.page_tree.yview)
        self.page_tree.configure(yscrollcommand=page_scroll.set)
        self.page_tree.grid(row=0, column=0, columnspan=3, sticky="nsew")
        page_scroll.grid(row=0, column=3, sticky="ns")
        self.page_tree.bind("<<TreeviewSelect>>", self._page_selected)
        self.page_tree.bind("<Double-1>", self._page_double_clicked)
        self.page_tree.tag_configure("needs_attention", foreground="#B6402F")
        self.page_tree.tag_configure("manual", foreground="#216E55")
        self.page_tree.tag_configure("excluded", foreground="#777777")
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        ttk.Button(left, text="↑ 上移", command=lambda: self._move_page(-1)).grid(
            row=1, column=0, sticky="ew", pady=(7, 3), padx=(0, 3)
        )
        ttk.Button(left, text="↓ 下移", command=lambda: self._move_page(1)).grid(
            row=1, column=1, sticky="ew", pady=(7, 3), padx=3
        )
        ttk.Button(left, text="依檔名排序", command=self._sort_pages).grid(
            row=1, column=2, sticky="ew", pady=(7, 3), padx=(3, 0)
        )
        ttk.Button(
            left,
            text="儲存並確認頁序",
            command=self._save_page_order,
        ).grid(row=2, column=0, columnspan=3, sticky="ew", pady=3)
        ttk.Button(
            left,
            text="在本機分析全部頁面",
            command=self._analyze_all,
        ).grid(row=3, column=0, columnspan=3, sticky="ew", pady=3)
        self.manual_measure_button = ttk.Button(
            left,
            text="輸入選取頁的小節數…",
            command=self._manual_correct_selected_page,
        )
        self.manual_measure_button.grid(
            row=4, column=0, columnspan=2, sticky="ew", pady=3, padx=(0, 3)
        )
        self.exclude_page_button = ttk.Button(
            left,
            text="排除選取頁",
            command=self._toggle_selected_page_excluded,
        )
        self.exclude_page_button.grid(row=4, column=2, sticky="ew", pady=3, padx=(3, 0))
        self.restore_auto_button = ttk.Button(
            left,
            text="恢復選取頁的自動分析",
            command=self._restore_selected_page_automatic,
        )
        self.restore_auto_button.grid(
            row=5, column=0, columnspan=3, sticky="ew", pady=(3, 0)
        )

        preview_group = ttk.LabelFrame(right, text="2　本機版面分析預覽", padding=7)
        preview_group.pack(fill="both", expand=True)
        layout_tools = ttk.Frame(preview_group)
        layout_tools.pack(fill="x", pady=(0, 5))
        ttk.Label(layout_tools, text="調整工具：").pack(side="left")
        ttk.Radiobutton(
            layout_tools,
            text="橘線／小節左右邊界",
            variable=self.layout_edit_mode_var,
            value="measures",
            command=self._layout_mode_changed,
        ).pack(side="left", padx=(2, 10))
        ttk.Radiobutton(
            layout_tools,
            text="藍框／整行譜表範圍",
            variable=self.layout_edit_mode_var,
            value="system",
            command=self._layout_mode_changed,
        ).pack(side="left")
        self.preview_canvas = tk.Canvas(
            preview_group,
            background="#343434",
            height=300,
            highlightthickness=0,
        )
        self.preview_canvas.pack(fill="both", expand=True)
        self.preview_canvas.bind("<Configure>", self._preview_configured)
        self.preview_canvas.bind("<Motion>", self._preview_pointer_moved)
        self.preview_canvas.bind("<Leave>", self._preview_pointer_left)
        self.preview_canvas.bind("<ButtonPress-1>", self._preview_boundary_pressed)
        self.preview_canvas.bind("<B1-Motion>", self._preview_boundary_dragged)
        self.preview_canvas.bind("<ButtonRelease-1>", self._preview_boundary_released)

        segment_group = ttk.LabelFrame(right, text="3　建議段落（可拆分、合併與命名）", padding=7)
        segment_group.pack(fill="x", pady=(8, 0))
        ttk.Label(
            segment_group,
            textvariable=self.planning_issue_var,
            foreground="#B6402F",
            wraplength=840,
            justify="left",
        ).pack(fill="x", pady=(0, 5))
        controls = ttk.Frame(segment_group)
        controls.pack(fill="x", pady=(0, 5))
        ttk.Label(controls, text="預設每段").pack(side="left")
        target_combo = ttk.Combobox(
            controls,
            textvariable=self.target_bars_var,
            values=("4", "8"),
            state="readonly",
            width=4,
        )
        target_combo.pack(side="left", padx=4)
        ttk.Label(controls, text="小節").pack(side="left")
        self.generate_button = ttk.Button(
            controls, text="產生／重建段落建議", command=self._generate_proposals
        )
        self.generate_button.pack(
            side="left", padx=8
        )
        self.split_button = ttk.Button(
            controls, text="拆分選取段落", command=self._split_proposal
        )
        self.split_button.pack(
            side="left", padx=3
        )
        self.merge_button = ttk.Button(
            controls, text="與下一段合併", command=self._merge_proposal
        )
        self.merge_button.pack(
            side="left", padx=3
        )
        self.name_button = ttk.Button(
            controls, text="命名選取段落", command=self._name_proposal
        )
        self.name_button.pack(
            side="left", padx=3
        )

        self.segment_tree = ttk.Treeview(
            segment_group,
            columns=("index", "name", "start", "end", "bars", "pages"),
            show="headings",
            height=5,
            selectmode="browse",
        )
        for column, label, width in (
            ("index", "段", 45),
            ("name", "名稱", 150),
            ("start", "起始小節", 75),
            ("end", "結束小節", 75),
            ("bars", "小節數", 60),
            ("pages", "來源頁", 150),
        ):
            self.segment_tree.heading(column, text=label)
            self.segment_tree.column(
                column,
                width=width,
                anchor="center" if column != "name" else "w",
                stretch=column in {"name", "pages"},
            )
        self.segment_tree.pack(fill="x")
        self.segment_tree.bind(
            "<<TreeviewSelect>>", lambda _event: self._update_planning_state()
        )
        ttk.Label(
            segment_group,
            textvariable=self.empty_state_var,
            foreground="#6F736F",
            justify="left",
        ).pack(fill="x", pady=(4, 0))

        footer = ttk.Frame(outer)
        footer.pack(side="bottom", fill="x", pady=(10, 0))
        ttk.Label(
            footer,
            textvariable=self.status_var,
            foreground="#B64D3A",
            wraplength=780,
            justify="left",
        ).pack(side="left", fill="x", expand=True)
        ttk.Button(footer, text="取消", command=self.destroy).pack(side="right")
        self.apply_button = ttk.Button(
            footer,
            text="建立這些跨頁段落",
            command=self._apply_segments,
            style="Accent.TButton",
        )
        self.apply_button.pack(side="right", padx=7)
        body.pack(fill="both", expand=True)

    @staticmethod
    def _analysis_measure_count(analysis: object) -> int:
        if not isinstance(analysis, dict):
            return 0
        return sum(
            len(system.get("measure_regions", []))
            for system in analysis.get("systems", [])
            if isinstance(system, dict)
        )

    def _included_assets(self) -> list[dict]:
        return [
            asset
            for asset in self.assets
            if str(asset.get("asset_id") or "") not in self.excluded_asset_ids
        ]

    def _missing_measure_assets(self) -> list[tuple[int, dict]]:
        return [
            (index, asset)
            for index, asset in enumerate(self.assets)
            if str(asset.get("asset_id") or "") not in self.excluded_asset_ids
            and self._analysis_measure_count(
                self.analyses.get(str(asset.get("asset_id") or ""))
            ) == 0
        ]

    def _plan_has_live_created_segments(self) -> bool:
        plan = self.app.project.get("score_plan")
        if not isinstance(plan, dict):
            return False
        created_ids = {
            str(value)
            for value in plan.get("created_segment_ids", [])
            if isinstance(value, str) and value
        }
        if not created_ids:
            return False
        live_ids = {
            str(segment.get("segment_id"))
            for segment in self.app.project.get("segments", [])
            if isinstance(segment, dict)
        }
        return bool(created_ids & live_ids)

    def _select_page(self, index: int) -> None:
        if not 0 <= index < len(self.assets):
            return
        item = f"page-{index}"
        self.page_tree.selection_set(item)
        self.page_tree.focus(item)
        self.page_tree.see(item)
        self._draw_preview()
        self._update_planning_state()

    def _update_planning_state(self) -> None:
        missing = self._missing_measure_assets()
        has_live_segments = self._plan_has_live_created_segments()
        selected_page = self._selected_page_index()
        if selected_page is None:
            self.manual_measure_button.configure(state="disabled")
            self.exclude_page_button.configure(state="disabled")
            self.restore_auto_button.configure(state="disabled")
        else:
            asset = self.assets[selected_page]
            asset_id = str(asset.get("asset_id") or "")
            self.manual_measure_button.configure(
                state="disabled" if asset_id in self.excluded_asset_ids else "normal"
            )
            self.exclude_page_button.configure(
                state="normal",
                text=(
                    "恢復選取頁"
                    if asset_id in self.excluded_asset_ids
                    else "排除選取頁"
                ),
            )
            analysis = self.analyses.get(asset_id)
            self.restore_auto_button.configure(
                state=(
                    "normal"
                    if not has_live_segments
                    and asset_id not in self.excluded_asset_ids
                    and isinstance(analysis, dict)
                    and isinstance(analysis.get("automatic_systems"), list)
                    and bool(analysis.get("manual_override"))
                    else "disabled"
                )
            )
            if has_live_segments:
                self.manual_measure_button.configure(state="disabled")
                self.exclude_page_button.configure(state="disabled")

        selected_proposal = self._selected_proposal_index()
        has_proposals = bool(self.proposals)
        complete = bool(self._included_assets()) and not missing
        self.generate_button.configure(
            state=(
                "normal"
                if self._included_assets() and not has_live_segments
                else "disabled"
            )
        )
        self.split_button.configure(
            state=(
                "normal"
                if selected_proposal is not None and not has_live_segments
                else "disabled"
            )
        )
        self.name_button.configure(
            state=(
                "normal"
                if selected_proposal is not None and not has_live_segments
                else "disabled"
            )
        )
        self.merge_button.configure(
            state=(
                "normal"
                if selected_proposal is not None
                and selected_proposal + 1 < len(self.proposals)
                and not has_live_segments
                else "disabled"
            )
        )
        self.apply_button.configure(
            state=(
                "normal"
                if complete and has_proposals and not has_live_segments
                else "disabled"
            )
        )

        if has_live_segments:
            self.planning_issue_var.set(
                "這份規劃已建立正式段落。要重新切分前，請先備份專案並刪除這批段落。"
            )
            self.empty_state_var.set("")
        elif not self._included_assets():
            self.planning_issue_var.set(
                "目前所有照片都被排除；請至少恢復一張譜面照片。"
            )
            self.empty_state_var.set("沒有可建立段落的頁面。")
        elif missing:
            labels = [
                f"第 {index + 1} 頁 {asset.get('original_filename') or asset.get('asset_id')}"
                for index, asset in missing[:5]
            ]
            suffix = f"，另有 {len(missing) - 5} 頁" if len(missing) > 5 else ""
            self.planning_issue_var.set(
                f"尚不能建立段落：有 {len(missing)} 頁沒有小節資料（"
                + "、".join(labels)
                + suffix
                + "）。請選取紅色頁面後按「輸入選取頁的小節數…」，"
                "非譜面照片則按「排除選取頁」。"
            )
            self.empty_state_var.set(
                "處理完所有紅色頁面後，程式才會產生段落建議。"
            )
        elif not has_proposals:
            self.planning_issue_var.set(
                "每頁都已有小節資料。請按「產生／重建段落建議」。"
            )
            self.empty_state_var.set(
                "尚無段落；產生建議後，先點選一列，才能拆分、合併或命名。"
            )
        else:
            self.planning_issue_var.set(
                "段落建議已建立。先點選一列，再使用拆分、合併或命名；確認後建立跨頁段落。"
            )
            self.empty_state_var.set("")

    def _selected_page_index(self) -> int | None:
        selected = self.page_tree.selection()
        if not selected:
            return None
        try:
            return int(str(selected[0]).rsplit("-", 1)[-1])
        except ValueError:
            return None

    def _refresh_pages(self, *, select_index: int | None = None) -> None:
        self.page_tree.delete(*self.page_tree.get_children())
        for index, asset in enumerate(self.assets):
            asset_id = str(asset.get("asset_id") or "")
            analysis = self.analyses.get(asset_id)
            confidence = analysis.get("confidence") if isinstance(analysis, dict) else None
            is_excluded = asset_id in self.excluded_asset_ids
            is_manual = isinstance(analysis, dict) and bool(analysis.get("manual_override"))
            measure_count = self._analysis_measure_count(analysis)
            if is_excluded:
                measure_text = "排除"
                confidence_text = "—"
                tags = ("excluded",)
            elif is_manual:
                measure_text = f"{measure_count}（人工）"
                confidence_text = "人工"
                tags = ("manual",)
            elif measure_count:
                measure_text = measure_count
                confidence_text = (
                    f"{float(confidence) * 100:.0f}%"
                    if isinstance(confidence, (int, float))
                    else "—"
                )
                tags = ()
            else:
                measure_text = "需處理"
                confidence_text = (
                    f"{float(confidence) * 100:.0f}%"
                    if isinstance(confidence, (int, float))
                    else "—"
                )
                tags = ("needs_attention",)
            self.page_tree.insert(
                "",
                "end",
                iid=f"page-{index}",
                values=(
                    index + 1,
                    asset.get("original_filename") or asset_id,
                    measure_text,
                    confidence_text,
                ),
                tags=tags,
            )
        if self.assets:
            index = min(max(0, select_index or 0), len(self.assets) - 1)
            item = f"page-{index}"
            self.page_tree.selection_set(item)
            self.page_tree.focus(item)
            self.page_tree.see(item)
            self._draw_preview()
        self._update_planning_state()

    def _move_page(self, delta: int) -> None:
        index = self._selected_page_index()
        if index is None:
            return
        target = index + delta
        if not 0 <= target < len(self.assets):
            self.status_var.set(
                "這張已經是第一頁。" if delta < 0 else "這張已經是最後一頁。"
            )
            return
        self.assets[index], self.assets[target] = self.assets[target], self.assets[index]
        self._refresh_pages(select_index=target)
        self.status_var.set("頁序尚未儲存；請按「儲存並確認頁序」。")

    def _sort_pages(self) -> None:
        self.assets.sort(key=lambda item: _natural_key(item.get("original_filename")))
        self._refresh_pages(select_index=0)
        self.status_var.set("已依自然檔名排序；請確認後儲存頁序。")

    def _persist_layout_change(self, message: str, *, select_index: int) -> bool:
        self.proposals = []
        plan = self.app.project.setdefault("score_plan", {})
        plan["analyses_by_asset"] = deepcopy(self.analyses)
        plan["excluded_asset_ids"] = [
            str(asset.get("asset_id"))
            for asset in self.assets
            if str(asset.get("asset_id")) in self.excluded_asset_ids
        ]
        plan["measure_sequence"] = []
        plan["segment_proposals"] = []
        plan["status"] = "layout_analyzed"
        plan["updated_at"] = _now_iso()
        self.app._mark_dirty()
        self.app._commit_history()
        if not self.app.save():
            self._refresh_pages(select_index=select_index)
            self._refresh_proposals()
            self.status_var.set("修改仍在記憶體中，但尚未成功儲存。")
            return False
        self._refresh_pages(select_index=select_index)
        self._refresh_proposals()
        self.status_var.set(message)
        return True

    def _persist_boundary_change(self, message: str, *, select_index: int) -> bool:
        """Save changed crop geometry without discarding edited Segment ranges."""

        sequence_error: Exception | None = None
        try:
            measure_sequence = self._measure_sequence()
        except (ScorePlanError, ValueError, TypeError) as error:
            # Boundary correction is also needed while another page is still
            # marked "needs attention".  Preserve this page's geometry now;
            # the complete cross-page sequence can be rebuilt after the other
            # pages receive measure data or are excluded.
            measure_sequence = []
            sequence_error = error
        plan = self.app.project.setdefault("score_plan", {})
        plan["analyses_by_asset"] = deepcopy(self.analyses)
        plan["excluded_asset_ids"] = [
            str(asset.get("asset_id"))
            for asset in self.assets
            if str(asset.get("asset_id")) in self.excluded_asset_ids
        ]
        plan["measure_sequence"] = deepcopy(measure_sequence)
        plan["segment_proposals"] = deepcopy(self.proposals)
        plan["status"] = (
            "segments_proposed"
            if self.proposals and sequence_error is None
            else "layout_analyzed"
        )
        plan["updated_at"] = _now_iso()
        self.app._mark_dirty()
        self.app._commit_history()
        if not self.app.save():
            self._refresh_pages(select_index=select_index)
            self._refresh_proposals()
            self.status_var.set("小節線修改仍在記憶體中，但尚未成功儲存。")
            return False
        self._refresh_pages(select_index=select_index)
        self._refresh_proposals()
        if sequence_error is None:
            self.status_var.set(message)
        else:
            self.status_var.set(
                f"{message} 其他頁仍需處理，完整段落順序會稍後重建。"
            )
        return True

    def _manual_correct_selected_page(self) -> None:
        index = self._selected_page_index()
        if index is None:
            messagebox.showinfo("尚未選擇頁面", "請先點選左側一張照片。", parent=self)
            return
        if self._plan_has_live_created_segments():
            messagebox.showwarning(
                "段落已建立",
                "這份規劃已建立正式 Segment，現在修改小節數會使既有來源範圍失效。\n\n"
                "請先備份專案並刪除該批 Segment，再回來重新規劃。",
                parent=self,
            )
            return
        asset = self.assets[index]
        asset_id = str(asset.get("asset_id") or "")
        if asset_id in self.excluded_asset_ids:
            messagebox.showinfo(
                "本頁已排除", "請先按「恢復選取頁」，再修正小節數。", parent=self
            )
            return
        analysis = self.analyses.get(asset_id)
        if not isinstance(analysis, dict):
            analysis = {
                "schema_version": "1.0",
                "coordinate_space": "normalized_image",
                "confidence": 0.0,
                "content_region": None,
                "systems": [],
                "warnings": [],
            }
        systems = analysis.get("systems")
        system_records = [item for item in systems if isinstance(item, dict)] if isinstance(systems, list) else []
        current_counts = [
            max(1, len(system.get("measure_regions", []))) for system in system_records
        ]
        initial = ",".join(str(value) for value in current_counts) if current_counts else "4"
        answer = simpledialog.askstring(
            "手動修正小節數",
            "請輸入這張照片每一行譜表的小節數。\n\n"
            "只有一行就輸入例如：4\n"
            "若由上到下有兩行、各 4 小節，輸入：4,4\n\n"
            "程式會先平均估計橘色小節框；儲存後請回到預覽核對。",
            initialvalue=initial,
            parent=self,
        )
        if answer is None:
            return
        try:
            parts = [part.strip() for part in answer.replace("，", ",").split(",")]
            if not parts or any(not part for part in parts):
                raise ValueError
            counts = [int(part) for part in parts]
        except ValueError:
            messagebox.showerror(
                "格式不正確",
                "請只輸入正整數；多行譜表用逗號分隔，例如 4,4。",
                parent=self,
            )
            return
        try:
            corrected = apply_manual_measure_counts(analysis, counts)
        except ManualLayoutError as error:
            messagebox.showerror("無法修正小節", str(error), parent=self)
            return
        corrected["asset_id"] = asset_id
        corrected["analyzed_at"] = _now_iso()
        self.analyses[asset_id] = corrected
        if not self._persist_layout_change(
            f"已人工設定 {asset.get('original_filename') or asset_id}：共 {sum(counts)} 小節。",
            select_index=index,
        ):
            return
        if not self._missing_measure_assets():
            self._generate_proposals(quiet=True)

    def _restore_selected_page_automatic(self) -> None:
        index = self._selected_page_index()
        if index is None:
            return
        if self._plan_has_live_created_segments():
            messagebox.showwarning(
                "段落已建立",
                "請先備份並刪除由這份規劃建立的 Segment，才能恢復舊版面資料。",
                parent=self,
            )
            return
        asset = self.assets[index]
        asset_id = str(asset.get("asset_id") or "")
        analysis = self.analyses.get(asset_id)
        if not isinstance(analysis, dict):
            return
        try:
            restored = restore_automatic_layout(analysis)
        except ManualLayoutError as error:
            messagebox.showinfo("沒有自動備份", str(error), parent=self)
            return
        measure_count_unchanged = self._analysis_measure_count(
            restored
        ) == self._analysis_measure_count(analysis)
        restored["asset_id"] = asset_id
        self.analyses[asset_id] = restored
        message = f"已恢復 {asset.get('original_filename') or asset_id} 的自動分析。"
        if measure_count_unchanged:
            had_proposals = bool(self.proposals)
            saved = self._persist_boundary_change(message, select_index=index)
            if (
                saved
                and not had_proposals
                and not self._missing_measure_assets()
                and self._included_assets()
            ):
                self._generate_proposals(quiet=True)
            return
        if self._persist_layout_change(
            message, select_index=index
        ) and not self._missing_measure_assets() and self._included_assets():
            self._generate_proposals(quiet=True)

    def _toggle_selected_page_excluded(self) -> None:
        index = self._selected_page_index()
        if index is None:
            return
        if self._plan_has_live_created_segments():
            messagebox.showwarning(
                "段落已建立",
                "請先備份並刪除由這份規劃建立的 Segment，才能變更納入的頁面。",
                parent=self,
            )
            return
        asset = self.assets[index]
        asset_id = str(asset.get("asset_id") or "")
        if asset_id in self.excluded_asset_ids:
            self.excluded_asset_ids.remove(asset_id)
            action = "恢復納入"
        else:
            self.excluded_asset_ids.add(asset_id)
            action = "排除"
        if self._persist_layout_change(
            f"已{action} {asset.get('original_filename') or asset_id}。",
            select_index=index,
        ) and not self._missing_measure_assets() and self._included_assets():
            self._generate_proposals(quiet=True)

    def _save_page_order(self, *, quiet: bool = False) -> bool:
        if not self.assets:
            if not quiet:
                messagebox.showinfo("尚無照片", "請先匯入整份樂譜照片。", parent=self)
            return False
        try:
            ordered = reorder_page_assets(
                self.app.project.get("assets", []),
                [str(asset.get("asset_id")) for asset in self.assets],
                source="manual",
            )
        except ScorePlanError as error:
            messagebox.showerror("無法儲存頁序", str(error), parent=self)
            return False
        self.assets = ordered
        self.app.project["assets"] = deepcopy(ordered)
        plan = self.app.project.setdefault("score_plan", {})
        ordered_ids = [asset["asset_id"] for asset in ordered]
        previous_ids = plan.get("page_asset_ids")
        order_changed = (
            isinstance(previous_ids, list)
            and bool(previous_ids)
            and previous_ids != ordered_ids
        )
        plan.update({
            "version": "1.0",
            "page_asset_ids": ordered_ids,
            "page_order_confirmed": True,
            "excluded_asset_ids": [
                str(asset.get("asset_id"))
                for asset in ordered
                if str(asset.get("asset_id")) in self.excluded_asset_ids
            ],
            "updated_at": _now_iso(),
        })
        if order_changed:
            plan["status"] = "page_order_confirmed"
            plan["measure_sequence"] = []
            plan["segment_proposals"] = []
            self.proposals = []
            self._refresh_proposals()
        self.app._mark_dirty()
        self.app._commit_history()
        if not self.app.save():
            return False
        if not quiet:
            self.status_var.set("頁面順序已保存；尚未使用 OpenAI。")
        return True

    def _analyze_all(self) -> None:
        if not self._save_page_order(quiet=True):
            return
        if self._plan_has_live_created_segments():
            messagebox.showwarning(
                "段落已建立",
                "這份規劃已建立正式 Segment。請先備份並刪除這批 Segment，"
                "再重新分析版面。",
                parent=self,
            )
            return

        # 新版面分析開始後，舊的小節順序與段落建議便不再可靠。
        # 先清除並立即存檔，確保中途關閉後不會把新分析搭配舊規劃。
        self.proposals = []
        plan = self.app.project.setdefault("score_plan", {})
        plan["measure_sequence"] = []
        plan["segment_proposals"] = []
        plan["status"] = "layout_analyzing"
        plan["updated_at"] = _now_iso()
        self.app._mark_dirty()
        self._refresh_proposals()
        if not self.app.save():
            self.status_var.set("無法先儲存分析進度；尚未開始重新分析。")
            return
        failures: list[str] = []
        for index, asset in enumerate(self.assets, start=1):
            name = str(asset.get("original_filename") or asset.get("asset_id"))
            asset_id = str(asset.get("asset_id") or "")
            if asset_id in self.excluded_asset_ids:
                continue
            existing_analysis = self.analyses.get(asset_id)
            if isinstance(existing_analysis, dict) and existing_analysis.get("manual_override"):
                self.status_var.set(
                    f"保留人工修正，第 {index}/{len(self.assets)} 頁：{name}"
                )
                self.update_idletasks()
                continue
            self.status_var.set(f"本機分析第 {index}/{len(self.assets)} 頁：{name}")
            self.update_idletasks()
            try:
                path = asset_file_path(self.project_directory, asset, must_exist=True)
                result = analyze_score_page(path)
            except (OSError, PhotoAssetError, ScoreLayoutError, ValueError) as error:
                failures.append(f"{name}：{error}")
                result = {
                    "schema_version": "1.0",
                    "confidence": 0.0,
                    "content_region": None,
                    "systems": [],
                    "system_candidates": [],
                    "barline_candidates": [],
                    "warnings": [{
                        "code": "analysis_failed",
                        "severity": "error",
                        "message": str(error),
                    }],
                }
            result["asset_id"] = asset["asset_id"]
            result["analyzed_at"] = _now_iso()
            self.analyses[str(asset["asset_id"])] = result

            # Checkpoint every page.  A long whole-score analysis should remain
            # recoverable if a later photo fails or the application closes.
            checkpoint = self.app.project.setdefault("score_plan", {})
            checkpoint["analyses_by_asset"] = deepcopy(self.analyses)
            checkpoint["excluded_asset_ids"] = [
                str(item.get("asset_id"))
                for item in self.assets
                if str(item.get("asset_id")) in self.excluded_asset_ids
            ]
            checkpoint["status"] = "layout_analyzing"
            checkpoint["updated_at"] = _now_iso()
            self.app._mark_dirty()
            if not self.app.save():
                self.status_var.set(
                    f"第 {index} 頁分析結果仍在記憶體中，但未能儲存；已停止後續分析。"
                )
                return

        plan = self.app.project.setdefault("score_plan", {})
        plan["analyses_by_asset"] = deepcopy(self.analyses)
        plan["excluded_asset_ids"] = [
            str(item.get("asset_id"))
            for item in self.assets
            if str(item.get("asset_id")) in self.excluded_asset_ids
        ]
        plan["status"] = "layout_analyzed"
        plan["updated_at"] = _now_iso()
        self.app._mark_dirty()
        self.app._commit_history()
        if not self.app.save():
            self.status_var.set("版面分析已完成，但最終進度未能儲存；尚未建立段落建議。")
            return
        self._refresh_pages(select_index=self._selected_page_index() or 0)
        missing = self._missing_measure_assets()
        if missing:
            self.proposals = []
            self._refresh_proposals()
            self._select_page(missing[0][0])
        else:
            self._generate_proposals(quiet=True)
        total = sum(
            self._analysis_measure_count(self.analyses.get(str(asset.get("asset_id") or "")))
            for asset in self._included_assets()
        )
        failure_text = f"；{len(failures)} 頁需人工處理" if failures else ""
        if missing:
            self.status_var.set(
                f"本機分析完成，但仍有 {len(missing)} 頁沒有小節資料。"
                "已選取第一個問題頁，請按「輸入選取頁的小節數…」。"
            )
        else:
            self.status_var.set(
                f"本機分析完成：找到 {total} 個小節{failure_text}。請核對框線與建議段落。"
            )

    def _page_selected(self, _event=None) -> None:
        self._draw_preview()
        self._update_planning_state()

    def _preview_configured(self, _event=None) -> None:
        # A status-label reflow or window resize may emit Configure while the
        # mouse button is still down.  Keep the active handle alive; release
        # will redraw the page at its latest size after saving.
        if self._active_boundary_drag is None and self._active_system_drag is None:
            self._draw_preview()

    def _layout_mode_changed(self) -> None:
        self._active_boundary_drag = None
        self._active_system_drag = None
        self._draw_preview()
        if self._plan_has_live_created_segments():
            self.status_var.set(
                "正式段落已建立，本頁框線已鎖定；"
                "可到「照片辨識」重新框選並更新目前段落範圍。"
            )

    def _page_double_clicked(self, event) -> None:
        row = self.page_tree.identify_row(event.y)
        if not row:
            return
        self.page_tree.selection_set(row)
        self.page_tree.focus(row)
        self._draw_preview()
        index = self._selected_page_index()
        if index is None:
            return
        asset_id = str(self.assets[index].get("asset_id") or "")
        if asset_id not in self.excluded_asset_ids:
            self._manual_correct_selected_page()

    def _draw_preview(self) -> None:
        self._preview_transform = None
        self._boundary_handles = []
        self._system_edge_handles = []
        self._active_boundary_drag = None
        self._active_system_drag = None
        self.preview_canvas.configure(cursor="")
        self.preview_canvas.delete("all")
        index = self._selected_page_index()
        if index is None or not 0 <= index < len(self.assets):
            return
        asset = self.assets[index]
        asset_id = str(asset.get("asset_id") or "")
        try:
            path = asset_file_path(self.project_directory, asset, must_exist=True)
            with Image.open(path) as opened:
                source = ImageOps.exif_transpose(opened).convert("RGB")
        except Exception as error:
            self.preview_canvas.create_text(
                20, 20, text=f"無法預覽：{error}", fill="white", anchor="nw"
            )
            return
        available_width = max(200, self.preview_canvas.winfo_width() - 16)
        available_height = max(180, self.preview_canvas.winfo_height() - 16)
        scale = min(available_width / source.width, available_height / source.height, 1.0)
        size = (max(1, round(source.width * scale)), max(1, round(source.height * scale)))
        preview = source.resize(size, Image.Resampling.LANCZOS)
        self.preview_photo = ImageTk.PhotoImage(preview)
        offset_x = max(4, (self.preview_canvas.winfo_width() - size[0]) // 2)
        offset_y = max(4, (self.preview_canvas.winfo_height() - size[1]) // 2)
        self.preview_canvas.create_image(offset_x, offset_y, image=self.preview_photo, anchor="nw")
        self._preview_transform = {
            "asset_id": asset_id,
            "size": size,
            "offset_x": offset_x,
            "offset_y": offset_y,
        }
        if asset_id in self.excluded_asset_ids:
            self.preview_canvas.create_text(
                offset_x + 8,
                offset_y + 8,
                text="本頁已排除，不會加入段落規劃",
                fill="#FFE08A",
                anchor="nw",
            )
            return
        analysis = self.analyses.get(asset_id)
        if not isinstance(analysis, dict):
            self.preview_canvas.create_text(
                offset_x + 8,
                offset_y + 8,
                text="尚未進行本機分析",
                fill="#FFE08A",
                anchor="nw",
            )
            return
        systems = [
            system
            for system in analysis.get("systems", [])
            if isinstance(system, dict)
        ]
        has_measures = any(
            any(isinstance(measure, dict) for measure in system.get("measure_regions", []))
            for system in systems
        )
        can_adjust = has_measures and not self._plan_has_live_created_segments()
        if can_adjust:
            mode = self.layout_edit_mode_var.get()
            self.preview_canvas.create_text(
                offset_x + 8,
                offset_y + 8,
                text=(
                    "小節數不對：按左側輸入；"
                    + (
                        "拖曳橘色直線對齊小節線，放開即儲存。"
                        if mode == "measures"
                        else "拖曳藍框四邊包住這一整行譜表；小節的上下範圍會同步。"
                    )
                ),
                fill="#9DF0C8",
                anchor="nw",
            )
        elif self._plan_has_live_created_segments():
            self.preview_canvas.create_text(
                offset_x + 8,
                offset_y + 8,
                text=(
                    "正式段落已建立，這裡的藍框與橘線已鎖定。\n"
                    "若只要修正 OpenAI 辨識範圍，請到「照片辨識」"
                    "重新框選，再按「連結／更新目前段落範圍」。"
                ),
                fill="#FFE08A",
                anchor="nw",
            )
        for system_index, system in enumerate(systems):
            self._draw_bbox(system.get("bbox"), size, offset_x, offset_y, "#38A3DB", 3)
            self._draw_measure_boundary_handles(
                system,
                system_index,
                asset_id,
                index,
                size,
                offset_x,
                offset_y,
                interactive=(
                    can_adjust and self.layout_edit_mode_var.get() == "measures"
                ),
            )
            if can_adjust and self.layout_edit_mode_var.get() == "system":
                self._draw_system_edge_handles(
                    system,
                    system_index,
                    systems,
                    asset_id,
                    index,
                    size,
                    offset_x,
                    offset_y,
                )

    @staticmethod
    def _measure_boundary_positions(system: object) -> list[float]:
        if not isinstance(system, dict):
            return []
        measures = system.get("measure_regions")
        if not isinstance(measures, list) or not measures:
            return []
        boxes: list[tuple[float, float]] = []
        for measure in measures:
            if not isinstance(measure, dict) or not isinstance(measure.get("bbox"), dict):
                return []
            bbox = measure["bbox"]
            try:
                left = float(bbox["x"])
                width = float(bbox["width"])
            except (KeyError, TypeError, ValueError):
                return []
            right = left + width
            if not math.isfinite(left) or not math.isfinite(right) or width <= 0:
                return []
            boxes.append((left, right))
        positions = [boxes[0][0]]
        positions.extend(
            (boxes[index - 1][1] + boxes[index][0]) / 2
            for index in range(1, len(boxes))
        )
        positions.append(boxes[-1][1])
        if any(right <= left for left, right in zip(positions, positions[1:])):
            return []
        return positions

    def _draw_measure_boundary_handles(
        self,
        system: dict,
        system_index: int,
        asset_id: str,
        page_index: int,
        size: tuple[int, int],
        offset_x: int,
        offset_y: int,
        *,
        interactive: bool = True,
    ) -> None:
        bbox = system.get("bbox")
        positions = self._measure_boundary_positions(system)
        if not isinstance(bbox, dict) or len(positions) < 2:
            return
        try:
            system_left = float(bbox["x"])
            system_top = float(bbox["y"])
            system_right = system_left + float(bbox["width"])
            system_bottom = system_top + float(bbox["height"])
        except (KeyError, TypeError, ValueError):
            return
        values = (system_left, system_top, system_right, system_bottom, *positions)
        if not all(math.isfinite(value) for value in values):
            return
        if (
            system_right <= system_left
            or system_bottom <= system_top
            or positions[0] < system_left
            or positions[-1] > system_right
        ):
            return

        y1 = offset_y + system_top * size[1]
        y2 = offset_y + system_bottom * size[1]
        system_ref: int | str = system_index
        if isinstance(system.get("system_id"), str) and system["system_id"].strip():
            system_ref = system["system_id"].strip()
        display_gap = max(6.0 / max(1, size[0]), 0.002)
        for boundary_index, normalized_x in enumerate(positions):
            raw_min = system_left if boundary_index == 0 else positions[boundary_index - 1]
            raw_max = (
                system_right
                if boundary_index == len(positions) - 1
                else positions[boundary_index + 1]
            )
            available = raw_max - raw_min
            if available <= 0:
                continue
            gap = min(display_gap, available / 3)
            minimum = raw_min if boundary_index == 0 else raw_min + gap
            maximum = (
                raw_max
                if boundary_index == len(positions) - 1
                else raw_max - gap
            )
            if maximum < minimum:
                continue
            # Never displace an already-stored boundary merely because an
            # unusually narrow neighbouring measure is under six display
            # pixels.  The gap is a movement constraint, not a render offset.
            minimum = min(minimum, normalized_x)
            maximum = max(maximum, normalized_x)
            canvas_x = offset_x + normalized_x * size[0]
            grip_y = (y1 + y2) / 2
            line_id = self.preview_canvas.create_line(
                canvas_x,
                y1,
                canvas_x,
                y2,
                fill="#FF9D24",
                width=2 if interactive else 1,
                activefill="#FFF080" if interactive else "#FF9D24",
                activewidth=4 if interactive else 1,
                tags=("measure-boundary",),
            )
            if not interactive:
                continue
            grip_id = self.preview_canvas.create_oval(
                canvas_x - 4,
                grip_y - 4,
                canvas_x + 4,
                grip_y + 4,
                fill="#FF9D24",
                outline="#FFF4C2",
                width=1,
                tags=("measure-boundary",),
            )
            self._boundary_handles.append({
                "asset_id": asset_id,
                "page_index": page_index,
                "system": system_ref,
                "system_index": system_index,
                "boundary_index": boundary_index,
                "normalized_start": normalized_x,
                "normalized_min": minimum,
                "normalized_max": maximum,
                "canvas_x": canvas_x,
                "y1": y1,
                "y2": y2,
                "grip_y": grip_y,
                "line_id": line_id,
                "grip_id": grip_id,
                "image_width": size[0],
                "offset_x": offset_x,
            })

    def _draw_system_edge_handles(
        self,
        system: dict,
        system_index: int,
        systems: list[dict],
        asset_id: str,
        page_index: int,
        size: tuple[int, int],
        offset_x: int,
        offset_y: int,
    ) -> None:
        bbox = system.get("bbox")
        if not isinstance(bbox, dict):
            return
        try:
            left = float(bbox["x"])
            top = float(bbox["y"])
            right = left + float(bbox["width"])
            bottom = top + float(bbox["height"])
        except (KeyError, TypeError, ValueError):
            return
        if not all(math.isfinite(value) for value in (left, top, right, bottom)):
            return
        if right <= left or bottom <= top:
            return
        measures = [
            item
            for item in system.get("measure_regions", [])
            if isinstance(item, dict) and isinstance(item.get("bbox"), dict)
        ]
        measure_left = left
        measure_right = right
        if measures:
            try:
                measure_left = min(float(item["bbox"]["x"]) for item in measures)
                measure_right = max(
                    float(item["bbox"]["x"]) + float(item["bbox"]["width"])
                    for item in measures
                )
            except (KeyError, TypeError, ValueError):
                return
        min_size_x = max(2.0 / max(1, size[0]), 0.000001)
        min_size_y = max(2.0 / max(1, size[1]), 0.000001)
        previous_midpoint = 0.0
        next_midpoint = 1.0
        if system_index > 0 and isinstance(systems[system_index - 1].get("bbox"), dict):
            previous = systems[system_index - 1]["bbox"]
            try:
                previous_midpoint = float(previous["y"]) + float(previous["height"]) / 2
            except (KeyError, TypeError, ValueError):
                pass
        if system_index + 1 < len(systems) and isinstance(
            systems[system_index + 1].get("bbox"), dict
        ):
            following = systems[system_index + 1]["bbox"]
            try:
                next_midpoint = float(following["y"]) + float(following["height"]) / 2
            except (KeyError, TypeError, ValueError):
                pass
        system_ref: int | str = system_index
        if isinstance(system.get("system_id"), str) and system["system_id"].strip():
            system_ref = system["system_id"].strip()
        edges = (
            ("left", "x", left, 0.0, min(measure_left, right - min_size_x)),
            ("right", "x", right, max(measure_right, left + min_size_x), 1.0),
            (
                "top",
                "y",
                top,
                max(0.0, 2 * previous_midpoint - bottom + min_size_y),
                bottom - min_size_y,
            ),
            (
                "bottom",
                "y",
                bottom,
                top + min_size_y,
                min(1.0, 2 * next_midpoint - top - min_size_y),
            ),
        )
        canvas_left = offset_x + left * size[0]
        canvas_right = offset_x + right * size[0]
        canvas_top = offset_y + top * size[1]
        canvas_bottom = offset_y + bottom * size[1]
        for edge, axis, normalized, minimum, maximum in edges:
            minimum = min(minimum, normalized)
            maximum = max(maximum, normalized)
            if axis == "x":
                coordinate = offset_x + normalized * size[0]
                line_coords = (coordinate, canvas_top, coordinate, canvas_bottom)
                grip_x, grip_y = coordinate, (canvas_top + canvas_bottom) / 2
            else:
                coordinate = offset_y + normalized * size[1]
                line_coords = (canvas_left, coordinate, canvas_right, coordinate)
                grip_x, grip_y = (canvas_left + canvas_right) / 2, coordinate
            line_id = self.preview_canvas.create_line(
                *line_coords,
                fill="#67CCFF",
                width=4,
                activefill="#D7F4FF",
                activewidth=6,
                tags=("system-edge",),
            )
            grip_id = self.preview_canvas.create_rectangle(
                grip_x - 5,
                grip_y - 5,
                grip_x + 5,
                grip_y + 5,
                fill="#67CCFF",
                outline="#E7FAFF",
                width=1,
                tags=("system-edge",),
            )
            self._system_edge_handles.append({
                "asset_id": asset_id,
                "page_index": page_index,
                "system": system_ref,
                "system_index": system_index,
                "edge": edge,
                "axis": axis,
                "bbox_start": dict(bbox),
                "normalized_start": normalized,
                "normalized_min": minimum,
                "normalized_max": maximum,
                "canvas_coordinate": coordinate,
                "line_coords": line_coords,
                "grip_x": grip_x,
                "grip_y": grip_y,
                "line_id": line_id,
                "grip_id": grip_id,
                "image_width": size[0],
                "image_height": size[1],
                "offset_x": offset_x,
                "offset_y": offset_y,
            })

    def _boundary_handle_at(self, x: float, y: float) -> dict[str, object] | None:
        candidates = []
        for handle in self._boundary_handles:
            try:
                dx = abs(float(handle["canvas_x"]) - x)
                y1 = float(handle["y1"])
                y2 = float(handle["y2"])
            except (KeyError, TypeError, ValueError):
                continue
            if dx <= 9 and y1 - 5 <= y <= y2 + 5:
                candidates.append((dx, abs((y1 + y2) / 2 - y), handle))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]))
        return candidates[0][2]

    def _system_edge_handle_at(
        self, x: float, y: float
    ) -> dict[str, object] | None:
        candidates: list[tuple[float, dict[str, object]]] = []
        for handle in self._system_edge_handles:
            try:
                x1, y1, x2, y2 = (
                    float(value) for value in handle["line_coords"]
                )
            except (KeyError, TypeError, ValueError):
                continue
            if handle.get("axis") == "x":
                distance = abs(x1 - x)
                within = min(y1, y2) - 7 <= y <= max(y1, y2) + 7
            else:
                distance = abs(y1 - y)
                within = min(x1, x2) - 7 <= x <= max(x1, x2) + 7
            if distance <= 9 and within:
                candidates.append((distance, handle))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    def _preview_pointer_moved(self, event) -> None:
        if self._active_boundary_drag is not None or self._active_system_drag is not None:
            return
        x = float(self.preview_canvas.canvasx(event.x))
        y = float(self.preview_canvas.canvasy(event.y))
        if self.layout_edit_mode_var.get() == "system":
            handle = self._system_edge_handle_at(x, y)
            cursor = (
                "sb_h_double_arrow"
                if handle is not None and handle.get("axis") == "x"
                else "sb_v_double_arrow" if handle is not None else ""
            )
        else:
            cursor = "sb_h_double_arrow" if self._boundary_handle_at(x, y) else ""
        self.preview_canvas.configure(
            cursor=cursor
        )

    def _preview_pointer_left(self, _event=None) -> None:
        if self._active_boundary_drag is None and self._active_system_drag is None:
            self.preview_canvas.configure(cursor="")

    def _preview_boundary_pressed(self, event):
        if self._plan_has_live_created_segments():
            self.status_var.set(
                "正式段落已建立，規劃框線已鎖定。"
                "請到照片辨識頁更新當前段落範圍。"
            )
            return None
        x = float(self.preview_canvas.canvasx(event.x))
        y = float(self.preview_canvas.canvasy(event.y))
        if self.layout_edit_mode_var.get() == "system":
            handle = self._system_edge_handle_at(x, y)
            if handle is None:
                return None
            self._active_system_drag = dict(handle)
            self._active_system_drag["normalized_current"] = handle[
                "normalized_start"
            ]
            self.preview_canvas.itemconfigure(
                handle["line_id"], fill="#D7F4FF", width=6
            )
            self.preview_canvas.itemconfigure(handle["grip_id"], fill="#D7F4FF")
            self.preview_canvas.configure(
                cursor=(
                    "sb_h_double_arrow"
                    if handle.get("axis") == "x"
                    else "sb_v_double_arrow"
                )
            )
            edge_labels = {
                "left": "左邊",
                "right": "右邊",
                "top": "上邊",
                "bottom": "下邊",
            }
            self.status_var.set(
                f"拖曳中：第 {int(handle['system_index']) + 1} 行藍框"
                f"{edge_labels.get(str(handle['edge']), str(handle['edge']))}。"
            )
            return "break"
        handle = self._boundary_handle_at(x, y)
        if handle is None:
            return None
        self._active_boundary_drag = dict(handle)
        self._active_boundary_drag["normalized_current"] = handle["normalized_start"]
        self.preview_canvas.itemconfigure(
            handle["line_id"], fill="#FFF080", width=4
        )
        self.preview_canvas.itemconfigure(handle["grip_id"], fill="#FFF080")
        self.preview_canvas.configure(cursor="sb_h_double_arrow")
        self.status_var.set(
            f"拖曳中：第 {int(handle['system_index']) + 1} 行、"
            f"第 {int(handle['boundary_index']) + 1} 條橘線。"
        )
        return "break"

    def _drag_normalized_x(self, event, handle: dict[str, object]) -> float:
        canvas_x = float(self.preview_canvas.canvasx(event.x))
        normalized = (
            canvas_x - float(handle["offset_x"])
        ) / max(1.0, float(handle["image_width"]))
        return min(
            max(normalized, float(handle["normalized_min"])),
            float(handle["normalized_max"]),
        )

    def _drag_system_normalized(
        self, event, handle: dict[str, object]
    ) -> float:
        if handle.get("axis") == "x":
            canvas_value = float(self.preview_canvas.canvasx(event.x))
            normalized = (
                canvas_value - float(handle["offset_x"])
            ) / max(1.0, float(handle["image_width"]))
        else:
            canvas_value = float(self.preview_canvas.canvasy(event.y))
            normalized = (
                canvas_value - float(handle["offset_y"])
            ) / max(1.0, float(handle["image_height"]))
        return min(
            max(normalized, float(handle["normalized_min"])),
            float(handle["normalized_max"]),
        )

    def _preview_boundary_dragged(self, event):
        system_handle = self._active_system_drag
        if system_handle is not None:
            normalized = self._drag_system_normalized(event, system_handle)
            system_handle["normalized_current"] = normalized
            if system_handle.get("axis") == "x":
                coordinate = float(system_handle["offset_x"]) + normalized * float(
                    system_handle["image_width"]
                )
                _old_x1, y1, _old_x2, y2 = system_handle["line_coords"]
                line_coords = (coordinate, y1, coordinate, y2)
                grip_x = coordinate
                grip_y = float(system_handle["grip_y"])
            else:
                coordinate = float(system_handle["offset_y"]) + normalized * float(
                    system_handle["image_height"]
                )
                x1, _old_y1, x2, _old_y2 = system_handle["line_coords"]
                line_coords = (x1, coordinate, x2, coordinate)
                grip_x = float(system_handle["grip_x"])
                grip_y = coordinate
            system_handle["canvas_coordinate"] = coordinate
            system_handle["line_coords"] = line_coords
            system_handle["grip_x"] = grip_x
            system_handle["grip_y"] = grip_y
            self.preview_canvas.coords(system_handle["line_id"], *line_coords)
            self.preview_canvas.coords(
                system_handle["grip_id"],
                grip_x - 5,
                grip_y - 5,
                grip_x + 5,
                grip_y + 5,
            )
            return "break"
        handle = self._active_boundary_drag
        if handle is None:
            return None
        normalized = self._drag_normalized_x(event, handle)
        handle["normalized_current"] = normalized
        canvas_x = float(handle["offset_x"]) + normalized * float(handle["image_width"])
        handle["canvas_x"] = canvas_x
        self.preview_canvas.coords(
            handle["line_id"], canvas_x, handle["y1"], canvas_x, handle["y2"]
        )
        grip_y = float(handle["grip_y"])
        self.preview_canvas.coords(
            handle["grip_id"],
            canvas_x - 4,
            grip_y - 4,
            canvas_x + 4,
            grip_y + 4,
        )
        return "break"

    def _preview_boundary_released(self, event):
        system_handle = self._active_system_drag
        if system_handle is not None:
            normalized = self._drag_system_normalized(event, system_handle)
            self._active_system_drag = None
            self.preview_canvas.configure(cursor="")
            pixel_scale = (
                float(system_handle["image_width"])
                if system_handle.get("axis") == "x"
                else float(system_handle["image_height"])
            )
            if abs(normalized - float(system_handle["normalized_start"])) * pixel_scale < 1:
                self._draw_preview()
                self.status_var.set("藍框範圍沒有變更。")
                return "break"
            page_index = int(system_handle["page_index"])
            if not 0 <= page_index < len(self.assets):
                self._draw_preview()
                return "break"
            asset = self.assets[page_index]
            asset_id = str(asset.get("asset_id") or "")
            if (
                asset_id != system_handle.get("asset_id")
                or page_index != self._selected_page_index()
            ):
                self._draw_preview()
                self.status_var.set("頁面已切換，因此取消這次藍框修改。")
                return "break"
            analysis = self.analyses.get(asset_id)
            if not isinstance(analysis, dict):
                self._draw_preview()
                return "break"
            try:
                start = system_handle["bbox_start"]
                left = float(start["x"])
                top = float(start["y"])
                right = left + float(start["width"])
                bottom = top + float(start["height"])
                edge = system_handle.get("edge")
                if edge == "left":
                    left = normalized
                elif edge == "right":
                    right = normalized
                elif edge == "top":
                    top = normalized
                elif edge == "bottom":
                    bottom = normalized
                corrected = adjust_system_bbox(
                    analysis,
                    system_handle["system"],
                    {
                        "x": left,
                        "y": top,
                        "width": right - left,
                        "height": bottom - top,
                    },
                )
            except (ManualLayoutError, KeyError, TypeError, ValueError) as error:
                self._draw_preview()
                messagebox.showerror("無法調整藍框", str(error), parent=self)
                return "break"
            corrected["asset_id"] = asset_id
            corrected["analyzed_at"] = _now_iso()
            self.analyses[asset_id] = corrected
            had_proposals = bool(self.proposals)
            saved = self._persist_boundary_change(
                f"已儲存 {asset.get('original_filename') or asset_id} 的整行譜表範圍。",
                select_index=page_index,
            )
            if saved and not had_proposals and not self._missing_measure_assets():
                self._generate_proposals(quiet=True)
            return "break"
        handle = self._active_boundary_drag
        if handle is None:
            return None
        normalized = self._drag_normalized_x(event, handle)
        self._active_boundary_drag = None
        self.preview_canvas.configure(cursor="")
        moved_pixels = abs(
            normalized - float(handle["normalized_start"])
        ) * float(handle["image_width"])
        if moved_pixels < 1:
            self._draw_preview()
            self.status_var.set("小節線位置沒有變更。")
            return "break"

        page_index = int(handle["page_index"])
        if not 0 <= page_index < len(self.assets):
            self._draw_preview()
            return "break"
        asset = self.assets[page_index]
        asset_id = str(asset.get("asset_id") or "")
        if asset_id != handle.get("asset_id") or page_index != self._selected_page_index():
            self._draw_preview()
            self.status_var.set("頁面已切換，因此取消這次小節線修改。")
            return "break"
        analysis = self.analyses.get(asset_id)
        if not isinstance(analysis, dict):
            self._draw_preview()
            return "break"
        try:
            corrected = adjust_measure_boundary(
                analysis,
                handle["system"],
                int(handle["boundary_index"]),
                normalized,
            )
        except ManualLayoutError as error:
            self._draw_preview()
            messagebox.showerror("無法調整小節線", str(error), parent=self)
            return "break"
        corrected["asset_id"] = asset_id
        corrected["analyzed_at"] = _now_iso()
        self.analyses[asset_id] = corrected
        had_proposals = bool(self.proposals)
        saved = self._persist_boundary_change(
            f"已儲存 {asset.get('original_filename') or asset_id} 的小節線位置。",
            select_index=page_index,
        )
        if saved and not had_proposals and not self._missing_measure_assets():
            self._generate_proposals(quiet=True)
        return "break"

    def _cancel_boundary_drag(self, _event=None):
        if self._active_boundary_drag is None and self._active_system_drag is None:
            return None
        self._active_boundary_drag = None
        self._active_system_drag = None
        self.preview_canvas.configure(cursor="")
        self._draw_preview()
        self.status_var.set("已取消框線調整。")
        return "break"

    def _draw_bbox(
        self,
        bbox: object,
        size: tuple[int, int],
        offset_x: int,
        offset_y: int,
        color: str,
        width: int,
    ) -> None:
        if not isinstance(bbox, dict):
            return
        try:
            x1 = offset_x + float(bbox["x"]) * size[0]
            y1 = offset_y + float(bbox["y"]) * size[1]
            x2 = x1 + float(bbox["width"]) * size[0]
            y2 = y1 + float(bbox["height"]) * size[1]
        except (KeyError, TypeError, ValueError):
            return
        self.preview_canvas.create_rectangle(x1, y1, x2, y2, outline=color, width=width)

    def _temporary_project(self) -> dict:
        project = deepcopy(self.app.project)
        project["assets"] = deepcopy(self.assets)
        return project

    def _measure_sequence(self) -> list[dict]:
        included = self._included_assets()
        if not included:
            raise ScorePlanError("至少要保留一張譜面照片")
        return flatten_measure_sequence(self._included_analyses(), included)

    def _included_analyses(self) -> dict[str, dict]:
        included_ids = {
            str(asset.get("asset_id") or "") for asset in self._included_assets()
        }
        return {
            asset_id: analysis
            for asset_id, analysis in self.analyses.items()
            if asset_id in included_ids
        }

    def _generate_proposals(self, *, quiet: bool = False) -> None:
        missing = self._missing_measure_assets()
        if missing:
            self.proposals = []
            self._refresh_proposals()
            self._select_page(missing[0][0])
            message = (
                f"還有 {len(missing)} 頁沒有小節資料；已選取第一個問題頁。"
                "請手動修正小節數，或將非譜面頁排除。"
            )
            self.status_var.set(message)
            if not quiet:
                messagebox.showwarning("尚不能產生段落", message, parent=self)
            return
        try:
            measures = self._measure_sequence()
        except (ScorePlanError, ValueError, TypeError) as error:
            self.status_var.set(
                "無法建立完整小節順序。請用「輸入選取頁的小節數…」修正問題頁，"
                "或將非譜面照片排除。"
            )
            if not quiet:
                messagebox.showwarning(
                    "小節規劃尚不完整",
                    f"{error}\n\n請先手動修正問題頁，或將非譜面頁排除。",
                    parent=self,
                )
            return
        if not measures:
            if not quiet:
                messagebox.showinfo(
                    "尚無小節候選",
                    "請先執行本機分析，或手動設定選取頁的小節數。",
                    parent=self,
                )
            return
        target = int(self.target_bars_var.get())
        self.proposals = suggest_segment_ranges(1, len(measures), target_bars=target)
        for index, proposal in enumerate(self.proposals, start=1):
            proposal["section_name"] = f"建議段落 {index}"
        saved = self._save_plan_draft()
        self._refresh_proposals()
        if saved:
            self.status_var.set(
                f"已產生 {len(self.proposals)} 個建議段落，並選取第一段。"
                "可直接拆分、合併或命名。"
            )

    def _save_plan_draft(self) -> bool:
        try:
            measure_sequence = self._measure_sequence()
        except (ScorePlanError, ValueError, TypeError) as error:
            self.status_var.set(f"規劃草稿尚未儲存：{error}")
            return False
        plan = self.app.project.setdefault("score_plan", {})
        plan["analyses_by_asset"] = deepcopy(self.analyses)
        plan["excluded_asset_ids"] = [
            str(asset.get("asset_id"))
            for asset in self.assets
            if str(asset.get("asset_id")) in self.excluded_asset_ids
        ]
        plan["measure_sequence"] = deepcopy(measure_sequence)
        plan["segment_proposals"] = deepcopy(self.proposals)
        plan["status"] = "segments_proposed"
        plan["updated_at"] = _now_iso()
        self.app._mark_dirty()
        self.app._commit_history()
        if not self.app.save():
            self.status_var.set("規劃草稿仍在記憶體中，但尚未成功儲存。")
            return False
        self.status_var.set("段落規劃草稿已儲存。")
        return True

    def _proposal_pages(self, proposal: dict, measures: list[dict]) -> str:
        start = _positive_bar_or_none(proposal.get("start_bar"))
        end = _positive_bar_or_none(proposal.get("end_bar"))
        if start is None or end is None or end < start:
            return "—"
        sequences = []
        asset_sequences = {
            str(asset.get("asset_id")): index
            for index, asset in enumerate(self.assets, start=1)
        }
        for measure in measures[start - 1 : end]:
            page = asset_sequences.get(str(measure.get("asset_id")))
            if page is not None and page not in sequences:
                sequences.append(page)
        return "、".join(str(value) for value in sequences) or "—"

    def _refresh_proposals(self) -> None:
        selected_index = self._selected_proposal_index()
        self.segment_tree.delete(*self.segment_tree.get_children())
        try:
            measures = self._measure_sequence()
        except Exception:
            measures = []
        for index, proposal in enumerate(self.proposals):
            start = _positive_bar_or_none(proposal.get("start_bar"))
            end = _positive_bar_or_none(proposal.get("end_bar"))
            valid_range = start is not None and end is not None and end >= start
            self.segment_tree.insert(
                "",
                "end",
                iid=f"segment-{index}",
                values=(
                    index + 1,
                    proposal.get("section_name") or f"段落 {index + 1}",
                    start if start is not None else "?",
                    end if end is not None else "?",
                    end - start + 1 if valid_range else "?",
                    self._proposal_pages(proposal, measures),
                ),
            )
        if self.proposals:
            target = min(
                selected_index if selected_index is not None else 0,
                len(self.proposals) - 1,
            )
            item = f"segment-{target}"
            self.segment_tree.selection_set(item)
            self.segment_tree.focus(item)
            self.segment_tree.see(item)
        self._update_planning_state()

    def _selected_proposal_index(self) -> int | None:
        selected = self.segment_tree.selection()
        if not selected:
            return None
        try:
            return int(str(selected[0]).rsplit("-", 1)[-1])
        except ValueError:
            return None

    def _split_proposal(self) -> None:
        index = self._selected_proposal_index()
        if index is None:
            messagebox.showinfo("尚未選擇", "請先選擇要拆分的段落。", parent=self)
            return
        proposal = self.proposals[index]
        start = int(proposal["start_bar"])
        end = int(proposal["end_bar"])
        if start >= end:
            messagebox.showinfo("無法拆分", "單一小節段落不能再拆分。", parent=self)
            return
        split_after = simpledialog.askinteger(
            "拆分段落",
            f"在第幾小節之後拆分？（{start}～{end - 1}）",
            parent=self,
            minvalue=start,
            maxvalue=end - 1,
        )
        if split_after is None:
            return
        left = deepcopy(proposal)
        right = deepcopy(proposal)
        left["end_bar"] = split_after
        left["bar_count"] = split_after - start + 1
        right["start_bar"] = split_after + 1
        right["bar_count"] = end - split_after
        right["section_name"] = f"{proposal.get('section_name') or '段落'}（後段）"
        self.proposals[index : index + 1] = [left, right]
        self._save_plan_draft()
        self._refresh_proposals()

    def _merge_proposal(self) -> None:
        index = self._selected_proposal_index()
        if index is None or index + 1 >= len(self.proposals):
            messagebox.showinfo("無法合併", "請選擇不是最後一筆的段落。", parent=self)
            return
        left = deepcopy(self.proposals[index])
        right = self.proposals[index + 1]
        if int(left["end_bar"]) + 1 != int(right["start_bar"]):
            messagebox.showerror("段落不連續", "只有相鄰段落可以合併。", parent=self)
            return
        left["end_bar"] = int(right["end_bar"])
        left["bar_count"] = int(left["end_bar"]) - int(left["start_bar"]) + 1
        self.proposals[index : index + 2] = [left]
        self._save_plan_draft()
        self._refresh_proposals()

    def _name_proposal(self) -> None:
        index = self._selected_proposal_index()
        if index is None:
            messagebox.showinfo("尚未選擇", "請先選擇一個段落。", parent=self)
            return
        proposal = self.proposals[index]
        name = simpledialog.askstring(
            "段落名稱",
            "輸入段落名稱：",
            initialvalue=str(proposal.get("section_name") or ""),
            parent=self,
        )
        if name is None:
            return
        proposal["section_name"] = name.strip() or f"段落 {index + 1}"
        self._save_plan_draft()
        self._refresh_proposals()

    def _apply_segments(self) -> None:
        if not self._save_page_order(quiet=True):
            return
        existing_plan = self.app.project.get("score_plan")
        if isinstance(existing_plan, dict):
            live_ids = {
                str(segment.get("segment_id"))
                for segment in self.app.project.get("segments", [])
                if isinstance(segment, dict)
            }
            created_ids = {
                str(value)
                for value in existing_plan.get("created_segment_ids", [])
                if isinstance(value, str) and value
            }
            proposal_ranges = set()
            for item in self.proposals:
                if not isinstance(item, dict):
                    continue
                start = _positive_bar_or_none(item.get("start_bar"))
                end = _positive_bar_or_none(item.get("end_bar"))
                if start is not None and end is not None and end >= start:
                    proposal_ranges.add((start, end))
            matching_ranges = set()
            for segment in self.app.project.get("segments", []):
                if not isinstance(segment, dict) or not segment.get("source_refs"):
                    continue
                if not all(
                    isinstance(ref, dict)
                    and ref.get("origin") in {"detected", "manual_layout"}
                    for ref in segment.get("source_refs", [])
                ):
                    continue
                start = _positive_bar_or_none(segment.get("source_start_bar"))
                end = _positive_bar_or_none(segment.get("source_end_bar"))
                if start is not None and end is not None and end >= start:
                    matching_ranges.add((start, end))
            if (created_ids & live_ids) or (proposal_ranges & matching_ranges):
                messagebox.showinfo(
                    "這份規劃已建立",
                    "這份規劃仍有已建立的 Segment，因此不會再次複製，避免重疊段落與重複 API 費用。\n\n"
                    "名稱、BPM 與節奏請直接在「段落資料／格狀標註」調整。若確定要重新切分，"
                    "請先備份專案並刪除這一批已建立的 Segment，再重新開啟整份樂譜規劃。",
                    parent=self,
                )
                return
        if not self.proposals:
            self._generate_proposals()
            if not self.proposals:
                return
        try:
            measures = self._measure_sequence()
        except (ScorePlanError, ValueError, TypeError) as error:
            messagebox.showerror("無法建立段落", str(error), parent=self)
            return
        if not measures:
            return
        if not messagebox.askyesno(
            "建立跨頁段落",
            f"將依 {len(measures)} 個已確認小節建立 {len(self.proposals)} 個段落。\n\n"
            "已有人工節奏的段落不會被刪除；空白預設段落可安全重用。"
            "建立後仍可在段落資料頁調整名稱與 BPM。要繼續嗎？",
            parent=self,
        ):
            return
        try:
            project, summary = apply_segment_proposals(
                self._temporary_project(),
                self._included_analyses(),
                proposals=self.proposals,
                page_sequence=self._included_assets(),
            )
        except (ScorePlanError, ValueError, TypeError) as error:
            messagebox.showerror("無法建立段落", str(error), parent=self)
            return
        created_ids = set(summary.get("created_segment_ids", []))
        shared_source = self.app.current_segment
        proposal_by_range = {
            (int(item["start_bar"]), int(item["end_bar"])): item
            for item in self.proposals
        }
        for segment in project.get("segments", []):
            if not isinstance(segment, dict) or segment.get("segment_id") not in created_ids:
                continue
            for key in ("piece_id", "piece_name", "source", "system"):
                segment[key] = shared_source.get(key, segment.get(key, ""))
            key = (int(segment.get("source_start_bar", 0)), int(segment.get("source_end_bar", 0)))
            proposal = proposal_by_range.get(key)
            if proposal is not None:
                segment["section_name"] = str(
                    proposal.get("section_name") or segment.get("section_name") or ""
                )
            segment["selection_reason"] = "依整份樂譜本機版面分析建立；需人工確認。"
        project_plan = project.setdefault("score_plan", {})
        project_plan.update({
            "version": "1.0",
            "page_asset_ids": [asset["asset_id"] for asset in self.assets],
            "page_order_confirmed": True,
            "excluded_asset_ids": [
                str(asset.get("asset_id"))
                for asset in self.assets
                if str(asset.get("asset_id")) in self.excluded_asset_ids
            ],
            "analyses_by_asset": deepcopy(self.analyses),
            "measure_sequence": deepcopy(measures),
            "segment_proposals": deepcopy(self.proposals),
            "created_segment_ids": list(summary.get("created_segment_ids", [])),
            "status": "segments_created",
            "updated_at": _now_iso(),
        })
        self.app.project = project
        self.app.segment_index = 0
        self.app.track_index = 0
        self.app.page_start_bar = 1
        self.app._normalize_project()
        # save() first applies the currently displayed metadata fields.  Load
        # them from the new Segment now so stale pre-plan values cannot rewrite
        # its bar count, ID, or title during that save.
        self.app.refresh_all()
        self.app._mark_dirty()
        self.app._commit_history()
        if not self.app.save():
            self.status_var.set("段落已建立在記憶體中，但尚未成功儲存；請先處理儲存錯誤。")
            return
        self.app.refresh_all()
        created = summary.get("proposal_count", len(self.proposals))
        messagebox.showinfo(
            "跨頁段落已建立",
            f"已建立 {created} 個段落，總計 {len(measures)} 小節。\n\n"
            "下一步可先逐段確認來源框，再選擇單段或批次送交 OpenAI。",
            parent=self,
        )
        self.destroy()


def show_score_plan_dialog(master: tk.Misc, app, project_directory: Path) -> None:
    if not app.project.get("assets"):
        messagebox.showinfo("尚無照片", "請先一次匯入整份樂譜的 JPEG／PNG。", parent=master)
        return
    dialog = ScorePlanDialog(master, app, project_directory)
    master.wait_window(dialog)


__all__ = ["ScorePlanDialog", "show_score_plan_dialog"]
