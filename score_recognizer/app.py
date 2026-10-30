"""程式 A 的本地 Tk 介面；辨識排程與所有畫面更新在不同執行緒。"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import queue
import re
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from PIL import ImageTk

from guoyue_annotator.openai_settings import OpenAICredentialStore, OpenAISettingsStore, create_default_settings
from guoyue_annotator.photo_assets import asset_file_path
from .canvas import ScoreCanvas
from .contract import NOTATIONS, ContractError
from .provider import crop_image, recognize
from .project_setup import ask_new_project, default_projects_directory
from .workspace import Workspace, MANIFEST
from .geometry import rectangle_quad, quad_bounds, validate_quad
from .project_lock import ProjectLock, load_locked
from .omr import find_audiveris, save_audiveris_path, recognize_local, supplement_with_openai

MODEL_CHOICES = ("gpt-6-luna", "gpt-6.1-terra", "gpt-6.1-sol", "gpt-6-astra")
ENGINES = ("本機 OMR（印刷五線譜）", "OpenAI（只補辨 OMR 疑問小節）", "OpenAI（整個所選範圍）")

STATUS = {"draft": "待確認範圍", "pending": "待辨識", "running": "辨識中",
          "succeeded": "已辨識・待核對", "empty": "無聲部結果・需檢查", "failed": "失敗・可重試",
          "interrupted": "已中斷・可重試", "stale": "來源已改・需重辨"}


class RecognizerApp:
    def __init__(self, root, workspace=None, project_lock=None):
        self.root = root
        self.project_lock = project_lock or (ProjectLock(workspace.root) if workspace else None)
        root.bind("<Destroy>", lambda e: self.release_lock() if e.widget == root else None, add="+")
        self.workspace = workspace
        self.asset_id = None
        self.region_id = None
        self.form_region_id = None
        self.busy = False
        self.messages = queue.Queue()
        self.stop_requested = threading.Event()
        self.session_key = None
        self.settings_store = OpenAISettingsStore()
        self.credentials = OpenAICredentialStore()
        try:
            self.settings = self.settings_store.load(create_if_missing=False)
        except Exception:
            self.settings = create_default_settings()
        root.title("MusicAI A｜樂譜辨識")
        root.geometry("1400x900")
        root.minsize(1100, 720)
        ui_font = "Microsoft JhengHei UI"
        root.option_add("*Font", (ui_font, 10))
        style = ttk.Style(root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TButton", padding=(9, 6))
        style.configure("Header.TLabel", font=(ui_font, 15, "bold"))
        style.configure("Treeview", rowheight=28)
        self.build()
        self.refresh_pages()
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll_id = root.after(150, self.poll)

    def guarded(self, action):
        if self.busy:
            messagebox.showinfo("辨識進行中", "請等待目前這批完成，或按「停止後續辨識」。", parent=self.root)
            return
        try:
            action()
        except Exception as error:
            messagebox.showerror("無法完成", str(error), parent=self.root)

    def button(self, parent, text, action, **kwargs):
        return ttk.Button(parent, text=text, command=lambda: self.guarded(action), **kwargs)

    def build(self):
        header = ttk.Frame(self.root, padding=(14, 12, 14, 6))
        header.pack(fill="x")
        ttk.Label(header, text="樂譜辨識", style="Header.TLabel").pack(side="left")
        ttk.Label(header, text="  ① 匯入照片　② 登記範圍與小節　③ 確認送出　④ 匯出待核對資料").pack(side="left", padx=15)
        toolbar = ttk.Frame(self.root, padding=(12, 2, 12, 8))
        toolbar.pack(fill="x")
        for label, action in (("新增專案", self.new_project), ("開啟專案", self.open_project),
                              ("匯入 JPG／PNG", self.import_images), ("保存", self.save),
                              ("匯出辨識套件", self.export), ("樂譜共通資訊", self.score_context),
                              ("OpenAI 設定", self.openai_settings), ("本機 OMR 設定", self.omr_settings)):
            self.button(toolbar, label, action).pack(side="left", padx=3)
        self.project_text = tk.StringVar(value="尚未建立專案")
        ttk.Label(self.root, textvariable=self.project_text, padding=(16, 0, 10, 7), foreground="#53657c").pack(fill="x")
        panes = ttk.Panedwindow(self.root, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=12)
        left = ttk.Frame(panes, padding=(0, 0, 8, 0))
        panes.add(left, weight=0)
        ttk.Label(left, text="來源頁面", style="Header.TLabel").pack(anchor="w", pady=5)
        self.pages = ttk.Treeview(left, columns=("page", "name"), show="headings", height=15, selectmode="browse")
        self.pages.heading("page", text="頁碼")
        self.pages.heading("name", text="檔名")
        self.pages.column("page", width=48, stretch=False)
        self.pages.column("name", width=150)
        self.pages.pack(fill="both", expand=True)
        self.pages.bind("<<TreeviewSelect>>", self.page_selected)
        page_controls = ttk.Frame(left)
        page_controls.pack(fill="x", pady=4)
        self.button(page_controls, "↑", lambda: self.move_page(-1)).pack(side="left")
        self.button(page_controls, "↓", lambda: self.move_page(1)).pack(side="left", padx=3)
        self.button(page_controls, "改頁碼", self.page_number).pack(side="left")
        ttk.Label(left, text="依檔名中的數字排序\n頁碼可修改、頁序可上下調整", foreground="#53657c").pack(anchor="w", pady=6)
        center = ttk.Frame(panes)
        panes.add(center, weight=1)
        image_tools = ttk.Frame(center)
        image_tools.pack(fill="x", pady=4)
        image_primary = ttk.Frame(image_tools)
        image_primary.pack(fill="x")
        image_auxiliary = ttk.Frame(image_tools)
        image_auxiliary.pack(fill="x",pady=(3,0))
        for label, mode in (("拖曳小節線", "select"), ("框選主範圍", "main"), ("框選參考資訊", "context")):
            self.button(image_primary, label, lambda value=mode: self.set_mode(value)).pack(side="left", padx=2)
        self.button(image_auxiliary, "整頁參考", self.use_full_page_context).pack(side="left", padx=2)
        self.button(image_auxiliary, "調整斜框", self.edit_quad).pack(side="left", padx=2)
        self.button(image_auxiliary, "＋", lambda: self.viewer.zoom(1.2)).pack(side="right")
        self.button(image_auxiliary, "－", lambda: self.viewer.zoom(1 / 1.2)).pack(side="right", padx=2)
        self.viewer = ScoreCanvas(center, self.box_drawn, self.boundaries_changed, self.quad_changed)
        self.viewer.pack(fill="both", expand=True)
        self.mode_text = tk.StringVar(value="藍框：辨識來源　橘線：小節分界　紫框：參考資訊　綠框：結果證據")
        ttk.Label(center, textvariable=self.mode_text, wraplength=650, padding=5).pack(fill="x")
        right_host = ttk.Frame(panes, padding=(10, 0, 0, 0))
        panes.add(right_host, weight=0)
        right_host.rowconfigure(0, weight=1)
        right_host.columnconfigure(0, weight=1)
        settings_canvas = tk.Canvas(right_host, width=380, highlightthickness=0, bg="#dcdad5")
        settings_scroll = ttk.Scrollbar(right_host, orient="vertical", command=settings_canvas.yview)
        settings_canvas.configure(yscrollcommand=settings_scroll.set)
        settings_canvas.grid(row=0, column=0, sticky="nsew")
        settings_scroll.grid(row=0, column=1, sticky="ns")
        right = ttk.Frame(settings_canvas, padding=(0, 0, 5, 0))
        settings_window = settings_canvas.create_window((0, 0), window=right, anchor="nw")
        right.bind("<Configure>", lambda event: settings_canvas.configure(scrollregion=settings_canvas.bbox("all")))
        settings_canvas.bind("<Configure>", lambda event: settings_canvas.itemconfigure(settings_window, width=event.width))
        def scroll_settings(event):
            target = self.root.winfo_containing(event.x_root, event.y_root)
            if target and (str(target).startswith(str(right)) or target == settings_canvas):
                units = -int(event.delta / 120)*3 if abs(event.delta) >= 120 else (-1 if event.delta > 0 else 1)
                settings_canvas.yview_scroll(units, "units")
        self.root.bind_all("<MouseWheel>", scroll_settings, add="+")
        ttk.Label(right, text="辨識範圍", style="Header.TLabel").pack(anchor="w", pady=5)
        controls = ttk.Frame(right)
        controls.pack(fill="x")
        self.button(controls, "新增範圍", self.new_region_mode).pack(side="left")
        self.button(controls, "裁切預覽", self.preview).pack(side="left", padx=4)
        self.button(controls, "清除參考框", self.clear_context).pack(side="left")
        self.button(right, "移除此範圍（保留歷史辨識）", self.remove_region).pack(anchor="w", pady=(3, 0))
        self.button(right, "拆成逐小節（舊結果留存）", self.split_current_region).pack(anchor="w", pady=(3, 0))
        self.button(right, "確認本頁所有拆分小節", self.confirm_current_page_splits).pack(anchor="w", pady=(3, 0))
        self.regions = ttk.Treeview(right, columns=("bars", "status"), show="headings", height=5, selectmode="browse")
        self.regions.heading("bars", text="原譜小節")
        self.regions.heading("status", text="狀態")
        self.regions.column("bars", width=100, stretch=False)
        self.regions.column("status", width=235)
        self.regions.pack(fill="x", pady=7)
        self.regions.bind("<<TreeviewSelect>>", self.region_selected)
        defaults = ttk.Frame(right)
        defaults.pack(fill="x", pady=(2, 0))
        ttk.Label(defaults, text="新範圍預設").pack(side="left")
        self.new_range_bar_count = tk.StringVar(value="4")
        counts = (4, 5, 8) + tuple(n for n in range(1, 33) if n not in {4, 5, 8})
        count_selector = ttk.Combobox(defaults, textvariable=self.new_range_bar_count, values=counts, state="readonly", width=5)
        count_selector.pack(side="left", padx=(12, 4))
        def remember_count(event):
            self.guarded(self.remember_new_range_count)
            self.new_range_bar_count.set(str(self.workspace.default_region_bar_count()) if self.workspace else "4")
        count_selector.bind("<<ComboboxSelected>>", remember_count)
        ttk.Label(defaults, text="小節").pack(side="left")
        ttk.Label(right, text="選好自動記住於此專案；已登記的範圍不更動。", foreground="#53657c").pack(anchor="w", pady=(2, 6))
        self.fields = {}
        form = ttk.Frame(right)
        form.pack(fill="x")
        for index, (key, label) in enumerate((("start_bar", "起始小節"), ("bar_count", "小節數"),
                                               ("notation", "譜種"), ("time_signature", "拍號（可留白）"),
                                               ("key_signature", "調號（可留白）"))):
            ttk.Label(form, text=label).grid(row=index, column=0, sticky="w", pady=3)
            variable = self.fields[key] = tk.StringVar()
            if key == "notation":
                widget = ttk.Combobox(form, textvariable=variable, values=list(NOTATIONS.values()), state="readonly", width=21)
            else:
                widget = ttk.Entry(form, textvariable=variable, width=24)
            widget.grid(row=index, column=1, sticky="ew", padx=(12, 0), pady=3)
        form.columnconfigure(1, weight=1)
        ttk.Label(right, text="預期樂器（選填，以逗號分隔）").pack(anchor="w", pady=(7, 2))
        self.expected = tk.StringVar()
        ttk.Entry(right, textvariable=self.expected).pack(fill="x")
        ttk.Label(right, text="範圍補充（拍號承接、樂器文字等）").pack(anchor="w", pady=(7, 2))
        self.notes = tk.Text(right, height=3, width=37, wrap="word", undo=True)
        self.notes.pack(fill="x")
        form_buttons = ttk.Frame(right)
        form_buttons.pack(fill="x", pady=7)
        self.button(form_buttons, "保存設定", lambda: self.apply_form(False)).pack(side="left")
        self.button(form_buttons, "依小節數重排分界", self.redistribute).pack(side="left", padx=4)
        self.button(right, "確認範圍與分界", lambda: self.apply_form(True)).pack(fill="x", pady=3)
        ttk.Label(right, text="修改範圍後需再次確認；舊辨識保留為歷史。", foreground="#53657c").pack(anchor="w", pady=3)
        actions = ttk.Frame(right_host)
        actions.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(7, 0))
        self.engine = tk.StringVar(value=ENGINES[0])
        ttk.Combobox(actions, textvariable=self.engine, values=ENGINES, state="readonly").pack(fill="x", pady=(0, 4))
        self.button(actions, "辨識目前範圍…", lambda: self.start_batch("current")).pack(fill="x", pady=(0, 3))
        self.button(actions, "辨識所有已確認待辦…", lambda: self.start_batch("pending")).pack(fill="x", pady=3)
        self.button(actions, "只重試失敗／中斷…", lambda: self.start_batch("failed")).pack(fill="x", pady=3)
        ttk.Button(actions, text="停止後續辨識", command=self.stop_requested.set).pack(fill="x", pady=3)
        self.button(actions, "查看結果／待確認項目", self.show_results).pack(fill="x", pady=(8, 3))
        self.progress = ttk.Progressbar(actions, mode="determinate")
        self.progress.pack(fill="x", pady=6)
        self.status_text = tk.StringVar(value="準備就緒")
        ttk.Label(self.root, textvariable=self.status_text, padding=(16, 7), foreground="#285271").pack(fill="x")

    def require_workspace(self):
        if not self.workspace:
            raise ContractError("請先建立或開啟新版辨識專案")
        return self.workspace

    def current_region(self):
        self.require_workspace()
        if not self.region_id:
            raise ContractError("請先選取或框選一個辨識範圍")
        return self.workspace.region(self.region_id)

    def new_project(self):
        if self.workspace:
            self.save()
        workspace = ask_new_project(self.root)
        if workspace is not None:
            lock = ProjectLock(workspace.root)
            self.release_lock()
            self.project_lock = lock
            self.workspace = workspace
            self.reset_selection()

    def open_project(self):
        if self.workspace:
            self.save()
        initial = self.workspace.root.parent if self.workspace else default_projects_directory()
        if not initial.is_dir():
            initial = initial.parent
        path = filedialog.askopenfilename(title="開啟辨識專案", initialdir=str(initial), filetypes=[("辨識專案", MANIFEST), ("JSON", "*.json")], parent=self.root)
        if path:
            target = Path(path).resolve().parent
            if self.workspace and target == self.workspace.root:
                return
            workspace, lock = load_locked(path)
            self.release_lock()
            self.project_lock = lock
            self.workspace = workspace
            self.reset_selection()

    def reset_selection(self):
        self.asset_id = self.region_id = None
        self.form_region_id = None
        self.viewer.image = None
        self.viewer.set_region(None)
        self.refresh_pages()

    def save(self):
        self.require_workspace()
        if self.region_id:
            self.apply_form(None)
        self.workspace.save()
        self.status_text.set("已保存照片、範圍與所有辨識進度")

    def import_images(self):
        workspace = self.require_workspace()
        paths = filedialog.askopenfilenames(title="選取整份樂譜照片", filetypes=[("照片", "*.jpg *.jpeg *.png")], parent=self.root)
        if not paths:
            return
        added, duplicates, errors = workspace.import_images(paths)
        self.refresh_pages()
        self.status_text.set(f"匯入 {len(added)} 頁；相同照片略過 {len(duplicates)} 張")
        if errors:
            messagebox.showwarning("部分照片未匯入", "\n".join(errors), parent=self.root)

    def refresh_pages(self):
        self.pages.delete(*self.pages.get_children())
        self.new_range_bar_count.set(str(self.workspace.default_region_bar_count()) if self.workspace else "4")
        if not self.workspace:
            return
        self.project_text.set(f"{self.workspace.package['title']}　｜　{self.workspace.root}")
        for asset in self.workspace.package["assets"]:
            self.pages.insert("", "end", iid=asset["asset_id"], values=(asset["printed_page_number"], asset["original_filename"]))
        ids = self.pages.get_children()
        if ids:
            selected = self.asset_id if self.asset_id in ids else ids[0]
            self.pages.selection_set(selected)
            self.pages.see(selected)
        self.refresh_regions()

    def page_selected(self, event=None):
        selection = self.pages.selection()
        if not selection:
            return
        if self.busy:
            if self.asset_id and selection[0] != self.asset_id:
                self.pages.selection_set(self.asset_id)
            return
        asset_id = selection[0]
        if self.asset_id == asset_id:
            return
        if self.form_region_id:
            try:
                self.save_form(None)
            except Exception as error:
                self.pages.selection_set(self.asset_id)
                messagebox.showerror("請先完成目前範圍設定", str(error), parent=self.root)
                return
        self.asset_id = asset_id
        self.region_id = None
        self.form_region_id = None
        try:
            self.viewer.load(asset_file_path(self.workspace.root, self.workspace.asset(asset_id), must_exist=True))
            self.viewer.set_region(None)
            self.refresh_regions()
        except Exception as error:
            messagebox.showerror("無法開啟照片", str(error), parent=self.root)

    def refresh_regions(self):
        self.regions.delete(*self.regions.get_children())
        if not self.workspace or not self.asset_id:
            return
        for region in self.workspace.package["regions"]:
            if region["asset_id"] == self.asset_id:
                status = STATUS[self.workspace.status(region)]
                run = self.workspace.latest_run(region)
                if run and run.get("omr_review") and run["status"] == "succeeded":
                    status = f"OMR：{len(run['omr_review'])} 小節有結構疑問"
                self.regions.insert("", "end", iid=region["region_id"], values=(f"{region['start_bar']}–{region['start_bar'] + region['bar_count'] - 1}", status))
        ids = self.regions.get_children()
        if ids:
            self.regions.selection_set(self.region_id if self.region_id in ids else ids[0])

    def region_selected(self, event=None):
        selection = self.regions.selection()
        if not selection:
            return
        target = selection[0]
        if self.busy and self.region_id and target != self.region_id:
            self.regions.selection_set(self.region_id)
            return
        if self.form_region_id == target:
            self.viewer.set_region(self.workspace.region(target))
            return
        if self.form_region_id:
            try:
                self.save_form(None)
            except Exception as error:
                self.regions.selection_set(self.form_region_id)
                messagebox.showerror("請先完成目前範圍設定", str(error), parent=self.root)
                return
        self.region_id = target
        self.form_region_id = target
        region = self.workspace.region(self.region_id)
        for key, variable in self.fields.items():
            variable.set(NOTATIONS[region[key]] if key == "notation" else region[key])
        self.expected.set("，".join(region["expected_instruments"]))
        self.notes.delete("1.0", "end")
        self.notes.insert("1.0", region["context_notes"])
        self.viewer.set_region(region)

    def move_page(self, delta):
        self.require_workspace()
        if self.asset_id:
            self.workspace.move_page(self.asset_id, delta)
            self.refresh_pages()

    def page_number(self):
        self.require_workspace()
        if not self.asset_id:
            return
        asset = self.workspace.asset(self.asset_id)
        value = simpledialog.askstring("確認頁碼", "譜上頁碼（可使用文字）：", initialvalue=asset["printed_page_number"], parent=self.root)
        if value is not None:
            asset.update(printed_page_number=value.strip(), page_number_source="manual")
            self.workspace.changed()
            self.workspace.save()
            self.refresh_pages()

    def set_mode(self, mode):
        self.viewer.mode = mode
        self.mode_text.set({"select": "拖曳橘線修正小節。第一條線放在音樂開始處，左側保留樂器名稱。",
                            "main": "在圖片上拖曳主範圍，包含所有節奏樂器、左側名稱與上方演奏記號。",
                            "context": "拖曳參考框：拍號、調號、速度或整頁反覆記號等。參考框不提供額外節奏。",
                            "quad": "拖曳四個藍點，框住所需聲部；保留符頭、符尾與上方記號。按裁切預覽查看攤正結果，再用「拖曳小節線」對齊橘線。"}[mode])
        self.viewer.redraw()

    def release_lock(self):
        if self.project_lock:
            self.project_lock.close()
            self.project_lock = None

    def edit_quad(self):
        region = self.current_region()
        if not region.get("quad"):
            self.workspace.update_region(self.region_id, quad=rectangle_quad(region["bbox"]), confirmed=False)
        self.viewer.set_region(self.current_region())
        self.set_mode("quad")

    def quad_changed(self, points):
        def action():
            validate_quad(points)
            self.workspace.update_region(self.region_id, bbox=quad_bounds(points), quad=points, confirmed=False)
            self.refresh_regions()
            self.set_mode("quad")
            self.status_text.set("斜框已保存；請查看裁切預覽、調整小節線並重新確認範圍")
        self.guarded(action)

    def new_region_mode(self):
        self.require_workspace()
        if not self.asset_id:
            raise ContractError("請先匯入並選取照片")
        if self.form_region_id:
            self.save_form(None)
        self.region_id = None
        self.form_region_id = None
        self.regions.selection_remove(*self.regions.selection())
        self.viewer.set_region(None)
        self.set_mode("main")

    def box_drawn(self, mode, box):
        def action():
            self.require_workspace()
            if not self.asset_id:
                return
            if mode == "context":
                region = self.current_region()
                self.workspace.update_region(region["region_id"], context_bbox=box, confirmed=False)
            elif self.region_id:
                self.workspace.update_region(self.region_id, bbox=box, quad=None, confirmed=False)
            else:
                self.region_id = self.workspace.new_region(self.asset_id, box)["region_id"]
            self.set_mode("select")
            self.refresh_regions()
        self.guarded(action)

    def boundaries_changed(self, boundaries):
        def action():
            self.workspace.update_region(self.region_id, boundaries=boundaries, confirmed=False)
            self.viewer.set_region(self.current_region())
            self.refresh_regions()
            self.status_text.set("小節線已保存；請再次按「確認範圍與分界」")
        self.guarded(action)

    def form_values(self):
        try:
            start = int(self.fields["start_bar"].get())
            count = int(self.fields["bar_count"].get())
        except ValueError as error:
            raise ContractError("起始小節與小節數請填整數") from error
        notation = {value: key for key, value in NOTATIONS.items()}.get(self.fields["notation"].get(), "auto")
        return {"start_bar": start, "bar_count": count, "notation": notation,
                  "time_signature": self.fields["time_signature"].get().strip(),
                  "key_signature": self.fields["key_signature"].get().strip(),
                  "context_notes": self.notes.get("1.0", "end").strip(),
                  "expected_instruments": [text.strip() for text in re.split(r"[,，、;；\n]", self.expected.get()) if text.strip()]}

    def save_form(self, confirmed):
        region = self.workspace.region(self.form_region_id or self.region_id)
        values = self.form_values()
        if values["bar_count"] != region["bar_count"]:
            raise ContractError("小節數已改，請先按「依小節數重排分界」，再拖曳橘線")
        has_changes = any(region[key] != value for key, value in values.items())
        values["confirmed"] = confirmed if confirmed is not None else (region["confirmed"] and not has_changes)
        return self.workspace.update_region(region["region_id"], **values)

    def apply_form(self, confirmed):
        self.current_region()
        region = self.save_form(confirmed)
        self.viewer.set_region(region)
        self.refresh_regions()
        self.status_text.set("範圍已確認，可以辨識" if region["confirmed"] else "設定已保存，請確認範圍與分界")

    def redistribute(self):
        region = self.current_region()
        values = self.form_values()
        count = values["bar_count"]
        if not 1 <= count <= 32:
            raise ContractError("每個範圍請填 1–32 小節")
        first, last = region["boundaries"][0], region["boundaries"][-1]
        boundaries = [first + (last - first) * i / count for i in range(count + 1)]
        self.workspace.update_region(self.region_id, **values, boundaries=boundaries, confirmed=False)
        self.refresh_regions()
        self.status_text.set("已按小節數等分；請拖曳橘線對準實際小節線，再確認")

    def remember_new_range_count(self):
        workspace = self.require_workspace()
        try:
            count = int(self.new_range_bar_count.get())
            workspace.set_default_region_bar_count(count)
        except (ValueError, ContractError):
            self.new_range_bar_count.set(str(workspace.default_region_bar_count()))
            raise
        self.status_text.set(f"已記住：本專案後續新增範圍預設 {count} 小節，現有範圍不變；橘線仍需對齊原譜。")

    def clear_context(self):
        region = self.current_region()
        self.workspace.update_region(region["region_id"], context_bbox=None, confirmed=False)
        self.refresh_regions()

    def use_full_page_context(self):
        region = self.current_region()
        self.workspace.update_region(region["region_id"], context_bbox={"x": 0, "y": 0, "width": 1, "height": 1}, confirmed=False)
        self.viewer.set_region(self.current_region())
        self.refresh_regions()
        self.status_text.set("已加入整頁參考，檢查裁圖外的 D.C.／Coda、調號與速度；送出時增加一張參考圖片，請先預覽並重新確認。不會自動辨識。")

    def split_current_region(self):
        self.apply_form(None)
        region = self.current_region()
        children = self.workspace.split_region_by_measure(region["region_id"], replace_existing=True)
        self.region_id = children[0]["region_id"]
        self.form_region_id = None
        self.refresh_regions()
        self.region_selected()
        self.status_text.set(f"已拆成 {len(children)} 個逐小節草稿，原範圍與舊結果已留存；檢查後按「確認本頁所有拆分小節」，再選 OpenAI 引擎。尚未送出辨識。")

    def confirm_current_page_splits(self):
        workspace = self.require_workspace()
        if not self.asset_id:
            raise ContractError("請先選擇要確認的來源頁面")
        if self.form_region_id:
            self.save_form(None)
        total, changed = workspace.confirm_page_split_regions(self.asset_id)
        self.refresh_regions()
        if self.region_id:
            self.viewer.set_region(self.current_region())
        page = workspace.asset(self.asset_id)["printed_page_number"]
        self.status_text.set(f"第 {page} 頁共 {total} 個拆分小節已確認（本次新增 {changed} 個）；尚未送出辨識。")

    def remove_region(self):
        region = self.current_region()
        if messagebox.askyesno("移除辨識範圍", "移除此範圍？照片及歷史辨識仍保留。", parent=self.root):
            self.workspace.remove_region(region["region_id"])
            self.region_id = None
            self.form_region_id = None
            self.viewer.set_region(None)
            self.refresh_regions()

    def preview(self):
        self.apply_form(None)
        region = self.current_region()
        window = tk.Toplevel(self.root)
        window.title("實際送出的圖片預覽")
        path = asset_file_path(self.workspace.root, self.workspace.asset(region["asset_id"]))
        window.photos = []
        for label, box in (("主要辨識圖片", region["bbox"]), ("參考圖片（只讀背景資訊）", region.get("context_bbox"))):
            if not box:
                continue
            ttk.Label(window, text=label, padding=6).pack(anchor="w")
            picture = crop_image(path, box, region.get("quad") if label == "主要辨識圖片" else None)
            picture.thumbnail((1000, 380))
            photo = ImageTk.PhotoImage(picture)
            window.photos.append(photo)
            ttk.Label(window, image=photo).pack(padx=10, pady=5)
        ttk.Label(window, text=f"小節 {region['start_bar']}–{region['start_bar'] + region['bar_count'] - 1}　｜　{NOTATIONS[region['notation']]}　｜　模型 {self.settings['model']}", padding=10).pack()

    def score_context(self):
        workspace = self.require_workspace()
        window = tk.Toplevel(self.root)
        window.title("樂譜共通資訊")
        ttk.Label(window, text="填寫確定的背景：起始拍號、調號、速度、樂器縮寫。\n遇變拍號可在各範圍補充；未知請留白。", padding=12).pack(anchor="w")
        text = tk.Text(window, width=75, height=12, wrap="word")
        text.pack(padx=12)
        text.insert("1.0", workspace.package["score_context"])
        def save():
            if self.busy:
                return
            value = text.get("1.0", "end").strip()
            if value != workspace.package["score_context"]:
                workspace.package["score_context"] = value
                workspace.changed()
                workspace.save()
                self.refresh_regions()
            window.destroy()
        ttk.Label(window, text="修改共通資訊會使既有辨識過期，原始結果仍保留。", padding=10).pack()
        ttk.Button(window, text="保存", command=lambda: self.guarded(save)).pack(pady=10)

    def openai_settings(self):
        window = tk.Toplevel(self.root)
        window.title("OpenAI 辨識設定")
        frame = ttk.Frame(window, padding=15)
        frame.pack(fill="both", expand=True)
        model = tk.StringVar(value=self.settings["model"])
        detail = tk.StringVar(value=self.settings["image_detail"])
        key = tk.StringVar()
        remember = tk.BooleanVar(value=False)
        ttk.Label(frame, text="選擇模型").grid(row=0, column=0, sticky="w", pady=5)
        choices = tuple(dict.fromkeys((*MODEL_CHOICES, self.settings["model"])))
        ttk.Combobox(frame, textvariable=model, values=choices, state="readonly", width=35).grid(row=0, column=1)
        ttk.Label(frame, text="Luna／Terra／Sol／Astra；只在送出 OpenAI 辨識時使用。", padding=(0, 5)).grid(row=6, columnspan=2, sticky="w")
        ttk.Label(frame, text="圖片細節").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Combobox(frame, textvariable=detail, values=("high", "original", "auto", "low"), state="readonly", width=35).grid(row=1, column=1)
        ttk.Label(frame, text="API 金鑰").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=key, show="●", width=38).grid(row=2, column=1)
        ttk.Label(frame, text="留白則沿用目前／環境變數／系統憑證中的金鑰。\n金鑰不會存入樂譜專案。", padding=(0, 8)).grid(row=3, columnspan=2, sticky="w")
        ttk.Checkbutton(frame, text="將新金鑰保存到系統憑證（未勾選只用於本次開啟）", variable=remember).grid(row=4, columnspan=2)
        def save():
            value = key.get().strip()
            if value and remember.get():
                self.credentials.set_api_key(value)
            settings = self.settings_store.update(model=model.get().strip(), image_detail=detail.get())
            if value:
                self.session_key = value
            self.settings = settings
            key.set("")
            window.destroy()
            self.status_text.set("OpenAI 設定已保存；只有按辨識並確認送出才會連線")
        ttk.Button(frame, text="保存設定", command=lambda: self.guarded(save)).grid(row=5, columnspan=2, pady=12)

    def start_batch(self, mode):
        workspace = self.require_workspace()
        if self.region_id:
            self.apply_form(None)
        numbering = workspace.page_order_issues()
        if numbering:
            raise ContractError("小節編號與來源頁序不一致，暫不開始辨識，以免沿用錯誤編號。\n"
                                + "\n".join(numbering[:3]) + "\n請先核對照片順序與各頁的起始小節；已有結果不必直接刪除重辨。")
        local = self.engine.get() == ENGINES[0]
        supplement = self.engine.get() == ENGINES[1]
        regions = workspace.package["regions"]
        if mode == "current":
            regions = [self.current_region()]
        elif mode == "failed":
            regions = [region for region in regions if workspace.status(region) in {"failed", "interrupted"}]
        elif supplement:
            regions = [r for r in regions if (workspace.latest_run(r) or {}).get("omr_review")]
        else:
            regions = [region for region in regions if workspace.status(region) in {"pending", "stale"}]
        regions = [region for region in regions if region["confirmed"]]
        if not regions:
            raise ContractError("沒有符合條件的已確認範圍；請先確認來源與小節分界")
        executable = find_audiveris() if local else None
        if local and not executable:
            raise ContractError("請先安裝／指定 Audiveris，按上方「本機 OMR 設定」；程式不會自動改用付費辨識。")
        bases = {r["region_id"]: workspace.latest_run(r) for r in regions}
        if supplement and any(not bases[r["region_id"]] or not bases[r["region_id"]].get("omr_review") or not (bases[r["region_id"]].get("result") or bases[r["region_id"]].get("partial_result")) for r in regions):
            raise ContractError("所選範圍沒有可補辨的 OMR 疑問結果。請先執行本機 OMR；若 OMR 本身失敗，可明確改選「OpenAI（整個所選範圍）」。")
        key = None if local else self.session_key or self.credentials.get_api_key()
        if not local and not key:
            raise ContractError("請先在 OpenAI 設定輸入 API 金鑰")
        lines = []
        for region in regions:
            asset = workspace.asset(region["asset_id"])
            lines.append(f"第 {asset['printed_page_number']} 頁／小節 {region['start_bar']}–{region['start_bar'] + region['bar_count'] - 1}／{NOTATIONS[region['notation']]}")
        if local:
            summary = f"在本機執行 {len(regions)} 個範圍的 Audiveris OMR，不送到 OpenAI、不產生 API 費用。\n只適用印刷五線譜；手寫譜、簡譜請改選 OpenAI。\n\n"
        elif supplement:
            requests = sum(len(bases[r["region_id"]]["omr_review"]) for r in regions)
            summary = f"將 {requests} 個 OMR 疑問小節分別送至 OpenAI，使用模型：{self.settings['model']}。\n每次包含一小節主圖與原範圍參考圖。其餘小節不重辨。\n\n"
        else:
            summary = f"將傳送 {len(regions)} 個已確認範圍至 OpenAI，使用模型：{self.settings['model']}。\n\n"
        summary += "\n".join(lines[:15])
        if len(lines) > 15:
            summary += f"\n另有 {len(lines) - 15} 個範圍"
        summary += ("\n\n本機只檢查結構疑問；拍數成立也可能漏音，結果仍待核對。不會自動付費補辨。" if local else "\n\n會依 API 用量計費；單一範圍失敗可單獨重試。") + "\n是否開始？（可先取消並查看裁切預覽）"
        if not messagebox.askyesno("確認辨識來源", summary, parent=self.root):
            return
        self.busy = True
        self.stop_requested.clear()
        settings = dict(self.settings)
        targets = [region["region_id"] for region in regions]
        self.progress.configure(maximum=len(targets), value=0)
        def worker():
            completed = succeeded = failed = 0
            try:
                for region_id in targets:
                    if self.stop_requested.is_set():
                        break
                    region = workspace.region(region_id)
                    run = None
                    self.messages.put(("status", f"辨識小節 {region['start_bar']}–{region['start_bar'] + region['bar_count'] - 1}（{completed + 1}/{len(targets)}）"))
                    try:
                        run = workspace.begin_run(region_id, "Audiveris（本機）" if local else settings["model"])
                        if local:
                            response = recognize_local(workspace,deepcopy(region),run,executable=executable,stop=self.stop_requested,
                                status=lambda text:self.messages.put(("status",text)))
                        elif supplement:
                            response = supplement_with_openai(workspace,deepcopy(region),bases[region_id],run,
                                model=settings["model"],api_key=key,detail=settings["image_detail"],stop=self.stop_requested,
                                status=lambda text:self.messages.put(("status",text)),recognizer=recognize)
                        else:
                            response = recognize(workspace, deepcopy(region), model=settings["model"], api_key=key, detail=settings["image_detail"])
                        workspace.finish_run(run, response)
                        succeeded += run["status"] == "succeeded"
                        failed += run["status"] != "succeeded"
                    except Exception as error:
                        if run is not None:
                            # 已知的本機與 provider 例外不含 key；其他錯誤只記類別。
                            from .provider import RecognitionError
                            message = str(error) if isinstance(error, (RecognitionError, ContractError)) else f"處理失敗（{type(error).__name__}）"
                            workspace.fail_run(run, message, getattr(error, "raw_output", None))
                        failed += 1
                    completed += 1
                    self.messages.put(("progress", completed))
            finally:
                self.messages.put(("done", f"完成 {completed}/{len(targets)} 個範圍：成功 {succeeded}，失敗或需檢查 {failed}。結果皆待人工核對。"))
        threading.Thread(target=worker, daemon=True).start()

    def omr_settings(self):
        window = tk.Toplevel(self.root)
        window.title("本機 OMR 設定")
        frame = ttk.Frame(window,padding=16)
        frame.pack(fill="both",expand=True)
        detected = find_audiveris()
        path = tk.StringVar(value=str(detected) if detected else "尚未找到 Audiveris")
        ttk.Label(frame,text="Audiveris 在本機辨識印刷五線譜，匯出 MusicXML 後轉成原有候選資料。\n簡譜／手寫譜仍走 OpenAI；本機失敗時不會自動付費。",wraplength=680).pack(anchor="w")
        ttk.Entry(frame,textvariable=path,width=85,state="readonly").pack(fill="x",pady=12)
        def choose():
            selected = filedialog.askopenfilename(title="選擇 Audiveris 執行檔（PC 選 Audiveris.exe）",parent=window)
            if selected:
                path.set(str(save_audiveris_path(selected)))
                self.status_text.set("本機 OMR 路徑已保存")
        self.button(frame,"選擇已安裝的 Audiveris…",choose).pack(anchor="w")
        import webbrowser
        ttk.Button(frame,text="開啟官方下載頁（PC 選 windows-x86_64.msi）",command=lambda:webbrowser.open("https://github.com/Audiveris/audiveris/releases")).pack(anchor="w",pady=6)
        ttk.Label(frame,text="可同時開不同樂譜的專案。同一份專案只開一個視窗。\nPC 程式 A 支援本機 OMR 與 OpenAI。先從一個 OMR 工作開始。",wraplength=680).pack(anchor="w",pady=8)

    def poll(self):
        try:
            while True:
                kind, value = self.messages.get_nowait()
                if kind == "status":
                    self.status_text.set(value)
                elif kind == "progress":
                    self.progress.configure(value=value)
                    self.refresh_regions()
                elif kind == "done":
                    self.busy = False
                    self.status_text.set(value)
                    self.refresh_regions()
        except queue.Empty:
            pass
        self.poll_id = self.root.after(150, self.poll)

    def show_results(self):
        region = self.current_region()
        run = self.workspace.latest_run(region)
        if not run:
            run = next((item for item in reversed(self.workspace.package["recognition_runs"]) if item["region_id"] == region["region_id"]), None)
        if not run:
            raise ContractError("這個範圍還沒有辨識紀錄")
        window = tk.Toplevel(self.root)
        window.title("辨識結果｜待第二個程式人工核對")
        window.geometry("1100x700")
        outdated = run["fingerprint"] != self.workspace.fingerprint(region)
        label = "歷史結果：來源已修改，請勿沿用" if outdated else STATUS[run["status"]]
        ttk.Label(window, text=f"{label}　｜　{run['error'] or ''}", padding=12).pack(anchor="w")
        notebook = ttk.Notebook(window)
        notebook.pack(fill="both", expand=True, padx=10, pady=5)
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="小節與樂器結果")
        tree = ttk.Treeview(frame, columns=("bar", "instrument", "symbol", "onset", "duration", "marks", "issues"), show="headings")
        for name, title, width in (("bar", "小節", 50), ("instrument", "樂器／原列名", 130), ("symbol", "原譜／休止", 110),
                                   ("onset", "起點（四分拍）", 110), ("duration", "時值（四分拍）", 110),
                                   ("marks", "演奏記號", 130), ("issues", "待確認", 280)):
            tree.heading(name, text=title)
            tree.column(name, width=width)
        scrollbar = ttk.Scrollbar(frame, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        observations = run.get("observations", [])
        for i, item in enumerate(observations):
            def timing(value):
                return "待填" if value is None else f"{value['n']}/{value['d']}"
            marks = [label for field, label in (("is_roll", "輪奏"), ("is_accent", "重音"), ("is_ornament", "裝飾音")) if item.get(field)]
            if item.get("tie") not in {None, "none"}:
                marks.append("延音：" + item["tie"])
            symbol = "全小節休止" if item["kind"] == "full_rest" else item.get("raw_symbol") or "未讀清"
            tree.insert("", "end", iid=str(i), values=(item["bar"], item.get("instrument_text") or item["staff_label"], symbol,
                                                       timing(item.get("onset")), timing(item.get("duration")), "、".join(marks), "；".join(item["uncertainties"])))
        def select(event):
            if tree.selection():
                item = observations[int(tree.selection()[0])]
                self.viewer.evidence = item["source_bbox"]
                self.viewer.redraw()
                self.viewer.canvas.yview_moveto(max(0, item["source_bbox"]["y"] - .12))
        tree.bind("<<TreeviewSelect>>", select)
        details = ttk.Frame(notebook)
        notebook.add(details, text="反覆與辨識提醒")
        text = tk.Text(details, wrap="word")
        text.pack(fill="both", expand=True)
        result = run.get("result") or {}
        lines = ["所有記號皆為辨識候選，由第二個程式核對。", ""]
        for marker in result.get("navigation", []):
            kind = {"repeat_start": "反覆起點", "repeat_end": "反覆終點", "ending": "不同結尾", "other": "其他記號"}[marker["kind"]]
            lines.append(f"{kind}：第 {marker['start_bar']} 小節" + (f" 至 {marker['end_bar']} 小節" if marker["end_bar"] is not None else ""))
            if marker["passes"]:
                lines.append("適用第 " + "、".join(str(value) for value in marker["passes"]) + " 次演奏")
            if marker["times"]:
                lines.append(f"共演奏 {marker['times']} 次")
            lines.extend(marker["uncertainties"])
            lines.append("")
        if not result.get("navigation"):
            lines.append("本範圍未回報反覆記號。")
        lines += ["", "辨識提醒：", *result.get("warnings", []), *run.get("coverage_warnings", [])]
        for item in observations:
            if item["uncertainties"]:
                lines.append(f"第 {item['bar']} 小節／{item.get('instrument_text') or item['staff_label']}／{item.get('raw_symbol', '')}：" + "；".join(item["uncertainties"]))
        usage = run.get("usage")
        if isinstance(usage, dict):
            lines += ["", f"API 用量：輸入 {usage.get('input_tokens', '未知')}，輸出 {usage.get('output_tokens', '未知')} tokens"]
        text.insert("1.0", "\n".join(lines))
        text.configure(state="disabled")
        raw_frame = ttk.Frame(notebook)
        notebook.add(raw_frame, text="原始紀錄")
        raw_text = tk.Text(raw_frame, wrap="word")
        raw_text.pack(fill="both", expand=True)
        raw_text.insert("1.0", json.dumps(run, ensure_ascii=False, indent=2))
        raw_text.configure(state="disabled")
        if run.get("engine") in {"audiveris","omr+openai"} or run.get("artifacts"):
            omr_frame = ttk.Frame(notebook)
            notebook.add(omr_frame,text="OMR 疑問與處理紀錄")
            omr_text = tk.Text(omr_frame,wrap="word")
            omr_scroll = ttk.Scrollbar(omr_frame,command=omr_text.yview)
            omr_text.configure(yscrollcommand=omr_scroll.set)
            omr_scroll.pack(side="right",fill="y")
            omr_text.pack(fill="both",expand=True)
            lines = ["本機結構檢查無法保證漏音、音高與所有符號都正確，仍需抽查。",run.get("error") or ""]
            lines.extend(f"小節 {item['bar']}：{'；'.join(item['reasons'])}" for item in run.get("omr_review",[]))
            lines.extend(["", "檔案位置：",json.dumps(run.get("artifacts",{}),ensure_ascii=False,indent=2)])
            log = run.get("artifacts",{}).get("log")
            if log:
                resolved = (self.workspace.root/log).resolve()
                if resolved.is_relative_to(self.workspace.root) and resolved.is_file():
                    lines += ["", "本機處理紀錄（末尾）：", "\n".join(resolved.read_text(encoding="utf-8",errors="replace").splitlines()[-100:])]
            omr_text.insert("1.0","\n".join(lines))
            omr_text.configure(state="disabled")
            if run["status"] == "failed":
                notebook.select(omr_frame)
        ttk.Label(window, text="點選結果可在主畫面定位原圖。此處只檢視辨識候選；人工補填與音效播放由程式 B 負責。", padding=10).pack(anchor="w")

    def export(self):
        workspace = self.require_workspace()
        if self.region_id:
            self.apply_form(None)
        report = workspace.report()
        summary = f"來源 {report['pages']} 頁／{report['regions']} 個範圍\n敲擊候選 {report['note_candidates']}／全休止聲部小節 {report['full_rest_measures']}\n含疑問觀察 {report['pending_observations']}\n\n範圍狀態：\n" + "\n".join(f"{STATUS[key]}：{count}" for key, count in report["region_status"].items())
        summary += "\n\n套件包含原圖、原始辨識、待確認項目與失敗紀錄。尚未辨識或部分失敗仍可匯出。"
        if report["pages_without_regions"]:
            summary += "\n\n尚未框選的頁面：" + "、".join(report["pages_without_regions"])
        if report["unregistered_bar_ranges"]:
            summary += "\n未登記的小節範圍：" + "、".join(f"{start}–{end}" for start, end in report["unregistered_bar_ranges"])
        if not messagebox.askyesno("確認匯出內容", summary, parent=self.root):
            return
        path = filedialog.asksaveasfilename(title="匯出待核對套件", initialfile=f"{workspace.root.stem}.recognition.zip", defaultextension=".zip", filetypes=[("辨識套件", "*.zip")], parent=self.root)
        if path:
            workspace.export(path)
            self.status_text.set(f"已匯出：{path}")

    def close(self):
        if self.busy:
            self.stop_requested.set()
            messagebox.showinfo("目前範圍尚在辨識", "已停止後續排程。請等目前範圍完成後再關閉，以保存回應。", parent=self.root)
            return
        try:
            if self.workspace:
                if self.form_region_id:
                    self.save_form(None)
                self.workspace.save()
        except Exception as error:
            messagebox.showerror("未能保存", str(error), parent=self.root)
            return
        self.session_key = None
        self.root.after_cancel(self.poll_id)
        self.root.destroy()


def run():
    parser = argparse.ArgumentParser(description="MusicAI 樂譜辨識程式 A")
    parser.add_argument("--project", help="開啟辨識工作資料夾或 recognition-package.json")
    args = parser.parse_args()
    root = tk.Tk()
    workspace = None
    lock = None
    if args.project:
        try:
            workspace, lock = load_locked(args.project)
        except Exception as error:
            messagebox.showerror("專案無法開啟", str(error), parent=root)
    RecognizerApp(root, workspace, lock)
    root.mainloop()
