"""Tkinter 格狀輸入版國樂打擊節奏標註工具。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .audio import play_wav, render_segment_wav, shutdown_audio, stop_audio
from .grid import (
    COMMON_TIME_SIGNATURES,
    LABEL_TO_SUBDIVISION,
    SUBDIVISION_LABELS,
    ensure_bar_settings,
    ensure_track_brushes,
    event_cell_index,
    grid_cell_count,
    grid_event_tick,
    grid_ticks,
    page_tick_range,
    reflow_events_after_settings_change,
    subdivision_label,
    tick_to_variable_bar_beat,
)
from .live_audio import LiveHitPlayer
from .instrument_library import (
    InstrumentLibraryStore,
    attach_project_snapshot,
    bind_project_to_library,
    create_default_library,
    get_instrument,
    get_project_snapshot,
)
from .instrument_library_ui import InstrumentLibraryDialog
from .models import (
    INSTRUMENT_PRESETS,
    duplicate_segment_structure,
    ensure_project_event_annotations,
    make_track,
    new_project,
    new_segment,
    next_event_id,
    next_segment_id,
    next_take_id,
    now_iso,
    selected_take,
)
from .navigation import (
    NavigationError,
    expand_playback_order,
    materialize_playback_view,
)
from .navigation_ui import NavigationDialog
from .openai_settings import (
    OpenAICredentialError,
    OpenAICredentialStore,
    OpenAISettingsError,
    OpenAISettingsStore,
)
from .openai_settings_ui import show_openai_settings_dialog
from .photo_workspace import PhotoWorkspace
from .symbol_mapping_ui import SymbolMappingManagerDialog
from .symbol_mappings import (
    SymbolMappingError,
    SymbolMappingStore,
    create_symbol_mappings,
    validate_symbol_mappings,
)
from .storage import (
    PROJECT_BUNDLE_SUFFIX,
    PROJECT_MANIFEST,
    export_dataset,
    load_project,
    migrate_project,
    project_manifest_path,
    save_project,
)
from .timeline import PPQ, parse_time_signature, ticks_per_beat


PAGE_SIZE = 4
BACKGROUND = "#EEE9DE"
PANEL = "#FAF7F0"
INK = "#202528"
MUTED = "#6F736F"
ACCENT = "#BB4F3D"
IS_MACOS = sys.platform == "darwin"
UI_FONT = "PingFang TC" if IS_MACOS else "Microsoft JhengHei UI"
SHORTCUT_LABEL = "⌘" if IS_MACOS else "Ctrl+"
SHORTCUT_MODIFIER = "Command" if IS_MACOS else "Control"


class MeasureGrid(ttk.Frame):
    """一小節的設定列與可點擊格線。"""

    def __init__(self, master: tk.Misc, app: "GridAnnotatorApp") -> None:
        super().__init__(master, style="Measure.TFrame", padding=(10, 7))
        self.app = app
        self.bar_number = 1
        self.event_by_cell: dict[int, dict] = {}
        self.brushes: list[dict] = []
        self.last_drag_cell: int | None = None

        header = ttk.Frame(self, style="Measure.TFrame")
        header.pack(fill="x", pady=(0, 5))
        self.bar_label = ttk.Label(header, text="第 1 小節", style="MeasureTitle.TLabel")
        self.bar_label.pack(side="left")
        ttk.Label(header, text="拍號", style="Subtle.TLabel").pack(side="left", padx=(18, 5))
        self.signature_var = tk.StringVar(value="4/4")
        self.signature_combo = ttk.Combobox(
            header,
            textvariable=self.signature_var,
            values=COMMON_TIME_SIGNATURES,
            width=6,
            state="normal",
        )
        self.signature_combo.pack(side="left")
        ttk.Label(header, text="格線", style="Subtle.TLabel").pack(side="left", padx=(15, 5))
        self.subdivision_var = tk.StringVar(value=SUBDIVISION_LABELS["1/16"])
        self.subdivision_combo = ttk.Combobox(
            header,
            textvariable=self.subdivision_var,
            values=tuple(SUBDIVISION_LABELS.values()),
            width=27,
            state="readonly",
        )
        self.subdivision_combo.pack(side="left")
        self.info_label = ttk.Label(header, text="16 格・每格 240 tick", style="Subtle.TLabel")
        self.info_label.pack(side="left", padx=12)
        ttk.Button(
            header, text="清除此小節", style="Quiet.TButton", command=self._clear
        ).pack(side="right")

        self.canvas = tk.Canvas(
            self,
            height=68,
            background="#FFFDF8",
            highlightthickness=1,
            highlightbackground="#CEC7B8",
            cursor="pencil",
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _event: self.redraw())
        self.canvas.bind("<Button-1>", self._paint)
        self.canvas.bind("<B1-Motion>", self._paint)
        self.canvas.bind("<ButtonRelease-1>", lambda _event: self._finish_drag())
        self.canvas.bind("<Button-2>", self._erase)
        self.canvas.bind("<Button-3>", self._erase)
        if IS_MACOS:
            self.canvas.bind("<Control-Button-1>", self._erase)
        self.signature_combo.bind("<<ComboboxSelected>>", self._setting_changed)
        self.signature_combo.bind("<FocusOut>", self._setting_changed)
        self.subdivision_combo.bind("<<ComboboxSelected>>", self._setting_changed)

    def load_bar(self, bar_number: int) -> None:
        self.bar_number = bar_number
        segment = self.app.current_segment
        settings = ensure_bar_settings(segment)
        if not 1 <= bar_number <= len(settings):
            self.pack_forget()
            return
        setting = settings[bar_number - 1]
        self.bar_label.config(text=f"第 {bar_number} 小節")
        self.signature_var.set(setting["time_signature"])
        self.subdivision_var.set(subdivision_label(setting["subdivision"]))
        self.refresh_events()
        self.pack(fill="both", expand=True, pady=4)

    def refresh_events(self) -> None:
        self.brushes = ensure_track_brushes(self.app.current_track)
        self.event_by_cell = {}
        take = selected_take(self.app.current_track)
        if take:
            for event in take.get("events", []):
                if int(event.get("bar", 0)) != self.bar_number:
                    continue
                cell = event_cell_index(self.app.current_segment, event)
                if cell is not None:
                    self.event_by_cell[cell] = event
        setting = ensure_bar_settings(self.app.current_segment)[self.bar_number - 1]
        try:
            count = grid_cell_count(setting["time_signature"], setting["subdivision"])
            step = grid_ticks(setting["subdivision"])
            self.info_label.config(text=f"{count} 格・每格 {step} tick")
        except ValueError:
            self.info_label.config(text="拍號與格線不相容")
        self.redraw()

    def redraw(self) -> None:
        if not self.winfo_exists():
            return
        self.canvas.delete("all")
        segment = self.app.current_segment
        settings = ensure_bar_settings(segment)
        if not 1 <= self.bar_number <= len(settings):
            return
        setting = settings[self.bar_number - 1]
        try:
            count = grid_cell_count(setting["time_signature"], setting["subdivision"])
        except ValueError:
            self.canvas.create_text(
                12, 30, anchor="w", text="這組拍號與格線無法整除", fill=ACCENT
            )
            return
        width = max(100, self.canvas.winfo_width())
        height = max(50, self.canvas.winfo_height())
        cell_width = width / count
        step = grid_ticks(setting["subdivision"])
        beat_cells = max(1, ticks_per_beat(setting["time_signature"]) // step)
        numerator, denominator = parse_time_signature(setting["time_signature"])
        group_cells = beat_cells
        if denominator == 8 and numerator in {6, 9, 12}:
            group_cells = beat_cells * 3

        brush_by_code = {brush["code"]: brush for brush in self.brushes}
        for cell in range(count):
            x0 = cell * cell_width
            x1 = (cell + 1) * cell_width
            event = self.event_by_cell.get(cell)
            brush = brush_by_code.get(event.get("stroke")) if event else None
            fill = brush["color"] if brush else ("#F4EFE5" if cell % 2 else "#FFFDF8")
            self.canvas.create_rectangle(
                x0,
                0,
                x1,
                height,
                fill=fill,
                outline="#D9D2C5",
                width=1,
            )
            if event and brush:
                label = brush["label"]
                shown = label if cell_width >= 45 else label[:1]
                has_annotations = any(
                    bool(event.get(key))
                    for key in ("is_accent", "is_ornament", "is_roll")
                )
                self.canvas.create_text(
                    (x0 + x1) / 2,
                    height / 2 - (5 if has_annotations else 0),
                    text=shown,
                    fill="white",
                    font=(UI_FONT, 9, "bold"),
                )
            if event:
                annotation_labels = []
                if event.get("is_accent"):
                    annotation_labels.append("重")
                if event.get("is_ornament"):
                    annotation_labels.append("飾")
                if event.get("is_roll"):
                    annotation_labels.append("滾")
                if annotation_labels:
                    self.canvas.create_text(
                        (x0 + x1) / 2,
                        height - 11,
                        text="".join(annotation_labels),
                        fill="white" if brush else ACCENT,
                        font=(UI_FONT, 7, "bold"),
                    )
            if cell > 0 and cell % beat_cells == 0:
                self.canvas.create_line(x0, 0, x0, height, fill="#8D897F", width=2)
            if cell > 0 and cell % group_cells == 0:
                self.canvas.create_line(x0, 0, x0, height, fill=INK, width=3)
            if count <= 24:
                self.canvas.create_text(
                    x0 + 4,
                    5,
                    anchor="nw",
                    text=str(cell + 1),
                    fill="#77776F" if not event else "#FFFFFF",
                    font=(UI_FONT, 7),
                )

        active = self.app.active_brush
        if active:
            self.canvas.create_rectangle(
                1, 1, width - 1, height - 1, outline=active["color"], width=2
            )

    def _cell_at(self, event: tk.Event) -> int | None:
        setting = ensure_bar_settings(self.app.current_segment)[self.bar_number - 1]
        try:
            count = grid_cell_count(setting["time_signature"], setting["subdivision"])
        except ValueError:
            return None
        width = max(1, self.canvas.winfo_width())
        return min(count - 1, max(0, int(event.x / width * count)))

    def _paint(self, event: tk.Event) -> None:
        cell = self._cell_at(event)
        if cell is None or cell == self.last_drag_cell:
            return
        self.last_drag_cell = cell
        self.app.paint_cell(self.bar_number, cell, erase=False, commit=False)

    def _erase(self, event: tk.Event) -> None:
        cell = self._cell_at(event)
        if cell is not None:
            self.app.paint_cell(self.bar_number, cell, erase=True)

    def _finish_drag(self) -> None:
        if self.last_drag_cell is not None:
            self.app.finish_paint_stroke()
        self.last_drag_cell = None

    def _setting_changed(self, _event: tk.Event | None = None) -> None:
        self.app.change_bar_setting(
            self.bar_number,
            self.signature_var.get(),
            LABEL_TO_SUBDIVISION.get(self.subdivision_var.get(), "1/16"),
        )

    def _clear(self) -> None:
        self.app.clear_bar(self.bar_number)


class TrackDialog(tk.Toplevel):
    def __init__(self, master: tk.Misc, track: dict | None = None) -> None:
        super().__init__(master)
        self.title("軌道與筆刷設定")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()
        self.result: dict | None = None
        self.track = deepcopy(track) if track else None

        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="樂器預設").grid(row=0, column=0, sticky="w", pady=4)
        self.preset_var = tk.StringVar(value="自訂" if track else "鼓")
        preset_values = tuple(INSTRUMENT_PRESETS.keys()) + ("自訂",)
        combo = ttk.Combobox(body, textvariable=self.preset_var, values=preset_values, state="readonly", width=18)
        combo.grid(row=0, column=1, sticky="ew", pady=4)
        combo.bind("<<ComboboxSelected>>", self._load_preset)

        ttk.Label(body, text="顯示名稱").grid(row=1, column=0, sticky="w", pady=4)
        self.name_var = tk.StringVar(value=track.get("instrument", "") if track else "鼓")
        ttk.Entry(body, textvariable=self.name_var, width=26).grid(row=1, column=1, sticky="ew", pady=4)

        ttk.Label(body, text="概念音色").grid(row=2, column=0, sticky="w", pady=4)
        self.profile_var = tk.StringVar(value=track.get("sound_profile", "drum") if track else "drum")
        ttk.Combobox(
            body,
            textvariable=self.profile_var,
            values=("drum", "large_gong", "small_gong", "cymbal", "bell", "timpani"),
            state="readonly",
            width=18,
        ).grid(row=2, column=1, sticky="ew", pady=4)

        ttk.Separator(body).grid(row=3, column=0, columnspan=2, sticky="ew", pady=10)
        ttk.Label(body, text="筆刷代碼（供資料使用）").grid(row=4, column=0, sticky="w")
        ttk.Label(body, text="畫面名稱").grid(row=4, column=1, sticky="w")
        self.code_vars: list[tk.StringVar] = []
        self.label_vars: list[tk.StringVar] = []
        brushes = ensure_track_brushes(self.track) if self.track else []
        for index in range(6):
            code = brushes[index]["code"] if index < len(brushes) else ""
            label = brushes[index]["label"] if index < len(brushes) else ""
            code_var = tk.StringVar(value=code)
            label_var = tk.StringVar(value=label)
            self.code_vars.append(code_var)
            self.label_vars.append(label_var)
            ttk.Entry(body, textvariable=code_var, width=20).grid(row=5 + index, column=0, padx=(0, 8), pady=3)
            ttk.Entry(body, textvariable=label_var, width=20).grid(row=5 + index, column=1, pady=3)

        buttons = ttk.Frame(body)
        buttons.grid(row=11, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="left", padx=4)
        ttk.Button(buttons, text="套用", style="Accent.TButton", command=self._accept).pack(side="left", padx=4)
        body.columnconfigure(1, weight=1)
        if not track:
            self._apply_preset("鼓")
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.wait_visibility()
        self.focus_set()

    def _load_preset(self, _event: tk.Event | None = None) -> None:
        self._apply_preset(self.preset_var.get())

    def _apply_preset(self, name: str) -> None:
        preset = INSTRUMENT_PRESETS.get(name)
        if not preset:
            return
        self.name_var.set(name)
        self.profile_var.set(preset["sound_profile"])
        for index in range(6):
            code, label = preset["brushes"][index] if index < len(preset["brushes"]) else ("", "")
            self.code_vars[index].set(code)
            self.label_vars[index].set(label)

    def _accept(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror("資料不完整", "請輸入樂器名稱。", parent=self)
            return
        colors = ["#D65A4A", "#D99536", "#3E9B75", "#3978B8", "#7A5BB5", "#B34B83"]
        brushes = []
        used = set()
        for index, (code_var, label_var) in enumerate(zip(self.code_vars, self.label_vars)):
            code = code_var.get().strip()
            label = label_var.get().strip()
            if not code and not label:
                continue
            if not code:
                code = f"stroke_{index + 1}"
            if code in used:
                messagebox.showerror("代碼重複", f"筆刷代碼「{code}」重複。", parent=self)
                return
            used.add(code)
            brushes.append({"code": code, "label": label or code, "color": colors[index]})
        if not brushes:
            messagebox.showerror("沒有筆刷", "至少要設定一個筆刷。", parent=self)
            return
        self.result = {
            "instrument": name,
            "sound_profile": self.profile_var.get(),
            "brushes": brushes,
        }
        self.destroy()


class GridAnnotatorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"國樂打擊樂譜格狀標註工具 v{__version__}")
        self.root.geometry("1420x920")
        self.root.minsize(1080, 720)
        self.root.configure(background=BACKGROUND)

        self.project = new_project()
        self.instrument_library_store = InstrumentLibraryStore()
        try:
            self.instrument_library = self.instrument_library_store.load()
        except Exception:
            # A damaged global preference must never make existing project data
            # impossible to open. The editor can save a repaired library later.
            self.instrument_library = create_default_library()
        self.symbol_mapping_store = SymbolMappingStore()
        self.symbol_mapping_load_error: str | None = None
        try:
            self.symbol_mappings = self.symbol_mapping_store.load(
                self.instrument_library
            )
        except (OSError, SymbolMappingError) as error:
            # Keep projects usable and never overwrite a damaged global file
            # merely because the application started.
            self.symbol_mappings = create_symbol_mappings()
            self.symbol_mapping_load_error = str(error)
        self.openai_settings_store = OpenAISettingsStore()
        self.openai_credential_store = OpenAICredentialStore()
        self.current_path: Path | None = None
        self._project_generation = 1
        self._closing = False
        self.segment_index = 0
        self.track_index = 0
        self.page_start_bar = 1
        self.active_brush_index = 0
        self.dirty = False
        self._paint_changed = False
        self.history: list[dict] = []
        self.history_index = -1
        self.live_hit_player = LiveHitPlayer()
        self.temp_directory = tempfile.TemporaryDirectory(prefix="guoyue-grid-")
        self.preview_path: Path | None = None
        self._playback_request_id = 0

        self._configure_style()
        self._init_variables()
        self._build_menu()
        self._build_ui()
        self._bind_shortcuts()
        self._normalize_project()
        self._reset_history()
        self.refresh_all()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    @property
    def current_segment(self) -> dict:
        segments = self.project.setdefault("segments", [])
        if not segments:
            segments.append(new_segment())
        self.segment_index = min(max(0, self.segment_index), len(segments) - 1)
        return segments[self.segment_index]

    @property
    def current_track(self) -> dict:
        tracks = self.current_segment.setdefault("tracks", [])
        if not tracks:
            tracks.append(make_track(self.current_segment["segment_id"], 1, preset_name="鼓"))
        self.track_index = min(max(0, self.track_index), len(tracks) - 1)
        return tracks[self.track_index]

    @property
    def active_brush(self) -> dict | None:
        brushes = ensure_track_brushes(self.current_track)
        if not brushes:
            return None
        self.active_brush_index = min(self.active_brush_index, len(brushes) - 1)
        return brushes[self.active_brush_index]

    def _configure_style(self) -> None:
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure(".", font=(UI_FONT, 10), background=BACKGROUND, foreground=INK)
        style.configure("TFrame", background=BACKGROUND)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("Measure.TFrame", background=PANEL, relief="flat")
        style.configure("TLabel", background=BACKGROUND, foreground=INK)
        style.configure("Panel.TLabel", background=PANEL, foreground=INK)
        style.configure("Title.TLabel", font=(UI_FONT, 20, "bold"), foreground=INK)
        style.configure("Section.TLabel", font=(UI_FONT, 11, "bold"), background=PANEL)
        style.configure("MeasureTitle.TLabel", font=(UI_FONT, 11, "bold"), background=PANEL)
        style.configure("Subtle.TLabel", foreground=MUTED, background=PANEL)
        style.configure("Accent.TButton", font=(UI_FONT, 10, "bold"), foreground="white", background=ACCENT)
        style.map("Accent.TButton", background=[("active", "#9E3F31")])
        style.configure("Quiet.TButton", foreground=MUTED)
        style.configure("Panel.TCheckbutton", background=PANEL, foreground=INK)
        style.configure("TNotebook", background=BACKGROUND, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(18, 9), font=(UI_FONT, 10, "bold"))

    def _init_variables(self) -> None:
        self.segment_choice_var = tk.StringVar()
        self.page_label_var = tk.StringVar()
        initial_status = (
            "準備就緒"
            if self.live_hit_player.available
            else f"提示音不可用：{self.live_hit_player.error_message}"
        )
        self.status_var = tk.StringVar(value=initial_status)
        self.track_complete_var = tk.BooleanVar(value=False)
        self.hit_sound_var = tk.BooleanVar(value=self.live_hit_player.available)
        self.accent_var = tk.BooleanVar(value=False)
        self.ornament_var = tk.BooleanVar(value=False)
        self.roll_var = tk.BooleanVar(value=False)
        self.roll_duration_var = tk.StringVar(value="1 格")
        self.metronome_var = tk.BooleanVar(value=True)
        self.loop_var = tk.BooleanVar(value=False)
        self.current_page_only_var = tk.BooleanVar(value=True)
        self.expand_repeats_var = tk.BooleanVar(value=True)
        self.play_scope_var = tk.StringVar(value="全部軌道")
        self.meta_vars = {
            "piece_id": tk.StringVar(),
            "segment_id": tk.StringVar(),
            "piece_name": tk.StringVar(),
            "pattern_name": tk.StringVar(),
            "section_name": tk.StringVar(),
            "selection_type": tk.StringVar(),
            "source": tk.StringVar(),
            "source_page": tk.StringVar(),
            "source_start_bar": tk.StringVar(),
            "source_end_bar": tk.StringVar(),
            "system": tk.StringVar(),
            "tempo": tk.StringVar(),
            "bars": tk.StringVar(),
            "version": tk.StringVar(),
        }

    def _build_menu(self) -> None:
        menu = tk.Menu(self.root)
        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="新增專案", accelerator=f"{SHORTCUT_LABEL}N", command=self.new_project_file)
        file_menu.add_command(label="開啟專案…", accelerator=f"{SHORTCUT_LABEL}O", command=self.open_project)
        file_menu.add_separator()
        file_menu.add_command(label="儲存", accelerator=f"{SHORTCUT_LABEL}S", command=self.save)
        file_menu.add_command(label="另存新檔…", accelerator=f"{SHORTCUT_LABEL}Shift+S", command=self.save_as)
        file_menu.add_separator()
        file_menu.add_command(label="匯出訓練資料…", command=self.export)
        file_menu.add_separator()
        file_menu.add_command(label="結束", command=self.on_close)
        menu.add_cascade(label="檔案", menu=file_menu)
        edit_menu = tk.Menu(menu, tearoff=False)
        edit_menu.add_command(label="復原", accelerator=f"{SHORTCUT_LABEL}Z", command=self.undo)
        edit_menu.add_command(
            label="重做",
            accelerator=f"{SHORTCUT_LABEL}{'Shift+Z' if IS_MACOS else 'Y'}",
            command=self.redo,
        )
        menu.add_cascade(label="編輯", menu=edit_menu)
        settings_menu = tk.Menu(menu, tearoff=False)
        settings_menu.add_command(
            label="樂器／擊法庫…",
            command=self.open_instrument_library,
        )
        settings_menu.add_command(
            label="辨識符號對應規則…",
            command=self.open_symbol_mapping_manager,
        )
        settings_menu.add_separator()
        settings_menu.add_command(
            label="OpenAI 辨識設定…",
            command=self.open_openai_settings,
        )
        menu.add_cascade(label="設定", menu=settings_menu)
        self.root.config(menu=menu)

    def _build_ui(self) -> None:
        header = ttk.Frame(self.root, padding=(18, 14, 18, 8))
        header.pack(fill="x")
        ttk.Label(
            header,
            text=f"國樂打擊樂譜格狀標註 v{__version__}",
            style="Title.TLabel",
        ).pack(side="left")
        ttk.Label(
            header,
            text="格線只負責輸入｜正式資料固定輸出為 960 PPQ tick",
            foreground=MUTED,
        ).pack(side="left", padx=18, pady=(7, 0))
        ttk.Label(header, textvariable=self.status_var, foreground=ACCENT).pack(side="right", pady=(7, 0))

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self.grid_tab = ttk.Frame(self.notebook)
        self.photo_tab = ttk.Frame(self.notebook)
        self.metadata_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.grid_tab, text="格狀標註")
        self.notebook.add(self.photo_tab, text="照片辨識")
        self.notebook.add(self.metadata_tab, text="段落資料")
        self._build_grid_tab()
        self.photo_workspace = PhotoWorkspace(self.photo_tab, self)
        self.photo_workspace.pack(fill="both", expand=True)
        self._build_metadata_tab()

    def _build_grid_tab(self) -> None:
        outer = ttk.Frame(self.grid_tab, padding=(0, 10, 0, 0))
        outer.pack(fill="both", expand=True)
        sidebar = ttk.Frame(outer, style="Panel.TFrame", padding=14, width=265)
        sidebar.pack(side="left", fill="y", padx=(0, 10))
        sidebar.pack_propagate(False)
        work = ttk.Frame(outer)
        work.pack(side="left", fill="both", expand=True)

        ttk.Label(sidebar, text="段落", style="Section.TLabel").pack(anchor="w")
        self.segment_combo = ttk.Combobox(sidebar, textvariable=self.segment_choice_var, state="readonly")
        self.segment_combo.pack(fill="x", pady=(6, 5))
        self.segment_combo.bind("<<ComboboxSelected>>", self._segment_selected)
        segment_buttons = ttk.Frame(sidebar, style="Panel.TFrame")
        segment_buttons.pack(fill="x", pady=(0, 14))
        ttk.Button(segment_buttons, text="＋ 新段落", command=self.add_segment).pack(side="left", expand=True, fill="x", padx=(0, 3))
        ttk.Button(segment_buttons, text="複製設定", command=self.duplicate_segment).pack(side="left", expand=True, fill="x", padx=(3, 0))
        ttk.Button(sidebar, text="反覆記號…", command=self.edit_navigation).pack(fill="x", pady=(0, 10))

        ttk.Separator(sidebar).pack(fill="x", pady=4)
        ttk.Label(sidebar, text="樂器軌道", style="Section.TLabel").pack(anchor="w", pady=(10, 4))
        self.track_list = tk.Listbox(
            sidebar,
            height=11,
            font=(UI_FONT, 10),
            activestyle="none",
            selectbackground="#D9B9A9",
            selectforeground=INK,
            relief="flat",
            highlightthickness=1,
            highlightbackground="#D5CFC3",
        )
        self.track_list.pack(fill="x")
        self.track_list.bind("<<ListboxSelect>>", self._track_selected)
        ttk.Checkbutton(
            sidebar,
            text="本軌已校對完成",
            variable=self.track_complete_var,
            command=self._track_complete_changed,
            style="Panel.TCheckbutton",
        ).pack(anchor="w", pady=(8, 3))
        track_buttons = ttk.Frame(sidebar, style="Panel.TFrame")
        track_buttons.pack(fill="x", pady=(4, 12))
        ttk.Button(track_buttons, text="新增", command=self.add_track).pack(side="left", expand=True, fill="x")
        ttk.Button(track_buttons, text="設定", command=self.edit_track).pack(side="left", expand=True, fill="x", padx=4)
        ttk.Button(track_buttons, text="刪除", command=self.delete_track).pack(side="left", expand=True, fill="x")

        ttk.Separator(sidebar).pack(fill="x", pady=4)
        ttk.Label(sidebar, text="操作提示", style="Section.TLabel").pack(anchor="w", pady=(10, 4))
        ttk.Label(
            sidebar,
            text=(
                "左鍵點擊／拖曳：上色\n右鍵／雙指點按：擦除\n"
                "數字 1–6：切換筆刷\nA：重音／G：裝飾音／R：滾奏\n"
                f"{SHORTCUT_LABEL}Z：復原"
            ),
            style="Subtle.TLabel",
            justify="left",
            wraplength=230,
        ).pack(anchor="w")

        brush_panel = ttk.Frame(work, style="Panel.TFrame", padding=(12, 9))
        brush_panel.pack(fill="x", pady=(0, 8))
        ttk.Label(brush_panel, text="筆刷", style="Section.TLabel").pack(side="left", padx=(0, 10))
        self.brush_frame = ttk.Frame(brush_panel, style="Panel.TFrame")
        self.brush_frame.pack(side="left", fill="x", expand=True)
        self.hit_sound_checkbutton = ttk.Checkbutton(
            brush_panel,
            text="點擊提示音" if self.live_hit_player.available else "提示音不可用",
            variable=self.hit_sound_var,
            command=self._hit_sound_changed,
            style="Panel.TCheckbutton",
        )
        self.hit_sound_checkbutton.pack(side="right")
        if not self.live_hit_player.available:
            self.hit_sound_checkbutton.state(["disabled"])
        annotation_tools = ttk.Frame(brush_panel, style="Panel.TFrame")
        annotation_tools.pack(side="right", padx=(8, 12))
        ttk.Label(annotation_tools, text="附加標記", style="Subtle.TLabel").pack(side="left", padx=(0, 4))
        ttk.Checkbutton(
            annotation_tools,
            text="重音 (A)",
            variable=self.accent_var,
            command=self._modifier_changed,
            style="Panel.TCheckbutton",
        ).pack(side="left", padx=2)
        ttk.Checkbutton(
            annotation_tools,
            text="裝飾音 (G)",
            variable=self.ornament_var,
            command=self._modifier_changed,
            style="Panel.TCheckbutton",
        ).pack(side="left", padx=2)
        ttk.Checkbutton(
            annotation_tools,
            text="滾奏 (R)",
            variable=self.roll_var,
            command=self._modifier_changed,
            style="Panel.TCheckbutton",
        ).pack(side="left", padx=2)
        ttk.Combobox(
            annotation_tools,
            textvariable=self.roll_duration_var,
            values=("1 格", "2 格", "1 拍", "整小節"),
            state="readonly",
            width=7,
        ).pack(side="left", padx=(3, 0))

        self.measure_container = ttk.Frame(work)
        self.measure_container.pack(fill="both", expand=True)
        self.measure_widgets = [MeasureGrid(self.measure_container, self) for _ in range(PAGE_SIZE)]
        for widget in self.measure_widgets:
            widget.pack(fill="both", expand=True, pady=4)

        page_bar = ttk.Frame(work, padding=(0, 8))
        page_bar.pack(fill="x")
        ttk.Button(page_bar, text="← 前四小節", command=self.previous_page).pack(side="left")
        ttk.Label(page_bar, textvariable=self.page_label_var, font=(UI_FONT, 10, "bold")).pack(side="left", expand=True)
        ttk.Button(page_bar, text="後四小節 →", command=self.next_page).pack(side="right")

        transport = ttk.Frame(work, style="Panel.TFrame", padding=(12, 10))
        transport.pack(fill="x")
        ttk.Button(transport, text="▶ 播放", style="Accent.TButton", command=self.play).pack(side="left")
        ttk.Button(transport, text="■ 停止", command=self.stop).pack(side="left", padx=(6, 14))
        ttk.Combobox(
            transport,
            textvariable=self.play_scope_var,
            values=("全部軌道", "目前軌道"),
            state="readonly",
            width=10,
        ).pack(side="left")
        ttk.Checkbutton(transport, text="只播目前四小節", variable=self.current_page_only_var, style="Panel.TCheckbutton").pack(side="left", padx=10)
        ttk.Checkbutton(transport, text="展開反覆", variable=self.expand_repeats_var, style="Panel.TCheckbutton").pack(side="left", padx=4)
        ttk.Checkbutton(transport, text="節拍器", variable=self.metronome_var, style="Panel.TCheckbutton").pack(side="left", padx=4)
        ttk.Checkbutton(transport, text="循環", variable=self.loop_var, style="Panel.TCheckbutton").pack(side="left", padx=4)
        ttk.Label(transport, text="BPM", style="Panel.TLabel").pack(side="left", padx=(16, 4))
        ttk.Entry(transport, textvariable=self.meta_vars["tempo"], width=6).pack(side="left")

    def _build_metadata_tab(self) -> None:
        wrapper = ttk.Frame(self.metadata_tab, style="Panel.TFrame", padding=22)
        wrapper.pack(fill="both", expand=True, pady=10)
        ttk.Label(wrapper, text="段落背景資料", style="Section.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 12))
        fields = [
            ("piece_id", "曲目 ID"), ("segment_id", "段落 ID"),
            ("piece_name", "曲目名稱"), ("pattern_name", "鼓介／節奏名稱"),
            ("section_name", "段落名稱"), ("selection_type", "選段類型"),
            ("source", "資料來源／譜集"), ("source_page", "來源頁碼"),
            ("source_start_bar", "原譜起始小節"), ("source_end_bar", "原譜結束小節"),
            ("system", "流派／系統"), ("tempo", "BPM"),
            ("bars", "片段小節數"), ("version", "資料版本"),
        ]
        for index, (key, label) in enumerate(fields):
            row = index // 2 + 1
            column = (index % 2) * 2
            ttk.Label(wrapper, text=label, style="Panel.TLabel").grid(row=row, column=column, sticky="w", padx=(0, 8), pady=6)
            if key == "selection_type":
                widget = ttk.Combobox(
                    wrapper,
                    textvariable=self.meta_vars[key],
                    values=("基本型", "變化型", "轉折型", "特殊型", "疏落型", "收束型"),
                    state="normal",
                )
            else:
                widget = ttk.Entry(wrapper, textvariable=self.meta_vars[key])
            widget.grid(row=row, column=column + 1, sticky="ew", padx=(0, 20), pady=6)

        note_row = len(fields) // 2 + 2
        ttk.Label(wrapper, text="選段原因", style="Panel.TLabel").grid(row=note_row, column=0, sticky="nw", pady=6)
        self.selection_reason_text = tk.Text(wrapper, height=3, wrap="word", font=(UI_FONT, 10))
        self.selection_reason_text.grid(row=note_row, column=1, columnspan=3, sticky="ew", padx=(0, 20), pady=6)
        ttk.Label(wrapper, text="備註", style="Panel.TLabel").grid(row=note_row + 1, column=0, sticky="nw", pady=6)
        self.notes_text = tk.Text(wrapper, height=5, wrap="word", font=(UI_FONT, 10))
        self.notes_text.grid(row=note_row + 1, column=1, columnspan=3, sticky="nsew", padx=(0, 20), pady=6)
        hint = ttk.Label(
            wrapper,
            text="拍號與格線可在「格狀標註」頁逐小節設定；改變拍號時，既有事件會依原小節內的相對位置重新貼齊格線。",
            style="Subtle.TLabel",
            wraplength=820,
            justify="left",
        )
        hint.grid(row=note_row + 2, column=0, columnspan=3, sticky="w", pady=(12, 0))
        ttk.Button(wrapper, text="套用段落資料", style="Accent.TButton", command=self.apply_metadata).grid(row=note_row + 2, column=3, sticky="e", padx=(0, 20), pady=(12, 0))
        wrapper.columnconfigure(1, weight=1)
        wrapper.columnconfigure(3, weight=1)
        wrapper.rowconfigure(note_row + 1, weight=1)

    def _bind_shortcuts(self) -> None:
        modifier = SHORTCUT_MODIFIER
        self.root.bind_all(f"<{modifier}-s>", lambda _event: self.save())
        self.root.bind_all(f"<{modifier}-Shift-S>", lambda _event: self.save_as())
        self.root.bind_all(f"<{modifier}-o>", lambda _event: self.open_project())
        self.root.bind_all(f"<{modifier}-n>", lambda _event: self.new_project_file())
        self.root.bind_all(f"<{modifier}-z>", lambda _event: self.undo())
        if IS_MACOS:
            self.root.bind_all("<Command-Shift-Z>", lambda _event: self.redo())
        else:
            self.root.bind_all("<Control-y>", lambda _event: self.redo())
        for number in range(1, 7):
            self.root.bind_all(
                f"<KeyPress-{number}>",
                lambda event, index=number - 1: self._number_brush(event, index),
            )
        self.root.bind_all("<KeyPress-a>", lambda event: self._toggle_modifier(event, "accent"))
        self.root.bind_all("<KeyPress-g>", lambda event: self._toggle_modifier(event, "ornament"))
        self.root.bind_all("<KeyPress-r>", lambda event: self._toggle_modifier(event, "roll"))

    def _number_brush(self, event: tk.Event, index: int) -> None:
        if isinstance(event.widget, (tk.Entry, tk.Text, ttk.Entry, ttk.Combobox)):
            return
        self.select_brush(index)

    def _toggle_modifier(self, event: tk.Event, modifier: str) -> None:
        if isinstance(event.widget, (tk.Entry, tk.Text, ttk.Entry, ttk.Combobox)):
            return
        variables = {
            "accent": self.accent_var,
            "ornament": self.ornament_var,
            "roll": self.roll_var,
        }
        variable = variables[modifier]
        variable.set(not variable.get())
        self._modifier_changed()

    def _modifier_changed(self) -> None:
        labels = []
        if self.accent_var.get():
            labels.append("重音")
        if self.ornament_var.get():
            labels.append("裝飾音")
        if self.roll_var.get():
            labels.append("滾奏")
        text = "＋".join(labels) if labels else "無"
        self.status_var.set(f"目前附加標記：{text}")

    def _normalize_project(self) -> None:
        migrate_project(self.project)
        self.project["ppq"] = PPQ
        self.project["input_mode"] = "mixed"
        try:
            snapshot = get_project_snapshot(self.project)
        except Exception:
            snapshot = None
        if snapshot is None:
            bind_project_to_library(self.project, self.instrument_library)
            attach_project_snapshot(self.project, self.instrument_library)
        else:
            bind_project_to_library(self.project, snapshot)
        for segment in self.project.get("segments", []):
            ensure_bar_settings(segment)
            for track in segment.get("tracks", []):
                ensure_track_brushes(track)
                for take in track.get("takes", []):
                    for event in take.get("events", []):
                        bar, beat = tick_to_variable_bar_beat(segment, int(event.get("tick", 0)))
                        event["bar"] = bar
                        event["beat"] = beat
                        event.setdefault("input_method", "tap")
                        event.setdefault("grid_index", event_cell_index(segment, event))

    def open_instrument_library(self) -> None:
        dialog = InstrumentLibraryDialog(
            self.root,
            self.instrument_library,
            self.instrument_library_store,
        )
        self.root.wait_window(dialog)
        if dialog.result_library is None:
            return
        self.instrument_library = dialog.result_library
        if dialog.apply_instrument_id:
            instrument = get_instrument(
                self.instrument_library, dialog.apply_instrument_id
            )
            if instrument is not None:
                track = self.current_track
                take = selected_take(track)
                if take and take.get("events"):
                    if not messagebox.askyesno(
                        "更換目前軌道樂器",
                        "目前軌道已有事件。更換樂器會保留節奏位置，但可能需要重新確認擊法。要繼續嗎？",
                    ):
                        instrument = None
                if instrument is not None:
                    track["instrument_id"] = instrument["instrument_id"]
                    track["instrument"] = instrument["name"]
                    track["sound_profile"] = instrument["sound_profile"]
                    track["brushes"] = [
                        {
                            "code": stroke["code"],
                            "label": stroke["name"],
                            "color": stroke["color"],
                            "stroke_id": stroke["stroke_id"],
                        }
                        for stroke in instrument.get("strokes", [])
                    ]
                    track["keymap"] = dict(
                        zip(("f", "g", "h", "j", "k"), [
                            stroke["code"] for stroke in instrument.get("strokes", [])[:5]
                        ])
                    )
        bind_project_to_library(self.project, self.instrument_library)
        attach_project_snapshot(self.project, self.instrument_library)
        self._mark_dirty()
        self._commit_history()
        self.refresh_all()
        self.status_var.set("樂器／擊法庫已儲存並同步到專案快照")

    def open_symbol_mapping_manager(self) -> None:
        """Open the explicit-save manager for cross-project suggestions."""

        if self.symbol_mapping_load_error:
            messagebox.showerror(
                "無法讀取辨識規則",
                "跨專案辨識規則檔目前無法讀取。為避免覆蓋原有資料，程式沒有自動重設。\n\n"
                f"設定檔：{self.symbol_mapping_store.path}\n"
                f"原因：{self.symbol_mapping_load_error}",
            )
            return
        try:
            validate_symbol_mappings(
                self.symbol_mappings, self.instrument_library
            )
            dialog = SymbolMappingManagerDialog(
                self.root,
                self.symbol_mappings,
                self.instrument_library,
                self.symbol_mapping_store,
            )
        except (OSError, SymbolMappingError, ValueError) as error:
            messagebox.showerror("無法開啟辨識規則", str(error))
            return
        self.root.wait_window(dialog)
        if dialog.result_mappings is None:
            return
        self.symbol_mappings = dialog.result_mappings
        self.symbol_mapping_load_error = None
        if hasattr(self, "photo_workspace"):
            self.photo_workspace._refresh_candidates()
        self.status_var.set("跨專案辨識符號對應規則已儲存")

    def open_openai_settings(self) -> bool:
        """Open the global provider settings without putting secrets in a project."""
        try:
            changed = show_openai_settings_dialog(
                self.root,
                self.openai_settings_store,
                self.openai_credential_store,
            )
        except (OpenAISettingsError, OpenAICredentialError) as error:
            messagebox.showerror("無法開啟 OpenAI 設定", str(error))
            return False
        if changed:
            self.status_var.set("OpenAI 設定已儲存；API key 不會寫入專案")
            if hasattr(self, "photo_workspace"):
                self.photo_workspace.update_provider_status()
        return changed

    def get_openai_settings(self) -> dict | None:
        try:
            return self.openai_settings_store.load()
        except OpenAISettingsError as error:
            messagebox.showerror("OpenAI 設定無效", str(error))
            return None

    def get_openai_api_key(self, *, prompt_if_missing: bool = False) -> str | None:
        try:
            api_key = self.openai_credential_store.get_api_key()
        except OpenAICredentialError as error:
            messagebox.showerror("無法讀取 OpenAI API key", str(error))
            return None
        if api_key or not prompt_if_missing:
            return api_key
        messagebox.showinfo(
            "需要 OpenAI API key",
            "請先設定 OpenAI API key。金鑰會存進 Windows 認證管理員，"
            "不會寫入樂譜專案。",
        )
        self.open_openai_settings()
        try:
            return self.openai_credential_store.get_api_key()
        except OpenAICredentialError as error:
            messagebox.showerror("無法讀取 OpenAI API key", str(error))
            return None

    def refresh_all(self) -> None:
        self._refresh_segment_choices()
        self._load_metadata_vars()
        self._refresh_tracks()
        self._refresh_brushes()
        self._refresh_measures()
        self.photo_workspace.refresh()
        self._update_title()

    def _refresh_segment_choices(self) -> None:
        labels = []
        for segment in self.project.get("segments", []):
            name = segment.get("piece_name") or "未命名曲目"
            section = segment.get("section_name") or segment.get("pattern_name") or "未命名段落"
            labels.append(f"{segment['segment_id']}｜{name}・{section}")
        self.segment_combo["values"] = labels
        if labels:
            self.segment_index = min(self.segment_index, len(labels) - 1)
            self.segment_choice_var.set(labels[self.segment_index])

    def _refresh_tracks(self) -> None:
        self.track_list.delete(0, "end")
        tracks = self.current_segment.get("tracks", [])
        for index, track in enumerate(tracks, start=1):
            marker = "✓" if track.get("is_complete") else "○"
            take = selected_take(track)
            count = len(take.get("events", [])) if take else 0
            self.track_list.insert("end", f"{marker} {index}. {track['instrument']}  ·  {count} 擊")
        self.track_index = min(self.track_index, max(0, len(tracks) - 1))
        if tracks:
            self.track_list.selection_set(self.track_index)
            self.track_list.activate(self.track_index)
            self.track_complete_var.set(bool(self.current_track.get("is_complete")))

    def _refresh_brushes(self) -> None:
        for child in self.brush_frame.winfo_children():
            child.destroy()
        brushes = ensure_track_brushes(self.current_track)
        self.active_brush_index = min(self.active_brush_index, len(brushes) - 1)
        for index, brush in enumerate(brushes):
            selected = index == self.active_brush_index
            button = tk.Button(
                self.brush_frame,
                text=f"{index + 1}  {brush['label']}",
                command=lambda value=index: self.select_brush(value),
                background=brush["color"] if selected else "#EEE8DD",
                foreground="white" if selected else INK,
                activebackground=brush["color"],
                activeforeground="white",
                relief="solid",
                borderwidth=1,
                padx=10,
                pady=6,
                font=(UI_FONT, 9, "bold" if selected else "normal"),
            )
            button.pack(side="left", padx=3)

    def _refresh_measures(self) -> None:
        bars = int(self.current_segment.get("bars", 1))
        max_start = ((bars - 1) // PAGE_SIZE) * PAGE_SIZE + 1
        self.page_start_bar = min(max(1, self.page_start_bar), max_start)
        for offset, widget in enumerate(self.measure_widgets):
            bar_number = self.page_start_bar + offset
            if bar_number <= bars:
                widget.load_bar(bar_number)
            else:
                widget.pack_forget()
        end = min(bars, self.page_start_bar + PAGE_SIZE - 1)
        self.page_label_var.set(f"第 {self.page_start_bar}–{end} 小節／共 {bars} 小節")

    def _load_metadata_vars(self) -> None:
        segment = self.current_segment
        for key, variable in self.meta_vars.items():
            variable.set(str(segment.get(key, "")))
        self.selection_reason_text.delete("1.0", "end")
        self.selection_reason_text.insert("1.0", segment.get("selection_reason", ""))
        self.notes_text.delete("1.0", "end")
        self.notes_text.insert("1.0", segment.get("notes", ""))

    def _update_title(self) -> None:
        if self.current_path and self.current_path.name == PROJECT_MANIFEST:
            filename = self.current_path.parent.name
        else:
            filename = self.current_path.name if self.current_path else "未命名專案"
        marker = " *" if self.dirty else ""
        self.root.title(
            f"{filename}{marker}｜國樂打擊樂譜格狀標註工具 v{__version__}"
        )

    def _segment_selected(self, _event: tk.Event | None = None) -> None:
        chosen_index = max(0, self.segment_combo.current())
        self.apply_metadata(silent=True)
        self.segment_index = min(chosen_index, len(self.project.get("segments", [])) - 1)
        self.track_index = 0
        self.page_start_bar = 1
        self.active_brush_index = 0
        self.refresh_all()

    def _track_selected(self, _event: tk.Event | None = None) -> None:
        selection = self.track_list.curselection()
        if not selection:
            return
        self.track_index = int(selection[0])
        self.active_brush_index = 0
        self.track_complete_var.set(bool(self.current_track.get("is_complete")))
        self._refresh_brushes()
        self._refresh_measures()

    def select_brush(self, index: int) -> None:
        brushes = ensure_track_brushes(self.current_track)
        if not 0 <= index < len(brushes):
            return
        self.active_brush_index = index
        self._refresh_brushes()
        for widget in self.measure_widgets:
            widget.redraw()
        self.status_var.set(f"筆刷 {index + 1}：{brushes[index]['label']}")

    def _ensure_take(self) -> dict:
        track = self.current_track
        take = selected_take(track)
        if take is None:
            take_id = next_take_id(track)
            take = {
                "take_id": take_id,
                "created_at": now_iso(),
                "quantization": "per-bar-grid",
                "count_in_bars": 0,
                "output_latency_ms": 0,
                "input_method": "grid",
                "review_status": "accepted",
                "events": [],
            }
            track.setdefault("takes", []).append(take)
            track["selected_take_id"] = take_id
        return take

    def _roll_duration_ticks(self, bar: int) -> int | None:
        if not self.roll_var.get():
            return None
        setting = ensure_bar_settings(self.current_segment)[bar - 1]
        step = grid_ticks(setting["subdivision"])
        choice = self.roll_duration_var.get()
        if choice == "2 格":
            return step * 2
        if choice == "1 拍":
            return ticks_per_beat(setting["time_signature"])
        if choice == "整小節":
            return step * grid_cell_count(
                setting["time_signature"], setting["subdivision"]
            )
        return step

    def paint_cell(self, bar: int, cell: int, *, erase: bool, commit: bool = True) -> None:
        take = self._ensure_take()
        tick = grid_event_tick(self.current_segment, bar, cell)
        existing = next((event for event in take["events"] if int(event.get("tick", -1)) == tick), None)
        changed = False
        if erase:
            if existing:
                take["events"].remove(existing)
                changed = True
        else:
            brush = self.active_brush
            if brush is None:
                return
            is_accent = bool(self.accent_var.get())
            is_ornament = bool(self.ornament_var.get())
            is_roll = bool(self.roll_var.get())
            duration = self._roll_duration_ticks(bar)
            setting = ensure_bar_settings(self.current_segment)[bar - 1]
            beat = (cell * grid_ticks(setting["subdivision"])) / ticks_per_beat(setting["time_signature"]) + 1
            if existing:
                updates = {
                    "stroke": brush["code"],
                    "stroke_id": brush.get("stroke_id"),
                    "key_code": str(self.active_brush_index + 1),
                    "is_accent": is_accent,
                    "is_ornament": is_ornament,
                    "is_roll": is_roll,
                    "duration": duration,
                }
                if any(existing.get(key) != value for key, value in updates.items()):
                    existing.update(updates)
                    changed = True
            else:
                take["events"].append({
                    "event_id": next_event_id(self.project),
                    "segment_id": self.current_segment["segment_id"],
                    "track_id": self.current_track["track_id"],
                    "take_id": take["take_id"],
                    "bar": bar,
                    "beat": round(beat, 6),
                    "tick": tick,
                    "raw_tick": tick,
                    "onset_ms": None,
                    "stroke": brush["code"],
                    "stroke_id": brush.get("stroke_id"),
                    "is_accent": is_accent,
                    "is_ornament": is_ornament,
                    "is_roll": is_roll,
                    "duration": duration,
                    "velocity": 100,
                    "key_code": str(self.active_brush_index + 1),
                    "input_group": "grid",
                    "quantization": setting["subdivision"],
                    "grid_index": cell,
                    "input_method": "grid",
                })
                changed = True
            if self.hit_sound_var.get() and self.live_hit_player.available:
                self.live_hit_player.play(
                    self.current_track.get("sound_profile", "bell"),
                    brush["code"],
                    accent=is_accent,
                    ornament=is_ornament,
                    roll=is_roll,
                    duration_ticks=duration,
                    tempo=float(self.current_segment.get("tempo", 90)),
                )

        if changed:
            take["events"].sort(key=lambda item: int(item["tick"]))
            self._paint_changed = True
            self._mark_dirty()
            self._refresh_measures()
            if commit:
                self.finish_paint_stroke()

    def finish_paint_stroke(self) -> None:
        if self._paint_changed:
            self._paint_changed = False
            self._commit_history()
            self._refresh_tracks()

    def clear_bar(self, bar: int) -> None:
        take = selected_take(self.current_track)
        if not take:
            return
        before = len(take.get("events", []))
        take["events"] = [event for event in take.get("events", []) if int(event.get("bar", 0)) != bar]
        if len(take["events"]) != before:
            self._mark_dirty()
            self._commit_history()
            self._refresh_tracks()
            self._refresh_measures()

    def change_bar_setting(self, bar: int, signature: str, subdivision: str) -> None:
        try:
            parse_time_signature(signature)
            grid_cell_count(signature, subdivision)
        except ValueError as exc:
            self.status_var.set(str(exc))
            self._refresh_measures()
            return
        settings = ensure_bar_settings(self.current_segment)
        old_settings = deepcopy(settings)
        current = settings[bar - 1]
        if current["time_signature"] == signature and current["subdivision"] == subdivision:
            return
        current["time_signature"] = signature
        current["subdivision"] = subdivision
        if bar == 1:
            self.current_segment["time_signature"] = signature
            self.current_segment["subdivision"] = subdivision
        reflow_events_after_settings_change(self.current_segment, old_settings)
        self._mark_dirty()
        self._commit_history()
        self._refresh_tracks()
        self._refresh_measures()
        self.status_var.set(f"第 {bar} 小節已改為 {signature}・{subdivision_label(subdivision)}")

    def previous_page(self) -> None:
        self.page_start_bar = max(1, self.page_start_bar - PAGE_SIZE)
        self._refresh_measures()

    def next_page(self) -> None:
        bars = int(self.current_segment.get("bars", 1))
        if self.page_start_bar + PAGE_SIZE <= bars:
            self.page_start_bar += PAGE_SIZE
        self._refresh_measures()

    def add_segment(self) -> None:
        self.apply_metadata(silent=True)
        segment = new_segment(next_segment_id(self.project))
        if self.project.get("segments"):
            source = self.current_segment
            segment["piece_id"] = source.get("piece_id", "P0001")
            segment["piece_name"] = source.get("piece_name", "")
            segment["source"] = source.get("source", "")
            segment["system"] = source.get("system", "")
        self.project.setdefault("segments", []).append(segment)
        self.segment_index = len(self.project["segments"]) - 1
        self.track_index = 0
        self.page_start_bar = 1
        self._mark_dirty()
        self._commit_history()
        self.refresh_all()

    def edit_navigation(self) -> None:
        """Open the paper-score repeat editor for the current segment."""
        if not self.apply_metadata(silent=True):
            return
        navigation = self.current_segment.setdefault("navigation", {})
        markers = navigation.get("markers", [])
        if not isinstance(markers, list):
            markers = []
        dialog = NavigationDialog(
            self.root,
            int(self.current_segment.get("bars", 1)),
            markers,
        )
        self.root.wait_window(dialog)
        if dialog.result is None:
            return
        navigation["markers"] = dialog.result
        navigation["playback_order"] = []
        navigation["review_status"] = "manually_reviewed"
        self._mark_dirty()
        self._commit_history()
        marker_count = len(dialog.result)
        self.status_var.set(f"已套用 {marker_count} 個反覆／結尾記號")

    def duplicate_segment(self) -> None:
        self.apply_metadata(silent=True)
        duplicate = duplicate_segment_structure(self.project, self.current_segment)
        self.project["segments"].append(duplicate)
        self.segment_index = len(self.project["segments"]) - 1
        self.track_index = 0
        self.page_start_bar = 1
        self._mark_dirty()
        self._commit_history()
        self.refresh_all()

    def add_track(self) -> None:
        dialog = TrackDialog(self.root)
        self.root.wait_window(dialog)
        if not dialog.result:
            return
        tracks = self.current_segment.setdefault("tracks", [])
        track = make_track(self.current_segment["segment_id"], len(tracks) + 1)
        track.update(dialog.result)
        track["keymap"] = dict(
            zip(("f", "g", "h", "j", "k"), [item["code"] for item in track["brushes"][:5]])
        )
        tracks.append(track)
        bind_project_to_library(self.project, self.instrument_library)
        attach_project_snapshot(self.project, self.instrument_library)
        self.track_index = len(tracks) - 1
        self.active_brush_index = 0
        self._mark_dirty()
        self._commit_history()
        self._refresh_tracks()
        self._refresh_brushes()
        self._refresh_measures()

    def edit_track(self) -> None:
        dialog = TrackDialog(self.root, self.current_track)
        self.root.wait_window(dialog)
        if not dialog.result:
            return
        self.current_track.update(dialog.result)
        brushes = ensure_track_brushes(self.current_track)
        self.current_track["keymap"] = dict(zip(("f", "g", "h", "j", "k"), [item["code"] for item in brushes[:5]]))
        bind_project_to_library(self.project, self.instrument_library)
        attach_project_snapshot(self.project, self.instrument_library)
        self.active_brush_index = min(self.active_brush_index, len(brushes) - 1)
        self._mark_dirty()
        self._commit_history()
        self._refresh_tracks()
        self._refresh_brushes()
        self._refresh_measures()

    def delete_track(self) -> None:
        tracks = self.current_segment.get("tracks", [])
        if len(tracks) <= 1:
            messagebox.showinfo("保留一條軌道", "每個段落至少需要一條樂器軌道。")
            return
        if not messagebox.askyesno("刪除軌道", f"確定刪除「{self.current_track['instrument']}」及其所有事件嗎？"):
            return
        tracks.pop(self.track_index)
        for index, track in enumerate(tracks, start=1):
            track["track_order"] = index
        self.track_index = min(self.track_index, len(tracks) - 1)
        self.active_brush_index = 0
        self._mark_dirty()
        self._commit_history()
        self._refresh_tracks()
        self._refresh_brushes()
        self._refresh_measures()

    def _track_complete_changed(self) -> None:
        self.current_track["is_complete"] = bool(self.track_complete_var.get())
        self._mark_dirty()
        self._commit_history()
        self._refresh_tracks()

    def _hit_sound_changed(self) -> None:
        if not self.live_hit_player.available:
            self.hit_sound_var.set(False)
            self.status_var.set(f"提示音不可用：{self.live_hit_player.error_message}")
            return
        enabled = bool(self.hit_sound_var.get()) and self.live_hit_player.available
        self.hit_sound_var.set(enabled)
        self.project["hit_sound_enabled"] = enabled
        self._mark_dirty()

    def apply_metadata(self, silent: bool = False) -> bool:
        segment = self.current_segment
        try:
            tempo = float(self.meta_vars["tempo"].get())
            bars = int(self.meta_vars["bars"].get())
            if tempo <= 0 or bars <= 0 or bars > 256:
                raise ValueError
        except ValueError:
            if not silent:
                messagebox.showerror("數值錯誤", "BPM 必須大於 0；小節數必須介於 1–256。")
            return False
        segment_id = self.meta_vars["segment_id"].get().strip()
        if not segment_id:
            if not silent:
                messagebox.showerror("資料不完整", "段落 ID 不可空白。")
            return False
        duplicates = [
            item for index, item in enumerate(self.project.get("segments", []))
            if index != self.segment_index and item.get("segment_id") == segment_id
        ]
        if duplicates:
            if not silent:
                messagebox.showerror("段落 ID 重複", "每個段落必須有唯一的 segment_id。")
            return False

        old_settings = deepcopy(ensure_bar_settings(segment))
        old_id = segment.get("segment_id")
        for key, variable in self.meta_vars.items():
            if key in {"tempo", "bars"}:
                continue
            segment[key] = variable.get().strip()
        segment["tempo"] = tempo
        segment["bars"] = bars
        segment["selection_reason"] = self.selection_reason_text.get("1.0", "end-1c").strip()
        segment["notes"] = self.notes_text.get("1.0", "end-1c").strip()
        segment["updated_at"] = now_iso()
        ensure_bar_settings(segment)
        if bars != len(old_settings):
            reflow_events_after_settings_change(segment, old_settings)
        if segment_id != old_id:
            for index, track in enumerate(segment.get("tracks", []), start=1):
                track["segment_id"] = segment_id
                track["track_id"] = f"{segment_id}-T{index:02d}"
                for take in track.get("takes", []):
                    for event in take.get("events", []):
                        event["segment_id"] = segment_id
                        event["track_id"] = track["track_id"]

        self.page_start_bar = min(self.page_start_bar, ((bars - 1) // PAGE_SIZE) * PAGE_SIZE + 1)
        self._mark_dirty()
        self._commit_history()
        self.refresh_all()
        if not silent:
            self.status_var.set("段落資料已套用")
        return True

    def play(self) -> None:
        if not self.apply_metadata(silent=True):
            return
        self._playback_request_id += 1
        request_id = self._playback_request_id
        stop_audio()
        self._discard_preview(self.preview_path)
        self.preview_path = None
        preview_path = Path(self.temp_directory.name) / f"preview-{request_id}.wav"
        segment = deepcopy(self.current_segment)
        tracks = segment.get("tracks", [])
        if self.play_scope_var.get() == "目前軌道":
            tracks = [deepcopy(segment["tracks"][self.track_index])]
        if self.current_page_only_var.get():
            start_tick, end_tick = page_tick_range(segment, self.page_start_bar, PAGE_SIZE)
        else:
            start_tick, end_tick = 0, None
            navigation = segment.get("navigation", {})
            markers = navigation.get("markers", []) if isinstance(navigation, dict) else []
            if self.expand_repeats_var.get() and markers:
                try:
                    playback_order = expand_playback_order(int(segment.get("bars", 1)), markers)
                    segment, tracks = materialize_playback_view(segment, tracks, playback_order)
                except NavigationError as exc:
                    self.status_var.set("反覆記號資料需要修正")
                    messagebox.showerror("無法展開反覆", str(exc))
                    return
        include_metronome = bool(self.metronome_var.get())
        loop = bool(self.loop_var.get())
        self.status_var.set("正在產生播放預覽…")

        def worker() -> None:
            try:
                render_segment_wav(
                    preview_path,
                    segment,
                    tracks,
                    include_metronome=include_metronome,
                    start_tick=start_tick,
                    end_tick=end_tick,
                )
                self.root.after(
                    0,
                    lambda: self._begin_playback(preview_path, loop, request_id),
                )
            except Exception as exc:
                message = str(exc)
                self.root.after(
                    0,
                    lambda: self._playback_failed(message, preview_path, request_id),
                )

        threading.Thread(target=worker, name="guoyue-grid-render", daemon=True).start()

    @staticmethod
    def _discard_preview(path: Path | None) -> None:
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _begin_playback(self, path: Path, loop: bool, request_id: int) -> None:
        if request_id != self._playback_request_id:
            self._discard_preview(path)
            return
        try:
            play_wav(path, loop=loop)
            self.preview_path = path
            self.status_var.set("循環播放中" if loop else "播放中")
        except Exception as exc:
            self._playback_failed(str(exc), path, request_id)

    def _playback_failed(self, message: str, path: Path, request_id: int) -> None:
        self._discard_preview(path)
        if request_id != self._playback_request_id:
            return
        self.preview_path = None
        self.status_var.set("播放預覽產生失敗")
        messagebox.showerror("播放失敗", message)

    def stop(self) -> None:
        self._playback_request_id += 1
        stop_audio()
        self._discard_preview(self.preview_path)
        self.preview_path = None
        self.status_var.set("已停止")

    def _mark_dirty(self) -> None:
        self.dirty = True
        self._update_title()

    def _reset_history(self) -> None:
        self.history = [deepcopy(self.project)]
        self.history_index = 0

    def _commit_history(self) -> None:
        snapshot = deepcopy(self.project)
        self.history = self.history[: self.history_index + 1]
        self.history.append(snapshot)
        if len(self.history) > 80:
            self.history.pop(0)
        else:
            self.history_index += 1

    def undo(self) -> None:
        if self.history_index <= 0:
            return
        self.history_index -= 1
        self.project = deepcopy(self.history[self.history_index])
        self._normalize_project()
        self._mark_dirty()
        self.refresh_all()
        self.status_var.set("已復原")

    def redo(self) -> None:
        if self.history_index >= len(self.history) - 1:
            return
        self.history_index += 1
        self.project = deepcopy(self.history[self.history_index])
        self._normalize_project()
        self._mark_dirty()
        self.refresh_all()
        self.status_var.set("已重做")

    def _confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        answer = messagebox.askyesnocancel("尚未儲存", "目前變更尚未儲存。要先儲存嗎？")
        if answer is None:
            return False
        if answer:
            return self.save()
        return True

    def new_project_file(self) -> None:
        if not self._confirm_discard():
            return
        stop_audio()
        self._project_generation += 1
        self.project = new_project()
        self.current_path = None
        self.segment_index = self.track_index = 0
        self.page_start_bar = 1
        self.active_brush_index = 0
        self.dirty = False
        self._normalize_project()
        self._reset_history()
        self.refresh_all()
        self.status_var.set("已建立新專案")

    def open_project(self) -> None:
        if not self._confirm_discard():
            return
        path = filedialog.askopenfilename(
            title="開啟標註專案（專案資料夾內請選 project.json）",
            filetypes=(("國樂標註專案", "*.json"), ("所有檔案", "*.*")),
        )
        if not path:
            return
        try:
            self._project_generation += 1
            self.project = load_project(path)
            self.current_path = Path(path)
            self.segment_index = self.track_index = 0
            self.page_start_bar = 1
            self.active_brush_index = 0
            self._normalize_project()
            self.hit_sound_var.set(bool(self.project.get("hit_sound_enabled", True)) and self.live_hit_player.available)
            self.dirty = False
            self._reset_history()
            self.refresh_all()
            self.status_var.set(f"已開啟 {self.current_path.name}")
        except Exception as exc:
            messagebox.showerror("無法開啟", str(exc))

    def save(self) -> bool:
        if not self.apply_metadata(silent=True):
            return False
        if self.current_path is None:
            return self.save_as()
        try:
            save_project(self.project, self.current_path)
            self.dirty = False
            self._update_title()
            self.status_var.set(f"已儲存 {self.current_path.name}")
            return True
        except Exception as exc:
            messagebox.showerror("儲存失敗", str(exc))
            return False

    def save_as(self, *, dialog_title: str | None = None) -> bool:
        path = filedialog.asksaveasfilename(
            title=dialog_title or "建立可攜式專案資料夾",
            defaultextension=PROJECT_BUNDLE_SUFFIX,
            filetypes=(("國樂標註專案資料夾", f"*{PROJECT_BUNDLE_SUFFIX}"),),
            initialfile=f"guoyue_rhythm{PROJECT_BUNDLE_SUFFIX}",
        )
        if not path:
            return False
        self.current_path = project_manifest_path(path)
        return self.save()

    def export(self) -> None:
        if not self.apply_metadata(silent=True):
            return
        directory = filedialog.askdirectory(title="選擇匯出資料夾")
        if not directory:
            return
        try:
            paths = export_dataset(self.project, directory)
            messagebox.showinfo("匯出完成", "已產生：\n" + "\n".join(path.name for path in paths))
            self.status_var.set("訓練資料匯出完成")
        except Exception as exc:
            messagebox.showerror("匯出失敗", str(exc))

    def on_close(self) -> None:
        if not self._confirm_discard():
            return
        self._closing = True
        self._playback_request_id += 1
        shutdown_audio()
        self.temp_directory.cleanup()
        self.root.destroy()


def run() -> None:
    root = tk.Tk()
    GridAnnotatorApp(root)
    root.mainloop()
