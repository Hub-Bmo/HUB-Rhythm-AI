"""新增專案流程：預設由程式建立資料夾，使用者不必在系統選擇器內新增。"""

from pathlib import Path
import re
import tkinter as tk
from tkinter import filedialog, ttk

from .contract import ContractError
from .workspace import Workspace


def default_projects_directory():
    return Path(__file__).resolve().parents[1] / "projects"


def project_destination(parent, title):
    title = title.strip()
    if not title:
        raise ContractError("請輸入樂曲名稱。")
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")
    if not name:
        raise ContractError("請使用文字或數字作為樂曲名稱。")
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", name.split(".")[0], re.IGNORECASE):
        name = "_" + name
    return Path(parent).expanduser().resolve() / f"{name}.score-recognition"


def create_project(parent, title):
    destination = project_destination(parent, title)
    if destination.exists():
        raise ContractError("這個位置已有同名專案或資料夾，請改用其他名稱；要繼續既有專案請使用「開啟專案」。")
    return Workspace.create(destination, title.strip())


class ProjectSetup(ttk.Frame):
    def __init__(self, master, on_created, on_cancel, default_parent=None):
        super().__init__(master, padding=18)
        self.default_parent = Path(default_parent) if default_parent is not None else default_projects_directory()
        self.on_created = on_created
        self.created_workspace = None
        self.name_var = tk.StringVar()
        self.parent_var = tk.StringVar(value=str(self.default_parent))
        self.preview_var = tk.StringVar(value="輸入曲名後，程式會自動建立專案資料夾。")
        self.error_var = tk.StringVar()
        self.columnconfigure(0, weight=1)
        ttk.Label(self, text="建立一首樂曲的辨識專案", font=("Microsoft JhengHei UI", 13, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 14))
        ttk.Label(self, text="樂曲名稱").grid(row=1, column=0, sticky="w")
        self.name_entry = ttk.Entry(self, textvariable=self.name_var, width=66)
        self.name_entry.grid(row=2, column=0, sticky="ew", pady=(5, 12))
        ttk.Label(self, text="存放位置（預設為 MusicAI 資料夾內的 projects）").grid(row=3, column=0, sticky="w")
        ttk.Label(self, textvariable=self.parent_var, wraplength=600).grid(row=4, column=0, sticky="w", pady=(5, 8))
        location_buttons = ttk.Frame(self)
        location_buttons.grid(row=5, column=0, sticky="w")
        ttk.Button(location_buttons, text="選擇其他位置…", command=self.choose_parent).pack(side="left")
        ttk.Button(location_buttons, text="改用預設位置", command=lambda: self.parent_var.set(str(self.default_parent))).pack(side="left", padx=6)
        ttk.Label(self, text="不用自己新增資料夾；程式會依曲名建立。", foreground="#53657c").grid(row=6, column=0, sticky="w", pady=(12, 5))
        ttk.Label(self, textvariable=self.preview_var, wraplength=600).grid(row=7, column=0, sticky="w", pady=5)
        ttk.Label(self, textvariable=self.error_var, foreground="#a73527", wraplength=600).grid(row=8, column=0, sticky="w", pady=8)
        actions = ttk.Frame(self)
        actions.grid(row=9, column=0, sticky="e", pady=(8, 0))
        ttk.Button(actions, text="取消", command=on_cancel).pack(side="left", padx=5)
        ttk.Button(actions, text="建立專案", command=self.create).pack(side="left")
        self.name_var.trace_add("write", self.update_preview)
        self.parent_var.trace_add("write", self.update_preview)

    def update_preview(self, *_):
        self.error_var.set("")
        try:
            self.preview_var.set("專案資料夾：" + str(project_destination(self.parent_var.get(), self.name_var.get())))
        except (ContractError, OSError, ValueError):
            self.preview_var.set("輸入曲名後，程式會自動建立專案資料夾。")

    def choose_parent(self):
        initial = Path(self.parent_var.get())
        while not initial.is_dir() and initial != initial.parent:
            initial = initial.parent
        selected = filedialog.askdirectory(
            title="選擇存放位置（不用新增資料夾）", parent=self.winfo_toplevel(),
            initialdir=str(initial), mustexist=True,
        )
        if selected:
            self.parent_var.set(selected)

    def create(self):
        try:
            workspace = create_project(self.parent_var.get(), self.name_var.get())
        except ContractError as error:
            self.error_var.set(str(error))
            return
        except OSError:
            self.error_var.set("這個位置目前無法建立或寫入專案。請按「改用預設位置」，或選擇其他可存放檔案的資料夾，再按「建立專案」。")
            return
        self.error_var.set("")
        self.created_workspace = workspace
        self.on_created(workspace)


def ask_new_project(parent):
    window = tk.Toplevel(parent)
    window.title("新增辨識專案")
    window.transient(parent)
    window.resizable(False, False)
    form = ProjectSetup(window, lambda workspace: window.destroy(), window.destroy)
    form.pack(fill="both", expand=True)
    window.protocol("WM_DELETE_WINDOW", window.destroy)
    window.grab_set()
    form.name_entry.focus_set()
    parent.wait_window(window)
    return form.created_workspace
