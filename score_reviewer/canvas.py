"""校對原譜及單小節節奏格，沒有辨識或來源框編輯功能。"""
from __future__ import annotations

from fractions import Fraction
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageOps, ImageTk

from score_recognizer.contract import fraction, meter_length


class EvidenceView(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.canvas = tk.Canvas(self, background="#e9edf1", highlightthickness=0)
        horizontal = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        vertical = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=horizontal.set, yscrollcommand=vertical.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.image = self.photo = self.bar_box = self.evidence = None
        self.scale = 1
        self.cached_path = None
        self.source_image = None
        self.crop = {"x": 0, "y": 0, "width": 1, "height": 1}
        self.canvas.bind("<Configure>", lambda e: self.redraw())
        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-int(e.delta / 120) * 3, "units"))
        self.canvas.bind("<Control-MouseWheel>", self.zoom_wheel)

    def zoom_wheel(self, event):
        self.scale = max(.5, min(5, self.scale * (1.2 if event.delta > 0 else 1 / 1.2)))
        self.redraw()
        return "break"

    def load(self, path, crop=None, bar_box=None, evidence=None):
        if str(path) != self.cached_path:
            with Image.open(path) as image:
                self.source_image = ImageOps.exif_transpose(image).convert("RGB")
            self.cached_path = str(path)
        self.crop = crop or {"x": 0, "y": 0, "width": 1, "height": 1}
        box, im = self.crop, self.source_image
        self.image = im.crop((round(box["x"] * im.width), round(box["y"] * im.height),
                              round((box["x"] + box["width"]) * im.width), round((box["y"] + box["height"]) * im.height)))
        self.bar_box, self.evidence = bar_box, evidence
        self.redraw()

    def set_evidence(self, box):
        self.evidence = box
        self.redraw()

    def redraw(self):
        self.canvas.delete("all")
        if self.image is None:
            self.canvas.create_text(20, 25, anchor="nw", text="匯入已完成的辨識資料後，在這裡對照原譜。", fill="#52647a")
            return
        fit = max(200, self.canvas.winfo_width() - 25) / self.image.width * self.scale
        w, h = max(1, round(self.image.width * fit)), max(1, round(self.image.height * fit))
        self.photo = ImageTk.PhotoImage(self.image.resize((w, h), Image.Resampling.LANCZOS))
        self.canvas.create_image(10, 10, anchor="nw", image=self.photo)
        self.canvas.configure(scrollregion=(0, 0, w + 22, h + 22))
        for box, color, dash in ((self.bar_box, "#d47716", ()), (self.evidence, "#16886c", (5, 3))):
            if not box:
                continue
            x = (box["x"] - self.crop["x"]) / self.crop["width"] * w + 10
            y = (box["y"] - self.crop["y"]) / self.crop["height"] * h + 10
            self.canvas.create_rectangle(x, y, x + box["width"] / self.crop["width"] * w,
                                         y + box["height"] / self.crop["height"] * h, outline=color, width=2, dash=dash)


class RhythmGrid(ttk.Frame):
    mapping_scopes = {"本小節這一列": "bar", "本頁同名聲部": "page", "整份譜同名聲部": "score"}

    def __init__(self, master, on_select, on_add, on_reject=None, on_map_row=None):
        super().__init__(master)
        self.on_select, self.on_add = on_select, on_add
        self.on_reject = on_reject
        self.on_map_row = on_map_row
        self.left_margin = 210 if on_map_row else 130
        self.row_height = 68 if on_map_row else 44
        self.row_controls = {}
        self.controls_context = None
        self.mapping_scope = tk.StringVar(master=self, value="本小節這一列")
        if on_map_row:
            toolbar = ttk.Frame(self, padding=(5, 3))
            toolbar.pack(side="top", fill="x")
            ttk.Label(toolbar, text="套用範圍").pack(side="left")
            ttk.Combobox(toolbar, textvariable=self.mapping_scope, values=list(self.mapping_scopes),
                         state="readonly", width=18).pack(side="left", padx=5)
            ttk.Label(toolbar, text="樂器名可輸入文字篩選", foreground="#52647a").pack(side="left", padx=3)
        self.enable_add = False
        self.canvas = tk.Canvas(self, background="#fbfcfd", highlightthickness=0)
        vertical = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vertical.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        vertical.pack(side="right", fill="y")
        self.canvas.bind("<Configure>", lambda e: self.redraw())
        self.canvas.bind("<Double-Button-1>", self.add)
        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-int(e.delta / 120) * 3, "units"))
        self.doc = None
        self.bar = None
        self.selected = None
        self.rows = []
        self.row_spans = []
        self.content_height = 80
        self.units = Fraction(4)
        self.step = Fraction(1, 4)

    def set_bar(self, doc, bar, selected=None):
        self.doc, self.bar, self.selected = doc, bar, selected
        self.redraw()

    def redraw(self):
        context = (id(self.doc), self.bar)
        if context != self.controls_context:
            for control in self.row_controls.values():
                control["frame"].destroy()
            self.row_controls.clear()
            self.controls_context = context
        self.canvas.delete("all")
        if self.doc is None or self.bar is None:
            self.canvas.create_text(20, 25, anchor="nw", text="節奏格：橘色待核對、綠色已接受、灰色休止。")
            return
        meter, _ = self.doc.bar_setting(self.bar)
        self.units = meter_length(meter) if meter else Fraction(4)
        entries = [e for e in self.doc.entries(self.bar) if e["status"] != "rejected"]
        keys = []
        for entry in entries:
            v = entry["value"]
            key = v["staff_label"] or v.get("instrument_text") or "未辨明聲部"
            if key not in keys:
                keys.append(key)
        self.rows = keys
        for key in set(self.row_controls) - set(keys):
            self.row_controls.pop(key)["frame"].destroy()
        width, left = max(450, self.canvas.winfo_width()), self.left_margin
        available = width - left - 18
        chip_width = min(196, available)
        chip_columns = max(1, int(available // (chip_width + 6)))
        tops, unknowns, cursor = {}, {}, 34
        self.row_spans = []
        for key in keys:
            pending = [e for e in entries if (e["value"]["staff_label"] or e["value"].get("instrument_text") or "未辨明聲部") == key
                       and e["value"]["kind"] != "full_rest" and e["value"].get("onset") is None]
            pending.sort(key=lambda e: (e["value"].get("source_bbox") or {}).get("x", 2))
            unknowns[key] = pending
            tops[key] = cursor
            height = self.row_height + (32 * ((len(pending) + chip_columns - 1) // chip_columns) + 5 if pending else 0)
            self.row_spans.append((cursor, cursor + height))
            cursor += height
        self.content_height = cursor + 12
        for index in range(int(self.units / self.step) + 1):
            pos = self.step * index
            x = left + float(pos / self.units) * available
            self.canvas.create_line(x, 30, x, self.content_height, fill="#b1bcc9" if pos.denominator == 1 else "#e4e9ef")
            if pos.denominator == 1 and pos < self.units:
                self.canvas.create_text(x + 5, 15, anchor="w", text=str(int(pos) + 1), fill="#52647a")
        for row, key in enumerate(keys):
            y = tops[key]
            row_entries = [e for e in entries if (e["value"]["staff_label"] or e["value"].get("instrument_text") or "未辨明聲部") == key]
            ids = {e["value"].get("instrument_id") for e in row_entries}
            ins = self.doc.instrument(next(iter(ids))) if len(ids) == 1 else None
            label = f"{ins['name']}（{key}）" if ins and ins["name"] != key else key
            self.canvas.create_text(8, y + (12 if self.on_map_row else 18), anchor="w", text=label[:16], fill="#35475d")
            if self.on_map_row:
                self.draw_row_control(key, y, ins)
            self.canvas.create_line(0, self.row_spans[row][1], width, self.row_spans[row][1], fill="#e4e9ef")
            if unknowns[key]:
                self.canvas.create_text(8, y + self.row_height + 14, anchor="w", text="待定位（非拍點）", fill="#986017")
            for index, entry in enumerate(unknowns[key]):
                x = left + (index % chip_columns) * (chip_width + 6)
                cy = y + self.row_height + (index // chip_columns) * 32
                tag = "event:" + entry["id"]
                self.canvas.create_rectangle(x, cy, x + chip_width - 2, cy + 27, fill="#fff0d7",
                    outline="#143b66" if self.selected == entry["id"] else "#d88b37", width=3 if self.selected == entry["id"] else 1, tags=(tag,))
                label = "休止 0" if entry["value"]["kind"] == "rest" else ("敲擊" if entry["value"]["kind"] == "note" else "未辨明符號")
                self.canvas.create_text(x + 7, cy + 14, anchor="w", text=label + "｜第幾拍待填", fill="#81511b", tags=(tag,))
                self.bind_entry(tag, entry["id"])
                self.draw_delete(entry["id"], x + chip_width - 15, cy + 14)
        for entry in entries:
            v = entry["value"]
            key = v["staff_label"] or v.get("instrument_text") or "未辨明聲部"
            row = keys.index(key)
            y = tops[key]
            tag = "event:" + entry["id"]
            if v["kind"] == "full_rest":
                self.canvas.create_rectangle(left, y + 3, width - 18, y + 32, fill="#f0f3f7",
                    outline="#143b66" if self.selected == entry["id"] else "#e4e9ef", tags=(tag,))
                self.canvas.create_text(left + available / 2, y + 18, text="整小節休止", fill="#8995a3", tags=(tag,))
                self.bind_entry(tag, entry["id"])
                if self.selected == entry["id"]:
                    self.draw_delete(entry["id"], width - 31, y + 18)
                continue
            if v.get("onset") is None:
                continue
            onset = fraction(v["onset"])
            duration = fraction(v["duration"]) if v.get("duration") else Fraction(1, 8)
            x1 = left + float(onset / self.units) * available
            x2 = min(width - 18, x1 + max(10, float(duration / self.units) * available - 3))
            y += 4
            color = "#27987b" if entry["status"] == "accepted" else "#d88b37"
            if v["kind"] == "rest":
                color = "#a5afb9"
            self.canvas.create_rectangle(x1 + 1, y, x2, y + 28, fill=color,
                                         outline="#143b66" if self.selected == entry["id"] else color,
                                         width=3 if self.selected == entry["id"] else 1, tags=(tag,))
            label = "0" if v["kind"] == "rest" else ("≋" if v.get("is_roll") else "●")
            degrees = [str(p["degree"]) for p in v.get("pitches", []) if p.get("degree") is not None]
            if v["kind"] == "note" and degrees:
                label = "+".join(degrees) + ("≋" if v.get("is_roll") else "")
            if v.get("is_accent"):
                label += ">"
            self.canvas.create_text(x1 + 8, y + 14, text=label, anchor="w", fill="white", tags=(tag,))
            self.bind_entry(tag, entry["id"])
        self.canvas.configure(scrollregion=(0, 0, width, max(130, self.content_height)))
        if not meter:
            self.canvas.create_text(left, self.content_height - 5, anchor="w", text="拍號待填；目前僅以 4 拍顯示位置", fill="#bc6225")

    def instrument_names(self):
        instruments = self.doc.data["instruments"]
        recent = self.doc.data.get("recent_instrument_ids", [])
        return [i["name"] for i in sorted(instruments, key=lambda i: (
            recent.index(i["instrument_id"]) if i["instrument_id"] in recent else len(recent), i["name"]))]

    def filter_row_instruments(self, key, typed=False):
        control = self.row_controls[key]
        if typed:
            control["search"] = True
        text = control["variable"].get().strip().casefold()
        names = self.instrument_names()
        control["combo"].configure(values=[n for n in names if text in n.casefold()]
                                  if control["search"] and text else names)

    def draw_row_control(self, key, y, instrument):
        name = instrument["name"] if instrument else ""
        control = self.row_controls.get(key)
        if control is None:
            frame = ttk.Frame(self.canvas)
            variable = tk.StringVar(master=self, value=name)
            combo = ttk.Combobox(frame, textvariable=variable, width=12,
                                 postcommand=lambda k=key: self.filter_row_instruments(k))
            combo.pack(side="left", fill="x", expand=True)
            button = ttk.Button(frame, text="套用", width=4,
                command=lambda doc=self.doc, bar=self.bar, k=key, v=variable: self.on_map_row(doc, bar, k, v.get().strip()))
            button.pack(side="left", padx=(3, 0))
            control = {"frame": frame, "variable": variable, "combo": combo, "button": button,
                       "search": False, "mapped_name": name}
            self.row_controls[key] = control
            combo.bind("<KeyRelease>", lambda e, k=key: self.filter_row_instruments(k, typed=True)
                       if e.char or e.keysym in {"BackSpace", "Delete"} else None)
            combo.bind("<<ComboboxSelected>>", lambda e, c=control: c.update(search=False))
        elif control["mapped_name"] != name:
            control["variable"].set(name)
            control.update(mapped_name=name, search=False)
        self.filter_row_instruments(key)
        self.canvas.create_window(8, y + 26, anchor="nw", window=control["frame"],
                                  width=self.left_margin - 18, height=29)

    def bind_entry(self, tag, entry_id):
        def select(event):
            self.canvas.focus_set()
            self.on_select(entry_id)
            return "break"
        self.canvas.tag_bind(tag, "<Button-1>", select)

    def draw_delete(self, entry_id, x, y):
        if self.on_reject is None:
            return
        tag = "delete:" + entry_id
        self.canvas.create_rectangle(x - 10, y - 11, x + 10, y + 11, fill="#ffe1d9", outline="", tags=(tag,))
        self.canvas.create_text(x, y, text="×", fill="#aa352c", font=("Arial", 13, "bold"), tags=(tag,))
        self.canvas.tag_bind(tag, "<Button-1>", lambda event, eid=entry_id: (self.on_reject(eid), "break")[1])

    def add(self, event):
        if not self.enable_add or self.doc is None or self.bar is None or self.canvas.find_withtag("current"):
            return
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        row = next((i for i, (top, bottom) in enumerate(self.row_spans) if top <= y < top + self.row_height - 4), -1)
        width = max(450, self.canvas.winfo_width())
        if x < self.left_margin or not 0 <= row < len(self.rows):
            return
        onset = min(self.units - self.step, max(Fraction(0), round((x - self.left_margin) / (width - self.left_margin - 18) * float(self.units / self.step)) * self.step))
        key = self.rows[row]
        ins = next((ins for ins in self.doc.data["instruments"] if ins["name"] == key), None)
        ids = {e["value"].get("instrument_id") for e in self.doc.entries(self.bar) if e["status"] != "rejected" and e["value"]["staff_label"] == key and e["value"].get("instrument_id")}
        instrument_id = ins["instrument_id"] if ins else (next(iter(ids)) if len(ids) == 1 else None)
        self.on_add(instrument_id, {"n": onset.numerator, "d": onset.denominator})

    def playhead(self, progress):
        self.canvas.delete("playhead")
        width = max(450, self.canvas.winfo_width())
        x = self.left_margin + max(0, min(1, progress)) * (width - self.left_margin - 18)
        self.canvas.create_line(x, 30, x, max(80, self.content_height), fill="#c44145", width=2, tags=("playhead",))
