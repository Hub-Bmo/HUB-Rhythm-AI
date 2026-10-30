"""每小節的同圖結果、候選差異、用量及人工比較筆記。"""
import json
import tkinter as tk
from tkinter import ttk

from guoyue_annotator.photo_assets import safe_project_path
from score_recognizer.canvas import ScoreCanvas
from .comparison import differences, event_rows, readable_row

STATES = {"queued": "尚未送出", "running": "等待回應", "succeeded": "已收到候選", "empty": "無聲部・需核對", "invalid": "格式需檢查",
          "failed": "失敗", "cancelled": "未送出・已停止", "interrupted": "中斷・未自動重送"}


def usage_text(run):
    if not run:
        return "尚無結果"
    usage = run.get("usage") or {}
    cost = "未知" if run.get("cost_usd") is None else f"US${run['cost_usd']:.6f}（粗估，未扣快取）"
    seconds = "—" if run.get("elapsed_seconds") is None else f"{run['elapsed_seconds']:.1f} 秒"
    return f"{STATES.get(run['status'], run['status'])}｜{seconds}｜輸入 {usage.get('input_tokens', '未知')}／輸出 {usage.get('output_tokens', '未知')} tokens｜{cost}" + ("\n" + run["error"] if run.get("error") else "")


class ResultsPanel(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=8)
        self.app, self.selected_case = app, None
        self.bind("<Visibility>", lambda e: self.after_idle(self.fit_images) if e.widget is self else None)
        self.bind("<Map>", lambda e: self.after(50, self.fit_images) if e.widget is self else None)
        self.left_model, self.right_model = tk.StringVar(), tk.StringVar()
        self.note, self.verdict = tk.StringVar(), tk.StringVar(value="未判定")
        header = ttk.Frame(self)
        header.pack(fill="x")
        ttk.Label(header, text="差異是待核對位置，不是準確率；即使相同也可能一起讀錯。", foreground="#8a5019").pack(side="left")
        self.app.button(header, "重新整理", self.refresh).pack(side="right")
        self.app.button(header, "只重試此筆失敗模型…", self.app.retry_selected_case).pack(side="right", padx=6)
        body = ttk.Panedwindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, pady=6)
        left = ttk.Frame(body)
        body.add(left, weight=0)
        self.cases = ttk.Treeview(left, columns=("bar", "page", "state"), show="headings", selectmode="browse", height=15)
        for key, label, width in (("bar", "小節", 55), ("page", "頁", 45), ("state", "完成／總數", 105)):
            self.cases.heading(key, text=label)
            self.cases.column(key, width=width, stretch=False)
        self.cases.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(left, command=self.cases.yview)
        self.cases.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.cases.bind("<<TreeviewSelect>>", lambda e: self.select_case())
        center = ttk.Frame(body)
        body.add(center, weight=1)
        self.case_label = ttk.Label(center, text="選擇一筆比較紀錄", wraplength=1100)
        self.case_label.pack(fill="x", pady=3)
        images = ttk.Notebook(center, height=195)
        images.pack(fill="x")
        self.images = []
        for title in ("實際送出的單小節主圖（可縮放；選事件顯示證據框）", "相同的參考圖"):
            canvas = ScoreCanvas(images, lambda *args: None, lambda *args: None)
            canvas.canvas.unbind("<ButtonPress-1>")
            canvas.canvas.unbind("<B1-Motion>")
            canvas.canvas.unbind("<ButtonRelease-1>")
            images.add(canvas, text=title)
            self.images.append(canvas)
        image_tools = ttk.Frame(center)
        image_tools.pack(fill="x")
        def zoom(factor):
            canvas = self.images[images.index(images.select())]
            canvas.scale = max(.03, min(8, canvas.scale * factor))
            canvas.redraw()
        ttk.Button(image_tools, text="＋ 放大圖片", command=lambda: zoom(1.4)).pack(side="left", pady=3)
        ttk.Button(image_tools, text="－ 縮小圖片", command=lambda: zoom(1 / 1.4)).pack(side="left", padx=5)
        ttk.Button(image_tools, text="看整張圖片", command=self.fit_images).pack(side="left")
        choices = ttk.Frame(center)
        choices.pack(fill="x", pady=4)
        self.selectors = []
        for title, variable in (("左模型", self.left_model), ("右模型", self.right_model)):
            ttk.Label(choices, text=title).pack(side="left", padx=4)
            combo = ttk.Combobox(choices, textvariable=variable, state="readonly", width=23)
            combo.pack(side="left", padx=(0, 15))
            combo.bind("<<ComboboxSelected>>", lambda e: self.change_pair())
            self.selectors.append(combo)
        self.tabs = ttk.Notebook(center)
        self.tabs.pack(fill="both", expand=True)
        candidates = ttk.Panedwindow(self.tabs, orient="horizontal")
        self.tabs.add(candidates, text="並排事件（可左右捲動查看音高與記號）")
        self.tables, self.summaries, self.table_rows = [], [], [[], []]
        columns = ("差異", "樂器", "種類", "第幾拍", "時值", "原符號", "音高", "輪奏", "重音", "裝飾音", "延音線", "擊法文字", "疑問")
        for side in range(2):
            pane = ttk.Frame(candidates)
            candidates.add(pane, weight=1)
            summary = ttk.Label(pane, text="", wraplength=510)
            summary.pack(fill="x", pady=3)
            self.summaries.append(summary)
            pane.bind("<Configure>", lambda e, label=summary: label.configure(wraplength=max(180, e.width - 12)))
            table = ttk.Treeview(pane, columns=columns, show="headings", height=7, selectmode="browse")
            for key in columns:
                table.heading(key, text=key)
                table.column(key, width=210 if key in {"音高", "疑問"} else 85, stretch=False)
            table.tag_configure("different", background="#fff0cf")
            scroll = ttk.Scrollbar(pane, orient="horizontal", command=table.xview)
            vertical = ttk.Scrollbar(pane, orient="vertical", command=table.yview)
            table.configure(xscrollcommand=scroll.set, yscrollcommand=vertical.set)
            scroll.pack(side="bottom", fill="x")
            vertical.pack(side="right", fill="y")
            table.pack(fill="both", expand=True)
            table.bind("<<TreeviewSelect>>", lambda e, n=side: self.show_evidence(n))
            self.tables.append(table)
        diff_host = ttk.Frame(self.tabs)
        self.tabs.add(diff_host, text="差異清單")
        self.diff_label = ttk.Label(diff_host, text="", wraplength=1000)
        self.diff_label.pack(fill="x")
        self.diff_table = ttk.Treeview(diff_host, columns=("state", "left", "right", "fields"), show="headings")
        for key, label, width in (("state", "狀態", 130), ("left", "左模型", 320), ("right", "右模型", 320), ("fields", "差異欄位", 220)):
            self.diff_table.heading(key, text=label)
            self.diff_table.column(key, width=width)
        diff_scroll = ttk.Scrollbar(diff_host, command=self.diff_table.yview)
        self.diff_table.configure(yscrollcommand=diff_scroll.set)
        diff_scroll.pack(side="right", fill="y")
        self.diff_table.pack(fill="both", expand=True)
        self.diff_table.bind("<<TreeviewSelect>>", self.locate_difference)
        raw_host = ttk.Frame(self.tabs)
        self.tabs.add(raw_host, text="完整回應／反覆／辨識疑問")
        self.raw = tk.Text(raw_host, wrap="word")
        raw_scroll = ttk.Scrollbar(raw_host, command=self.raw.yview)
        self.raw.configure(yscrollcommand=raw_scroll.set)
        raw_scroll.pack(side="right", fill="y")
        self.raw.pack(fill="both", expand=True)
        judgment = ttk.Frame(center)
        judgment.pack(fill="x", pady=5)
        ttk.Label(judgment, text="人工比較").pack(side="left")
        ttk.Combobox(judgment, textvariable=self.verdict, values=("未判定", "左側較接近原譜", "右側較接近原譜", "兩者皆需修改", "兩者皆已核對"), state="readonly", width=19).pack(side="left", padx=4)
        ttk.Entry(judgment, textvariable=self.note).pack(side="left", fill="x", expand=True, padx=4)
        self.app.button(judgment, "保存比較筆記", self.save_judgment).pack(side="left")
        self.tabs.pack_forget()
        judgment.pack_forget()
        judgment.pack(side="bottom", fill="x", pady=5)
        self.tabs.pack(fill="both", expand=True)

    def fit_images(self):
        for canvas in self.images:
            if canvas.image is not None:
                fit = max(200, canvas.canvas.winfo_width() - 35) / canvas.image.width
                available_height = max(120, canvas.canvas.winfo_height() - 35)
                canvas.scale = min(1, available_height / (canvas.image.height * fit))
                canvas.canvas.yview_moveto(0)
                canvas.redraw()

    def change_pair(self):
        case = self.current_case()
        if not case:
            return
        if self.left_model.get() == self.right_model.get():
            self.right_model.set(next(r["model"] for r in case["runs"] if r["model"] != self.left_model.get()))
        self.verdict.set("未判定")
        self.render()

    def refresh(self, case_id=None):
        selected = case_id or self.selected_case
        self.cases.delete(*self.cases.get_children())
        project = self.app.project
        if project:
            for case in reversed(project.data["cases"]):
                done = sum(r["status"] not in {"queued", "running"} for r in case["runs"])
                self.cases.insert("", "end", iid=case["case_id"], values=(case["bar"], case["page"], f"{done}/{len(case['runs'])}"))
        if self.cases.get_children():
            chosen = selected if selected and self.cases.exists(selected) else self.cases.get_children()[0]
            self.cases.selection_set(chosen)
            self.cases.see(chosen)
            self.select_case()
        else:
            self.selected_case = None
            self.case_label.configure(text="尚無比較結果；到第一頁選擇小節，確認後送出比較。")
            for table in (*self.tables, self.diff_table):
                table.delete(*table.get_children())
            for canvas in self.images:
                canvas.image = None
                canvas.evidence = None
                canvas.redraw()
            for summary in self.summaries:
                summary.configure(text="尚無結果")
            self.diff_label.configure(text="")
            self.raw.configure(state="normal")
            self.raw.delete("1.0", "end")
            self.raw.configure(state="disabled")
            self.note.set("")
            self.verdict.set("未判定")

    def current_case(self):
        if not self.app.project or not self.selected_case:
            return None
        return self.app.project.case(self.selected_case)

    def select_case(self):
        if not self.cases.selection() or not self.app.project:
            return
        case_id = self.cases.selection()[0]
        changed = self.selected_case != case_id
        self.selected_case = case_id
        case = self.current_case()
        names = [r["model"] for r in case["runs"]]
        for selector in self.selectors:
            selector.configure(values=names)
        if changed or self.left_model.get() not in names or self.right_model.get() not in names:
            self.left_model.set(names[0])
            self.right_model.set(names[1])
        if changed:
            for canvas, name in zip(self.images, ("primary.png", "context.png")):
                path = self.app.project.root / "cases" / case_id / name
                if path.is_file():
                    canvas.load(path)
                    canvas.evidence = None
                    canvas.redraw()
                else:
                    canvas.image = None
                    canvas.redraw()
            prior = case.get("judgment") or {}
            self.verdict.set(prior.get("verdict", "未判定"))
            self.note.set(prior.get("note", ""))
            self.after_idle(self.fit_images)
        self.render()

    def render(self):
        case = self.current_case()
        if not case:
            return
        a = next(r for r in case["runs"] if r["model"] == self.left_model.get())
        b = next(r for r in case["runs"] if r["model"] == self.right_model.get())
        report = differences(a.get("result"), b.get("result"))
        stale = not any(r["region_id"] == case["region_id"] and self.app.workspace.fingerprint(r) == case["region_fingerprint"] for r in self.app.workspace.package["regions"])
        self.case_label.configure(text=f"第 {case['page']} 頁／小節 {case['bar']}｜{case['created_at']}｜圖片 {case['image_detail']}／推理 {case['reasoning']}｜拍點從 1 起，四分音符時值 = 1" + ("｜範圍已修改，這是先前的輸入快照" if stale else ""))
        changed = [{}, {}]
        for row in report["rows"]:
            if row["state"] != "相同":
                for side, key in enumerate(("left", "right")):
                    if row[key] is not None:
                        changed[side][row[key]] = "、".join(row["fields"]) if row["state"].startswith("不同") else row["state"]
        for side, run in enumerate((a, b)):
            self.summaries[side].configure(text=run["model"] + "\n" + usage_text(run))
            table = self.tables[side]
            table.delete(*table.get_children())
            self.table_rows[side] = report["left" if side == 0 else "right"]
            for index, row in enumerate(self.table_rows[side]):
                table.insert("", "end", iid=str(index), values=tuple(changed[side].get(index, "相同") if k == "差異" else row.get(k, "") for k in table["columns"]), tags=("different",) if index in changed[side] else ())
        self.diff_table.delete(*self.diff_table.get_children())
        self.diff_rows = []
        valid_pair = bool(a.get("result") and b.get("result"))
        if valid_pair:
            self.diff_label.configure(text=f"相同 {report['same']} 列；差異 {report['different']} 列。其他差異：{'、'.join(report['metadata']) or '無'}。配對僅供定位；疑問文字與框位置不計入相同／不同。")
            for row in report["rows"]:
                if row["state"] == "相同":
                    continue
                self.diff_rows.append(row)
                left = report["left"][row["left"]] if row["left"] is not None else None
                right = report["right"][row["right"]] if row["right"] is not None else None
                self.diff_table.insert("", "end", iid=str(len(self.diff_rows) - 1), values=(row["state"], readable_row(left), readable_row(right), "、".join(row["fields"])))
        else:
            self.diff_label.configure(text="需兩個模型都收到結構可讀的結果才能比較；失敗或尚未回應不等於沒有音符。")
        self.raw.configure(state="normal")
        self.raw.delete("1.0", "end")
        self.raw.insert("1.0", json.dumps({"輸入指紋": case["input_sha256"], "左側": a, "右側": b,
                                          "先前嘗試（可能另有用量）": case.get("previous_attempts", [])}, ensure_ascii=False, indent=2))
        self.raw.configure(state="disabled")

    def show_evidence(self, side):
        table = self.tables[side]
        if table.selection():
            self.images[0].evidence = self.table_rows[side][int(table.selection()[0])]["證據"]
            self.images[0].redraw()

    def locate_difference(self, event=None):
        if self.diff_table.selection():
            row = self.diff_rows[int(self.diff_table.selection()[0])]
            for side, key in enumerate(("left", "right")):
                if row[key] is not None:
                    self.tables[side].selection_set(str(row[key]))
                    self.tables[side].see(str(row[key]))

    def save_judgment(self):
        case = self.current_case()
        if case:
            case["judgment"] = {"models": [self.left_model.get(), self.right_model.get()],
                                "verdict": self.verdict.get(), "note": self.note.get()}
            self.app.project.save()
            self.app.status_text.set("人工比較筆記已保存；沒有把任何模型結果自動當作正確答案。")
