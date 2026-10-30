"""獨立比較視窗；只借用 A 的框選回呼，不修改 A 的畫面或排程。"""
import argparse
import json
from pathlib import Path
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from PIL import ImageTk
from guoyue_annotator.photo_assets import asset_file_path

from score_recognizer.app import RecognizerApp
from score_recognizer.canvas import ScoreCanvas
from score_recognizer.contract import ContractError, NOTATIONS
from score_recognizer.project_lock import ProjectLock
from score_recognizer.project_setup import default_projects_directory, project_destination
from .project import ComparisonProject, MANIFEST, MODELS, model_names, single_measure
from score_recognizer.provider import crop_image
from .provider import run_cases
from .comparison import export_report
from .results import ResultsPanel


def ask_project(parent, source=None):
    window = tk.Toplevel(parent)
    window.title("建立獨立比較專案")
    window.transient(parent)
    window.grab_set()
    frame = ttk.Frame(window, padding=18)
    frame.pack(fill="both", expand=True)
    default_title = "新樂譜比較"
    if source:
        default_title = json.loads(Path(source).read_text(encoding="utf-8"))["title"] + " 模型比較"
    name, location, error = tk.StringVar(value=default_title), tk.StringVar(value=str(default_projects_directory())), tk.StringVar()
    ttk.Label(frame, text="比較專案名稱").pack(anchor="w")
    entry = ttk.Entry(frame, textvariable=name, width=62)
    entry.pack(fill="x", pady=5)
    ttk.Label(frame, text="程式會建立 .score-comparison 資料夾，不需自行新增資料夾。", wraplength=600).pack(anchor="w", pady=4)
    ttk.Label(frame, textvariable=location, wraplength=600).pack(anchor="w")

    def choose():
        value = filedialog.askdirectory(parent=window, initialdir=location.get(), title="選擇存放比較專案的位置")
        if value:
            location.set(value)

    ttk.Button(frame, text="選擇其他存放位置…", command=choose).pack(anchor="w", pady=6)
    ttk.Label(frame, text="會複製照片與框選設定；原 A／B 的辨識、校對結果留在原處。" if source else "可以匯入 JPG／PNG，再框選與調整小節。", wraplength=600).pack(anchor="w")
    ttk.Label(frame, textvariable=error, foreground="#b02c22", wraplength=600).pack(anchor="w", pady=7)
    result = []

    def create():
        try:
            destination = project_destination(location.get(), name.get()).with_suffix(".score-comparison")
            project = ComparisonProject.copy_source(source, destination, name.get()) if source else ComparisonProject.create(destination, name.get())
            result.append(project)
            window.destroy()
        except Exception as exc:
            error.set(str(exc))

    actions = ttk.Frame(frame)
    actions.pack(fill="x")
    ttk.Button(actions, text="建立比較專案", command=create).pack(side="right")
    ttk.Button(actions, text="取消", command=window.destroy).pack(side="right", padx=7)
    entry.focus_set()
    parent.wait_window(window)
    return result[0] if result else None


class ComparatorApp(RecognizerApp):
    def __init__(self, root, project=None, project_lock=None):
        self.project = project
        self.measure_region_id = None
        self.active_started = None
        self.active_description = ""
        if project is not None and project_lock is None:
            project_lock = ProjectLock(project.root)
        super().__init__(root, project.workspace if project else None, project_lock)
        root.title("MusicAI｜模型比較工具（獨立專案）")
        root.geometry("1500x960")
        root.minsize(1150, 760)
        self.results.refresh()

    def build(self):
        header = ttk.Frame(self.root, padding=10)
        header.pack(fill="x")
        ttk.Label(header, text="樂譜模型比較", style="Header.TLabel").pack(side="left")
        ttk.Label(header, text="  同一小節 → 相同輸入 → 多模型獨立辨識 → 對照差異").pack(side="left", padx=10)
        tools = ttk.Frame(self.root, padding=(10, 0, 10, 7))
        tools.pack(fill="x")
        for text, command in (("新增比較專案", self.new_project), ("複製 A 的照片與範圍…", self.copy_source),
                              ("開啟比較專案", self.open_project), ("匯入 JPG／PNG", self.import_images),
                              ("保存", self.save), ("模型／金鑰設定", self.openai_settings),
                              ("樂譜共通資訊", self.score_context), ("匯出比較報告…", self.export)):
            self.button(tools, text, command).pack(side="left", padx=2)
        self.project_text = tk.StringVar(value="尚未建立比較專案；可先複製 A 已調整好的範圍。")
        ttk.Label(self.root, textvariable=self.project_text, foreground="#53657c", padding=(12, 0, 12, 5)).pack(fill="x")
        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill="both", expand=True, padx=10)
        self.source_tab = ttk.Frame(self.tabs)
        self.tabs.add(self.source_tab, text="① 照片、範圍與小節")
        panes = ttk.Panedwindow(self.source_tab, orient="horizontal")
        panes.pack(fill="both", expand=True)
        left = ttk.Frame(panes, padding=5)
        panes.add(left, weight=0)
        self.pages = ttk.Treeview(left, columns=("page", "name"), show="headings", selectmode="browse", height=15)
        for key, title, width in (("page", "頁碼", 45), ("name", "檔名", 145)):
            self.pages.heading(key, text=title)
            self.pages.column(key, width=width)
        scroll = ttk.Scrollbar(left, command=self.pages.yview)
        self.pages.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.pages.pack(fill="both", expand=True)
        self.pages.bind("<<TreeviewSelect>>", self.page_selected)
        self.button(left, "確認／修改頁碼", self.page_number).pack(fill="x", pady=4)
        center = ttk.Frame(panes, padding=5)
        panes.add(center, weight=1)
        picture_tools = ttk.Frame(center)
        picture_tools.pack(fill="x")
        for text, mode in (("拖曳小節線", "select"), ("框選主範圍", "main"), ("框選參考圖", "context")):
            self.button(picture_tools, text, lambda m=mode: self.set_mode(m)).pack(side="left", padx=2)
        extra = ttk.Frame(center)
        extra.pack(fill="x", pady=4)
        for text, command in (("新增範圍", self.new_region_mode), ("調整斜框", self.edit_quad),
                              ("整頁參考", self.use_full_page_context), ("清除參考框", self.clear_context),
                              ("＋", lambda: self.viewer.zoom(1.2)), ("－", lambda: self.viewer.zoom(1 / 1.2))):
            self.button(extra, text, command, **({"width": 3} if text in {"＋", "－"} else {})).pack(side="left", padx=2)
        self.viewer = ScoreCanvas(center, self.box_drawn, self.boundaries_changed, self.quad_changed)
        self.viewer.pack(fill="both", expand=True)
        self.mode_text = tk.StringVar(value="藍框：主範圍　橘線：小節分界　紫框：參考資訊。送出時會逐小節裁切。")
        ttk.Label(center, textvariable=self.mode_text, wraplength=620).pack(fill="x", pady=5)
        right_host = ttk.Frame(panes, padding=7)
        panes.add(right_host, weight=0)
        right_host.rowconfigure(0, weight=1)
        right_host.columnconfigure(0, weight=1)
        settings_canvas = tk.Canvas(right_host, width=365, highlightthickness=0)
        scrollbar = ttk.Scrollbar(right_host, command=settings_canvas.yview)
        settings_canvas.configure(yscrollcommand=scrollbar.set)
        settings_canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        right = ttk.Frame(settings_canvas)
        item = settings_canvas.create_window((0, 0), window=right, anchor="nw")
        right.bind("<Configure>", lambda e: settings_canvas.configure(scrollregion=settings_canvas.bbox("all")))
        settings_canvas.bind("<Configure>", lambda e: settings_canvas.itemconfigure(item, width=e.width))
        def wheel(event):
            target = self.root.winfo_containing(event.x_root, event.y_root)
            if target and (str(target).startswith(str(right)) or target == settings_canvas):
                settings_canvas.yview_scroll(-3 if event.delta > 0 else 3, "units")
        self.root.bind_all("<MouseWheel>", wheel, add="+")
        ttk.Label(right, text="範圍設定", style="Header.TLabel").pack(anchor="w")
        region_host = ttk.Frame(right)
        region_host.pack(fill="x", pady=5)
        self.regions = ttk.Treeview(region_host, columns=("bars", "status"), show="headings", height=4, selectmode="browse")
        self.regions.heading("bars", text="小節範圍")
        self.regions.heading("status", text="狀態")
        self.regions.column("bars", width=95, stretch=False)
        self.regions.column("status", width=230)
        region_scroll = ttk.Scrollbar(region_host, command=self.regions.yview)
        self.regions.configure(yscrollcommand=region_scroll.set)
        region_scroll.pack(side="right", fill="y")
        self.regions.pack(fill="x", expand=True)
        self.regions.bind("<<TreeviewSelect>>", self.region_selected)
        defaults = ttk.Frame(right)
        defaults.pack(fill="x", pady=4)
        self.new_range_bar_count = tk.StringVar(value="4")
        ttk.Label(defaults, text="新範圍預設小節數").pack(side="left")
        count = ttk.Combobox(defaults, textvariable=self.new_range_bar_count, values=list(range(1, 33)), state="readonly", width=5)
        count.pack(side="left", padx=8)
        count.bind("<<ComboboxSelected>>", lambda e: self.guarded(self.remember_new_range_count))
        form = ttk.Frame(right)
        form.pack(fill="x")
        self.fields = {}
        for n, (key, label) in enumerate((("start_bar", "起始小節"), ("bar_count", "本範圍小節數"),
                                          ("notation", "譜種"), ("time_signature", "拍號（可空白）"), ("key_signature", "調號（可空白）"))):
            ttk.Label(form, text=label).grid(row=n, column=0, sticky="w", pady=3)
            self.fields[key] = tk.StringVar()
            widget = ttk.Combobox(form, textvariable=self.fields[key], values=list(NOTATIONS.values()), state="readonly", width=22) if key == "notation" else ttk.Entry(form, textvariable=self.fields[key], width=24)
            widget.grid(row=n, column=1, padx=7, sticky="ew", pady=3)
        self.expected = tk.StringVar()
        ttk.Label(right, text="預期樂器（選填，以逗號分隔）").pack(anchor="w", pady=(5, 0))
        ttk.Entry(right, textvariable=self.expected).pack(fill="x")
        ttk.Label(right, text="範圍補充說明").pack(anchor="w", pady=(5, 0))
        self.notes = tk.Text(right, height=3, width=35, wrap="word", undo=True)
        self.notes.pack(fill="x")
        for text, command in (("依小節數重排分界", self.redistribute), ("裁切預覽", self.preview),
                              ("確認範圍與分界", lambda: self.apply_form(True)), ("移除此範圍（保留比較歷史）", self.remove_region)):
            self.button(right, text, command).pack(fill="x", pady=3)
        actions = ttk.Frame(right_host)
        actions.grid(row=1, column=0, columnspan=2, sticky="ew", pady=6)
        ttk.Label(actions, text="選擇要比較的小節（Ctrl／Shift 可複選）").pack(anchor="w", pady=(5, 3))
        measure_host = ttk.Frame(actions)
        measure_host.pack(fill="x")
        self.measure_list = ttk.Treeview(measure_host, columns=("bar",), show="headings", selectmode="extended", height=3)
        self.measure_list.heading("bar", text="原譜小節號")
        measure_scroll = ttk.Scrollbar(measure_host, command=self.measure_list.yview)
        self.measure_list.configure(yscrollcommand=measure_scroll.set)
        measure_scroll.pack(side="right", fill="y")
        self.measure_list.pack(fill="x", expand=True)
        self.button(actions, "選取本範圍全部小節", lambda: self.measure_list.selection_set(self.measure_list.get_children())).pack(fill="x", pady=3)
        self.model_summary = ttk.Label(actions, text="預設 Terra ＋ Luna；可在模型設定增減。", wraplength=350)
        self.model_summary.pack(fill="x", pady=3)
        self.button(actions, "比較所選小節…", self.start_comparison).pack(fill="x", pady=3)
        ttk.Button(actions, text="停止後續比較（等目前回應保存）", command=self.stop_requested.set).pack(fill="x", pady=3)
        self.progress = ttk.Progressbar(actions, mode="determinate")
        self.progress.pack(fill="x", pady=5)
        self.results = ResultsPanel(self.tabs, self)
        self.tabs.add(self.results, text="② 模型結果與差異")
        self.status_text = tk.StringVar(value="準備就緒；尚未送出任何比較請求。")
        ttk.Label(self.root, textvariable=self.status_text, foreground="#285271", padding=9, wraplength=1400).pack(side="bottom", fill="x")
        self.tabs.pack_forget()
        self.tabs.pack(fill="both", expand=True, padx=10)

    def activate(self, project, lock=None):
        lock = lock or ProjectLock(project.root)
        self.release_lock()
        self.project, self.workspace, self.project_lock = project, project.workspace, lock
        self.measure_region_id = None
        self.results.selected_case = None
        self.reset_selection()
        self.results.refresh()
        self.tabs.select(self.source_tab)
        self.status_text.set("已開啟獨立比較專案；照片與範圍可在此調整。")

    def new_project(self):
        if self.project:
            self.save()
        project = ask_project(self.root)
        if project:
            self.activate(project)

    def copy_source(self):
        if self.project:
            self.save()
        source = filedialog.askopenfilename(parent=self.root, title="唯讀複製程式 A 的照片與範圍", initialdir=str(default_projects_directory()), filetypes=[("程式 A 專案", "recognition-package.json")])
        if source:
            project = ask_project(self.root, source)
            if project:
                self.activate(project)

    def open_project(self):
        if self.project:
            self.save()
        path = filedialog.askopenfilename(parent=self.root, title="開啟比較專案", initialdir=str(default_projects_directory()), filetypes=[("比較專案", MANIFEST)])
        if path:
            if self.project and Path(path).resolve().parent == self.project.root:
                return
            # 原 A 誤選時，在取得鎖或復原任何狀態前即拒絕。
            if json.loads(Path(path).read_text(encoding="utf-8")).get("format") != "musicai-model-comparison":
                raise ContractError("請開啟比較專案；A 的資料請使用『複製 A 的照片與範圍』")
            lock = ProjectLock(path)
            try:
                project = ComparisonProject.load(path)
            except Exception:
                lock.close()
                raise
            self.activate(project, lock)

    def save(self):
        super().save()
        self.project.save()
        self.status_text.set("比較專案、框選設定與歷史結果已保存。")

    def refresh_pages(self):
        super().refresh_pages()
        if self.project:
            self.project_text.set(f"{self.project.data['title']}　｜　比較專案：{self.project.root}")
            settings = self.project.data["settings"]
            self.model_summary.configure(text="比較模型：" + " ＋ ".join(settings["models"]))
        if not self.workspace or not self.workspace.package["assets"]:
            self.regions.delete(*self.regions.get_children())
            self.measure_list.delete(*self.measure_list.get_children())

    def refresh_regions(self):
        self.regions.delete(*self.regions.get_children())
        if not self.workspace or not self.asset_id:
            return
        for region in self.workspace.package["regions"]:
            if region["asset_id"] != self.asset_id:
                continue
            cases = [c for c in self.project.data["cases"] if c["region_id"] == region["region_id"] and c["region_fingerprint"] == self.workspace.fingerprint(region)]
            label = "已確認" if region["confirmed"] else "待確認分界"
            if cases:
                label += f"｜{len(cases)} 筆比較紀錄"
            self.regions.insert("", "end", iid=region["region_id"], values=(f"{region['start_bar']}–{region['start_bar'] + region['bar_count'] - 1}", label))
        ids = self.regions.get_children()
        if ids:
            self.regions.selection_set(self.region_id if self.region_id in ids else ids[0])

    def region_selected(self, event=None):
        changed = self.regions.selection() and self.form_region_id != self.regions.selection()[0]
        super().region_selected(event)
        if not self.region_id:
            return
        old = self.measure_list.selection() if self.measure_region_id == self.region_id else ()
        self.measure_list.delete(*self.measure_list.get_children())
        region = self.current_region()
        for index in range(region["bar_count"]):
            self.measure_list.insert("", "end", iid=str(index), values=(region["start_bar"] + index,))
        old = [i for i in old if self.measure_list.exists(i)]
        self.measure_list.selection_set(old or ["0"])
        self.measure_region_id = self.region_id
        if changed:
            self.viewer.canvas.yview_moveto(max(0, region["bbox"]["y"] - .06))

    def preview(self):
        self.apply_form(None)
        selected = self.measure_list.selection()
        sub = single_measure(self.current_region(), int(selected[0]) if selected else 0)
        path = asset_file_path(self.workspace.root, self.workspace.asset(sub["asset_id"]), must_exist=True)
        window = tk.Toplevel(self.root)
        window.title(f"實際單小節裁圖預覽｜小節 {sub['start_bar']}")
        window.photos = []
        for label, box in (("主要辨識圖片", sub["bbox"]), ("參考圖片", sub.get("context_bbox"))):
            if box is not None:
                ttk.Label(window, text=label, padding=5).pack(anchor="w")
                picture = crop_image(path, box, sub.get("quad") if label == "主要辨識圖片" else None)
                picture.thumbnail((950, 330))
                photo = ImageTk.PhotoImage(picture)
                window.photos.append(photo)
                ttk.Label(window, image=photo).pack(padx=8, pady=4)
        ttk.Label(window, text="此處預覽第一個所選小節；比較時每小節獨立送出。所有模型使用相同裁圖。", padding=8).pack()

    def openai_settings(self):
        if not self.project:
            raise ContractError("請先建立或開啟比較專案")
        project = self.project
        settings = project.data["settings"]
        window = tk.Toplevel(self.root)
        window.title("比較模型與連線設定")
        window.transient(self.root)
        frame = ttk.Frame(window, padding=15)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="選擇至少兩個模型；設定只保存在這份比較專案。").pack(anchor="w")
        checks, options = {}, ttk.Frame(frame)
        options.pack(fill="x", pady=5)
        def add_choice(name, selected):
            if name in checks:
                checks[name].set(selected)
                return
            checks[name] = tk.BooleanVar(value=selected)
            ttk.Checkbutton(options, text=name, variable=checks[name]).pack(anchor="w")
        for name in dict.fromkeys((*MODELS, *settings["models"])):
            add_choice(name, name in settings["models"])
        custom = tk.StringVar()
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=5)
        ttk.Entry(row, textvariable=custom, width=30).pack(side="left")
        def add_custom():
            name = custom.get().strip()
            if not re.fullmatch(r"[A-Za-z0-9_.:/-]+", name):
                raise ContractError("請輸入有效模型 ID，不可包含空白")
            add_choice(name, True)
            custom.set("")
        ttk.Button(row, text="加入其他模型 ID", command=lambda: self.guarded(add_custom)).pack(side="left", padx=5)
        detail, reasoning = tk.StringVar(value=settings["image_detail"]), tk.StringVar(value=settings["reasoning"])
        for label, variable, values in (("圖片細節（所有模型相同）", detail, ("high", "original", "auto", "low")),
                                        ("推理設定（default 為各模型預設）", reasoning, ("medium", "low", "high", "xhigh", "max", "default"))):
            ttk.Label(frame, text=label).pack(anchor="w", pady=(7, 0))
            ttk.Combobox(frame, textvariable=variable, values=values, state="readonly", width=35).pack(fill="x", pady=3)
        ttk.Label(frame, text="API 金鑰（留白沿用本次／環境變數／系統憑證）").pack(anchor="w", pady=(8, 0))
        key, remember = tk.StringVar(), tk.BooleanVar()
        ttk.Entry(frame, textvariable=key, show="●", width=48).pack(fill="x", pady=4)
        ttk.Checkbutton(frame, text="將新金鑰保存至系統憑證（與 A 共用）；未勾選只留本次", variable=remember).pack(anchor="w")
        ttk.Label(frame, text="金鑰不保存到專案。其他模型須支援圖片及 JSON Schema；不支援時會記錄錯誤。", wraplength=570).pack(anchor="w", pady=7)
        def save_settings():
            if project is not self.project:
                raise ContractError("專案已切換，請重新開啟設定")
            names = model_names([n for n, checked in checks.items() if checked.get()])
            if key.get().strip():
                if remember.get():
                    self.credentials.set_api_key(key.get().strip())
                self.session_key = key.get().strip()
            project.data["settings"] = {"models": names, "image_detail": detail.get(), "reasoning": reasoning.get()}
            project.save()
            self.model_summary.configure(text="比較模型：" + " ＋ ".join(names))
            key.set("")
            window.destroy()
            self.status_text.set("比較設定已保存；尚未送出請求。")
        ttk.Button(frame, text="保存設定", command=lambda: self.guarded(save_settings)).pack(pady=10)

    def start_comparison(self):
        self.apply_form(None)
        region = self.current_region()
        selected = self.measure_list.selection()
        if not selected:
            raise ContractError("請在右下清單選擇要比較的小節")
        if not region["confirmed"]:
            raise ContractError("請先按『確認範圍與分界』")
        settings = self.project.data["settings"]
        names = model_names(settings["models"])
        bars = [region["start_bar"] + int(i) for i in selected]
        count = len(selected) * len(names)
        summary = (f"將比較 {len(selected)} 個小節：{'、'.join(map(str, bars))}\n模型：{' ＋ '.join(names)}\n"
                   f"共 {count} 次獨立付費請求，逐小節、逐模型執行。\n圖片細節：{settings['image_detail']}；推理：{settings['reasoning']}\n\n"
                   "各模型收到相同主圖、參考圖與指令，不會互看答案。\n重新比較會新增紀錄及費用，不覆蓋舊結果；沒有自動重試。\n要開始嗎？")
        if not messagebox.askyesno("確認比較小節與付費請求數", summary, parent=self.root):
            return
        key = self.session_key or self.credentials.get_api_key()
        if not key:
            raise ContractError("請先在『模型／金鑰設定』輸入金鑰")
        project = self.project
        cases = project.prepare([(region["region_id"], int(i)) for i in selected], names, settings["image_detail"], settings["reasoning"])
        self.begin_comparison(cases, key, count)

    def retry_selected_case(self):
        case = self.results.current_case()
        if not case:
            raise ContractError("請先在比較結果選擇一筆紀錄")
        names = [r["model"] for r in case["runs"] if r["status"] in {"failed", "invalid", "interrupted", "cancelled", "empty"}]
        if not names:
            raise ContractError("這筆紀錄沒有失敗或中斷的模型")
        if not messagebox.askyesno("確認只重送失敗模型", f"小節 {case['bar']}：{'、'.join(names)}\n共 {len(names)} 次付費請求。\n\n沿用這筆比較的固定圖片與設定；已成功模型不重送。先前回應與用量會保留，失敗的請求也可能已計費。\n要開始嗎？", parent=self.root):
            return
        key = self.session_key or self.credentials.get_api_key()
        if not key:
            raise ContractError("請先設定 API 金鑰")
        case, count = self.project.retry_failed(case["case_id"])
        self.results.selected_case = None
        self.begin_comparison([case], key, count)

    def begin_comparison(self, cases, key, count):
        project = self.project
        self.busy = True
        self.stop_requested.clear()
        self.progress.configure(maximum=count, value=0)
        self.results.refresh(cases[0]["case_id"])
        self.tabs.select(self.results)
        def work():
            try:
                run_cases(project, cases, api_key=key, stop=self.stop_requested,
                          notify=lambda kind, case_id, model: self.messages.put((kind, case_id, model)))
                self.messages.put(("done", "本輪比較完成；請對照原譜檢查差異。", None))
            except Exception as exc:
                # 在途回應會先落盤；未送完的資料重新開啟時標示中斷，不自動重試。
                self.messages.put(("done", f"比較排程中止（{type(exc).__name__}）；已保存的回應保留，未自動重送。", None))
        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        try:
            while True:
                kind, value, model = self.messages.get_nowait()
                if kind == "started":
                    self.active_started = time.monotonic()
                    case = self.project.case(value)
                    self.active_description = f"小節 {case['bar']}／{model} 等待回應"
                elif kind == "result":
                    self.active_started = None
                    self.progress.configure(value=float(self.progress["value"]) + 1)
                    self.results.refresh()
                elif kind == "done":
                    self.busy = False
                    self.active_started = None
                    self.status_text.set(value)
                    self.results.refresh()
                    self.refresh_regions()
        except queue.Empty:
            pass
        if self.busy and self.active_started is not None:
            elapsed = int(time.monotonic() - self.active_started)
            self.status_text.set(f"{self.active_description}，已等待 {elapsed} 秒。停止只阻止後續請求；單次最多約 10 分鐘。")
        self.poll_id = self.root.after(200, self.poll)

    def export(self):
        if not self.project:
            raise ContractError("請先開啟比較專案")
        parent = filedialog.askdirectory(parent=self.root, title="選擇存放比較報告的位置")
        if parent:
            from datetime import datetime
            folder = Path(parent) / ("模型比較報告-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
            export_report(self.project, folder)
            self.status_text.set(f"已匯出 CSV 與 JSON：{folder}")

    def close(self):
        if not self.busy and self.project:
            try:
                self.project.save()
            except Exception as error:
                messagebox.showerror("未能保存比較紀錄", str(error), parent=self.root)
                return
        super().close()


def run():
    parser = argparse.ArgumentParser(description="MusicAI 獨立模型比較工具")
    parser.add_argument("--project", help="比較專案資料夾或 comparison-project.json")
    args = parser.parse_args()
    root = tk.Tk()
    project = lock = None
    if args.project:
        try:
            path = Path(args.project)
            manifest = path / MANIFEST if path.is_dir() else path
            if json.loads(manifest.read_text(encoding="utf-8")).get("format") != "musicai-model-comparison":
                raise ContractError("請使用比較專案；原 A 的資料請在工具內選擇唯讀複製")
            lock = ProjectLock(path)
            project = ComparisonProject.load(path)
        except Exception as error:
            if lock:
                lock.close()
            lock = None
            messagebox.showerror("比較專案無法開啟", str(error), parent=root)
    ComparatorApp(root, project, lock)
    root.mainloop()
