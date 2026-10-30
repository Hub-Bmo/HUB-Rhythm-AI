"""Tkinter workspace for durable score-photo import and source linking."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from uuid import uuid4

from PIL import Image, ImageOps, ImageTk

from .candidate_correction_ui import (
    CandidateMappingDialog,
    candidate_original_instrument_id,
    candidate_visible_symbol,
    correct_event_candidates,
    is_numbered_rest_candidate,
    matching_event_candidate_count,
    run_notation_profile,
)
from .candidate_application import (
    CandidateApplicationError,
    apply_application_plan,
    build_application_plan,
)
from .instrument_library import create_project_snapshot, get_instrument, get_stroke
from .photo_assets import (
    PhotoAssetError,
    asset_file_path,
    import_image_asset,
)
from .openai_provider import OpenAIProviderError, recognize_score_region
from .openai_settings import credential_status_text
from .recognition import (
    new_recognition_run,
    review_candidate,
    update_recognition_run,
)
from .recognition_batch import (
    RETRYABLE_REGION_STATUSES,
    build_segment_context,
    plan_recognition_batch,
    summarize_batch_results,
    target_fingerprint,
)
from .recognition_contract import RecognitionContractError, build_recognition_request
from .score_plan_ui import show_score_plan_dialog
from .symbol_mappings import (
    SymbolMappingError,
    apply_mapping_rule_to_candidate,
    candidate_observed_symbol,
    mapping_suggestion_for_candidate,
    upsert_mapping_rule,
)
from .usage_cost import (
    estimate_recognition_run_cost,
    estimate_usage_cost,
    summarize_recognition_runs,
)


PROFILE_LABELS = {
    "auto": "自動判斷",
    "staff": "五線譜",
    "numbered": "數字譜／簡譜",
    "gongjing": "鑼鼓經",
}
LABEL_TO_PROFILE = {label: code for code, label in PROFILE_LABELS.items()}


class BatchRecognitionSummaryDialog(tk.Toplevel):
    """Explain one completed batch and offer an explicit, visible retry."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        summary_text: str,
        rows: list[tuple[str, str, str]],
        retry_count: int,
    ) -> None:
        super().__init__(master)
        self.title("批次辨識完成")
        self.geometry("820x430")
        self.minsize(700, 360)
        self.transient(master.winfo_toplevel())
        self.retry_requested = False

        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)
        ttk.Label(
            outer,
            text=summary_text,
            justify="left",
            wraplength=760,
        ).pack(fill="x", anchor="w", pady=(0, 10))

        if rows:
            ttk.Label(
                outer,
                text="需要注意的項目",
                font=("", 10, "bold"),
            ).pack(anchor="w", pady=(0, 4))
            table_frame = ttk.Frame(outer)
            table_frame.pack(fill="both", expand=True)
            table = ttk.Treeview(
                table_frame,
                columns=("source", "result", "detail"),
                show="headings",
                height=8,
            )
            table.heading("source", text="照片／段落")
            table.heading("result", text="結果")
            table.heading("detail", text="原因／下一步")
            table.column("source", width=235, minwidth=150, stretch=True)
            table.column("result", width=145, minwidth=120, stretch=False)
            table.column("detail", width=390, minwidth=220, stretch=True)
            scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=table.yview)
            table.configure(yscrollcommand=scrollbar.set)
            table.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")
            for row in rows:
                table.insert("", "end", values=row)
        else:
            ttk.Label(
                outer,
                text="這一批沒有失敗或空白結果。",
            ).pack(fill="both", expand=True, anchor="w")

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="關閉", command=self._close).pack(side="right")
        if retry_count:
            ttk.Button(
                actions,
                text=f"只重試失敗項目（{retry_count}）",
                style="Accent.TButton",
                command=self._retry,
            ).pack(side="right", padx=(0, 8))

        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _event: self._close())
        self.grab_set()

    def _retry(self) -> None:
        self.retry_requested = True
        self.destroy()

    def _close(self) -> None:
        self.destroy()


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class PhotoWorkspace(ttk.Frame):
    """A source-image tab that keeps all edits in the owning project dict."""

    def __init__(self, master: tk.Misc, app) -> None:
        super().__init__(master)
        self.app = app
        self.asset_ids: list[str] = []
        self.current_asset: dict | None = None
        self.source_image: Image.Image | None = None
        self.display_image: Image.Image | None = None
        self.photo_image: ImageTk.PhotoImage | None = None
        self.image_scale = 1.0
        self.drag_start: tuple[float, float] | None = None
        self.selection_bbox: dict | None = None
        self.selection_item: int | None = None
        self.overlay_items: list[int] = []
        self.candidate_items: dict[str, int] = {}
        self.candidate_lookup: dict[str, tuple[dict, dict]] = {}
        self.selected_candidate_id: str | None = None
        self._recognition_results: queue.Queue = queue.Queue()
        self._recognition_active: dict | None = None
        self._recognition_batch: dict | None = None
        self._recognition_polling = False

        self.profile_var = tk.StringVar(value=PROFILE_LABELS["auto"])
        self.zoom_var = tk.StringVar(value="適合視窗")
        self.info_var = tk.StringVar(value="尚未匯入樂譜照片")
        self.selection_var = tk.StringVar(value="拖曳框選譜面範圍")
        self.progress_var = tk.StringVar(value="")
        self.usage_var = tk.StringVar(value="用量：尚無成功辨識紀錄")
        self.provider_status_var = tk.StringVar(value="OpenAI：尚未檢查設定")
        self.candidate_summary_var = tk.StringVar(value="尚無 AI 辨識候選")
        self.current_segment_var = tk.StringVar(value="目前段落：—")
        self._build()
        self.after_idle(self.update_provider_status)

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=(0, 10, 0, 0))
        outer.pack(fill="both", expand=True)
        sidebar = ttk.Frame(outer, style="Panel.TFrame", padding=14, width=285)
        sidebar.pack(side="left", fill="y", padx=(0, 10))
        sidebar.pack_propagate(False)
        viewer = ttk.Frame(outer, style="Panel.TFrame", padding=8)
        viewer.pack(side="left", fill="both", expand=True)

        ttk.Label(sidebar, text="來源照片", style="Section.TLabel").pack(anchor="w")
        ttk.Button(
            sidebar,
            text="＋ 匯入 JPEG／PNG",
            style="Accent.TButton",
            command=self.import_photos,
        ).pack(fill="x", pady=(7, 7))
        ttk.Button(
            sidebar,
            text="整份樂譜規劃…",
            command=self.open_score_plan,
        ).pack(fill="x", pady=(0, 7))
        self.asset_list = tk.Listbox(sidebar, height=10, activestyle="none")
        self.asset_list.pack(fill="x")
        self.asset_list.bind("<<ListboxSelect>>", self._asset_selected)
        ttk.Label(
            sidebar,
            textvariable=self.info_var,
            style="Subtle.TLabel",
            justify="left",
            wraplength=250,
        ).pack(fill="x", pady=(7, 12))

        ttk.Separator(sidebar).pack(fill="x", pady=4)
        ttk.Label(sidebar, text="譜面 Profile", style="Section.TLabel").pack(anchor="w", pady=(9, 3))
        profile = ttk.Combobox(
            sidebar,
            textvariable=self.profile_var,
            values=tuple(PROFILE_LABELS.values()),
            state="readonly",
        )
        profile.pack(fill="x")
        profile.bind("<<ComboboxSelected>>", self._profile_changed)

        ttk.Label(sidebar, text="框選與段落", style="Section.TLabel").pack(anchor="w", pady=(14, 3))
        ttk.Label(
            sidebar,
            textvariable=self.current_segment_var,
            justify="left",
            wraplength=250,
        ).pack(fill="x", pady=(0, 3))
        ttk.Label(
            sidebar,
            textvariable=self.selection_var,
            style="Subtle.TLabel",
            justify="left",
            wraplength=250,
        ).pack(fill="x")
        self.link_selection_button = ttk.Button(
            sidebar,
            text="連結／更新目前段落範圍",
            command=self.link_selection_to_segment,
        )
        self.link_selection_button.pack(fill="x", pady=(7, 3))
        self.recognize_button = ttk.Button(
            sidebar,
            text="使用 OpenAI 辨識框選範圍",
            command=self.create_recognition_job,
        )
        self.recognize_button.pack(fill="x", pady=3)
        self.batch_recognize_button = ttk.Button(
            sidebar,
            text="批次辨識所有已確認段落範圍",
            command=self.create_batch_recognition_jobs,
        )
        self.batch_recognize_button.pack(fill="x", pady=3)
        self.retry_failed_button = ttk.Button(
            sidebar,
            text="只重試失敗項目",
            command=self.retry_failed_recognition_jobs,
        )
        self.retry_failed_button.pack(fill="x", pady=3)
        self.retry_failed_button.state(["disabled"])
        ttk.Button(
            sidebar,
            text="OpenAI 設定…",
            command=self.app.open_openai_settings,
        ).pack(fill="x", pady=3)
        ttk.Label(
            sidebar,
            textvariable=self.progress_var,
            style="Subtle.TLabel",
            justify="left",
            wraplength=250,
        ).pack(fill="x", pady=(7, 0))
        ttk.Label(
            sidebar,
            textvariable=self.usage_var,
            style="Subtle.TLabel",
            justify="left",
            wraplength=250,
        ).pack(fill="x", pady=(4, 0))
        ttk.Label(
            sidebar,
            textvariable=self.provider_status_var,
            style="Subtle.TLabel",
            justify="left",
            wraplength=250,
        ).pack(fill="x", pady=(4, 0))

        tools = ttk.Frame(viewer, style="Panel.TFrame")
        tools.pack(fill="x", pady=(0, 6))
        ttk.Label(tools, text="預覽", style="Section.TLabel").pack(side="left")
        zoom = ttk.Combobox(
            tools,
            textvariable=self.zoom_var,
            values=("適合視窗", "25%", "50%", "75%", "100%", "150%", "200%"),
            state="readonly",
            width=10,
        )
        zoom.pack(side="right")
        zoom.bind("<<ComboboxSelected>>", lambda _event: self.redraw())
        ttk.Label(
            viewer,
            text=(
                "框線：紫色實線＝這次要辨識的範圍　"
                "藍色虛線＝目前段落已連結　"
                "灰色虛線＝其他段落\n"
                "AI 候選：橘色＝待確認　綠色＝已接受　"
                "藍色＝已更正　灰色＝已拒絕"
            ),
            foreground="#6F736F",
            justify="left",
        ).pack(fill="x", pady=(0, 5))

        review_panel = ttk.LabelFrame(
            viewer,
            text="AI 辨識候選（接受／拒絕前不會寫入正式節奏）",
            padding=7,
        )
        review_panel.pack(side="bottom", fill="x", pady=(7, 0))
        ttk.Label(
            review_panel,
            textvariable=self.candidate_summary_var,
            justify="left",
            wraplength=900,
        ).pack(anchor="w", pady=(0, 4))
        candidate_body = ttk.Frame(review_panel)
        candidate_body.pack(fill="x")
        self.candidate_tree = ttk.Treeview(
            candidate_body,
            columns=("kind", "evidence", "suggestion", "confidence", "status"),
            show="headings",
            height=5,
            selectmode="browse",
        )
        self.candidate_tree.heading("kind", text="類型")
        self.candidate_tree.heading("evidence", text="辨識內容／視覺證據")
        self.candidate_tree.heading("suggestion", text="規則建議")
        self.candidate_tree.heading("confidence", text="信心")
        self.candidate_tree.heading("status", text="複核")
        self.candidate_tree.column("kind", width=90, anchor="center", stretch=False)
        self.candidate_tree.column("evidence", width=340)
        self.candidate_tree.column("suggestion", width=220)
        self.candidate_tree.column("confidence", width=70, anchor="center", stretch=False)
        self.candidate_tree.column("status", width=90, anchor="center", stretch=False)
        candidate_scroll = ttk.Scrollbar(
            candidate_body, orient="vertical", command=self.candidate_tree.yview
        )
        self.candidate_tree.configure(yscrollcommand=candidate_scroll.set)
        self.candidate_tree.pack(side="left", fill="x", expand=True)
        candidate_scroll.pack(side="right", fill="y")
        self.candidate_tree.bind("<<TreeviewSelect>>", self._candidate_selected)
        candidate_actions = ttk.Frame(review_panel)
        candidate_actions.pack(fill="x", pady=(5, 0))
        selected_actions = ttk.Frame(candidate_actions)
        selected_actions.pack(fill="x")
        ttk.Button(
            selected_actions,
            text="接受選取候選",
            command=lambda: self._review_selected_candidate("accepted"),
        ).pack(side="left")
        ttk.Button(
            selected_actions,
            text="拒絕選取候選",
            command=lambda: self._review_selected_candidate("rejected"),
        ).pack(side="left", padx=6)
        ttk.Button(
            selected_actions,
            text="更正／對應選取候選…",
            command=self._correct_selected_candidate,
        ).pack(side="left", padx=(0, 6))
        ttk.Button(
            selected_actions,
            text="套用規則建議並接受",
            command=self._accept_selected_mapping_suggestion,
        ).pack(side="left")
        batch_actions = ttk.Frame(candidate_actions)
        batch_actions.pack(fill="x", pady=(5, 0))
        ttk.Button(
            batch_actions,
            text="接受目前照片／段落全部待確認",
            command=self._accept_all_pending_candidates,
        ).pack(side="left")
        ttk.Button(
            batch_actions,
            text="預覽並套用已接受候選",
            style="Accent.TButton",
            command=self.apply_accepted_candidates,
        ).pack(side="left", padx=6)
        self.candidate_hint = ttk.Label(
            batch_actions,
            text="套用會建立獨立 AI Take，不覆蓋人工輸入",
            foreground="#6F736F",
        )
        self.candidate_hint.pack(side="right")

        canvas_frame = ttk.Frame(viewer)
        canvas_frame.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(
            canvas_frame,
            background="#343434",
            highlightthickness=0,
            cursor="crosshair",
        )
        x_scroll = ttk.Scrollbar(canvas_frame, orient="horizontal", command=self.canvas.xview)
        y_scroll = ttk.Scrollbar(canvas_frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=x_scroll.set, yscrollcommand=y_scroll.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        canvas_frame.rowconfigure(0, weight=1)
        canvas_frame.columnconfigure(0, weight=1)
        self.canvas.bind("<ButtonPress-1>", self._drag_begin)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        self.canvas.bind("<Configure>", self._canvas_resized)

    @property
    def project_directory(self) -> Path | None:
        return self.app.current_path.parent if self.app.current_path else None

    def _ensure_saved_project(self) -> bool:
        if self.app.current_path is not None and self.app.current_path.name == "project.json":
            return True
        if self.app.current_path is not None:
            messagebox.showinfo(
                "匯入照片・步驟 1/2",
                "加入照片前，請先把舊 JSON 另存成可攜式專案資料夾。\n\n"
                "接下來的畫面是選擇專案名稱與儲存位置；按下「儲存」後，"
                "才會進入步驟 2/2 選擇 JPEG／PNG 照片。",
            )
        else:
            messagebox.showinfo(
                "匯入照片・步驟 1/2",
                "第一次匯入照片前，需要先建立專案資料夾，讓照片與辨識進度有固定的保存位置。\n\n"
                "接下來的畫面不是選照片：請先選擇專案名稱與儲存位置並按「儲存」。"
                "完成後會自動開啟 JPEG／PNG 選擇視窗。",
            )
        self.app.status_var.set("匯入照片・步驟 1/2：先建立專案資料夾")
        return bool(
            self.app.save_as(
                dialog_title="步驟 1/2：先建立專案資料夾（下一步再選照片）"
            )
        )

    def import_photos(self) -> None:
        if not self._ensure_saved_project():
            return
        self.app.status_var.set("匯入照片・步驟 2/2：選擇 JPEG／PNG")
        paths = filedialog.askopenfilenames(
            title="步驟 2/2：選擇樂譜照片（JPEG／PNG）",
            filetypes=(("樂譜照片", "*.jpg *.jpeg *.png"), ("所有檔案", "*.*")),
        )
        if not paths:
            return
        assert self.project_directory is not None
        imported: list[dict] = []
        try:
            for path in paths:
                imported.append(import_image_asset(self.project_directory, path))
        except (OSError, PhotoAssetError) as error:
            messagebox.showerror("照片匯入失敗", str(error))
            return

        assets = self.app.project.setdefault("assets", [])
        by_id = {asset.get("asset_id"): asset for asset in assets if isinstance(asset, dict)}
        for asset in imported:
            existing = by_id.get(asset["asset_id"])
            if existing is None:
                asset["notation_profile"] = "auto"
                assets.append(asset)
                by_id[asset["asset_id"]] = asset
            else:
                existing.setdefault("notation_profile", "auto")
        # Give new pages an immediately visible stable order. Storage migration
        # performs the same validation when an old project is reopened.
        used_sequences = {
            int(asset.get("page_sequence"))
            for asset in assets
            if isinstance(asset, dict)
            and isinstance(asset.get("page_sequence"), int)
            and not isinstance(asset.get("page_sequence"), bool)
            and int(asset["page_sequence"]) > 0
        }
        next_sequence = max(used_sequences, default=0) + 1
        for asset in assets:
            if not isinstance(asset, dict) or asset.get("page_sequence") is not None:
                continue
            while next_sequence in used_sequences:
                next_sequence += 1
            asset["page_sequence"] = next_sequence
            asset["page_sequence_source"] = "import"
            used_sequences.add(next_sequence)
            next_sequence += 1
        self.app.project.setdefault("workspace_state", {})["photo_asset_id"] = imported[-1]["asset_id"]
        self.app._mark_dirty()
        self.app._commit_history()
        self.refresh(select_asset_id=imported[-1]["asset_id"])
        self.app.status_var.set(f"已匯入 {len(imported)} 張樂譜照片")

    def open_score_plan(self) -> None:
        if not self._ensure_saved_project():
            return
        if self.project_directory is None:
            return
        show_score_plan_dialog(self, self.app, self.project_directory)
        self.refresh()

    def refresh(self, *, select_asset_id: str | None = None) -> None:
        segment = self.app.current_segment
        segment_name = segment.get("section_name") or segment.get("pattern_name") or "未命名段落"
        self.current_segment_var.set(
            f"目前段落：{segment.get('segment_id', '—')}｜{segment_name}"
        )
        if (
            self._recognition_active is not None
            and self._recognition_active.get("project_generation")
            != self.app._project_generation
        ):
            self._recognition_active = None
            self._recognition_batch = None
            self._set_recognition_controls_enabled(True)
        assets = [asset for asset in self.app.project.get("assets", []) if isinstance(asset, dict)]
        self.asset_list.delete(0, "end")
        self.asset_ids = []
        for index, asset in enumerate(assets, start=1):
            asset_id = str(asset.get("asset_id", ""))
            self.asset_ids.append(asset_id)
            name = asset.get("original_filename") or asset_id or f"照片 {index}"
            profile = PROFILE_LABELS.get(str(asset.get("notation_profile", "auto")), "自動判斷")
            self.asset_list.insert("end", f"{index}. {name}｜{profile}")

        desired = select_asset_id or self.app.project.get("workspace_state", {}).get("photo_asset_id")
        if desired in self.asset_ids:
            selected = self.asset_ids.index(desired)
        elif self.asset_ids:
            selected = 0
        else:
            self.current_asset = None
            self.source_image = None
            self.canvas.delete("all")
            self.info_var.set("尚未匯入樂譜照片")
            self.progress_var.set("")
            self._refresh_usage_summary()
            self._refresh_retry_failed_button()
            self._clear_candidate_tree()
            return
        self.asset_list.selection_clear(0, "end")
        self.asset_list.selection_set(selected)
        self.asset_list.activate(selected)
        self._load_asset(self.asset_ids[selected])

    def _asset_selected(self, _event=None) -> None:
        selected = self.asset_list.curselection()
        if selected:
            self._load_asset(self.asset_ids[int(selected[0])])

    def _load_asset(self, asset_id: str) -> None:
        self.current_asset = next(
            (asset for asset in self.app.project.get("assets", []) if asset.get("asset_id") == asset_id),
            None,
        )
        if self.current_asset is None or self.project_directory is None:
            return
        self.app.project.setdefault("workspace_state", {})["photo_asset_id"] = asset_id
        profile = str(self.current_asset.get("notation_profile", "auto"))
        self.profile_var.set(PROFILE_LABELS.get(profile, PROFILE_LABELS["auto"]))
        try:
            path = asset_file_path(self.project_directory, self.current_asset, must_exist=True)
            with Image.open(path) as opened:
                self.source_image = ImageOps.exif_transpose(opened).convert("RGB")
        except Exception as error:
            self.source_image = None
            self.canvas.delete("all")
            self.info_var.set(f"來源照片遺失或無法讀取：\n{error}")
            return
        self.current_asset["coordinate_space"] = "exif_transposed_v1"
        self.current_asset["display_width"] = self.source_image.width
        self.current_asset["display_height"] = self.source_image.height
        self.info_var.set(
            f"{self.current_asset.get('original_filename', asset_id)}\n"
            f"{self.source_image.width} × {self.source_image.height}px\n"
            f"ID：{asset_id}"
        )
        linked = self._current_segment_source_refs(asset_id)
        if len(linked) == 1 and isinstance(linked[0].get("bbox"), dict):
            self.selection_bbox = dict(linked[0]["bbox"])
            self.selection_var.set("已載入目前段落的已連結範圍；可直接辨識或重新框選")
        else:
            self.selection_bbox = None
            if len(linked) > 1:
                self.selection_var.set("目前段落在此頁有多個區域；請重新拖曳選擇要辨識的範圍")
            else:
                self.selection_var.set("拖曳框選譜面範圍")
        self.redraw()
        self._refresh_progress()
        self._refresh_candidates()

    def _profile_changed(self, _event=None) -> None:
        if self.current_asset is None:
            return
        self.current_asset["notation_profile"] = LABEL_TO_PROFILE.get(self.profile_var.get(), "auto")
        self.app._mark_dirty()
        self.app._commit_history()
        self.refresh(select_asset_id=str(self.current_asset["asset_id"]))

    def _canvas_resized(self, _event=None) -> None:
        if self.zoom_var.get() == "適合視窗" and self.source_image is not None:
            self.redraw()

    def _requested_scale(self) -> float:
        if self.source_image is None:
            return 1.0
        value = self.zoom_var.get()
        if value.endswith("%"):
            return max(0.1, float(value[:-1]) / 100.0)
        available_width = max(100, self.canvas.winfo_width() - 20)
        available_height = max(100, self.canvas.winfo_height() - 20)
        return min(
            1.0,
            available_width / self.source_image.width,
            available_height / self.source_image.height,
        )

    def redraw(self) -> None:
        if self.source_image is None:
            return
        self.image_scale = self._requested_scale()
        size = (
            max(1, round(self.source_image.width * self.image_scale)),
            max(1, round(self.source_image.height * self.image_scale)),
        )
        self.display_image = self.source_image.resize(size, Image.Resampling.LANCZOS)
        self.photo_image = ImageTk.PhotoImage(self.display_image)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, image=self.photo_image, anchor="nw", tags=("page",))
        self.canvas.configure(scrollregion=(0, 0, size[0], size[1]))
        self.selection_item = None
        self.overlay_items = []
        self.candidate_items = {}
        self._draw_segment_overlays()
        self._draw_candidate_overlays()
        self._draw_active_selection()

    def _current_segment_source_refs(self, asset_id: object | None = None) -> list[dict]:
        """Return source regions belonging to the current Segment and page."""
        if asset_id is None and self.current_asset is not None:
            asset_id = self.current_asset.get("asset_id")
        return [
            source_ref
            for source_ref in self.app.current_segment.get("source_refs", [])
            if isinstance(source_ref, dict) and source_ref.get("asset_id") == asset_id
        ]

    def _draw_active_selection(self) -> None:
        if (
            self.selection_bbox is None
            or self.display_image is None
            or self.source_image is None
        ):
            return
        try:
            x1 = float(self.selection_bbox["x"]) * self.display_image.width
            y1 = float(self.selection_bbox["y"]) * self.display_image.height
            x2 = x1 + float(self.selection_bbox["width"]) * self.display_image.width
            y2 = y1 + float(self.selection_bbox["height"]) * self.display_image.height
        except (KeyError, TypeError, ValueError):
            self.selection_bbox = None
            return
        self.selection_item = self.canvas.create_rectangle(
            x1, y1, x2, y2, outline="#C05CFF", width=3
        )

    def _draw_segment_overlays(self) -> None:
        if self.current_asset is None or self.display_image is None:
            return
        asset_id = self.current_asset.get("asset_id")
        current_segment_id = self.app.current_segment.get("segment_id")
        for segment in self.app.project.get("segments", []):
            for source_ref in segment.get("source_refs", []):
                if source_ref.get("asset_id") != asset_id:
                    continue
                bbox = source_ref.get("bbox") or {}
                try:
                    x1 = float(bbox["x"]) * self.display_image.width
                    y1 = float(bbox["y"]) * self.display_image.height
                    x2 = x1 + float(bbox["width"]) * self.display_image.width
                    y2 = y1 + float(bbox["height"]) * self.display_image.height
                except (KeyError, TypeError, ValueError):
                    continue
                item = self.canvas.create_rectangle(
                    x1,
                    y1,
                    x2,
                    y2,
                    outline=(
                        "#38A3DB"
                        if segment.get("segment_id") == current_segment_id
                        else "#8A8A8A"
                    ),
                    width=2,
                    dash=(6, 3),
                )
                self.overlay_items.append(item)

    def update_provider_status(self) -> None:
        """Refresh a key-safe status label; never display credential contents."""
        try:
            status = self.app.openai_credential_store.get_status()
            settings = self.app.openai_settings_store.load()
            self.provider_status_var.set(
                f"OpenAI：{credential_status_text(status)}\n模型：{settings['model']}・detail={settings['image_detail']}"
            )
        except Exception as error:
            # This label is informational. The settings dialog provides the
            # actionable error without exposing backend exception details.
            self.provider_status_var.set(f"OpenAI 設定尚不可用：{type(error).__name__}")

    def _runs_for_current_asset(self) -> list[dict]:
        if self.current_asset is None:
            return []
        asset_id = self.current_asset.get("asset_id")
        segment_id = self.app.current_segment.get("segment_id")
        return [
            run
            for run in self.app.project.get("recognition_runs", [])
            if isinstance(run, dict)
            and run.get("asset_id") == asset_id
            and run.get("segment_id") == segment_id
        ]

    def _visible_run(self) -> dict | None:
        runs = self._runs_for_current_asset()
        for run in reversed(runs):
            if run.get("candidates") and run.get("status") not in {
                "failed",
                "stale",
                "superseded",
            }:
                return run
        return runs[-1] if runs else None

    @staticmethod
    def _run_status_label(status: object) -> str:
        return {
            "pending": "待處理",
            "running": "辨識中",
            "interrupted": "已中斷",
            "ready_for_review": "待複核",
            "reviewed": "已複核",
            "completed_empty": "成功，但無候選",
            "failed": "失敗",
            "stale": "範圍已變更",
            "superseded": "已由新工作取代",
        }.get(str(status), str(status or "待處理"))

    @staticmethod
    def _run_error_text(run: dict) -> str:
        error = run.get("error")
        if not isinstance(error, dict):
            return ""
        message = str(error.get("message") or "").strip()
        error_type = str(error.get("type") or "").strip()
        if message and error_type and error_type not in message:
            return f"{error_type}：{message}"
        return message or error_type

    @classmethod
    def _recognition_error_advice(cls, run: dict) -> str:
        error_text = cls._run_error_text(run)
        if "timeout" in error_text.casefold():
            return (
                "這個圖片區域辨識逾時；不代表 API key 或整批失敗。"
                "批次會繼續其他區域，稍後再按批次辨識會略過已成功區域。"
            )
        return "請檢查 API key、模型名稱、網路與 API 額度後再試一次。"

    def _candidate_runs_for_current_asset(self) -> list[dict]:
        return [
            run
            for run in self._runs_for_current_asset()
            if run.get("candidates")
            and self._run_is_reviewable(run)
        ]

    def _run_is_reviewable(self, run: dict) -> bool:
        """Reject obsolete evidence without breaking older fingerprint-less projects."""

        if run.get("status") in {"failed", "stale", "superseded"}:
            return False
        if isinstance(run.get("target_fingerprint"), str):
            return self._run_target_is_current(run)
        return True

    @staticmethod
    def _format_skipped_reasons(skipped: object, *, limit: int = 6) -> str:
        if not isinstance(skipped, list):
            return ""
        counts = Counter(
            str(item.get("reason"))
            for item in skipped
            if isinstance(item, dict) and item.get("reason")
        )
        if not counts:
            return ""
        lines = [
            f"{count} 筆：{reason}" if count > 1 else reason
            for reason, count in counts.most_common(limit)
        ]
        remaining = len(counts) - len(lines)
        if remaining > 0:
            lines.append(f"另有 {remaining} 種原因")
        return "\n・" + "\n・".join(lines)

    @staticmethod
    def _candidate_evidence(candidate: dict) -> str:
        evidence = candidate.get("source_evidence")
        if isinstance(evidence, dict):
            observed = evidence.get("observed_text")
            if isinstance(observed, str) and observed.strip():
                return observed.strip()
        data = candidate.get("data")
        if isinstance(data, dict):
            for key in ("raw_symbol", "text", "note"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            instrument_id = data.get("instrument_id")
            stroke_id = data.get("stroke_id")
            if instrument_id or stroke_id:
                return f"{instrument_id or 'unknown'}／{stroke_id or 'unknown'}"
        return "（未提供文字證據）"

    @staticmethod
    def _review_label(status: object) -> str:
        return {
            "pending": "待確認",
            "accepted": "已接受",
            "corrected": "已更正",
            "rejected": "已拒絕",
        }.get(str(status), str(status or "待確認"))

    def _clear_candidate_tree(self) -> None:
        if not hasattr(self, "candidate_tree"):
            return
        self.candidate_tree.delete(*self.candidate_tree.get_children())
        self.candidate_lookup = {}
        self.selected_candidate_id = None
        self.candidate_summary_var.set("尚無 AI 辨識候選")

    def _mapping_suggestion(self, run: dict, candidate: dict) -> dict | None:
        """Return a display-only suggestion; never review a candidate here."""

        if candidate.get("review_status", "pending") != "pending":
            return None
        profile = run_notation_profile(run)
        if profile is None or getattr(self.app, "symbol_mapping_load_error", None):
            return None
        try:
            return mapping_suggestion_for_candidate(
                candidate,
                notation_profile=profile,
                mappings=self.app.symbol_mappings,
                library=self.app.instrument_library,
            )
        except SymbolMappingError:
            # A damaged or stale global preference must not make project
            # candidates inaccessible.  The settings dialog reports the
            # actionable error.
            return None

    def _mapping_suggestion_label(self, suggestion: dict | None) -> str:
        if suggestion is None:
            return "—"
        instrument_id = str(suggestion.get("instrument_id") or "")
        stroke_id = str(suggestion.get("stroke_id") or "")
        instrument = get_instrument(self.app.instrument_library, instrument_id)
        stroke = get_stroke(self.app.instrument_library, instrument_id, stroke_id)
        if instrument is None or stroke is None:
            return "—"
        return f"{instrument.get('name', instrument_id)}／{stroke.get('name', stroke_id)}"

    @staticmethod
    def _candidate_source_key(run: dict, candidate: dict) -> str:
        return (
            f"{str(run.get('run_id') or 'run')}:"
            f"{str(candidate.get('candidate_id') or 'candidate')}"
        )

    def _candidate_is_materialized(self, run: dict, candidate: dict) -> bool:
        """Prevent review edits after an event/marker was copied into a Take."""

        segment = self._segment_by_id(run.get("segment_id")) or self.app.current_segment
        target = self._candidate_source_key(run, candidate)
        for track in segment.get("tracks", []):
            if not isinstance(track, dict):
                continue
            for take in track.get("takes", []):
                if not isinstance(take, dict):
                    continue
                if any(
                    isinstance(event, dict)
                    and event.get("source_candidate_key") == target
                    for event in take.get("events", [])
                ):
                    return True
        navigation = segment.get("navigation")
        if isinstance(navigation, dict) and any(
            isinstance(marker, dict)
            and marker.get("source_candidate_key") == target
            for marker in navigation.get("markers", [])
        ):
            return True
        return False

    def _warn_materialized_candidate(self) -> None:
        messagebox.showwarning(
            "候選已套用",
            "這筆候選已經建立成正式節奏或反覆記號，現在改候選會讓兩份資料不一致。\n\n"
            "請直接在格狀標註／段落資料中修改已建立的內容。",
        )

    def _refresh_candidates(self) -> None:
        self._clear_candidate_tree()
        runs = self._runs_for_current_asset()
        if not runs:
            return
        candidate_runs = self._candidate_runs_for_current_asset()
        if not candidate_runs:
            latest = runs[-1]
            model = latest.get("model") or latest.get("engine") or "AI"
            error_text = self._run_error_text(latest)
            error_suffix = f"\n失敗原因：{error_text}" if error_text else ""
            advice_suffix = (
                f"\n{self._recognition_error_advice(latest)}"
                if latest.get("status") in {"failed", "stale"}
                else ""
            )
            self.candidate_summary_var.set(
                f"{model}｜尚無候選｜工作狀態："
                f"{self._run_status_label(latest.get('status', 'pending'))}"
                f"{self._format_run_usage(latest)}"
                f"{error_suffix}{advice_suffix}"
            )
            return
        total_candidates = sum(
            len([item for item in run.get("candidates", []) if isinstance(item, dict)])
            for run in candidate_runs
        )
        pending = sum(
            candidate.get("review_status", "pending") == "pending"
            for run in candidate_runs
            for candidate in run.get("candidates", [])
            if isinstance(candidate, dict)
        )
        models = {
            str(run.get("model") or run.get("engine") or "AI")
            for run in candidate_runs
        }
        self.candidate_summary_var.set(
            f"{'／'.join(sorted(models))}｜{len(candidate_runs)} 個區域｜"
            f"{total_candidates} 個候選｜待確認 {pending}"
        )
        if getattr(self.app, "symbol_mapping_load_error", None):
            self.candidate_summary_var.set(
                self.candidate_summary_var.get()
                + "\n跨專案辨識規則目前無法讀取；候選仍可照常人工複核。"
            )
        latest = runs[-1]
        if latest not in candidate_runs and latest.get("status") in {"failed", "stale"}:
            error_text = self._run_error_text(latest)
            suffix = f"：{error_text}" if error_text else ""
            self.candidate_summary_var.set(
                self.candidate_summary_var.get()
                + f"\n最新一次工作{self._run_status_label(latest.get('status'))}{suffix}"
                + f"\n{self._recognition_error_advice(latest)}"
            )
        for run in candidate_runs:
            for index, candidate in enumerate(run.get("candidates", [])):
                if not isinstance(candidate, dict):
                    continue
                candidate_id = str(candidate.get("candidate_id") or f"candidate-{index + 1}")
                tree_id = f"{run.get('run_id', 'run')}:{index}:{candidate_id}"
                self.candidate_lookup[tree_id] = (run, candidate)
                confidence = candidate.get("confidence")
                confidence_text = (
                    f"{float(confidence) * 100:.0f}%"
                    if isinstance(confidence, (int, float))
                    else "—"
                )
                context = run.get("segment_context")
                sequence = (
                    context.get("region_sequence")
                    if isinstance(context, dict)
                    else None
                )
                evidence = self._candidate_evidence(candidate)
                suggestion = self._mapping_suggestion(run, candidate)
                if sequence is not None:
                    evidence = f"區域 {sequence}｜{evidence}"
                self.candidate_tree.insert(
                    "",
                    "end",
                    iid=tree_id,
                    values=(
                        candidate.get("kind", "other"),
                        evidence,
                        self._mapping_suggestion_label(suggestion),
                        confidence_text,
                        self._review_label(candidate.get("review_status")),
                    ),
                )

    def _draw_candidate_overlays(self) -> None:
        if self.display_image is None:
            return
        runs = self._candidate_runs_for_current_asset()
        if not runs:
            return
        colors = {
            "pending": "#F0A43B",
            "accepted": "#42A66B",
            "corrected": "#38A3DB",
            "rejected": "#888888",
        }
        for run in runs:
            for index, candidate in enumerate(run.get("candidates", [])):
                if not isinstance(candidate, dict):
                    continue
                bbox = candidate.get("bbox")
                if not isinstance(bbox, dict):
                    continue
                try:
                    x1 = float(bbox["x"]) * self.display_image.width
                    y1 = float(bbox["y"]) * self.display_image.height
                    x2 = x1 + float(bbox["width"]) * self.display_image.width
                    y2 = y1 + float(bbox["height"]) * self.display_image.height
                except (KeyError, TypeError, ValueError):
                    continue
                candidate_id = str(candidate.get("candidate_id") or f"candidate-{index + 1}")
                tree_id = f"{run.get('run_id', 'run')}:{index}:{candidate_id}"
                selected = tree_id == self.selected_candidate_id
                status = str(candidate.get("review_status") or "pending")
                item = self.canvas.create_rectangle(
                    x1,
                    y1,
                    x2,
                    y2,
                    outline=colors.get(status, "#F0A43B"),
                    width=4 if selected else 2,
                    dash=() if selected else (4, 2),
                )
                self.candidate_items[tree_id] = item

    def _candidate_selected(self, _event=None) -> None:
        selected = self.candidate_tree.selection()
        if not selected:
            return
        self.selected_candidate_id = str(selected[0])
        self.redraw()
        pair = self.candidate_lookup.get(self.selected_candidate_id)
        if pair is None or self.display_image is None:
            return
        _run, candidate = pair
        bbox = candidate.get("bbox")
        if not isinstance(bbox, dict):
            return
        try:
            center_x = (float(bbox["x"]) + float(bbox["width"]) / 2) * self.display_image.width
            center_y = (float(bbox["y"]) + float(bbox["height"]) / 2) * self.display_image.height
        except (KeyError, TypeError, ValueError):
            return
        view_width = max(1, self.canvas.winfo_width())
        view_height = max(1, self.canvas.winfo_height())
        if self.display_image.width > view_width:
            self.canvas.xview_moveto(max(0.0, min(1.0, (center_x - view_width / 2) / self.display_image.width)))
        if self.display_image.height > view_height:
            self.canvas.yview_moveto(max(0.0, min(1.0, (center_y - view_height / 2) / self.display_image.height)))

    def _review_selected_candidate(self, decision: str) -> None:
        selected = self.candidate_tree.selection()
        pair = self.candidate_lookup.get(str(selected[0])) if selected else None
        if pair is None:
            messagebox.showinfo("尚未選擇", "請先在候選清單選擇一項。")
            return
        run, candidate = pair
        if not self._run_is_reviewable(run):
            self._refresh_progress()
            self._refresh_candidates()
            self.redraw()
            messagebox.showwarning(
                "候選已過期",
                "這筆候選所屬的框選範圍已變更，因此不能再接受或拒絕。"
                "請使用目前範圍重新辨識。",
            )
            return
        if self._candidate_is_materialized(run, candidate):
            self._warn_materialized_candidate()
            return
        candidates = list(run.get("candidates", []))
        try:
            index = next(
                index
                for index, item in enumerate(candidates)
                if item is candidate
                or (
                    isinstance(item, dict)
                    and item.get("candidate_id") == candidate.get("candidate_id")
                )
            )
            candidates[index] = review_candidate(candidate, decision)
        except (StopIteration, TypeError, ValueError) as error:
            messagebox.showerror("無法複核候選", str(error))
            return
        updated = update_recognition_run(run, candidates)
        run.clear()
        run.update(updated)
        self.app._mark_dirty()
        self.app.save()
        self._refresh_progress()
        self._refresh_candidates()
        self.redraw()

    def _accept_selected_mapping_suggestion(self) -> None:
        """Explicitly turn one displayed global-rule suggestion into a correction."""

        selected = self.candidate_tree.selection()
        pair = self.candidate_lookup.get(str(selected[0])) if selected else None
        if pair is None:
            messagebox.showinfo("尚未選擇", "請先在候選清單選擇一項。")
            return
        run, candidate = pair
        if not self._run_is_reviewable(run):
            messagebox.showwarning(
                "候選已過期",
                "這筆候選所屬的框選範圍已變更，請使用目前範圍重新辨識。",
            )
            return
        if self._candidate_is_materialized(run, candidate):
            self._warn_materialized_candidate()
            return
        if candidate.get("review_status", "pending") != "pending":
            messagebox.showinfo(
                "候選已複核",
                "規則建議只會套用到尚待確認的候選。若要修改，請使用「更正／對應」。",
            )
            return
        profile = run_notation_profile(run)
        suggestion = self._mapping_suggestion(run, candidate)
        if profile is None or suggestion is None:
            messagebox.showinfo(
                "沒有可套用的規則建議",
                "這筆候選目前沒有符合「譜種＋原始樂器＋符號」的已啟用規則。\n"
                "你仍可按「更正／對應」做單次人工修正。",
            )
            return
        try:
            corrected = apply_mapping_rule_to_candidate(
                candidate,
                notation_profile=profile,
                mappings=self.app.symbol_mappings,
                library=self.app.instrument_library,
                note="使用者明確套用跨專案辨識建議",
            )
            if corrected.get("review_status") != "corrected":
                raise SymbolMappingError("規則建議已不再符合這筆候選")
            candidates = list(run.get("candidates", []))
            index = next(
                index
                for index, item in enumerate(candidates)
                if item is candidate
                or (
                    isinstance(item, dict)
                    and item.get("candidate_id") == candidate.get("candidate_id")
                )
            )
            candidates[index] = corrected
        except (StopIteration, TypeError, ValueError, SymbolMappingError) as error:
            messagebox.showerror("無法套用規則建議", str(error))
            return
        updated = update_recognition_run(run, candidates)
        run.clear()
        run.update(updated)
        self.app._mark_dirty()
        self.app.save()
        self._refresh_progress()
        self._refresh_candidates()
        self.redraw()
        self.app.status_var.set("已套用一筆辨識規則建議；原始 AI 結果仍保留供稽核")

    def _correct_selected_candidate(self) -> None:
        """Map one event candidate, optionally including the same visual symbol."""

        selected = self.candidate_tree.selection()
        pair = self.candidate_lookup.get(str(selected[0])) if selected else None
        if pair is None:
            messagebox.showinfo("尚未選擇", "請先在候選清單選擇一項。")
            return
        run, candidate = pair
        if not self._run_is_reviewable(run):
            self._refresh_progress()
            self._refresh_candidates()
            self.redraw()
            messagebox.showwarning(
                "候選已過期",
                "這筆候選所屬的框選範圍已變更，請使用目前範圍重新辨識。",
            )
            return
        if self._candidate_is_materialized(run, candidate):
            self._warn_materialized_candidate()
            return
        if candidate.get("kind") != "event":
            messagebox.showinfo(
                "這不是敲擊事件",
                "樂器／擊法對應只適用於 event 候選；反覆記號請直接接受或拒絕。",
            )
            return
        profile = run_notation_profile(run)
        if is_numbered_rest_candidate(candidate, profile):
            if messagebox.askyesno(
                "0 是休止符",
                "數字譜的 0 代表休止，不應對應成任何樂器敲擊。\n\n"
                "要把這筆候選標記為「已拒絕」嗎？",
            ):
                self._review_selected_candidate("rejected")
            return

        runs = self._candidate_runs_for_current_asset()
        matching_count = matching_event_candidate_count(
            runs,
            candidate,
            target_run_id=str(run.get("run_id") or ""),
        )
        dialog = CandidateMappingDialog(
            self,
            candidate,
            self.app.instrument_library,
            matching_count=matching_count,
            notation_profile=profile,
        )
        self.wait_window(dialog)
        result = dialog.result
        if result is None:
            return
        symbol = candidate_visible_symbol(candidate) or "未命名符號"
        try:
            updated_runs, corrected_count = correct_event_candidates(
                runs,
                target_run_id=str(run.get("run_id") or ""),
                target_candidate_id=str(candidate.get("candidate_id") or ""),
                instrument_id=str(result["instrument_id"]),
                stroke_id=str(result["stroke_id"]),
                apply_same_symbol=bool(result.get("apply_same_symbol")),
                note=f"人工對應辨識符號「{symbol}」",
            )
        except (KeyError, TypeError, ValueError) as error:
            messagebox.showerror("無法套用更正", str(error))
            return
        rule_saved = False
        if result.get("save_rule"):
            if getattr(self.app, "symbol_mapping_load_error", None):
                messagebox.showerror(
                    "無法保存辨識規則",
                    "原有的跨專案辨識規則檔目前無法讀取；為避免覆蓋舊資料，"
                    "這次尚未套用更正。請先到「設定 → 辨識符號對應規則」處理。",
                )
                return
            observed_symbol = candidate_observed_symbol(candidate)
            source_instrument_id = candidate_original_instrument_id(candidate)
            if profile is None or observed_symbol is None or source_instrument_id is None:
                messagebox.showerror(
                    "無法保存辨識規則",
                    "這筆候選缺少明確的譜種、原始樂器或辨識符號。",
                )
                return
            next_mappings = deepcopy(self.app.symbol_mappings)
            try:
                upsert_mapping_rule(
                    next_mappings,
                    self.app.instrument_library,
                    notation_profile=profile,
                    instrument_id=source_instrument_id,
                    observed_symbol=observed_symbol,
                    stroke_id=str(result["stroke_id"]),
                )
                self.app.symbol_mapping_store.save(
                    next_mappings, self.app.instrument_library
                )
            except (KeyError, TypeError, ValueError, SymbolMappingError) as error:
                messagebox.showerror("無法保存辨識規則", str(error))
                return
            self.app.symbol_mappings = next_mappings
            self.app.symbol_mapping_load_error = None
            rule_saved = True
        for existing, updated in zip(runs, updated_runs):
            existing.clear()
            existing.update(updated)
        self.app._mark_dirty()
        self.app.save()
        self._refresh_progress()
        self._refresh_candidates()
        self.redraw()
        self.app.status_var.set(
            f"已更正 {corrected_count} 筆「{symbol}」候選"
            + ("，並保存為跨專案建議規則" if rule_saved else "（只套用這次）")
        )

    def _accept_all_pending_candidates(self) -> None:
        runs = self._candidate_runs_for_current_asset()
        if not runs:
            messagebox.showinfo("尚無候選", "目前照片與段落還沒有辨識候選。")
            return
        pending_count = sum(
            candidate.get("review_status", "pending") == "pending"
            for run in runs
            for candidate in run.get("candidates", [])
            if isinstance(candidate, dict)
        )
        if pending_count == 0:
            messagebox.showinfo("沒有待確認候選", "這一批候選都已經複核。")
            return
        suggestion_count = sum(
            self._mapping_suggestion(run, candidate) is not None
            for run in runs
            for candidate in run.get("candidates", [])
            if isinstance(candidate, dict)
            and candidate.get("review_status", "pending") == "pending"
        )
        suggestion_warning = (
            f"\n\n其中 {suggestion_count} 筆有跨專案規則建議；"
            "「接受全部」會保留 AI 原結果，不會自動套用建議。"
            if suggestion_count
            else ""
        )
        if not messagebox.askyesno(
            "接受全部待確認候選",
            f"將接受目前照片、目前段落中 {len(runs)} 個區域的 "
            f"{pending_count} 個待確認候選。\n\n"
            "請只在已經對照原譜、確認這些區域可信時使用；之後仍可拒絕個別候選。"
            + suggestion_warning
            + "\n\n要繼續嗎？",
        ):
            return
        for run in runs:
            reviewed = [
                review_candidate(candidate, "accepted")
                if isinstance(candidate, dict)
                and candidate.get("review_status", "pending") == "pending"
                else candidate
                for candidate in run.get("candidates", [])
            ]
            updated = update_recognition_run(run, reviewed)
            run.clear()
            run.update(updated)
        self.app._mark_dirty()
        self.app.save()
        self._refresh_progress()
        self._refresh_candidates()
        self.redraw()

    def apply_accepted_candidates(self) -> None:
        """Preview and materialize accepted candidates into a separate AI Take."""
        plan = build_application_plan(
            self.app.current_segment,
            self.app.project.get("recognition_runs", []),
            self.app.instrument_library,
        )
        event_count = int(plan.get("event_count", 0))
        repeat_count = int(plan.get("repeat_count", 0))
        skipped = plan.get("skipped", [])
        if event_count == 0 and repeat_count == 0:
            details = self._format_skipped_reasons(skipped)
            if details:
                details = "\n\n原因：" + details
            messagebox.showinfo(
                "沒有可套用資料",
                "請先接受正確的 event／repeat 候選。若樂器、擊法或小節仍不明，"
                "請拒絕該候選，再到格狀標註人工補入。\n"
                "數字譜的 0 是休止，會保留為空格，不需要建立敲擊事件。"
                + details,
            )
            return
        instruments = "、".join(plan.get("instruments", [])) or "—"
        skipped_count = len(skipped) if isinstance(skipped, list) else 0
        if not messagebox.askyesno(
            "預覽套用 AI 候選",
            f"目前段落：{self.app.current_segment.get('segment_id')}\n"
            f"可建立節奏事件：{event_count}\n"
            f"可建立反覆記號：{repeat_count}\n"
            f"涉及樂器：{instruments}\n"
            f"略過／待修正：{skipped_count}\n\n"
            "節奏會寫入獨立的 AI 辨識 Take；既有人工 Take 不會被刪除或覆蓋。要套用嗎？",
        ):
            return
        try:
            result = apply_application_plan(
                self.app.project,
                self.app.current_segment,
                plan,
                self.app.instrument_library,
            )
        except CandidateApplicationError as error:
            messagebox.showerror("無法套用候選", str(error))
            return
        self.app._mark_dirty()
        self.app._commit_history()
        self.app.save()
        self.app.refresh_all()
        self.app.notebook.select(self.app.grid_tab)
        conflict_count = len(result.get("conflicts", []))
        messagebox.showinfo(
            "已套用至格狀標註",
            f"新增事件：{result.get('applied_events', 0)}\n"
            f"新增反覆記號：{result.get('applied_repeats', 0)}\n"
            f"新增樂器軌道：{result.get('created_tracks', 0)}\n"
            f"需要人工處理的同格衝突：{conflict_count}\n\n"
            "現在已切換到格狀標註；請播放並核對原譜。",
        )

    def _image_point(self, event) -> tuple[float, float] | None:
        if self.display_image is None:
            return None
        x = min(max(0.0, self.canvas.canvasx(event.x)), float(self.display_image.width))
        y = min(max(0.0, self.canvas.canvasy(event.y)), float(self.display_image.height))
        return x, y

    def _drag_begin(self, event) -> None:
        point = self._image_point(event)
        if point is None:
            return
        self.drag_start = point
        if self.selection_item is not None:
            self.canvas.delete(self.selection_item)
        self.selection_item = self.canvas.create_rectangle(
            point[0], point[1], point[0], point[1], outline="#C05CFF", width=3
        )

    def _drag_move(self, event) -> None:
        point = self._image_point(event)
        if point is None or self.drag_start is None or self.selection_item is None:
            return
        self.canvas.coords(self.selection_item, self.drag_start[0], self.drag_start[1], point[0], point[1])

    def _drag_end(self, event) -> None:
        point = self._image_point(event)
        if point is None or self.drag_start is None or self.display_image is None:
            return
        x1, x2 = sorted((self.drag_start[0], point[0]))
        y1, y2 = sorted((self.drag_start[1], point[1]))
        self.drag_start = None
        if x2 - x1 < 4 or y2 - y1 < 4:
            self.selection_bbox = None
            self.selection_var.set("框選範圍太小，請重新拖曳")
            return
        self.selection_bbox = {
            "x": x1 / self.display_image.width,
            "y": y1 / self.display_image.height,
            "width": (x2 - x1) / self.display_image.width,
            "height": (y2 - y1) / self.display_image.height,
        }
        self.selection_var.set(
            "已框選："
            f"x={self.selection_bbox['x']:.3f}, y={self.selection_bbox['y']:.3f}\n"
            f"寬={self.selection_bbox['width']:.3f}, 高={self.selection_bbox['height']:.3f}"
        )

    def link_selection_to_segment(self) -> None:
        if self.current_asset is None or self.selection_bbox is None:
            messagebox.showinfo("尚未框選", "請先在照片上拖曳框選一段譜面。")
            return
        if self._recognition_active is not None or self._recognition_batch is not None:
            messagebox.showinfo(
                "辨識進行中",
                "請等目前的 OpenAI 辨識結束後，再修改段落範圍。",
            )
            return
        existing_refs = self._current_segment_source_refs()
        exact = self._source_ref_for_bbox(self.selection_bbox, existing_refs)
        if exact is not None:
            self.app.status_var.set(
                f"此紫色框已連結到 {self.app.current_segment['segment_id']}"
                f"（{exact['region_id']}），可直接辨識。"
            )
            return

        replacement: dict | None = None
        previous_ref: dict | None = None
        previous_runs: list[dict] | None = None
        if existing_refs:
            replacement = max(
                existing_refs,
                key=lambda item: self._bbox_iou(item.get("bbox"), self.selection_bbox),
            )
            affected_run_ids = {
                str(run.get("run_id"))
                for run in self.app.project.get("recognition_runs", [])
                if isinstance(run, dict)
                and run.get("segment_id") == self.app.current_segment.get("segment_id")
                and run.get("region_id") == replacement.get("region_id")
                and run.get("run_id")
            }
            applied_count = sum(
                1
                for track in self.app.current_segment.get("tracks", [])
                if isinstance(track, dict)
                for take in track.get("takes", [])
                if isinstance(take, dict)
                for event in take.get("events", [])
                if isinstance(event, dict)
                and str(event.get("source_run_id")) in affected_run_ids
            )
            applied_warning = (
                f"\n\n注意：舊結果已有 {applied_count} 個事件寫入 AI Take；"
                "更新框線不會自動刪除那些事件，請到格狀標註人工核對。"
                if applied_count
                else ""
            )
            if not messagebox.askyesno(
                "更新段落範圍",
                f"目前段落在這張照片已有 {len(existing_refs)} 個連結範圍。\n\n"
                f"要用新紫框取代最相近的範圍 {replacement.get('region_id')} 嗎？\n"
                "舊辨識候選會標記為過期。\n\n"
                "若這其實是另一段小節，請回到「整份樂譜規劃」拆分段落；"
                "這裡不會建立小節範圍不明的第二個框。"
                + applied_warning,
            ):
                return
            previous_ref = deepcopy(replacement)
            previous_runs = deepcopy(self.app.project.get("recognition_runs", []))
            source_ref = self._replace_source_ref_bbox(
                replacement, self.selection_bbox
            )
            created = False
        else:
            source_ref, created = self._ensure_source_ref(self.selection_bbox)

        self.app._mark_dirty()
        self.app._commit_history()
        if not self.app.save():
            if created:
                refs = self.app.current_segment.get("source_refs", [])
                if isinstance(refs, list) and source_ref in refs:
                    refs.remove(source_ref)
            elif replacement is not None and previous_ref is not None:
                replacement.clear()
                replacement.update(previous_ref)
                if previous_runs is not None:
                    self.app.project["recognition_runs"] = previous_runs
            messagebox.showerror(
                "無法儲存範圍",
                "新範圍尚未成功寫入專案，請先儲存專案後再辦識。",
            )
            return
        self._refresh_progress()
        self._refresh_candidates()
        self.redraw()
        self.app.status_var.set(
            (
                f"已把新範圍連結到 {self.app.current_segment['segment_id']}"
                if created
                else f"已更新 {self.app.current_segment['segment_id']} 的連結範圍；"
                "舊辨識結果已標記為過期。"
            )
        )

    @staticmethod
    def _same_bbox(left: object, right: object, *, tolerance: float = 1e-7) -> bool:
        if not isinstance(left, dict) or not isinstance(right, dict):
            return False
        try:
            return all(
                abs(float(left[key]) - float(right[key])) <= tolerance
                for key in ("x", "y", "width", "height")
            )
        except (KeyError, TypeError, ValueError):
            return False

    @staticmethod
    def _bbox_iou(left: object, right: object) -> float:
        if not isinstance(left, dict) or not isinstance(right, dict):
            return 0.0
        try:
            left_x1 = float(left["x"])
            left_y1 = float(left["y"])
            left_x2 = left_x1 + float(left["width"])
            left_y2 = left_y1 + float(left["height"])
            right_x1 = float(right["x"])
            right_y1 = float(right["y"])
            right_x2 = right_x1 + float(right["width"])
            right_y2 = right_y1 + float(right["height"])
        except (KeyError, TypeError, ValueError):
            return 0.0
        intersection = max(0.0, min(left_x2, right_x2) - max(left_x1, right_x1)) * max(
            0.0, min(left_y2, right_y2) - max(left_y1, right_y1)
        )
        union = (
            max(0.0, left_x2 - left_x1) * max(0.0, left_y2 - left_y1)
            + max(0.0, right_x2 - right_x1) * max(0.0, right_y2 - right_y1)
            - intersection
        )
        return intersection / union if union > 0 else 0.0

    def _source_ref_for_bbox(
        self, bbox: dict, source_refs: list[dict] | None = None
    ) -> dict | None:
        refs = (
            self._current_segment_source_refs()
            if source_refs is None
            else source_refs
        )
        return next(
            (
                source_ref
                for source_ref in refs
                if self._same_bbox(source_ref.get("bbox"), bbox)
            ),
            None,
        )

    def _replace_source_ref_bbox(self, source_ref: dict, bbox: dict) -> dict:
        """Replace one linked crop and invalidate only its historical AI runs."""

        source_ref["bbox"] = dict(bbox)
        source_ref["origin"] = "manual"
        source_ref["review_status"] = "confirmed"
        source_ref["updated_at"] = _now_iso()
        # A hand-drawn replacement is already the full intended crop.  The old
        # system context belonged to the previous blue frame and must not be
        # sent as a second, now-misleading image.
        source_ref.pop("context_bbox", None)
        source_ref.pop("system_id", None)
        segment_id = self.app.current_segment.get("segment_id")
        region_id = source_ref.get("region_id")
        for run in self.app.project.get("recognition_runs", []):
            if (
                isinstance(run, dict)
                and run.get("segment_id") == segment_id
                and run.get("region_id") == region_id
                and run.get("status") != "superseded"
            ):
                run["status"] = "stale"
                run["error"] = {
                    "type": "SourceRegionChanged",
                    "message": "段落來源框已由使用者更新，這筆舊辨識結果不再套用",
                }
                run["updated_at"] = _now_iso()
        return source_ref

    def _ensure_source_ref(self, bbox: dict) -> tuple[dict, bool]:
        """Return a stable region record shared by the Segment and AI run."""
        if self.current_asset is None:
            raise ValueError("沒有目前照片")
        segment = self.app.current_segment
        for source_ref in segment.setdefault("source_refs", []):
            if (
                isinstance(source_ref, dict)
                and source_ref.get("asset_id") == self.current_asset.get("asset_id")
                and self._same_bbox(source_ref.get("bbox"), bbox)
            ):
                return source_ref, False
        source_ref = {
            "region_id": f"REG-{uuid4().hex[:16]}",
            "asset_id": self.current_asset["asset_id"],
            "bbox": dict(bbox),
            "source_start_bar": segment.get("source_start_bar", ""),
            "source_end_bar": segment.get("source_end_bar", ""),
            "sequence": len(segment.get("source_refs", [])) + 1,
            "origin": "manual",
            "review_status": "confirmed",
            "created_at": _now_iso(),
        }
        segment.setdefault("source_refs", []).append(source_ref)
        return source_ref, True

    def _set_recognition_controls_enabled(self, enabled: bool) -> None:
        state = ["!disabled"] if enabled else ["disabled"]
        self.recognize_button.state(state)
        if hasattr(self, "link_selection_button"):
            self.link_selection_button.state(state)
        if hasattr(self, "batch_recognize_button"):
            self.batch_recognize_button.state(state)
        if hasattr(self, "retry_failed_button"):
            if enabled:
                self._refresh_retry_failed_button()
            else:
                self.retry_failed_button.state(["disabled"])

    def _latest_retryable_run_ids(self) -> list[str]:
        """Return only the newest unfinished/failed run for each source target."""

        runs = self.app.project.get("recognition_runs", [])
        if not isinstance(runs, list):
            return []
        latest_by_target: dict[tuple[str, str], dict] = {}
        for run in runs:
            if not isinstance(run, dict):
                continue
            segment_id = run.get("segment_id")
            region_id = run.get("region_id")
            if not isinstance(segment_id, str) or not segment_id.strip():
                continue
            if not isinstance(region_id, str) or not region_id.strip():
                continue
            latest_by_target[(segment_id.strip(), region_id.strip())] = run
        return [
            str(run["run_id"])
            for run in latest_by_target.values()
            if str(run.get("status") or "").strip().casefold()
            in RETRYABLE_REGION_STATUSES
            and isinstance(run.get("run_id"), str)
            and str(run.get("run_id")).strip()
        ]

    def _refresh_retry_failed_button(self) -> None:
        if not hasattr(self, "retry_failed_button"):
            return
        run_ids = self._latest_retryable_run_ids()
        self.retry_failed_button.configure(
            text=(
                f"只重試失敗項目（{len(run_ids)}）"
                if run_ids
                else "只重試失敗項目"
            )
        )
        if (
            run_ids
            and self._recognition_active is None
            and self._recognition_batch is None
        ):
            self.retry_failed_button.state(["!disabled"])
        else:
            self.retry_failed_button.state(["disabled"])

    def _asset_by_id(self, asset_id: object) -> dict | None:
        return next(
            (
                asset
                for asset in self.app.project.get("assets", [])
                if isinstance(asset, dict) and asset.get("asset_id") == asset_id
            ),
            None,
        )

    def _segment_by_id(self, segment_id: object) -> dict | None:
        return next(
            (
                segment
                for segment in self.app.project.get("segments", [])
                if isinstance(segment, dict) and segment.get("segment_id") == segment_id
            ),
            None,
        )

    @staticmethod
    def _source_ref_by_id(segment: dict, region_id: object) -> dict | None:
        return next(
            (
                source_ref
                for source_ref in segment.get("source_refs", [])
                if isinstance(source_ref, dict)
                and source_ref.get("region_id") == region_id
            ),
            None,
        )

    def _prepare_recognition_run(
        self,
        segment: dict,
        asset: dict,
        source_ref: dict,
        settings: dict,
        recognition_snapshot: dict,
        *,
        batch_id: str | None = None,
    ) -> dict:
        bbox = source_ref.get("bbox")
        if not isinstance(bbox, dict):
            raise RecognitionContractError("來源區域缺少 bbox")
        run = new_recognition_run(
            str(asset["asset_id"]),
            engine="openai",
            model=str(settings["model"]),
            source_region=bbox,
        )
        run["segment_id"] = segment.get("segment_id")
        run["notation_profile"] = asset.get("notation_profile", "auto")
        run["region_id"] = source_ref.get("region_id")
        run["recognition_settings"] = {
            "model": str(settings["model"]),
            "image_detail": str(settings["image_detail"]),
        }
        run["segment_context"] = build_segment_context(segment, source_ref)
        run["provider_request"] = build_recognition_request(
            run,
            asset,
            recognition_snapshot,
            region=source_ref,
            notation_profile=run["notation_profile"],
        )
        run["target_fingerprint"] = target_fingerprint(
            segment, source_ref, asset, run["recognition_settings"]
        )
        if batch_id is not None:
            run["batch_id"] = batch_id
        return run

    def _target_records_for_run(
        self, run: dict
    ) -> tuple[dict, dict, dict] | None:
        segment = self._segment_by_id(run.get("segment_id"))
        asset = self._asset_by_id(run.get("asset_id"))
        if segment is None or asset is None:
            return None
        source_ref = self._source_ref_by_id(segment, run.get("region_id"))
        if source_ref is None:
            return None
        return segment, source_ref, asset

    def _run_target_is_current(self, run: dict) -> bool:
        records = self._target_records_for_run(run)
        settings = run.get("recognition_settings")
        expected = run.get("target_fingerprint")
        if records is None or not isinstance(settings, dict) or not isinstance(expected, str):
            return False
        segment, source_ref, asset = records
        return target_fingerprint(segment, source_ref, asset, settings) == expected

    def create_recognition_job(self) -> None:
        if self.current_asset is None:
            messagebox.showinfo("沒有照片", "請先匯入並選擇樂譜照片。")
            return
        if self._recognition_active is not None or self._recognition_batch is not None:
            messagebox.showinfo("辨識進行中", "請等目前的 OpenAI 辨識完成後再開始下一筆。")
            return
        active_bbox = self.selection_bbox
        linked = self._current_segment_source_refs()
        if active_bbox is None:
            if len(linked) == 1 and isinstance(linked[0].get("bbox"), dict):
                active_bbox = linked[0]["bbox"]
                self.selection_bbox = dict(active_bbox)
            elif len(linked) > 1:
                messagebox.showinfo(
                    "請選擇辨識範圍",
                    "目前段落在這張照片有多個已連結區域。請在照片上重新拖曳要送出的範圍。",
                )
                return
            else:
                messagebox.showinfo(
                    "尚未框選範圍",
                    "請先在照片上拖曳紫色框，再按「連結／更新目前段落範圍」。\n\n"
                    "這個按鈕不會再偷偷改送整張照片。",
                )
                return
        assert active_bbox is not None
        source_ref = self._source_ref_for_bbox(active_bbox, linked)
        if source_ref is None:
            messagebox.showinfo(
                "請先更新段落範圍",
                "紫色實線是新框選，但它尚未與目前段落連結。\n\n"
                "請先按「連結／更新目前段落範圍」，確認是取代舊範圍或新增區域。",
            )
            return
        api_key = self.app.get_openai_api_key(prompt_if_missing=True)
        if not api_key:
            self.update_provider_status()
            return
        settings = self.app.get_openai_settings()
        if settings is None:
            return
        scope_text = "目前段落的紫色框選區域"
        image_explanation = (
            "請求會送出紫色主範圍，並附上藍色整行譜表作為第二張參考圖。"
            if isinstance(source_ref.get("context_bbox"), dict)
            else "請求只會送出紫色框內的圖片。"
        )
        if not messagebox.askyesno(
            "送交 OpenAI 辨識",
            f"將把{scope_text}送至 OpenAI 模型 {settings['model']}。\n\n"
            f"{image_explanation}\n\n"
            "請求也會包含目前段落脈絡，以及樂器／擊法庫的名稱、別名與穩定 ID。\n\n"
            "圖片輸入會計入 API 用量並可能產生費用。辨識完成後請先複核候選，"
            "再用「預覽並套用已接受候選」建立獨立 AI Take。要繼續嗎？",
        ):
            return

        # Recognition may need an instrument that is defined globally but does
        # not yet have a Track in this Segment, so give the provider the full
        # stable library while keeping the project's historical snapshot intact.
        recognition_snapshot = create_project_snapshot(self.app.instrument_library)
        try:
            run = self._prepare_recognition_run(
                self.app.current_segment,
                self.current_asset,
                source_ref,
                settings,
                recognition_snapshot,
            )
        except (RecognitionContractError, TypeError) as error:
            messagebox.showerror("無法建立辨識工作", str(error))
            return
        self.app.project.setdefault("recognition_runs", []).append(run)
        self.app._mark_dirty()
        outcome = self._launch_recognition_run(
            run,
            api_key=api_key,
            scope_text=scope_text,
        )
        if outcome == "failed":
            message = run.get("error", {}).get("message", "無法開始辨識")
            messagebox.showerror("無法開始辨識", str(message))

    def retry_failed_recognition_jobs(
        self,
        run_ids: list[str] | None = None,
    ) -> None:
        """Retry only explicit failed/unfinished targets through the safe planner."""

        if self._recognition_active is not None or self._recognition_batch is not None:
            messagebox.showinfo("辨識進行中", "請等目前的 OpenAI 辨識完成後再重試。")
            return
        runs = self.app.project.get("recognition_runs", [])
        selected_ids = self._latest_retryable_run_ids() if run_ids is None else run_ids
        summary = summarize_batch_results(runs, selected_ids)
        retryable_ids = summary["retryable_run_ids"]
        retryable_set = set(retryable_ids)
        targets = {
            (
                str(run.get("segment_id")).strip(),
                str(run.get("region_id")).strip(),
            )
            for run in runs
            if isinstance(run, dict)
            and run.get("run_id") in retryable_set
            and isinstance(run.get("segment_id"), str)
            and str(run.get("segment_id")).strip()
            and isinstance(run.get("region_id"), str)
            and str(run.get("region_id")).strip()
        } if isinstance(runs, list) else set()
        if not targets:
            messagebox.showinfo(
                "沒有可重試項目",
                "目前沒有失敗或未完成的辨識工作可重試。\n\n"
                "成功但沒有候選的工作不會自動重送；請先調整來源框，再重新辨識。",
            )
            self._refresh_retry_failed_button()
            return
        self.create_batch_recognition_jobs(only_targets=targets)

    def create_batch_recognition_jobs(
        self,
        *,
        only_targets: set[tuple[str, str]] | None = None,
    ) -> None:
        """Prepare and sequentially run every confirmed, unfinished source ref."""

        if self._recognition_active is not None or self._recognition_batch is not None:
            messagebox.showinfo("辨識進行中", "請等目前的 OpenAI 辨識完成後再開始下一批。")
            return
        if not self._ensure_saved_project():
            return
        retry_only = only_targets is not None
        target_filter = set(only_targets or set())
        plan = plan_recognition_batch(self.app.project)
        planned_jobs = plan.get("jobs", [])
        if retry_only and isinstance(planned_jobs, list):
            planned_jobs = [
                descriptor
                for descriptor in planned_jobs
                if isinstance(descriptor, dict)
                and (
                    str(descriptor.get("segment_id")),
                    str(descriptor.get("region_id")),
                )
                in target_filter
            ]
        if not isinstance(planned_jobs, list) or not planned_jobs:
            if retry_only:
                messagebox.showinfo(
                    "沒有可安全重試的項目",
                    "指定的失敗工作目前沒有可送出的已確認來源範圍。\n\n"
                    "來源框可能已刪除、尚未確認，或資料已變更；請先到「整份樂譜規劃」檢查。",
                )
                self._refresh_retry_failed_button()
                return
            completed = len(plan.get("skipped_completed", []))
            unconfirmed = len(plan.get("skipped_unconfirmed", []))
            invalid = len(plan.get("invalid", []))
            messagebox.showinfo(
                "沒有待辨識區域",
                "目前沒有可送出的已確認來源區域。\n\n"
                f"已有結果：{completed}｜尚未確認：{unconfirmed}｜資料不完整：{invalid}\n"
                "請先在「整份樂譜規劃」完成頁序、小節與段落確認。",
            )
            return
        api_key = self.app.get_openai_api_key(prompt_if_missing=True)
        if not api_key:
            self.update_provider_status()
            return
        settings = self.app.get_openai_settings()
        if settings is None:
            return

        recognition_snapshot = create_project_snapshot(self.app.instrument_library)
        batch_id = f"BATCH-{uuid4().hex[:16]}"
        prepared: list[dict] = []
        preparation_errors: list[str] = []
        for descriptor in planned_jobs:
            if not isinstance(descriptor, dict):
                continue
            segment = self._segment_by_id(descriptor.get("segment_id"))
            asset = self._asset_by_id(descriptor.get("asset_id"))
            source_ref = (
                self._source_ref_by_id(segment, descriptor.get("region_id"))
                if segment is not None
                else None
            )
            if segment is None or asset is None or source_ref is None:
                preparation_errors.append(
                    f"{descriptor.get('segment_id', '未知段落')}／{descriptor.get('region_id', '未知區域')}"
                )
                continue
            try:
                prepared.append(
                    self._prepare_recognition_run(
                        segment,
                        asset,
                        source_ref,
                        settings,
                        recognition_snapshot,
                        batch_id=batch_id,
                    )
                )
            except (RecognitionContractError, TypeError, ValueError, KeyError):
                preparation_errors.append(
                    f"{descriptor.get('segment_id')}／{descriptor.get('region_id')}"
                )

        if not prepared:
            messagebox.showerror(
                "無法建立批次辨識",
                "所有待辦區域都未通過本機資料檢查，因此沒有送出任何 API 請求。",
            )
            return
        total = len(prepared)
        for position, run in enumerate(prepared, start=1):
            run["batch_position"] = position
            run["batch_total"] = total
        if retry_only:
            completed = 0
            stale = 0
            unconfirmed = 0
            invalid = max(0, len(target_filter) - len(prepared))
        else:
            completed = len(plan.get("skipped_completed", []))
            stale = len(plan.get("stale_completed", []))
            unconfirmed = len(plan.get("skipped_unconfirmed", []))
            invalid = len(plan.get("invalid", [])) + len(preparation_errors)
        segment_count = len({str(run.get("segment_id")) for run in prepared})
        if retry_only:
            confirmation_title = "只重試失敗項目"
            confirmation_text = (
                f"模型：{settings['model']}\n"
                f"準備重試：{total} 個圖片區域（跨 {segment_count} 個段落）\n"
                f"目前無法安全重試：{invalid}\n\n"
                "已成功與成功但無候選的工作不會送出。每個重試區域都是獨立 API 請求，"
                "可能產生費用；程式不會在背景偷偷重送。要開始嗎？"
            )
        else:
            confirmation_title = "批次送交 OpenAI 辨識"
            confirmation_text = (
                f"模型：{settings['model']}\n"
                f"準備送出：{total} 個圖片區域（跨 {segment_count} 個段落）\n"
                f"已有成功結果而略過：{completed}\n"
                f"舊範圍或無安全指紋，將重新辨識：{stale}\n"
                f"尚未確認而略過：{unconfirmed}\n"
                f"資料不完整而略過：{invalid}\n\n"
                "程式會一次送一個區域；每個區域都是獨立 API 請求並可能產生費用。"
                "中途關閉時，剩餘工作不會在下次啟動後自行送出。要開始嗎？"
            )
        if not messagebox.askyesno(
            confirmation_title,
            confirmation_text,
        ):
            return

        recognition_runs = self.app.project.setdefault("recognition_runs", [])
        previous_runs = deepcopy(recognition_runs)
        for new_run in prepared:
            previous = next(
                (
                    old_run
                    for old_run in reversed(recognition_runs)
                    if isinstance(old_run, dict)
                    and old_run.get("segment_id") == new_run.get("segment_id")
                    and old_run.get("region_id") == new_run.get("region_id")
                    and old_run.get("status")
                    in {"pending", "interrupted", "failed", "stale"}
                ),
                None,
            )
            if previous is not None:
                new_run["supersedes_run_id"] = previous.get("run_id")
                previous["status"] = "superseded"
                previous["superseded_by_run_id"] = new_run.get("run_id")
                previous["updated_at"] = _now_iso()
        recognition_runs.extend(prepared)
        self.app._mark_dirty()
        if not self.app.save():
            recognition_runs[:] = previous_runs
            self.app.status_var.set("批次待辦未能保存，因此沒有送出 API 請求")
            return
        self._recognition_batch = {
            "batch_id": batch_id,
            "project_generation": self.app._project_generation,
            "run_ids": [str(run["run_id"]) for run in prepared],
            "next_index": 0,
            "total": total,
            "succeeded": 0,
            "empty": 0,
            "failed": 0,
            "candidate_count": 0,
            "skipped_completed": completed,
            "stale_completed": stale,
            "skipped_unconfirmed": unconfirmed,
            "skipped_invalid": invalid,
            "retry_only": retry_only,
            "api_key": api_key,
        }
        self._set_recognition_controls_enabled(False)
        self._launch_next_batch_run()

    def _launch_recognition_run(
        self,
        run: dict,
        *,
        api_key: str,
        scope_text: str,
    ) -> str:
        """Start one saved run and return started, failed, or abort."""

        if not self._run_target_is_current(run):
            run["status"] = "stale"
            run["error"] = {
                "type": "StaleRecognitionTarget",
                "message": "段落或來源框已在送出前變更，未呼叫 API",
            }
            run["completed_at"] = _now_iso()
            self.app._mark_dirty()
            return "failed" if self.app.save() else "abort"
        records = self._target_records_for_run(run)
        if records is None or self.project_directory is None:
            run["status"] = "failed"
            run["error"] = {
                "type": "MissingRecognitionTarget",
                "message": "找不到辨識所需的段落、來源框或專案資料夾",
            }
            run["completed_at"] = _now_iso()
            self.app._mark_dirty()
            return "failed" if self.app.save() else "abort"
        _segment, _source_ref, asset = records
        try:
            image_path = asset_file_path(self.project_directory, asset, must_exist=True)
        except (OSError, PhotoAssetError) as error:
            run["status"] = "failed"
            run["error"] = {"type": type(error).__name__, "message": str(error)}
            run["completed_at"] = _now_iso()
            self.app._mark_dirty()
            return "failed" if self.app.save() else "abort"

        run["attempt_id"] = f"ATT-{uuid4().hex[:16]}"
        try:
            previous_attempts = int(run.get("attempt_count", 0))
        except (TypeError, ValueError):
            previous_attempts = 0
        run["attempt_count"] = previous_attempts + 1
        run["status"] = "running"
        run["error"] = None
        run["started_at"] = _now_iso()
        run["updated_at"] = _now_iso()
        self.app._mark_dirty()
        if not self.app.save():
            run["status"] = "pending"
            return "abort"

        identity = {
            "project_generation": self.app._project_generation,
            "manifest": str(self.app.current_path.resolve()) if self.app.current_path else None,
            "run_id": run["run_id"],
            "attempt_id": run["attempt_id"],
            "target_fingerprint": run.get("target_fingerprint"),
        }
        settings = run.get("recognition_settings", {})
        self._recognition_active = identity
        self._set_recognition_controls_enabled(False)
        self.app.status_var.set(f"OpenAI 正在辨識{scope_text}…")
        self._refresh_progress()
        self._start_openai_worker(
            identity=identity,
            provider_request=deepcopy(run["provider_request"]),
            image_path=image_path,
            api_key=api_key,
            model=str(settings.get("model") or run.get("model") or ""),
            detail=str(settings.get("image_detail") or "high"),
            segment_context=deepcopy(run.get("segment_context", {})),
        )
        return "started"

    def _launch_next_batch_run(self) -> None:
        batch = self._recognition_batch
        if batch is None:
            return
        if batch.get("project_generation") != self.app._project_generation:
            self._finish_recognition_batch(aborted=True, reason="專案已切換")
            return
        run_ids = batch.get("run_ids", [])
        while int(batch.get("next_index", 0)) < len(run_ids):
            index = int(batch.get("next_index", 0))
            batch["next_index"] = index + 1
            run_id = run_ids[index]
            run = next(
                (
                    item
                    for item in self.app.project.get("recognition_runs", [])
                    if isinstance(item, dict) and item.get("run_id") == run_id
                ),
                None,
            )
            if run is None:
                batch["failed"] = int(batch.get("failed", 0)) + 1
                continue
            outcome = self._launch_recognition_run(
                run,
                api_key=str(batch.get("api_key") or ""),
                scope_text=f"批次第 {index + 1}/{len(run_ids)} 筆",
            )
            if outcome == "started":
                return
            if outcome == "abort":
                self._finish_recognition_batch(
                    aborted=True, reason="無法先保存工作狀態"
                )
                return
            batch["failed"] = int(batch.get("failed", 0)) + 1
        self._finish_recognition_batch()

    def _recognition_run_source_label(self, run: dict | None, run_id: str) -> str:
        if not isinstance(run, dict):
            return run_id
        asset = self._asset_by_id(run.get("asset_id"))
        segment = self._segment_by_id(run.get("segment_id"))
        source_bits: list[str] = []
        if isinstance(asset, dict):
            page = asset.get("page_sequence")
            name = str(asset.get("original_filename") or asset.get("asset_id") or "照片")
            source_bits.append(f"第 {page} 頁 {name}" if page else name)
        else:
            source_bits.append(str(run.get("asset_id") or "未知照片"))
        segment_id = str(run.get("segment_id") or "未知段落")
        if isinstance(segment, dict):
            segment_name = str(
                segment.get("section_name")
                or segment.get("pattern_name")
                or ""
            ).strip()
            source_bits.append(
                f"{segment_id} {segment_name}" if segment_name else segment_id
            )
        else:
            source_bits.append(segment_id)
        return "｜".join(source_bits)

    def _batch_result_rows(
        self,
        result_summary: dict[str, list[str]],
    ) -> list[tuple[str, str, str]]:
        runs = self.app.project.get("recognition_runs", [])
        run_lookup = {
            str(run.get("run_id")): run
            for run in runs
            if isinstance(run, dict) and isinstance(run.get("run_id"), str)
        } if isinstance(runs, list) else {}
        rows: list[tuple[str, str, str]] = []

        def add_rows(run_ids: list[str], result: str, fallback: str) -> None:
            for run_id in run_ids:
                run = run_lookup.get(run_id)
                detail = self._run_error_text(run) if isinstance(run, dict) else ""
                detail = detail or fallback
                if len(detail) > 180:
                    detail = f"{detail[:177]}…"
                rows.append(
                    (
                        self._recognition_run_source_label(run, run_id),
                        result,
                        detail,
                    )
                )

        add_rows(
            result_summary["completed_empty_run_ids"],
            "成功但無候選",
            "不是連線失敗；如需重辨，請先調整來源框。",
        )
        add_rows(
            result_summary["timeout_run_ids"],
            "逾時，可重試",
            "這個區域等待過久；可只重試失敗項目。",
        )
        add_rows(
            result_summary["failed_run_ids"],
            "失敗，可重試",
            "辨識失敗；請查看設定、網路或額度後重試。",
        )
        add_rows(
            result_summary["unfinished_run_ids"],
            "未完成",
            "工作未完成；若仍可重試，會包含在重試按鈕中。",
        )
        return rows

    def _finish_recognition_batch(
        self, *, aborted: bool = False, reason: str = ""
    ) -> None:
        batch = self._recognition_batch
        if batch is None:
            self._set_recognition_controls_enabled(True)
            return
        # Drop the credential before showing any UI or retaining summaries.
        batch["api_key"] = None
        total = int(batch.get("total", 0))
        counted_succeeded = int(batch.get("succeeded", 0))
        counted_empty = int(batch.get("empty", 0))
        counted_failed = int(batch.get("failed", 0))
        candidate_count = int(batch.get("candidate_count", 0))
        run_ids = [
            str(run_id)
            for run_id in batch.get("run_ids", [])
            if isinstance(run_id, str) and run_id.strip()
        ]
        result_summary = summarize_batch_results(
            self.app.project.get("recognition_runs", []),
            run_ids,
        )
        succeeded = len(result_summary["successful_run_ids"])
        empty = len(result_summary["completed_empty_run_ids"])
        timeout = len(result_summary["timeout_run_ids"])
        other_failed = len(result_summary["failed_run_ids"])
        unfinished = len(result_summary["unfinished_run_ids"])
        failed = timeout + other_failed + unfinished
        skipped = (
            int(batch.get("skipped_completed", 0))
            + int(batch.get("skipped_unconfirmed", 0))
            + int(batch.get("skipped_invalid", 0))
        )
        self._recognition_batch = None
        self._recognition_active = None
        self._set_recognition_controls_enabled(True)
        self.update_provider_status()
        self._refresh_progress()
        self._refresh_candidates()
        self.redraw()
        if aborted:
            messagebox.showwarning(
                "批次辨識已停止",
                f"已完成 {counted_succeeded + counted_empty + counted_failed}/{total} 筆。\n"
                f"原因：{reason or '工作已中止'}\n\n"
                "尚未開始的待辦不會自動送出；修正原因後，可按「只重試失敗項目」。",
            )
            self.app.status_var.set("批次辨識已停止；可修正原因後只重試失敗項目")
            return
        self.app.status_var.set(
            f"批次辨識完成：{candidate_count} 個候選，{failed} 筆失敗"
        )
        summary_text = (
            f"有候選的成功工作：{succeeded}\n"
            f"成功但沒有候選：{empty}\n"
            f"逾時：{timeout}｜其他失敗：{other_failed}｜未完成：{unfinished}\n"
            f"候選總數：{candidate_count}｜事前略過：{skipped}\n\n"
            "「成功但沒有候選」不是連線錯誤，也不會列入失敗重試；"
            "請先調整來源框後再辨識。"
        )
        retryable_ids = list(result_summary["retryable_run_ids"])
        dialog = BatchRecognitionSummaryDialog(
            self,
            summary_text=summary_text,
            rows=self._batch_result_rows(result_summary),
            retry_count=len(retryable_ids),
        )
        self.wait_window(dialog)
        if dialog.retry_requested:
            try:
                if self.winfo_exists():
                    self.after_idle(
                        lambda ids=retryable_ids: self.retry_failed_recognition_jobs(ids)
                    )
            except tk.TclError:
                # The main window can be closed while the modal dialog is open.
                return

    def _start_openai_worker(
        self,
        *,
        identity: dict,
        provider_request: dict,
        image_path: Path,
        api_key: str,
        model: str,
        detail: str,
        segment_context: dict,
    ) -> None:
        """Run network/Pillow work off-thread; the worker never touches Tk/project."""

        def worker() -> None:
            try:
                result = recognize_score_region(
                    provider_request,
                    image_path,
                    model=model,
                    api_key=api_key,
                    detail=detail,
                    segment_context=segment_context,
                )
                payload = {
                    "ok": True,
                    "identity": identity,
                    "result": result,
                }
            except Exception as error:
                # Provider errors are deliberately credential-safe. Preserve
                # only their short public message, never repr/traceback.
                public_error = (
                    str(error)
                    if isinstance(error, OpenAIProviderError)
                    else f"辨識工作失敗（{type(error).__name__}）"
                )
                payload = {
                    "ok": False,
                    "identity": identity,
                    "error_type": type(error).__name__,
                    "error_message": public_error[:600],
                }
            self._recognition_results.put(payload)

        threading.Thread(
            target=worker,
            name="guoyue-openai-recognition",
            daemon=True,
        ).start()
        if not self._recognition_polling:
            self._recognition_polling = True
            self.after(120, self._poll_recognition_results)

    def _poll_recognition_results(self) -> None:
        if getattr(self.app, "_closing", False):
            self._recognition_polling = False
            return
        while True:
            try:
                payload = self._recognition_results.get_nowait()
            except queue.Empty:
                break
            self._complete_recognition(payload)
        if self._recognition_active is not None:
            self.after(120, self._poll_recognition_results)
        else:
            self._recognition_polling = False

    def _identity_is_current(self, identity: dict) -> bool:
        manifest = str(self.app.current_path.resolve()) if self.app.current_path else None
        return (
            self._recognition_active == identity
            and identity.get("project_generation") == self.app._project_generation
            and identity.get("manifest") == manifest
        )

    def _complete_recognition(self, payload: dict) -> None:
        identity = payload.get("identity")
        if not isinstance(identity, dict) or not self._identity_is_current(identity):
            # The user opened/created another project while the request was in
            # flight. Its old response must never be merged into the new file.
            if self._recognition_active == identity:
                self._recognition_active = None
                if self._recognition_batch is not None:
                    self._finish_recognition_batch(
                        aborted=True, reason="辨識期間專案已切換"
                    )
                else:
                    self._set_recognition_controls_enabled(True)
            return
        run = next(
            (
                item
                for item in self.app.project.get("recognition_runs", [])
                if isinstance(item, dict)
                and item.get("run_id") == identity.get("run_id")
                and item.get("attempt_id") == identity.get("attempt_id")
            ),
            None,
        )
        if run is None:
            self._recognition_active = None
            if self._recognition_batch is not None:
                self._recognition_batch["failed"] = (
                    int(self._recognition_batch.get("failed", 0)) + 1
                )
                self._launch_next_batch_run()
            else:
                self._set_recognition_controls_enabled(True)
            return

        if payload.get("ok") and not self._run_target_is_current(run):
            payload = {
                **payload,
                "ok": False,
                "error_type": "StaleRecognitionTarget",
                "error_message": "辨識期間段落或來源框已變更；結果未合併",
            }

        if payload.get("ok"):
            result = payload.get("result")
            if not isinstance(result, dict):
                payload = {
                    **payload,
                    "ok": False,
                    "error_type": "InvalidResult",
                    "error_message": "OpenAI adapter 回傳格式無效",
                }
            else:
                candidates = result.get("candidates", [])
                updated = update_recognition_run(run, candidates)
                run.clear()
                run.update(updated)
                if not candidates:
                    run["status"] = "completed_empty"
                run["provider_response_id"] = result.get("response_id")
                run["usage"] = result.get("usage")
                run["cost_estimate"] = estimate_usage_cost(
                    run["usage"], run.get("model")
                )
                run["warnings"] = result.get("warnings", [])
                run["detected_profile"] = result.get("detected_profile")
                run["completed_at"] = _now_iso()
                target_asset = self._asset_by_id(run.get("asset_id"))
                if (
                    target_asset is not None
                    and target_asset.get("notation_profile") == "auto"
                    and result.get("detected_profile") in PROFILE_LABELS
                ):
                    target_asset["notation_profile"] = result["detected_profile"]
                    if (
                        self.current_asset is not None
                        and self.current_asset.get("asset_id") == target_asset.get("asset_id")
                    ):
                        self.profile_var.set(PROFILE_LABELS[result["detected_profile"]])
        if not payload.get("ok"):
            error_type = str(payload.get("error_type") or "OpenAIProviderError")
            run["status"] = (
                "stale" if error_type == "StaleRecognitionTarget" else "failed"
            )
            run["error"] = {
                "type": error_type,
                "message": str(payload.get("error_message") or "OpenAI 辨識失敗"),
            }
            run["completed_at"] = _now_iso()

        self._recognition_active = None
        self.app._mark_dirty()
        saved = self.app.save()
        self.update_provider_status()
        self._refresh_progress()
        self._refresh_candidates()
        self.redraw()
        if self._recognition_batch is not None:
            batch = self._recognition_batch
            if payload.get("ok"):
                candidate_count = len(run.get("candidates", []))
                batch["candidate_count"] = int(batch.get("candidate_count", 0)) + candidate_count
                if candidate_count:
                    batch["succeeded"] = int(batch.get("succeeded", 0)) + 1
                else:
                    batch["empty"] = int(batch.get("empty", 0)) + 1
            else:
                batch["failed"] = int(batch.get("failed", 0)) + 1
            if not saved:
                self._finish_recognition_batch(
                    aborted=True,
                    reason="辨識結果無法安全保存",
                )
                return
            if not payload.get("ok") and self._batch_error_should_stop(run):
                message = str(run.get("error", {}).get("message") or "供應商設定錯誤")
                self._finish_recognition_batch(
                    aborted=True,
                    reason=message,
                )
                return
            self._launch_next_batch_run()
            return

        self._set_recognition_controls_enabled(True)
        if payload.get("ok"):
            candidate_count = len(run.get("candidates", []))
            warnings = run.get("warnings") or []
            warning_text = f"\n\nAI 提醒：{'；'.join(warnings[:3])}" if warnings else ""
            usage_text = self._format_run_usage(run, multiline=True)
            messagebox.showinfo(
                "OpenAI 辨識完成",
                f"已取得 {candidate_count} 個候選。請在下方清單逐項接受或拒絕。"
                f"{usage_text}"
                f"{warning_text}",
            )
            self.app.status_var.set(f"OpenAI 辨識完成：{candidate_count} 個候選待複核")
        else:
            message = run.get("error", {}).get("message", "OpenAI 辨識失敗")
            messagebox.showerror(
                "OpenAI 辨識失敗",
                f"{message}\n\n{self._recognition_error_advice(run)}",
            )
            self.app.status_var.set("OpenAI 辨識失敗；工作與錯誤狀態已保存")

    @staticmethod
    def _batch_error_should_stop(run: dict) -> bool:
        """Stop errors likely to affect every remaining request in the batch."""

        error = run.get("error")
        if not isinstance(error, dict):
            return False
        haystack = f"{error.get('type', '')} {error.get('message', '')}".casefold()
        stop_markers = (
            "authenticationerror",
            "permissiondeniederror",
            "ratelimiterror",
            "insufficient_quota",
            "quota",
            "notfounderror",
            "badrequesterror",
            "apiconnectionerror",
        )
        return any(marker in haystack for marker in stop_markers)

    def _refresh_progress(self) -> None:
        if self.current_asset is None:
            self.progress_var.set("")
            self._refresh_usage_summary()
            self._refresh_retry_failed_button()
            return
        runs = [
            run
            for run in self.app.project.get("recognition_runs", [])
            if run.get("asset_id") == self.current_asset.get("asset_id")
        ]
        pending = sum(run.get("status") in {"pending", "running", "interrupted"} for run in runs)
        ready = sum(run.get("status") in {"ready_for_review", "reviewed"} for run in runs)
        failed = sum(run.get("status") in {"failed", "stale"} for run in runs)
        empty = sum(run.get("status") == "completed_empty" for run in runs)
        self.progress_var.set(
            f"本頁辨識工作（所有段落）：{len(runs)}"
            f"｜處理中／待處理 {pending}｜已有結果 {ready}"
            f"｜無候選 {empty}｜失敗 {failed}"
        )
        self._refresh_usage_summary()
        self._refresh_retry_failed_button()

    @staticmethod
    def _format_usd(cost: object) -> str:
        if not isinstance(cost, (int, float)) or isinstance(cost, bool):
            return "無法估價"
        value = float(cost)
        if value == 0:
            return "US$0.0000"
        if 0 < value < 0.0001:
            return "US$<0.0001"
        if value < 1:
            return f"US${value:.4f}"
        return f"US${value:.3f}"

    def _format_run_usage(self, run: dict, *, multiline: bool = False) -> str:
        estimate = estimate_recognition_run_cost(run)
        usage = estimate["usage"]
        if estimate["status"] == "usage_unavailable":
            return ""
        separator = "\n" if multiline else "｜"
        if estimate["status"] == "estimated":
            price_text = f"估算 {self._format_usd(estimate.get('cost_usd'))}"
        else:
            price_text = "此模型未有內建價格"
        return (
            f"{separator}用量：輸入 {usage['input_tokens']:,}／輸出 "
            f"{usage['output_tokens']:,} tokens｜{price_text}"
        )

    def _refresh_usage_summary(self) -> None:
        summary = summarize_recognition_runs(
            self.app.project.get("recognition_runs", [])
        )
        if summary["run_count"] == 0:
            self.usage_var.set("用量：尚無成功辨識紀錄")
            return
        usage = summary["usage"]
        cost_text = self._format_usd(summary.get("cost_usd"))
        extra = (
            f"；另有 {summary['unpriced_run_count']} 筆無法估價"
            if summary["unpriced_run_count"]
            else ""
        )
        self.usage_var.set(
            f"本專案已成功保存的辨識累計：\n輸入 {usage['input_tokens']:,}／輸出 "
            f"{usage['output_tokens']:,} tokens\n估算 {cost_text}{extra}\n"
            "可能不含失敗或中斷請求；正式金額以 OpenAI 帳單為準"
        )


__all__ = ["PhotoWorkspace", "PROFILE_LABELS"]
