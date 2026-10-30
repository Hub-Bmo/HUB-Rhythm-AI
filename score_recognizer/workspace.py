"""可攜式程式 A 工作區；不產生正式 Track / Take / 訓練事件。"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
import time
from uuid import uuid4
import zipfile

from guoyue_annotator.photo_assets import import_image_asset, asset_file_path, safe_project_path
from guoyue_annotator.instrument_library import create_default_library, resolve_instrument
from .geometry import validate_quad, quad_bounds, source_box, region_point
from .contract import (FORMAT, SCHEMA_VERSION, PPQ, NOTATIONS, ContractError,
                       bbox, image_bbox, meter_length, ticks, validate_result)

MANIFEST = "recognition-package.json"


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def identity(prefix):
    return f"{prefix}-{uuid4().hex}"


def natural_key(value):
    return [(0, int(part)) if part.isdigit() else (1, part.casefold())
            for part in re.split(r"(\d+)", str(value)) if part]


def atomic_bytes(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    written = False
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # OneDrive／Windows 掃描可能短暫鎖住 manifest；仍採原子替換，不先刪原檔。
        for attempt in range(6):
            try:
                os.replace(name, path)
                written = True
                break
            except PermissionError:
                if attempt == 5:
                    raise OSError(f"檔案仍被占用，未能保存。最新資料保留於：{name}")
                time.sleep(.05 * 2 ** attempt)
    finally:
        if written and Path(name).exists():
            Path(name).unlink()


def atomic_json(path, data):
    payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    atomic_bytes(path, payload.encode("utf-8"))


def new_package(title="未命名樂譜"):
    return {"format": FORMAT, "schema_version": SCHEMA_VERSION,
            "package_id": identity("PKG"), "revision": 1, "ppq": PPQ,
            "title": title, "created_at": now(), "updated_at": now(),
            "assets": [], "regions": [], "recognition_runs": [],
            "instrument_library_snapshot": create_default_library(),
            "score_context": "", "navigation_reviews": [],
            "region_defaults": {"bar_count": 4},
            "review_policy": "所有辨識結果為待核對資料；人工核對由程式 B 保存"}


def validate_region(region):
    bbox(region["bbox"])
    if region.get("quad"):
        validate_quad(region["quad"])
        if any(abs(quad_bounds(region["quad"])[k]-region["bbox"][k]) > 1e-7 for k in region["bbox"]):
            raise ContractError("斜框與外框座標不一致")
    if region.get("context_bbox") is not None:
        bbox(region["context_bbox"])
    for name in ("start_bar", "bar_count"):
        if type(region.get(name)) is not int or region[name] < 1:
            raise ContractError("起始小節與小節數必須是正整數")
    if region["bar_count"] > 32:
        raise ContractError("單一範圍最多 32 小節，請分成數個範圍")
    bounds = region.get("boundaries", [])
    if len(bounds) != region["bar_count"] + 1:
        raise ContractError("分界線數量應為小節數加一（包含首尾邊界）")
    if any(not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 <= v <= 1 for v in bounds):
        raise ContractError("小節分界必須在辨識區域內")
    if any(right - left < .002 for left, right in zip(bounds, bounds[1:])):
        raise ContractError("小節分界不可交叉或重疊")
    if region["notation"] not in NOTATIONS:
        raise ContractError("譜種無效")
    meter_length(region.get("time_signature"))


class Workspace:
    def __init__(self, root, package):
        self.root = Path(root).resolve()
        self.package = package

    @classmethod
    def create(cls, path, title="未命名樂譜"):
        root = Path(path)
        root.mkdir(parents=True, exist_ok=True)
        if (root / MANIFEST).exists():
            raise ContractError("這個資料夾已經有辨識專案，請使用開啟專案")
        workspace = cls(root, new_package(title))
        workspace.save()
        return workspace

    @classmethod
    def load(cls, path):
        source = Path(path)
        if source.is_dir():
            source = source / MANIFEST
        data = json.loads(source.read_text(encoding="utf-8"))
        if data.get("format") != FORMAT or data.get("schema_version") != SCHEMA_VERSION:
            raise ContractError("不是新版辨識專案；舊版專案請繼續以舊程式開啟")
        if data.get("ppq") != PPQ:
            raise ContractError("專案時間基準必須是 960 PPQ")
        workspace = cls(source.parent, data)
        workspace.default_region_bar_count()
        ids = [asset["asset_id"] for asset in data["assets"]]
        if len(ids) != len(set(ids)):
            raise ContractError("照片識別碼重複")
        for asset in data["assets"]:
            asset_file_path(workspace.root, asset, must_exist=True)
        for region in data["regions"]:
            validate_region(region)
            if region["asset_id"] not in ids:
                raise ContractError("辨識範圍缺少來源照片")
        # 關閉或當機時未完成的請求只標示中斷；不會自動重新收費。
        interrupted = False
        for run in data["recognition_runs"]:
            if run["status"] == "running":
                run.update(status="interrupted", error="上次辨識中斷，請手動重試")
                interrupted = True
        if interrupted:
            workspace.save()
        return workspace

    def save(self):
        self.package["updated_at"] = now()
        path = self.root / MANIFEST
        if path.exists():
            # 備份不取代原子寫入；保留最近一次成功儲存。
            atomic_bytes(self.root / f"{MANIFEST}.bak", path.read_bytes())
        atomic_json(path, self.package)

    def changed(self):
        self.package["revision"] += 1

    def asset(self, asset_id):
        return next(item for item in self.package["assets"] if item["asset_id"] == asset_id)

    def region(self, region_id):
        return next(item for item in self.package["regions"] if item["region_id"] == region_id)

    def import_images(self, paths):
        added, duplicates, errors = [], [], []
        for path in sorted(paths, key=lambda p: natural_key(Path(p).name)):
            try:
                asset = import_image_asset(self.root, path)
                if any(item["asset_id"] == asset["asset_id"] for item in self.package["assets"]):
                    duplicates.append(Path(path).name)
                    continue
                asset["page_sequence"] = len(self.package["assets"]) + 1
                digits = re.findall(r"\d+", Path(path).stem)
                asset["printed_page_number"] = str(int(digits[-1])) if digits else ""
                asset["page_number_source"] = "filename_unconfirmed"
                self.package["assets"].append(asset)
                added.append(asset)
            except Exception as error:
                errors.append(f"{Path(path).name}：{error}")
        if added:
            self.changed()
            self.save()
        return added, duplicates, errors

    def move_page(self, asset_id, delta):
        assets = self.package["assets"]
        index = next(i for i, asset in enumerate(assets) if asset["asset_id"] == asset_id)
        other = index + delta
        if 0 <= other < len(assets):
            assets[index], assets[other] = assets[other], assets[index]
            for i, asset in enumerate(assets, 1):
                asset["page_sequence"] = i
            self.changed()
            self.save()

    def default_region_bar_count(self):
        defaults = self.package.get("region_defaults", {})
        if not isinstance(defaults, dict):
            raise ContractError("新增範圍的預設設定格式有誤")
        count = defaults.get("bar_count", 4)
        if type(count) is not int or not 1 <= count <= 32:
            raise ContractError("新範圍預設小節數請填 1–32 的整數")
        return count

    def set_default_region_bar_count(self, count):
        if type(count) is not int or not 1 <= count <= 32:
            raise ContractError("新範圍預設小節數請填 1–32 的整數")
        if count != self.default_region_bar_count():
            self.package.setdefault("region_defaults", {})["bar_count"] = count
            self.changed()
            self.save()

    def new_region(self, asset_id, box):
        count = self.default_region_bar_count()
        next_bar = max((item["start_bar"] + item["bar_count"] for item in self.package["regions"]), default=1)
        region = {"region_id": identity("REG"), "asset_id": asset_id, "bbox": dict(box),
                  "context_bbox": None, "start_bar": next_bar, "bar_count": count,
                  "boundaries": [round(.12 + .88 * i / count, 12) for i in range(count + 1)],
                  "notation": "auto", "time_signature": "", "key_signature": "",
                  "context_notes": "", "expected_instruments": [], "confirmed": False}
        validate_region(region)
        self.package["regions"].append(region)
        self.changed()
        self.save()
        return region

    def update_region(self, region_id, **values):
        region = self.region(region_id)
        replacement = deepcopy(region)
        replacement.update(values)
        validate_region(replacement)
        for other in self.package["regions"]:
            if other["region_id"] == region_id:
                continue
            if max(replacement["start_bar"], other["start_bar"]) < min(
                replacement["start_bar"] + replacement["bar_count"], other["start_bar"] + other["bar_count"]
            ):
                raise ContractError("這批小節已登記於另一個範圍，請檢查起始小節號")
        if region != replacement:
            region.clear()
            region.update(replacement)
            self.changed()
            self.save()
        return region

    def fingerprint(self, region):
        source = {key: value for key, value in region.items() if key != "confirmed"}
        source["asset_sha256"] = self.asset(region["asset_id"])["sha256"]
        source["score_context"] = self.package["score_context"]
        return sha256(json.dumps(source, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    def remove_region(self, region_id):
        self.region(region_id)
        self.package["regions"] = [item for item in self.package["regions"] if item["region_id"] != region_id]
        # 歷史 runs 保留，原始辨識不因刪除範圍而遺失。
        self.changed()
        self.save()

    def split_region_by_measure(self, region_id, *, replace_existing=False):
        """依分界建立逐小節草稿；明確取代時封存原範圍與所有結果。"""
        region = self.region(region_id)
        if region["bar_count"] < 2:
            raise ContractError("這個範圍已經只有一個小節")
        history = [r for r in self.package["recognition_runs"] if r["region_id"] == region_id]
        if any(r["status"] == "running" for r in history):
            raise ContractError("這個範圍仍在辨識，請等目前回應保存後再拆分")
        if not replace_existing and any(r["status"] in {"succeeded", "empty"} for r in history):
            raise ContractError("這個範圍已有結果；請使用介面的『拆成逐小節（舊結果留存）』建立新草稿")
        children = []
        box = region["bbox"]
        for index in range(region["bar_count"]):
            left, right = region["boundaries"][index:index + 2]
            crop_left = 0 if index == 0 else left
            child = deepcopy(region)
            child.update(region_id=identity("REG"), start_bar=region["start_bar"] + index, bar_count=1,
                bbox={"x": box["x"] + box["width"] * crop_left, "y": box["y"],
                      "width": box["width"] * (right - crop_left), "height": box["height"]},
                boundaries=[(left - crop_left) / (right - crop_left), 1.0],
                context_bbox=deepcopy(region.get("context_bbox") or box), confirmed=False,
                split_from_region_id=region_id)
            if region.get("quad"):
                child["quad"] = [region_point(region,u,v) for u,v in ((crop_left,0),(right,0),(right,1),(crop_left,1))]
                child["bbox"] = quad_bounds(child["quad"])
            child["context_notes"] = (region["context_notes"] + f"\n僅辨識小節 {child['start_bar']}；參考圖提供原範圍的聲部名稱、拍號及變拍資訊，不抄入其他小節音符。").strip()
            validate_region(child)
            children.append(child)
        before = self.package
        self.package = deepcopy(before)
        self.package["regions"] = [r for r in self.package["regions"] if r["region_id"] != region_id] + children
        self.package["regions"].sort(key=lambda r: r["start_bar"])
        self.package.setdefault("retired_regions", []).append({
            "region": deepcopy(region), "retired_at": now(), "reason": "split_by_measure",
            "replacement_region_ids": [child["region_id"] for child in children]})
        try:
            self.changed()
            self.save()
        except Exception:
            self.package = before
            raise
        return children

    def latest_run(self, region):
        fingerprint = self.fingerprint(region)
        return next((run for run in reversed(self.package["recognition_runs"])
                     if run["region_id"] == region["region_id"] and run["fingerprint"] == fingerprint), None)

    def page_order_issues(self):
        """只檢查已登記範圍的頁序，不要求尚未辨識的頁面有小節。"""
        issues = []
        previous_end, previous_page = None, None
        for asset in sorted(self.package["assets"], key=lambda a: a["page_sequence"]):
            regions = [r for r in self.package["regions"] if r["asset_id"] == asset["asset_id"]]
            if not regions:
                continue
            first = min(r["start_bar"] for r in regions)
            last = max(r["start_bar"] + r["bar_count"] - 1 for r in regions)
            page = asset["printed_page_number"]
            if previous_end is not None and first <= previous_end:
                issues.append(f"第 {page} 頁從小節 {first} 開始，卻排在第 {previous_page} 頁的小節 {previous_end} 之後")
            previous_end, previous_page = last, page
        return issues

    def confirm_page_split_regions(self, asset_id):
        self.asset(asset_id)
        regions = [r for r in self.package["regions"] if r["asset_id"] == asset_id
                   and r["bar_count"] == 1 and r.get("split_from_region_id")]
        if not regions:
            raise ContractError("本頁沒有拆分後的小節範圍，請先選取範圍並按「拆成逐小節」。")
        # 先檢查全部，避免某一筆無效時只確認了前面幾筆。
        for region in regions:
            validate_region(region)
        pending = [r for r in regions if not r["confirmed"]]
        if pending:
            revision, updated = self.package["revision"], self.package["updated_at"]
            try:
                for region in pending:
                    region["confirmed"] = True
                self.changed()
                self.save()
            except Exception:
                for region in pending:
                    region["confirmed"] = False
                self.package.update(revision=revision, updated_at=updated)
                raise
        return len(regions), len(pending)

    def status(self, region):
        run = self.latest_run(region)
        if run:
            return run["status"]
        if any(run["region_id"] == region["region_id"] for run in self.package["recognition_runs"]):
            return "stale"
        return "pending" if region["confirmed"] else "draft"

    def begin_run(self, region_id, model):
        region = self.region(region_id)
        validate_region(region)
        if not region["confirmed"]:
            raise ContractError("請先確認辨識範圍與小節分界")
        run = {"run_id": identity("RUN"), "region_id": region_id,
               "asset_id": region["asset_id"], "fingerprint": self.fingerprint(region),
               "source_snapshot": deepcopy(region), "model": model,
               "status": "running", "created_at": now(), "error": None,
               "raw_output": None, "result": None, "usage": None, "observations": []}
        self.package["recognition_runs"].append(run)
        self.save()
        # 資產錯誤也留下單一範圍的失敗紀錄，不只顯示整批失敗數。
        try:
            path = asset_file_path(self.root, self.asset(region["asset_id"]), must_exist=True)
            if sha256(path.read_bytes()).hexdigest() != self.asset(region["asset_id"])["sha256"]:
                raise ContractError("來源照片內容已變更，請重新匯入")
        except Exception as error:
            self.fail_run(run, "來源照片缺失或內容已變更，請重新匯入")
            raise ContractError(run["error"]) from error
        return run

    def finish_run(self, run, response):
        run["raw_output"] = response.get("raw_output")
        run["usage"] = response.get("usage")
        run["response_id"] = response.get("response_id")
        run["finished_at"] = now()
        try:
            result = validate_result(response["result"], run["source_snapshot"])
            run["result"] = result
            run["observations"] = normalize_observations(run, self.package["instrument_library_snapshot"])
            reported = {part["instrument_text"] for measure in result["measures"] for part in measure["parts"]}
            missing = [name for name in run["source_snapshot"]["expected_instruments"] if name not in reported]
            run["coverage_warnings"] = [f"預期樂器「{name}」未以相同名稱回報，請檢查有無漏辨或別名" for name in missing]
            run["status"] = "succeeded" if any(m["parts"] for m in result["measures"]) else "empty"
        except (ContractError, KeyError, TypeError, ValueError) as error:
            run.update(status="failed", error=str(error))
        self.save()

    def fail_run(self, run, message, raw_output=None):
        run.update(status="failed", error=message, raw_output=raw_output, finished_at=now())
        self.save()

    def report(self):
        counts = {}
        observations = []
        for region in self.package["regions"]:
            status = self.status(region)
            counts[status] = counts.get(status, 0) + 1
            run = self.latest_run(region)
            if run and run["status"] in {"succeeded", "empty"}:
                observations.extend(run["observations"])
        registered_assets = {region["asset_id"] for region in self.package["regions"]}
        uncovered = [asset["original_filename"] for asset in self.package["assets"] if asset["asset_id"] not in registered_assets]
        ranges = sorted((region["start_bar"], region["start_bar"] + region["bar_count"] - 1) for region in self.package["regions"])
        gaps = [[previous[1] + 1, current[0] - 1] for previous, current in zip(ranges, ranges[1:]) if current[0] > previous[1] + 1]
        return {"pages": len(self.package["assets"]), "regions": len(self.package["regions"]),
                "pages_without_regions": uncovered, "unregistered_bar_ranges": gaps,
                "region_status": counts, "observations": len(observations),
                "pending_observations": sum(bool(item["uncertainties"]) for item in observations),
                "full_rest_measures": sum(item["kind"] == "full_rest" for item in observations),
                "note_candidates": sum(item["kind"] == "note" for item in observations),
                "notice": "這是待核對辨識套件；不等同已確認訓練資料"}

    def export(self, path):
        self.save()
        path = Path(path).resolve()
        if path == (self.root / MANIFEST).resolve() or path.is_relative_to(self.root / "assets"):
            raise ContractError("匯出位置不可覆蓋工作專案或來源照片")
        package = deepcopy(self.package)
        package["export_summary"] = self.report()
        package["exported_at"] = now()
        package["active_run_ids"] = [run["run_id"] for region in package["regions"]
                                     if (run := self.latest_run(region)) and run["status"] in {"succeeded", "empty"}]
        files = []
        for asset in package["assets"]:
            source = asset_file_path(self.root, asset, must_exist=True)
            if sha256(source.read_bytes()).hexdigest() != asset["sha256"]:
                raise ContractError(f"照片完整性檢查失敗：{asset['original_filename']}")
            files.append((source, asset["relative_path"]))
        artifact_files = {}
        for run in package["recognition_runs"]:
            for key,value in run.get("artifacts",{}).items():
                if key == "directory":
                    continue
                for relative in value if isinstance(value,list) else [value]:
                    if isinstance(relative,str):
                        source = safe_project_path(self.root,relative)
                        if source.is_file():
                            artifact_files[relative] = source
        files.extend((source,relative) for relative,source in artifact_files.items())
        fd, temporary = tempfile.mkstemp(prefix=".recognition-export-", suffix=".zip", dir=path.parent)
        os.close(fd)
        try:
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(MANIFEST, json.dumps(package, ensure_ascii=False, indent=2, allow_nan=False))
                for source, relative in files:
                    archive.write(source, relative)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return package["export_summary"]


def normalize_observations(run, library):
    region = run["source_snapshot"]
    result = run["result"]
    observations = []
    all_labels = {part["staff_label"] for measure in result["measures"] for part in measure["parts"]}
    for measure in result["measures"]:
        bar = region["start_bar"] + measure["index"]
        labels = {part["staff_label"] for part in measure["parts"]}
        for missing in sorted(all_labels - labels):
            observations.append({"observation_id": f"{run['run_id']}:{len(observations)}",
                                 "kind": "unreadable", "bar": bar, "staff_label": missing,
                                 "instrument_id": None, "instrument_text": None,
                                 "source_asset_id": run["asset_id"], "source_bbox": region["bbox"],
                                 "uncertainties": ["這個聲部在其他小節出現，本小節未回報；不可視為休止"],
                                 "review_status": "pending"})
        for part in measure["parts"]:
            common = {"bar": bar, "staff_label": part["staff_label"],
                      "time_signature": measure["time_signature"] or region["time_signature"] or None,
                      "key_signature": measure["key_signature"] or region["key_signature"] or None,
                      "source_asset_id": run["asset_id"], "review_status": "pending"}
            entries = part["events"] or [{"kind": "full_rest" if part["status"] == "full_rest" else "unreadable", "bbox": part["bbox"]}]
            for event in entries:
                item = deepcopy(common)
                item.update(deepcopy(event))
                item["observation_id"] = f"{run['run_id']}:{len(observations)}"
                name = event.get("instrument_text") or part["instrument_text"]
                instrument = resolve_instrument(library, name) if name else "unknown"
                item["instrument_text"] = name
                item["instrument_id"] = None if instrument == "unknown" else instrument
                item["technique_id"] = None  # 意義與音效由程式 B 確認，不把數字當成擊法。
                item["source_bbox"] = source_box(region, event["bbox"]) if region.get("quad") else image_bbox(event["bbox"], region["bbox"])
                item["onset_tick_in_bar"] = ticks(event.get("onset"))
                item["duration_ticks"] = ticks(event.get("duration"))
                issues = list(measure["uncertainties"]) + list(part["uncertainties"]) + list(event.get("uncertainties", []))
                if not name or instrument == "unknown":
                    issues.append("樂器名稱需建立或對應（原始名稱及節奏均已保留）")
                if event["kind"] == "note":
                    if event.get("onset") is None or event.get("duration") is None:
                        issues.append("敲擊位置或時值待確認")
                    if not event.get("pitches") and not event.get("technique_text"):
                        issues.append("敲擊符號含義／擊法待確認")
                    elif event.get("technique_text"):
                        issues.append("辨識的擊法名稱需人工確認並對應固定編號")
                    for pitch in event.get("pitches", []):
                        if pitch["midi"] is None:
                            issues.append("完整實際音高尚待確認；已保留譜面音高資料")
                    if any(event.get(flag) is None for flag in ("is_roll", "is_accent", "is_ornament")):
                        issues.append("有演奏記號尚無法判定")
                    if event.get("tie") == "unknown":
                        issues.append("延音線待確認")
                if event["kind"] == "unreadable":
                    issues.append("這個位置未辨識清楚，需人工補填")
                item["uncertainties"] = list(dict.fromkeys(issues))
                observations.append(item)
    return observations
