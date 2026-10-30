"""圖片操作：藍框是主來源，紫框是參考，橘線只標示小節分界。"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from copy import deepcopy

from PIL import Image, ImageOps, ImageTk
from .geometry import region_point, region_unit


class ScoreCanvas(ttk.Frame):
    def __init__(self, master, on_box, on_boundaries, on_quad=None):
        super().__init__(master)
        self.on_box = on_box
        self.on_boundaries = on_boundaries
        self.on_quad = on_quad
        self.font_family = "Microsoft JhengHei UI"
        self.canvas = tk.Canvas(self, bg="#e8edf2", highlightthickness=0)
        vertical = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        horizontal = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(xscrollcommand=horizontal.set, yscrollcommand=vertical.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.image = None
        self.photo = None
        self.region = None
        self.evidence = None
        self.mode = "select"
        self.scale = 1.0
        self.origin = (0, 0)
        self.gesture = None
        self.canvas.bind("<Configure>", lambda event: self.redraw())
        self.canvas.bind("<ButtonPress-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.motion)
        self.canvas.bind("<ButtonRelease-1>", self.release)
        self.canvas.bind("<MouseWheel>", self.wheel)
        self.canvas.bind("<Control-MouseWheel>", self.zoom_wheel)

    def load(self, path):
        with Image.open(path) as source:
            self.image = ImageOps.exif_transpose(source).convert("RGB")
        self.scale = 1.0
        self.canvas.yview_moveto(0)
        self.redraw()

    def set_region(self, region):
        self.region = region
        self.evidence = None
        self.redraw()

    def wheel(self, event):
        units = -int(event.delta / 120) * 3 if abs(event.delta) >= 120 else (-1 if event.delta > 0 else 1)
        self.canvas.yview_scroll(units, "units")
        return "break"

    def zoom_wheel(self, event):
        self.zoom(1.2 if event.delta > 0 else 1 / 1.2)
        return "break"

    def zoom(self, factor):
        self.scale = max(.5, min(5, self.scale * factor))
        self.redraw()

    def point(self, event):
        if not self.image or not self.photo:
            return None
        x = (self.canvas.canvasx(event.x) - self.origin[0]) / self.photo.width()
        y = (self.canvas.canvasy(event.y) - self.origin[1]) / self.photo.height()
        return max(0, min(1, x)), max(0, min(1, y))

    def xy(self, x, y):
        return self.origin[0] + x * self.photo.width(), self.origin[1] + y * self.photo.height()

    def rectangle(self, box, **options):
        first = self.xy(box["x"], box["y"])
        last = self.xy(box["x"] + box["width"], box["y"] + box["height"])
        return self.canvas.create_rectangle(*first, *last, **options)

    def redraw(self):
        self.canvas.delete("all")
        if self.image is None:
            self.canvas.create_text(30, 30, anchor="nw", text="先建立專案，再匯入整份 JPG 樂譜", fill="#52647a", font=(self.font_family, 14))
            return
        fit = max(200, self.canvas.winfo_width() - 35) / self.image.width
        width = max(1, round(self.image.width * fit * self.scale))
        height = max(1, round(self.image.height * fit * self.scale))
        self.photo = ImageTk.PhotoImage(self.image.resize((width, height), Image.Resampling.LANCZOS))
        self.origin = (12, 12)
        self.canvas.create_image(*self.origin, anchor="nw", image=self.photo)
        self.canvas.configure(scrollregion=(0, 0, width + 25, height + 25))
        if self.region:
            if self.region.get("quad"):
                points = [v for p in self.region["quad"] for v in self.xy(*p)]
                self.canvas.create_polygon(*points, outline="#146bd1", fill="", width=3)
                if self.mode == "quad":
                    for i,p in enumerate(self.region["quad"]):
                        x,y = self.xy(*p)
                        self.canvas.create_oval(x-7,y-7,x+7,y+7,fill="#146bd1",outline="white",width=2)
                        self.canvas.create_text(x+10,y,anchor="w",text=("左上","右上","右下","左下")[i],fill="#146bd1")
            else:
                self.rectangle(self.region["bbox"], outline="#146bd1", width=3)
            if self.region.get("context_bbox"):
                self.rectangle(self.region["context_bbox"], outline="#9159ba", width=3, dash=(6, 3))
            box = self.region["bbox"]
            for i, value in enumerate(self.region["boundaries"]):
                x1, y1 = self.xy(*region_point(self.region,value,0))
                x2, y2 = self.xy(*region_point(self.region,value,1))
                self.canvas.create_line(x1, y1, x2, y2, fill="#d47716", width=2)
                self.canvas.create_text(x1 + 4, y1 - 3, anchor="sw", text="結束" if i == self.region["bar_count"] else str(self.region["start_bar"] + i), fill="#ad5100", font=(self.font_family, 10, "bold"))
        if self.evidence:
            self.rectangle(self.evidence, outline="#21966d", width=3, dash=(5, 2))

    def press(self, event):
        point = self.point(event)
        if point is None:
            return
        if self.mode in {"main", "context"}:
            self.gesture = ("box", point)
        elif self.mode == "quad" and self.region and self.region.get("quad"):
            corners = self.region["quad"]
            index = min(range(4),key=lambda i: (self.xy(*corners[i])[0]-self.xy(*point)[0])**2+(self.xy(*corners[i])[1]-self.xy(*point)[1])**2)
            x,y = self.xy(*corners[index]); px,py = self.xy(*point)
            if (x-px)**2+(y-py)**2 < 28**2:
                self.gesture = ("quad",index)
        elif self.mode == "select" and self.region:
            box = self.region["bbox"]
            relative, vertical = region_unit(self.region,*point)
            if 0 <= vertical <= 1:
                index = min(range(len(self.region["boundaries"])), key=lambda i: abs(self.region["boundaries"][i] - relative))
                pixels = abs(self.region["boundaries"][index] - relative) * box["width"] * self.photo.width()
                if pixels <= 15:
                    self.gesture = ("boundary", index)

    def motion(self, event):
        if not self.gesture:
            return
        point = self.point(event)
        self.canvas.delete("gesture")
        if self.gesture[0] == "box":
            start = self.gesture[1]
            self.canvas.create_rectangle(*self.xy(*start), *self.xy(*point), outline="#6a737f", dash=(4, 3), width=2, tags="gesture")
        elif self.gesture[0] == "quad":
            corners = deepcopy(self.region["quad"])
            corners[self.gesture[1]] = list(point)
            self.canvas.create_polygon(*[v for p in corners for v in self.xy(*p)],outline="#146bd1",fill="",width=3,tags="gesture")
        else:
            box = self.region["bbox"]
            u = max(0,min(1,region_unit(self.region,*point)[0]))
            self.canvas.create_line(*self.xy(*region_point(self.region,u,0)),*self.xy(*region_point(self.region,u,1)),fill="#d47716",width=4,tags="gesture")

    def release(self, event):
        if not self.gesture:
            return
        gesture, self.gesture = self.gesture, None
        self.canvas.delete("gesture")
        point = self.point(event)
        if gesture[0] == "box":
            start = gesture[1]
            left, right = sorted((start[0], point[0]))
            top, bottom = sorted((start[1], point[1]))
            if right - left > .005 and bottom - top > .005:
                self.on_box(self.mode, {"x": left, "y": top, "width": right - left, "height": bottom - top})
        elif gesture[0] == "quad":
            corners = deepcopy(self.region["quad"])
            corners[gesture[1]] = list(point)
            if self.on_quad:
                self.on_quad(corners)
            self.redraw()
        else:
            box = self.region["bbox"]
            boundaries = list(self.region["boundaries"])
            index = gesture[1]
            lower = boundaries[index - 1] + .005 if index else 0
            upper = boundaries[index + 1] - .005 if index < len(boundaries) - 1 else 1
            boundaries[index] = max(lower, min(upper, region_unit(self.region,*point)[0]))
            self.on_boundaries(boundaries)
