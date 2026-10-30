"""Standalone Tk interface for persistent mappings and selectable export fields."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from .core import FIELDS, OPTIONAL, Store, TokenizerError, atomic_json, normalized


DEFAULT_WORKSPACE = Path(__file__).resolve().parents[1] / "projects" / "tokenizer-workspace"


def table(parent, columns, widths, *, height=16):
    frame = ttk.Frame(parent)
    frame.pack(fill="both", expand=True, pady=5)
    tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="extended", height=height)
    for column, width in zip(columns, widths):
        tree.heading(column, text=column)
        tree.column(column, width=width, minwidth=65, stretch=column in columns[:2])
    vertical = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    horizontal = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vertical.grid(row=0, column=1, sticky="ns")
    horizontal.grid(row=1, column=0, sticky="ew")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    return tree


class TokenizerApp:
    def __init__(self, root, workspace=None):
        self.root = root
        self.store = Store(workspace or DEFAULT_WORKSPACE)
        self.store.save()
        self.rows = {}
        self.status = tk.StringVar(value="匯入已校對 JSON；既有規則會自動套用，新項目需要確認一次。")
        root.title("MusicAI｜國樂資料 Token 化")
        root.geometry("1240x850")
        root.minsize(980, 700)
        ttk.Style(root).configure("Treeview", rowheight=27)
        top = ttk.Frame(root, padding=(12, 10))
        top.pack(fill="x")
        ttk.Label(top, text="國樂資料 Token 化", font=("Microsoft JhengHei UI", 17, "bold")).pack(side="left")
        self.button(top, "開啟其他工作庫", self.open_workspace, side="right")
        self.button(top, "備份工作庫", self.backup, side="right")
        self.path_label = ttk.Label(root, text="", padding=(14, 0, 14, 8))
        self.path_label.pack(fill="x")
        self.tabs = ttk.Notebook(root)
        self.tabs.pack(fill="both", expand=True, padx=12, pady=4)
        self.pieces_tab = ttk.Frame(self.tabs, padding=12)
        self.mapping_tab = ttk.Frame(self.tabs, padding=12)
        self.settings_tab = ttk.Frame(self.tabs, padding=12)
        self.preview_tab = ttk.Frame(self.tabs, padding=12)
        for panel, label in ((self.pieces_tab, "1  作品"), (self.mapping_tab, "2  對應規則"),
                             (self.settings_tab, "3  輸出欄位與合併"), (self.preview_tab, "4  預覽與匯出")):
            self.tabs.add(panel, text=label)
        self.build_pieces()
        self.build_mapping()
        self.build_settings()
        self.build_preview()
        ttk.Label(root, textvariable=self.status, relief="sunken", anchor="w", padding=8, wraplength=1160).pack(fill="x", side="bottom")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh_all()

    def button(self, parent, text, command, **pack):
        button = ttk.Button(parent, text=text, command=lambda: self.action(command))
        button.pack(**({"side": "left", "padx": 3, "pady": 3} | pack))
        return button

    def action(self, command):
        try:
            return command()
        except Exception as error:
            self.status.set(str(error).splitlines()[0])
            messagebox.showerror("需要處理", str(error), parent=self.root)
            return None

    def build_pieces(self):
        ttk.Label(self.pieces_tab, text="可一次匯入多首作品。來源快照與已確認規則保存在工作庫，重新開啟仍會保留。", wraplength=1100).pack(anchor="w")
        toolbar = ttk.Frame(self.pieces_tab)
        toolbar.pack(fill="x", pady=6)
        self.button(toolbar, "匯入作品 JSON…", self.import_files)
        self.button(toolbar, "選擇全部作品", self.select_all)
        self.button(toolbar, "查看所選作品對應", lambda: self.tabs.select(self.mapping_tab))
        self.pieces_tree = table(self.pieces_tab, ("作品", "紙面小節", "段落", "紙面事件", "來源快照"), (330, 95, 75, 95, 450))
        self.pieces_tree.bind("<<TreeviewSelect>>", lambda e: self.selection_changed())
        ttk.Label(self.pieces_tab, text="Ctrl／Shift 可多選；後續預覽與匯出只使用這裡選取的作品。\n同名但內容不同的檔案會保留成獨立快照；完全相同內容不重複匯入。", wraplength=1100).pack(anchor="w", pady=8)

    def build_mapping(self):
        ttk.Label(self.mapping_tab, text="標準分類回答「這是什麼」；訓練時要合併哪些分類，請到下一頁設定。未啟用的欄位可稍後再處理。", wraplength=1100).pack(anchor="w")
        filters = ttk.Frame(self.mapping_tab)
        filters.pack(fill="x", pady=8)
        ttk.Label(filters, text="欄位").pack(side="left")
        self.field_var = tk.StringVar(value="樂器")
        self.field_combo = ttk.Combobox(filters, textvariable=self.field_var, values=list(FIELDS.values()), state="readonly", width=20)
        self.field_combo.pack(side="left", padx=6)
        self.field_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh_inventory())
        self.pending_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(filters, text="只看待確認", variable=self.pending_var, command=self.refresh_inventory).pack(side="left", padx=10)
        self.inventory_summary = ttk.Label(filters, text="")
        self.inventory_summary.pack(side="right")
        self.mapping_tree = table(self.mapping_tab, ("來源標記", "作品／樂器", "次數", "標準分類", "規則依據", "本次輸出"), (300, 260, 65, 170, 100, 170))
        self.mapping_tree.bind("<Double-1>", lambda e: self.action(self.map_selected))
        toolbar = ttk.Frame(self.mapping_tab)
        toolbar.pack(fill="x")
        self.button(toolbar, "設定所選對應…", self.map_selected)
        self.button(toolbar, "樂器依原名建立並確認", self.confirm_instrument_names)
        self.button(toolbar, "查看來源詳情", self.show_observation)
        self.button(toolbar, "移除所選規則…", self.remove_rules)
        ttk.Label(self.mapping_tab, text="定音鼓的 132、ABC、五線譜不會自動互換。可將來源標記對應到你確認的音高、音級或鼓位；\n音高／擊法預設只記住本作品規則，共用規則則限定相同來源樂器名稱。", wraplength=1100).pack(anchor="w", pady=8)

    def build_settings(self):
        header = ttk.Frame(self.settings_tab)
        header.pack(fill="x")
        ttk.Label(header, text="輸出設定").pack(side="left")
        self.profile_var = tk.StringVar()
        self.profile_combo = ttk.Combobox(header, textvariable=self.profile_var, state="readonly", width=37)
        self.profile_combo.pack(side="left", padx=6)
        self.profile_combo.bind("<<ComboboxSelected>>", lambda e: self.action(self.change_profile))
        self.button(header, "另存為新設定…", self.clone_profile)
        self.button(header, "重新命名", self.rename_profile)
        fixed = ttk.LabelFrame(self.settings_tab, text="固定骨架", padding=9)
        fixed.pack(fill="x", pady=(10, 5))
        ttk.Label(fixed, text="PPQ 960 · 時間位置 · 拍號 · 小節邊界 · 段落邊界 · HIT_SET 同時事件 · 起音／續接狀態", wraplength=1080).pack(anchor="w")
        fields = ttk.LabelFrame(self.settings_tab, text="本次要輸出的選填欄位（設定會自動保存）", padding=9)
        fields.pack(fill="x", pady=5)
        self.field_vars = {}
        for index, (field, label) in enumerate(OPTIONAL.items()):
            var = tk.BooleanVar()
            self.field_vars[field] = var
            ttk.Checkbutton(fields, text=label, variable=var, command=lambda: self.action(self.save_fields)).grid(row=index // 5, column=index % 5, sticky="w", padx=12, pady=5)
        ttk.Label(fields, text="關閉只影響本次 token；來源與對應規則仍完整保留。開啟後，未知欄位使用 UNKNOWN 並標記 loss_mask=0。", wraplength=1070).grid(row=3, column=0, columnspan=5, sticky="w", pady=5)
        merge = ttk.LabelFrame(self.settings_tab, text="訓練輸出合併（只影響目前設定；不改原始樂器身分）", padding=9)
        merge.pack(fill="both", expand=True, pady=6)
        merge_toolbar = ttk.Frame(merge)
        merge_toolbar.pack(fill="x")
        self.merge_field_var = tk.StringVar(value="樂器")
        self.merge_combo = ttk.Combobox(merge_toolbar, textvariable=self.merge_field_var, state="readonly", values=list(FIELDS.values()), width=20)
        self.merge_combo.pack(side="left", padx=5)
        self.merge_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh_projections())
        self.button(merge_toolbar, "設定所選輸出分類…", self.merge_selected)
        self.button(merge_toolbar, "恢復各自輸出", self.reset_projection)
        self.button(merge_toolbar, "重新命名分類", self.rename_concept)
        self.projection_tree = table(merge, ("標準分類", "這次輸出的分類", "分類固定編號"), (350, 350, 180), height=5)
        ttk.Label(merge, text="例如：保留「大鑼」身分，但在此設定中輸出成「小鑼」。合併後同時出現的事件會全部保留並列入報告。", wraplength=1080).pack(anchor="w")

    def build_preview(self):
        toolbar = ttk.Frame(self.preview_tab)
        toolbar.pack(fill="x")
        self.button(toolbar, "更新預覽／檢查待確認項目", self.preview)
        self.button(toolbar, "匯出選取作品…", self.export)
        self.preview_summary = ttk.Label(self.preview_tab, text="先選作品、確認樂器對應，再更新預覽。", wraplength=1110)
        self.preview_summary.pack(fill="x", pady=8)
        frame = ttk.Frame(self.preview_tab)
        frame.pack(fill="both", expand=True)
        self.preview_box = tk.Text(frame, wrap="word", font=("Microsoft JhengHei UI", 10), state="disabled")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.preview_box.yview)
        self.preview_box.configure(yscrollcommand=scroll.set)
        self.preview_box.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        ttk.Label(self.preview_tab, text="匯出包含：sequences.jsonl、vocab.json、規則快照、設定與來源清單、可讀預覽、轉換報告。\n這是獨立國樂 token v1；與既有 GMD 詞彙表接軌仍須另行驗證。", wraplength=1100).pack(anchor="w", pady=8)

    def selected_piece_ids(self):
        return list(self.pieces_tree.selection())

    def selected_field(self, var=None):
        label = (var or self.field_var).get()
        return next(field for field, value in FIELDS.items() if value == label)

    def refresh_all(self, select=None):
        self.path_label.configure(text="工作庫：" + str(self.store.root))
        chosen = set(select if select is not None else self.selected_piece_ids())
        self.pieces_tree.delete(*self.pieces_tree.get_children())
        for piece_id, piece in self.store.data["pieces"].items():
            data = piece["payload"]
            self.pieces_tree.insert("", "end", iid=piece_id, values=(piece["title"], len(data["bars"]), len(data["segments"]), sum(len(b["events"]) for b in data["bars"]), piece["source_path"]))
        self.pieces_tree.selection_set([p for p in chosen if p in self.store.data["pieces"]] or list(self.store.data["pieces"]))
        self.refresh_settings()
        self.refresh_inventory()

    def refresh_settings(self):
        self.profile_keys = list(self.store.data["profiles"])
        self.profile_combo.configure(values=[self.store.data["profiles"][key]["name"] for key in self.profile_keys])
        self.profile_combo.current(self.profile_keys.index(self.store.data["active_profile"]))
        for field, var in self.field_vars.items():
            var.set(self.store.profile["fields"].get(field, False))
        self.refresh_projections()

    def selection_changed(self):
        self.refresh_inventory()
        self.invalidate_preview()

    def select_all(self):
        self.pieces_tree.selection_set(self.pieces_tree.get_children())

    def refresh_inventory(self):
        if not hasattr(self, "mapping_tree"):
            return
        self.mapping_tree.delete(*self.mapping_tree.get_children())
        self.rows = {}
        field = self.selected_field()
        items = [row for row in self.store.inventory(self.selected_piece_ids()) if row["field"] == field]
        pending = sum(row["target"] is None for row in items)
        for index, row in enumerate(items):
            if self.pending_var.get() and row["target"] is not None:
                continue
            key = str(index)
            self.rows[key] = row
            target = self.store.concept(field, row["target"])["label"] if row["target"] else "待確認"
            output = "本次關閉" if field != "instrument" and not self.store.profile["fields"].get(field) else (
                self.store.concept(field, self.store.project(field, row["target"]))["label"] if row["target"] else "待確認")
            piece = self.store.data["pieces"][row["piece_id"]]
            context = piece["title"] + ("／" + row["instrument"] if field != "instrument" else "")
            self.mapping_tree.insert("", "end", iid=key, values=(row["source"], context, row["count"], target, row["reason"], output))
        self.inventory_summary.configure(text=f"已見 {len(items)} 項 · 待確認 {pending} 項")

    def refresh_projections(self):
        self.projection_tree.delete(*self.projection_tree.get_children())
        field = self.selected_field(self.merge_field_var)
        for concept in self.store.data["concepts"][field]:
            target = self.store.concept(field, self.store.project(field, concept["id"]))
            self.projection_tree.insert("", "end", iid=concept["id"], values=(concept["label"], target["label"], concept["id"]))

    def import_files(self, paths=None):
        paths = paths or filedialog.askopenfilenames(parent=self.root, title="匯入多份已校對國樂作品", filetypes=[("已校對 JSON", "*.json")])
        added, existing, errors = [], 0, []
        for path in paths:
            try:
                piece_id, new = self.store.import_file(path)
                added.append(piece_id)
                existing += int(not new)
            except Exception as error:
                errors.append(f"{Path(path).name}：{error}")
        self.refresh_all(select=self.selected_piece_ids() + added)
        self.invalidate_preview()
        self.status.set(f"匯入完成：新增 {len(added) - existing} 首，相同內容 {existing} 首，失敗 {len(errors)} 份。對應規則會持續保存。")
        if errors:
            messagebox.showerror("部分檔案未匯入", "\n".join(errors), parent=self.root)

    def selected_rows(self):
        rows = [self.rows[key] for key in self.mapping_tree.selection()]
        if not rows:
            raise TokenizerError("請先選擇來源標記，可用 Ctrl／Shift 多選")
        return rows

    def mapping_dialog(self, title, field, apply, *, source_hint="", default_label="", allow_new=True):
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.geometry("650x350")
        dialog.transient(self.root)
        dialog.grab_set()
        body = ttk.Frame(dialog, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=source_hint, wraplength=600).pack(anchor="w", pady=(0, 12))
        ttk.Label(body, text="選擇標準分類，或輸入新名稱" if allow_new else "選擇這次要輸出的標準分類").pack(anchor="w")
        target_var = tk.StringVar(value=default_label)
        combo = ttk.Combobox(body, textvariable=target_var, values=[c["label"] for c in self.store.data["concepts"][field]],
                             state="normal" if allow_new else "readonly", width=60)
        combo.pack(fill="x", pady=7)
        scope = tk.StringVar(value="global" if field in {"instrument", "is_roll", "is_accent", "is_ornament", "tie"} else "piece")
        if allow_new:
            ttk.Radiobutton(body, text="只套用所選來源的作品（相同作品優先）", variable=scope, value="piece").pack(anchor="w", pady=3)
            ttk.Radiobutton(body, text="共用規則：往後相同標記／來源樂器可自動套用", variable=scope, value="global").pack(anchor="w", pady=3)
            if field == "pitch":
                ttk.Label(body, text="例：MIDI:48、音級 1、低音鼓位。只有你確認對應後才保存；程式不猜 1／A 的意思。", wraplength=600).pack(anchor="w", pady=5)

        def submit():
            def operation():
                label = target_var.get().strip()
                if not label:
                    raise TokenizerError("請選擇或輸入分類名稱")
                concept = self.store.add_concept(field, label) if allow_new else next(c for c in self.store.data["concepts"][field] if c["label"] == label)
                apply(concept["id"], scope.get())
                self.store.save()
                self.refresh_inventory()
                self.refresh_projections()
                self.invalidate_preview()
                dialog.destroy()
            self.action(operation)
        self.button(body, "保存設定", submit, side="right")
        combo.focus_set()

    def map_selected(self):
        rows = self.selected_rows()
        field = rows[0]["field"]

        def apply(target, scope):
            for row in rows:
                self.store.set_rule(field, row["source"], row["instrument"], target, scope=scope, piece_id=row["piece_id"])
            self.status.set(f"已保存 {len(rows)} 項對應。新作品符合共用規則時會直接套用。")
        target = self.store.concept(field, rows[0]["target"])["label"] if rows[0]["target"] else ""
        self.mapping_dialog("設定來源對應", field, apply, source_hint=f"{FIELDS[field]}：{rows[0]['source']}" + (f" 等 {len(rows)} 項" if len(rows) > 1 else ""), default_label=target)

    def confirm_instrument_names(self):
        if self.selected_field() != "instrument":
            raise TokenizerError("此功能只用於樂器；音高與擊法請明確選擇對應")
        rows = self.selected_rows()
        for row in rows:
            concept = self.store.add_concept("instrument", row["source"])
            self.store.set_rule("instrument", row["source"], "", concept["id"], scope="global")
        self.store.save()
        self.refresh_inventory()
        self.refresh_projections()
        self.invalidate_preview()
        self.status.set(f"已依原名確認 {len(rows)} 項樂器共用規則。可再設定別名對應或訓練輸出合併。")

    def show_observation(self):
        rows = self.selected_rows()
        parts = []
        for row in rows[:8]:
            parts.append(f"作品：{self.store.data['pieces'][row['piece_id']]['title']}\n欄位：{FIELDS[row['field']]}\n來源：{row['source']}\n樂器：{row['instrument']}\n小節：{', '.join(map(str, sorted(row['bars'])))}\n符號範例：{row['example']}\n依據：{row['reason']}")
        messagebox.showinfo("來源詳情", "\n\n".join(parts), parent=self.root)

    def remove_rules(self):
        rows = self.selected_rows()
        dialog = tk.Toplevel(self.root)
        dialog.title("移除對應規則")
        dialog.transient(self.root)
        dialog.grab_set()
        body = ttk.Frame(dialog, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="選擇移除範圍。原始作品與標準分類會保留。", wraplength=500).pack(anchor="w")
        scope = tk.StringVar(value="piece")
        ttk.Radiobutton(body, text="只移除所選作品的專用規則", variable=scope, value="piece").pack(anchor="w", pady=8)
        ttk.Radiobutton(body, text="移除所選標記的共用規則（會影響其他作品）", variable=scope, value="global").pack(anchor="w", pady=8)

        def apply():
            remove = []
            for rule in self.store.data["rules"]:
                for row in rows:
                    context = "" if row["field"] in {"instrument", "is_roll", "is_accent", "is_ornament", "tie"} else normalized(row["instrument"])
                    if rule["scope"] == scope.get() and rule["field"] == row["field"] and rule["source"] == normalized(row["source"]) and rule["instrument"] == context and (rule["scope"] == "global" or rule["piece_id"] == row["piece_id"]):
                        remove.append(rule)
            self.store.data["rules"] = [r for r in self.store.data["rules"] if r not in remove]
            self.store.save()
            self.refresh_inventory()
            self.invalidate_preview()
            dialog.destroy()
        self.button(body, "移除選定範圍的規則", lambda: self.action(apply), side="right")

    def save_fields(self):
        self.store.profile["fields"] = {field: var.get() for field, var in self.field_vars.items()}
        self.store.save()
        self.refresh_inventory()
        self.invalidate_preview()
        self.status.set("已保存本次輸出欄位。關閉欄位的資料與對應仍完整保留。")

    def change_profile(self):
        self.store.data["active_profile"] = self.profile_keys[self.profile_combo.current()]
        self.store.save()
        self.refresh_settings()
        self.refresh_inventory()
        self.invalidate_preview()

    def clone_profile(self):
        name = simpledialog.askstring("另存輸出設定", "新設定名稱，例如「含輪奏版」：", parent=self.root)
        if name:
            self.store.clone_profile(name)
            self.refresh_settings()
            self.invalidate_preview()

    def rename_profile(self):
        name = simpledialog.askstring("重新命名", "設定名稱：", initialvalue=self.store.profile["name"], parent=self.root)
        if name and name.strip():
            self.store.profile["name"] = name.strip()
            self.store.save()
            self.refresh_settings()
            self.invalidate_preview()

    def merge_selected(self):
        selected = self.projection_tree.selection()
        if not selected:
            raise TokenizerError("請先選擇要合併的標準分類")
        field = self.selected_field(self.merge_field_var)

        def apply(target, scope):
            previous = deepcopy(self.store.profile["projections"][field])
            try:
                for source in selected:
                    self.store.set_projection(field, source, target)
            except Exception:
                self.store.profile["projections"][field] = previous
                raise
            self.status.set("已保存此輸出設定的合併方式。來源身分與其他設定不受影響。")
        self.mapping_dialog("設定訓練輸出分類", field, apply, source_hint=f"將 {len(selected)} 個標準分類輸出為同一類。", allow_new=False)

    def reset_projection(self):
        field = self.selected_field(self.merge_field_var)
        for source in self.projection_tree.selection():
            self.store.set_projection(field, source, source)
        self.store.save()
        self.refresh_projections()
        self.refresh_inventory()
        self.invalidate_preview()

    def rename_concept(self):
        selected = self.projection_tree.selection()
        if len(selected) != 1:
            raise TokenizerError("請選擇一個標準分類")
        field = self.selected_field(self.merge_field_var)
        concept = self.store.concept(field, selected[0])
        name = simpledialog.askstring("重新命名標準分類", "名稱會更新，分類與 token 固定編號保留：", initialvalue=concept["label"], parent=self.root)
        if name is not None:
            self.store.rename_concept(field, concept["id"], name)
            self.store.save()
            self.refresh_projections()
            self.refresh_inventory()
            self.invalidate_preview()

    def invalidate_preview(self):
        if hasattr(self, "preview_summary"):
            self.preview_summary.configure(text="作品或設定已更新；請重新產生預覽。")
            self.set_preview_text("")

    def set_preview_text(self, text):
        self.preview_box.configure(state="normal")
        self.preview_box.delete("1.0", "end")
        self.preview_box.insert("1.0", text)
        self.preview_box.configure(state="disabled")

    def preview(self):
        result = self.store.convert(self.selected_piece_ids(), preview=True)
        self.set_preview_text(self.store.preview_text(result, limit=180))
        self.preview_summary.configure(text=f"{len(self.selected_piece_ids())} 首作品 · {len(result['sequences'])} 段 · "
            f"{sum(len(s['tokens']) for s in result['sequences']):,} tokens · {result['unknown_tokens']} 個未知欄位 · "
            f"{len(result['collisions'])} 組相同同時事件（全部保留）")
        self.status.set("預覽完成。畫面每段最多顯示 180 tokens，正式匯出包含完整序列。")

    def export(self):
        # Validate before asking where to write output.
        self.store.convert(self.selected_piece_ids())
        parent = filedialog.askdirectory(parent=self.root, title="選擇輸出位置（將建立新的版本資料夾）")
        if not parent:
            return
        folder, manifest = self.store.export(self.selected_piece_ids(), parent)
        self.status.set(f"已匯出 {manifest['sequence_count']} 段、{manifest['token_count']:,} tokens：{folder}")
        messagebox.showinfo("匯出完成", f"{manifest['sequence_count']} 段，{manifest['token_count']:,} tokens\n{folder}\n\n原始 JSON 與舊輸出保留。接入 GMD 模型前仍須對齊詞彙表。", parent=self.root)

    def backup(self):
        path = filedialog.asksaveasfilename(parent=self.root, title="備份工作庫（含作品快照、規則、設定、詞彙表）", initialfile="tokenizer-library.json", defaultextension=".json", filetypes=[("JSON 工作庫", "*.json")])
        if path:
            destination = Path(path).resolve()
            protected = {Path(p["source_path"]).resolve() for p in self.store.data["pieces"].values()}
            if destination in protected or destination == self.store.path:
                raise TokenizerError("請使用新的備份位置，避免覆寫作品或目前工作庫")
            self.store.save()
            atomic_json(path, self.store.data)
            self.status.set("工作庫已備份。將備份命名為 tokenizer-library.json 放入資料夾，即可用「開啟其他工作庫」載入。")

    def open_workspace(self):
        path = filedialog.askdirectory(parent=self.root, title="選擇工作庫資料夾（可包含 tokenizer-library.json 備份）")
        if path:
            store = Store(path)
            store.save()
            self.store = store
            self.refresh_all()
            self.invalidate_preview()
            self.status.set("已開啟工作庫。此視窗的所有修改均保存到目前顯示的位置。")

    def close(self):
        self.store.save()
        self.root.destroy()


def run(workspace=None, sources=(), page="pieces"):
    root = tk.Tk()
    app = TokenizerApp(root, workspace)
    if sources:
        app.import_files(sources)
    app.tabs.select({"pieces": app.pieces_tab, "mapping": app.mapping_tab,
                     "settings": app.settings_tab, "preview": app.preview_tab}[page])
    root.mainloop()
