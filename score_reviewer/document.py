"""獨立校對文件。A 的專案一律唯讀，所有人工決定保存於 B。"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from uuid import UUID, uuid5
import zipfile

from guoyue_annotator.photo_assets import asset_file_path, safe_project_path
from .navigation import expand_playback_order, marker_range, evidence_resolved
from .segments import expand_ranges, segment_bars, segment_statistics
from .review_workflow import apply_name_rules, training_mask
from score_recognizer.contract import FORMAT as SOURCE_FORMAT, PPQ, ContractError, fraction, meter_length
from score_recognizer.workspace import Workspace, MANIFEST as SOURCE_MANIFEST, atomic_bytes, atomic_json, identity, now

MANIFEST = "review-project.json"
FORMAT = "musicai-score-review"
NS = UUID("6807b147-822a-49cf-9ed2-fad51cf95c75")
SOUND_PROFILES = {"drum": "鼓", "timpani": "定音鼓", "cymbal": "鈸", "large_gong": "大鑼",
                  "small_gong": "小鑼", "bell": "鈴／鐘", "metronome": "節拍提示音"}


def score_sound_key(value):
    """只合併同名聲部的空白與分隔符，不猜測不同樂器名稱的同義關係。"""
    name = value.get("instrument_text") or value.get("staff_label") or "未辨明樂器"
    return "／".join(p.strip() for p in re.split(r"[／/、，,\r\n]+", name) if p.strip())


def stable_id(prefix, text):
    return f"{prefix}_{uuid5(NS, text.strip().casefold()).hex}"


def invalidate_navigation(data, start, end):
    data["navigation_confirmed"] = False
    data["navigation_scopes"] = [s for s in data.get("navigation_scopes", [])
                                 if max(start, s["start_bar"]) > min(end, s["end_bar"])]


def merge_scopes(scopes):
    result = []
    for scope in sorted(scopes, key=lambda s: s["start_bar"]):
        if result and scope["start_bar"] <= result[-1]["end_bar"] + 1:
            result[-1]["end_bar"] = max(result[-1]["end_bar"], scope["end_bar"])
        else:
            result.append(dict(scope))
    return result


def rat(value):
    """編輯介面以四分音符為一拍，支援 1/3 等精確分數。"""
    from fractions import Fraction
    try:
        parsed = Fraction(str(value).strip())
        if parsed < 0:
            raise ValueError
        return {"n": parsed.numerator, "d": parsed.denominator}
    except (ValueError, ZeroDivisionError, OverflowError) as error:
        raise ContractError("時間請填非負數或分數，例如 0、1/2、3/2") from error


def display_time(value):
    return "" if value is None else str(fraction(value))


def midi_from_name(text):
    match = re.fullmatch(r"([A-Ga-g])([#b]?)(-?\d+)", text.strip())
    if not match:
        raise ContractError("音名請含八度，例如 G2、C#4、Bb3；八度採 C4 = MIDI 60")
    step, accidental, octave = match.groups()
    value = (int(octave) + 1) * 12 + {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}[step.upper()]
    value += {"": 0, "#": 1, "b": -1}[accidental]
    if not 0 <= value <= 127:
        raise ContractError("音高超出 MIDI 0–127")
    return value


def pitch_name(value):
    return "" if value is None else f"{('C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B')[value % 12]}{value // 12 - 1}"


def pitched_values(text, originals=()):
    tokens = text.replace("，", ",").split(",") if text.strip() else []
    result = []
    for index, token in enumerate(tokens):
        if token.strip() == "?":
            if index >= len(originals):
                raise ContractError("新的音高請填音名或 MIDI 數字")
            result.append(deepcopy(originals[index]))
            continue
        midi = int(token) if token.strip().isdigit() else midi_from_name(token)
        if not 0 <= midi <= 127:
            raise ContractError("音高超出 MIDI 0–127")
        value = deepcopy(originals[index]) if index < len(originals) else {
            "degree": None, "octave_shift": None, "accidental": None, "step": None,
            "octave": None, "midi": None, "clef": None, "written_text": token.strip()}
        value.update(midi=midi, step=pitch_name(midi).rstrip("-0123456789"), octave=midi // 12 - 1)
        result.append(value)
    return result


def read_source(path):
    """讀取活躍 manifest 或 A 匯出 zip，絕不使用會標記中斷的 Workspace.load。"""
    path = Path(path).resolve()
    if path.is_dir():
        path = path / SOURCE_MANIFEST
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            data = json.loads(archive.read(SOURCE_MANIFEST))
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("format") != SOURCE_FORMAT or data.get("schema_version") != "1.0" or data.get("ppq") != PPQ:
        raise ContractError("請選擇新版程式 A 的 recognition-package.json 或 .recognition.zip")
    return path, data


def source_bytes(path, asset):
    if path.suffix.lower() == ".zip":
        # 只讀 manifest 明確引用且通過路徑驗證的項目，不解壓任意檔案。
        safe_project_path(path.parent, asset["relative_path"])
        with zipfile.ZipFile(path) as archive:
            payload = archive.read(asset["relative_path"])
    else:
        payload = asset_file_path(path.parent, asset, must_exist=True).read_bytes()
    if sha256(payload).hexdigest() != asset["sha256"]:
        raise ContractError(f"來源圖片完整性檢查失敗：{asset['original_filename']}")
    return payload


def instrument_record(name, profile="drum", pitched=False):
    name = name.strip()
    if not name:
        raise ContractError("樂器名稱不可空白")
    if profile not in SOUND_PROFILES:
        raise ContractError("音效種類無效")
    ins_id = stable_id("ins", name)
    return {"instrument_id": ins_id, "name": name, "profile": profile, "pitched": bool(pitched),
            "strokes": [{"technique_id": stable_id("stk", ins_id + ":一般敲擊"), "name": "一般敲擊", "sample": None}]}


class ReviewDocument:
    def __init__(self, root, data):
        self.root, self.data = Path(root).resolve(), data
        maximum = max(map(int, data.get("bars", {})), default=0)
        data.setdefault("navigation_scopes", [{"start_bar": 1, "end_bar": maximum}] if data.get("navigation_confirmed") and maximum else [])
        data.setdefault("navigation_draft_scope", None)
        data.setdefault("score_sounds", {})
        data.setdefault("review_mode", "complete")
        data.setdefault("name_corrections", {})
        data.setdefault("exclusion_history", [])

    @classmethod
    def create(cls, root, title):
        root = Path(root).resolve()
        if root.exists():
            raise ContractError("已有同名資料夾，請選擇另一個校對專案名稱")
        root.mkdir(parents=True)
        data = {"format": FORMAT, "schema_version": "1.0", "review_id": identity("REVIEW"),
                "title": title, "ppq": PPQ, "created_at": now(), "updated_at": now(),
                "source_package_id": None, "source_path": None, "imports": [], "assets": [],
                "regions": {}, "bars": {}, "entries": [], "archived_entries": [], "navigation_candidates": [],
                "navigation": [], "navigation_draft": [], "navigation_confirmed": False, "segments": [], "instruments": [],
                "defaults": {"time_signature": None, "tempo_quarter": None, "tonic_midi": None}}
        document = cls(root, data)
        document.save()
        return document

    @classmethod
    def load(cls, path):
        path = Path(path)
        if path.is_dir():
            path /= MANIFEST
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("format") != FORMAT or data.get("schema_version") != "1.0" or data.get("ppq") != PPQ:
            raise ContractError("這不是新版校對專案，請選 review-project.json")
        doc = cls(path.parent, data)
        for asset in data["assets"]:
            asset_file_path(doc.root, asset, must_exist=True)
        for ins in data["instruments"]:
            for stroke in ins["strokes"]:
                if stroke.get("sample"):
                    safe_project_path(doc.root, stroke["sample"], must_exist=True)
        return doc

    def save(self):
        self.data["updated_at"] = now()
        path = self.root / MANIFEST
        if path.exists():
            atomic_bytes(self.root / (MANIFEST + ".bak"), path.read_bytes())
        atomic_json(path, self.data)

    def import_source(self, source):
        path, package = read_source(source)
        if self.data["source_package_id"] not in (None, package["package_id"]):
            raise ContractError("這是另一首樂譜的辨識專案，請另建校對專案")
        # 所有資產先驗證，再複製、再交易式替換資料；絕不回寫來源。
        updated = deepcopy(self.data)
        view = Workspace(path.parent, package)
        assets = {asset["asset_id"]: asset for asset in package["assets"]}
        ready = []
        current_regions = {r["region_id"] for r in package["regions"]}
        for region_id in set(updated["regions"]) - current_regions:
            old = updated["regions"][region_id]
            # 已成功範圍也可被 A 拆分取代：舊校對封存，不混入新小節候選。
            removed = [e for e in updated["entries"] if e["region_id"] == region_id]
            updated["archived_entries"].extend(removed)
            updated["entries"] = [e for e in updated["entries"] if e["region_id"] != region_id]
            if removed or old["status"] != "removed":
                invalidate_navigation(updated, old["start_bar"], old["start_bar"] + old["bar_count"] - 1)
            old["status"] = "removed"
        for region in package["regions"]:
            run = view.latest_run(region)
            status = view.status(region)
            old = updated["regions"].get(region["region_id"])
            if old and (old["run_id"] != (run or {}).get("run_id") or old["status"] != status):
                # 重新辨識先封存人工決定；新候選不能繼承核對狀態。
                removed = [e for e in updated["entries"] if e["region_id"] == region["region_id"]]
                updated["archived_entries"].extend(removed)
                updated["entries"] = [e for e in updated["entries"] if e["region_id"] != region["region_id"]]
                if removed or status in {"succeeded", "empty"}:
                    invalidate_navigation(updated, region["start_bar"], region["start_bar"] + region["bar_count"] - 1)
            updated["regions"][region["region_id"]] = {"run_id": (run or {}).get("run_id"), "status": status,
                                                        "start_bar": region["start_bar"], "bar_count": region["bar_count"]}
            measures = {m["index"]: m for m in (run or {}).get("result", {}).get("measures", [])} if (run or {}).get("result") else {}
            for index in range(region["bar_count"]):
                number = region["start_bar"] + index
                prior = updated["bars"].get(str(number), {})
                measure = measures.get(index, {})
                box = deepcopy(region["bbox"])
                left, right = region["boundaries"][index:index + 2]
                box["x"] += left * box["width"]
                box["width"] *= right - left
                updated["bars"][str(number)] = {
                    "number": number, "asset_id": region["asset_id"], "region_id": region["region_id"],
                    "region_bbox": deepcopy(region["bbox"]), "bar_bbox": box,
                    "page": assets[region["asset_id"]]["printed_page_number"],
                    "time_signature": prior.get("time_signature") or measure.get("time_signature") or region.get("time_signature") or None,
                    "tempo_quarter": prior.get("tempo_quarter") or measure.get("tempo_quarter"),
                    "key_signature": measure.get("key_signature") or region.get("key_signature") or None}
            if not run or status not in {"succeeded", "empty"}:
                continue
            ready.append((region, run))
        known_assets = {asset["asset_id"] for asset in updated["assets"]}
        payloads = []
        # 包含仍在跑的頁面原圖，稍後不用再依賴 A 的路徑即可看整首譜。
        for asset in package["assets"]:
            if asset["asset_id"] not in known_assets:
                payloads.append((asset, source_bytes(path, asset)))
                updated["assets"].append(deepcopy(asset))
        known_entries = {e["id"] for e in updated["entries"]}
        known_navigation = {m["candidate_id"] for m in updated["navigation_candidates"]}
        added = 0
        if not updated["instruments"]:
            for source_ins in package["instrument_library_snapshot"]["instruments"]:
                ins = instrument_record(source_ins["name"], source_ins.get("sound_profile", "drum"), source_ins["name"] == "定音鼓")
                ins["instrument_id"] = source_ins["instrument_id"]
                ins["strokes"][0]["technique_id"] = stable_id("stk", ins["instrument_id"] + ":一般敲擊")
                updated["instruments"].append(ins)
        for region, run in ready:
            for obs in run["observations"]:
                if obs["observation_id"] in known_entries:
                    continue
                value = deepcopy(obs)
                apply_name_rules(self, value, updated.get("name_corrections", {}))
                updated["entries"].append({"id": obs["observation_id"], "region_id": region["region_id"],
                    "run_id": run["run_id"], "original": deepcopy(obs), "value": value,
                    "status": "pending", "review_note": "", "acknowledged": False})
                known_entries.add(obs["observation_id"])
                added += 1
            for index, marker in enumerate(run["result"]["navigation"]):
                marker_id = f"{run['run_id']}:navigation:{index}"
                if marker_id not in known_navigation:
                    updated["navigation_candidates"].append({"candidate_id": marker_id, "region_id": region["region_id"],
                                                              "run_id": run["run_id"], "marker": deepcopy(marker)})
                    invalidate_navigation(updated, marker["start_bar"], marker.get("end_bar") or marker["start_bar"])
        updated.update(source_package_id=package["package_id"], source_path=str(path))
        maximum = max(map(int, updated["bars"]), default=0)
        updated["navigation_confirmed"] = bool(maximum and any(s["start_bar"] <= 1 and s["end_bar"] >= maximum for s in updated["navigation_scopes"]))
        updated["imports"].append({"revision": package["revision"], "read_at": now(), "new_observations": added})
        for asset, payload in payloads:
            atomic_bytes(safe_project_path(self.root, asset["relative_path"]), payload)
        before = self.data
        self.data = updated
        try:
            self.save()
        except Exception:
            self.data = before
            raise
        return {"added": added, "ready_regions": len(ready), "total_regions": len(package["regions"])}

    def entries(self, bar=None):
        return [e for e in self.data["entries"] if bar is None or e["value"]["bar"] == bar]

    def entry(self, entry_id):
        return next(e for e in self.data["entries"] if e["id"] == entry_id)

    def instrument(self, instrument_id):
        return next((i for i in self.data["instruments"] if i["instrument_id"] == instrument_id), None)

    def technique(self, instrument_id, technique_id):
        instrument = self.instrument(instrument_id)
        return next((s for s in (instrument or {}).get("strokes", []) if s["technique_id"] == technique_id), None)

    def score_sound_rows(self):
        """本譜實際候選的音色列，包含全休止；不把整個預設樂器庫列入。"""
        rows = {}
        for entry in self.entries():
            if entry["status"] == "rejected":
                continue
            value = entry["value"]
            key = score_sound_key(value)
            row = rows.setdefault(key, {"key": key, "name": key, "notes": 0, "rests": 0,
                                        "profiles": set(), "samples": set(), "unmapped": 0})
            row["notes" if value["kind"] == "note" else "rests"] += 1
            ins = self.instrument(value.get("instrument_id"))
            stroke = self.technique(value.get("instrument_id"), value.get("technique_id"))
            if ins:
                row["profiles"].add(ins["profile"])
            else:
                row["unmapped"] += 1
            if stroke and stroke.get("sample"):
                row["samples"].add(stroke["sample"])
        for row in rows.values():
            row["override"] = self.data["score_sounds"].get(row["key"])
        return list(rows.values())

    def set_score_sound(self, key, profile):
        if profile is not None and profile not in SOUND_PROFILES:
            raise ContractError("請選擇清單中的預設音色")
        if key not in {row["key"] for row in self.score_sound_rows()}:
            raise ContractError("本譜目前沒有這個樂器，請重新整理清單")
        if profile is None:
            self.data["score_sounds"].pop(key, None)
        else:
            self.data["score_sounds"][key] = profile
        self.save()

    def score_sound(self, value):
        profile = self.data["score_sounds"].get(score_sound_key(value))
        return profile if profile in SOUND_PROFILES else None

    def bar_setting(self, bar):
        value = self.data["bars"][str(bar)]
        defaults = self.data["defaults"]
        return value.get("time_signature") or defaults["time_signature"], value.get("tempo_quarter") or defaults["tempo_quarter"]

    def update_defaults(self, meter, tempo, tonic=""):
        if meter:
            meter_length(meter)
        speed = float(tempo) if str(tempo).strip() else None
        if speed is not None and (not math.isfinite(speed) or not 1 <= speed <= 600):
            raise ContractError("速度請填 1–600，單位為每分鐘四分音符數")
        self.data["defaults"] = {"time_signature": meter or None, "tempo_quarter": speed,
                                 "tonic_midi": midi_from_name(tonic) if tonic.strip() else None}
        # 變更拍號／速度會重新檢查已接受事件，但不抹掉人工的逐筆決定。
        self.save()

    def add_instrument(self, name, profile, pitched=False):
        if any(i["name"] == name.strip() for i in self.data["instruments"]):
            raise ContractError("已有同名樂器，請直接選用")
        ins = instrument_record(name, profile, pitched)
        self.data["instruments"].append(ins)
        self.save()
        return ins

    def add_technique(self, instrument_id, name):
        ins = self.instrument(instrument_id)
        if not ins or not name.strip():
            raise ContractError("請選樂器並輸入擊法名稱")
        if any(s["name"] == name.strip() for s in ins["strokes"]):
            raise ContractError("已有同名擊法")
        stroke = {"technique_id": stable_id("stk", instrument_id + ":" + name.strip()), "name": name.strip(), "sample": None}
        ins["strokes"].append(stroke)
        self.save()
        return stroke

    def map_name(self, raw_name, instrument_id, technique_id, raw_symbol=""):
        if not self.technique(instrument_id, technique_id):
            raise ContractError("請選擇有效的樂器與擊法")
        count = 0
        for entry in self.entries():
            v = entry["value"]
            if entry["status"] != "pending" or (v.get("instrument_text") or v.get("staff_label")) != raw_name:
                continue
            if raw_symbol and v.get("raw_symbol", "") != raw_symbol:
                continue
            v.update(instrument_id=instrument_id, technique_id=technique_id)
            entry.update(status="pending", acknowledged=False)
            count += 1
        self.save()
        return count

    def staff_mapping_targets(self, bar, staff, scope="bar"):
        if str(bar) not in self.data["bars"] or scope not in {"bar", "page", "score"}:
            raise ContractError("請選擇有效的小節與套用範圍")
        asset_id = self.data["bars"][str(bar)]["asset_id"]
        return [e for e in self.entries() if e["status"] != "rejected"
                and (e["value"].get("staff_label") or e["value"].get("instrument_text") or "未辨明聲部") == staff
                and (scope == "score" or (e["value"]["bar"] == bar if scope == "bar"
                     else self.data["bars"][str(e["value"]["bar"])]["asset_id"] == asset_id))]

    def map_staff_instrument(self, bar, staff, instrument_id, scope="bar"):
        """明確指定範圍內的整列樂器；聲部身分和原始辨識不變。"""
        instrument = self.instrument(instrument_id)
        if not instrument:
            raise ContractError("請從下拉選單選擇已有的樂器")
        targets = self.staff_mapping_targets(bar, staff, scope)
        if not targets:
            raise ContractError("這個範圍已沒有可套用的聲部資料")
        changes = []
        for entry in targets:
            value = entry["value"]
            technique_id = value.get("technique_id")
            if value.get("instrument_id") != instrument_id:
                old = self.technique(value.get("instrument_id"), technique_id)
                matching = next((s for s in instrument["strokes"] if old and s["name"] == old["name"]), None)
                technique_id = matching["technique_id"] if matching else None
            if (value.get("instrument_id"), value.get("instrument_text"), value.get("technique_id")) != (
                    instrument_id, instrument["name"], technique_id):
                changes.append((entry, technique_id))
        if changes:
            before = deepcopy(self.data)
            try:
                for entry, technique_id in changes:
                    entry["value"].update(instrument_id=instrument_id, instrument_text=instrument["name"], technique_id=technique_id)
                    note = entry.get("review_note") or ""
                    entry.update(status="pending", acknowledged=False,
                                 review_note=(note + "\n" if note else "") + f"整列樂器對應：{staff} → {instrument['name']}")
                recent = self.data.get("recent_instrument_ids", [])
                self.data["recent_instrument_ids"] = [instrument_id, *(i for i in recent if i != instrument_id)][:8]
                self.save()
            except Exception:
                self.data = before
                raise
        return {"mapped": len(changes), "matched": len(targets), "bars": len({e['value']['bar'] for e in targets})}

    def apply_basic_techniques(self):
        """依使用者指示，整譜補上未對應擊法；不更動其他演奏標註。"""
        targets, missing_instrument = [], 0
        for entry in self.entries():
            value = entry["value"]
            if entry["status"] == "rejected" or value["kind"] != "note":
                continue
            instrument = self.instrument(value.get("instrument_id"))
            if not instrument:
                missing_instrument += 1
            elif not self.technique(instrument["instrument_id"], value.get("technique_id")):
                targets.append((entry, instrument))
        if targets:
            before = deepcopy(self.data)
            try:
                for entry, instrument in targets:
                    stroke = next((s for s in instrument["strokes"] if s["name"] == "一般敲擊"), None)
                    if stroke is None:
                        stroke = {"technique_id": stable_id("stk", instrument["instrument_id"] + ":一般敲擊"),
                                  "name": "一般敲擊", "sample": None}
                        instrument["strokes"].append(stroke)
                    entry["value"]["technique_id"] = stroke["technique_id"]
                    note = entry.get("review_note") or ""
                    entry.update(status="pending", acknowledged=False,
                                 review_note=(note + "\n" if note else "") + "整譜套用基本擊法（一般敲擊）")
                self.save()
            except Exception:
                self.data = before
                raise
        return {"mapped": len(targets), "missing_instrument": missing_instrument}

    def set_sample(self, instrument_id, technique_id, path):
        import wave
        stroke = self.technique(instrument_id, technique_id)
        if not stroke:
            raise ContractError("請選擇擊法")
        path = Path(path)
        with wave.open(str(path), "rb") as audio:
            if audio.getsampwidth() != 2 or audio.getnchannels() not in (1, 2) or audio.getcomptype() != "NONE":
                raise ContractError("請使用 16-bit PCM、單聲道或雙聲道 WAV")
            if audio.getnframes() / audio.getframerate() > 15:
                raise ContractError("單次敲擊音效請使用 15 秒以內 WAV")
        payload = path.read_bytes()
        relative = f"assets/sounds/{sha256(payload).hexdigest()}.wav"
        atomic_bytes(safe_project_path(self.root, relative), payload)
        stroke["sample"] = relative
        self.save()

    def resolve_pitch(self, entries):
        tonic = self.data["defaults"]["tonic_midi"]
        if tonic is None:
            raise ContractError("先在共通設定填『簡譜 1 的實際音高』，例如 G2；不能只填 1=G")
        updated = []
        for entry in entries:
            value = deepcopy(entry["value"])
            changed = False
            for pitch in value.get("pitches", []):
                degree, octave = pitch.get("degree"), pitch.get("octave_shift")
                if pitch.get("midi") is not None or degree is None or octave is None:
                    continue
                accidental = pitch.get("accidental") or ""
                offset = {"": 0, "natural": 0, "♮": 0, "sharp": 1, "#": 1, "♯": 1,
                          "flat": -1, "b": -1, "♭": -1}.get(accidental)
                if offset is None:
                    continue
                midi = tonic + (0, 2, 4, 5, 7, 9, 11)[degree - 1] + 12 * octave + offset
                if not 0 <= midi <= 127:
                    raise ContractError("換算的音高超出範圍，請檢查基準八度")
                pitch.update(midi=midi, step=pitch_name(midi).rstrip("-0123456789"), octave=midi // 12 - 1)
                changed = True
            if changed:
                updated.append((entry, value))
        for entry, value in updated:
            entry.update(value=value, status="pending", acknowledged=False)
        self.save()
        return len(updated)

    def edit(self, entry_id, changes, note=""):
        entry = self.entry(entry_id)
        value = deepcopy(entry["value"])
        allowed = {"kind", "bar", "instrument_id", "technique_id", "onset", "duration", "pitches", "is_roll",
                   "is_accent", "is_ornament", "tie", "slur", "dynamic", "raw_symbol", "technique_text"}
        if set(changes) - allowed:
            raise ContractError("這些來源欄位不可改寫")
        value.update(deepcopy(changes))
        if str(value["bar"]) not in self.data["bars"]:
            raise ContractError("目標小節未登記")
        for field in ("onset", "duration"):
            if value.get(field) is not None:
                fraction(value[field], positive=field == "duration")
        entry.update(value=value, status="pending", acknowledged=False, review_note=note)
        self.save()
        return entry

    def add_event(self, bar, instrument_id=None, onset=None):
        if str(bar) not in self.data["bars"]:
            raise ContractError("小節未登記")
        source = self.data["bars"][str(bar)]
        ins = self.instrument(instrument_id)
        value = {"kind": "note", "bar": bar, "staff_label": (ins or {}).get("name", "手動補入"),
                 "instrument_text": (ins or {}).get("name"), "instrument_id": instrument_id,
                 "technique_id": ins["strokes"][0]["technique_id"] if ins else None,
                 "onset": onset or {"n": 0, "d": 1}, "duration": {"n": 1, "d": 4}, "pitches": [],
                 "is_roll": False, "is_accent": False, "is_ornament": False, "tie": "none", "slur": None,
                 "dynamic": None, "raw_symbol": "手動補入", "uncertainties": [],
                 "source_asset_id": source["asset_id"], "source_bbox": source["bar_bbox"]}
        entry = {"id": identity("MANUAL"), "region_id": source["region_id"], "run_id": None,
                 "original": None, "value": value, "status": "pending", "review_note": "手動補入", "acknowledged": False}
        self.data["entries"].append(entry)
        self.save()
        return entry

    def issues(self, entry, *, strict=None):
        strict = self.data["review_mode"] != "rhythm" if strict is None else strict
        value, issues = entry["value"], []
        kind = value["kind"]
        if kind == "unreadable":
            return ["尚未判定為敲擊或休止"]
        if kind not in {"note", "rest", "full_rest"}:
            return ["事件種類無效"]
        meter, tempo = self.bar_setting(value["bar"])
        if not meter:
            issues.append("缺少拍號")
        if kind != "full_rest":
            try:
                partial_rest = kind == "rest" and not strict
                onset = None if partial_rest and value.get("onset") is None else fraction(value.get("onset"))
                duration = None if partial_rest and value.get("duration") is None else fraction(value.get("duration"), positive=True)
                if meter and ((onset is not None and onset >= meter_length(meter))
                              or (duration is not None and duration > meter_length(meter))
                              or (onset is not None and duration is not None and onset + duration > meter_length(meter))):
                    issues.append("敲擊或休止超出小節")
                if any((v * PPQ).denominator != 1 for v in (onset, duration) if v is not None):
                    issues.append("此時間無法精確表示為 960 PPQ")
            except (ContractError, TypeError, KeyError):
                issues.append("起點或時值未填妥")
        if kind == "note":
            ins = self.instrument(value.get("instrument_id"))
            if not ins:
                issues.append("樂器尚未對應")
            elif not self.technique(ins["instrument_id"], value.get("technique_id")) and (strict or value.get("technique_id") is not None):
                issues.append("擊法尚未對應")
            pitches = value.get("pitches", [])
            if strict and (((ins or {}).get("pitched") and not pitches) or any(p.get("midi") is None for p in pitches)):
                issues.append("實際音高尚未填妥")
            if any(p.get("midi") is not None and (type(p["midi"]) is not int or not 0 <= p["midi"] <= 127) for p in pitches):
                issues.append("實際音高無效")
            if any(type(value.get(flag)) is not bool and (strict or value.get(flag) is not None) for flag in ("is_roll", "is_accent", "is_ornament")):
                issues.append("輪奏、重音或裝飾音仍有未知")
            if value.get("tie") not in {"none", "start", "continue", "stop"} and (strict or value.get("tie") not in {None, "unknown"}):
                issues.append("延音線尚未判定")
        return issues

    def optional_issues(self, entry):
        current = self.issues(entry)
        return [issue for issue in self.issues(entry, strict=True) if issue not in current]

    def issue_messages(self, entry):
        messages = self.issues(entry)
        if "起點或時值未填妥" in messages:
            value = entry["value"]
            missing = []
            if value.get("onset") is None:
                missing.append("缺少『第幾拍』")
            if value.get("duration") is None:
                missing.append("缺少『時值』")
            detail = "、".join(missing) or "第幾拍或時值格式無效"
            messages = [detail if m == "起點或時值未填妥" else m for m in messages]
        return messages

    def set_review_mode(self, mode):
        if mode not in {"complete", "rhythm"}:
            raise ContractError("請選核對方式")
        self.data["review_mode"] = mode
        self.save()

    def accept(self, entry_ids, acknowledged=False):
        if not acknowledged:
            raise ContractError("請勾選已核對原譜與辨識疑問，再接受這些資料")
        entries = [self.entry(e) for e in entry_ids]
        problems = [f"小節 {e['value']['bar']}｜{e['value'].get('instrument_text') or e['value']['staff_label']}｜"
                    + ({"rest": "休止", "full_rest": "整小節休止", "note": "敲擊"}.get(e["value"]["kind"], "未辨明"))
                    + f"『{e['value'].get('raw_symbol') or '—'}』：{'、'.join(self.issue_messages(e))}"
                    for e in entries if self.issues(e)]
        if problems:
            raise ContractError(f"有 {len(problems)} 筆待處理：\n" + "\n".join(list(dict.fromkeys(problems))[:12]))
        for entry in entries:
            entry.update(status="accepted", acknowledged=True, acceptance_profile=self.data["review_mode"])
        self.save()

    def reject(self, entry_ids, reason="人工排除"):
        entries = [self.entry(eid) for eid in dict.fromkeys(entry_ids)]
        ids = [e["id"] for e in entries if e["status"] != "rejected"]
        if not ids:
            return
        self.data["exclusion_history"].append({"entry_ids": ids, "reason": reason, "at": now()})
        for entry in entries:
            if entry["id"] in ids:
                entry.update(status="rejected", acknowledged=True, exclusion_reason=reason)
        self.save()

    def restore_excluded(self, entry_ids):
        restored = []
        for eid in entry_ids:
            entry = self.entry(eid)
            if entry["status"] == "rejected":
                entry.update(status="pending", acknowledged=False)
                restored.append(eid)
        self.save()
        return restored

    def undo_exclusion(self):
        while self.data["exclusion_history"]:
            record = self.data["exclusion_history"].pop()
            ids = [eid for eid in record["entry_ids"] if any(e["id"] == eid and e["status"] == "rejected" for e in self.entries())]
            if ids:
                return self.restore_excluded(ids)
        raise ContractError("目前沒有可復原的刪除紀錄；也可勾選『顯示已刪除』後還原選取")

    def add_segment(self, name, start, end):
        if not name.strip() or start > end or any(str(b) not in self.data["bars"] for b in range(start, end + 1)):
            raise ContractError("請填段落名稱與已登記、連續的小節範圍")
        if any(s.get("kind") != "sequence" and max(start, s["start_bar"]) <= min(end, s["end_bar"]) for s in self.data["segments"]):
            raise ContractError("段落範圍重疊，請先調整或移除原段落")
        self.data["segments"].append({"segment_id": identity("SEG"), "name": name.strip(), "start_bar": start, "end_bar": end})
        self.data["segments"].sort(key=lambda s: s["start_bar"])
        self.save()

    def update_segment(self, segment_id, name, start, end):
        segment = next(s for s in self.data["segments"] if s["segment_id"] == segment_id)
        if segment.get("kind") == "sequence":
            raise ContractError("這是跳轉段落，請使用『修改選取跳轉段落』")
        if not name.strip() or start > end or any(str(b) not in self.data["bars"] for b in range(start, end + 1)):
            raise ContractError("請填段落名稱與已登記、連續的小節範圍")
        if any(s is not segment and s.get("kind") != "sequence" and max(start, s["start_bar"]) <= min(end, s["end_bar"]) for s in self.data["segments"]):
            raise ContractError("段落範圍重疊")
        segment.update(name=name.strip(), start_bar=start, end_bar=end)
        self.data["segments"].sort(key=lambda s: s["start_bar"])
        self.save()

    def set_sequence_segment(self, name, ranges, *, acknowledged=False, segment_id=None):
        if not name.strip():
            raise ContractError("請填段落名稱")
        expand_ranges(ranges, self.data["bars"])
        value = {"name": name.strip(), "kind": "sequence", "ranges": deepcopy(ranges),
                 "start_bar": ranges[0]["start_bar"], "end_bar": ranges[-1]["end_bar"],
                 "sequence_acknowledged": bool(acknowledged)}
        if segment_id:
            segment = next(s for s in self.data["segments"] if s["segment_id"] == segment_id)
            if segment.get("kind") != "sequence":
                raise ContractError("請另建跳轉段落，保留原本的紙面範圍段落")
            segment.update(value)
        else:
            segment = {"segment_id": identity("SEG"), **value}
            self.data["segments"].append(segment)
        self.data["segments"].sort(key=lambda s: s["start_bar"])
        self.save()
        return segment

    def segment_order(self, segment):
        if segment.get("kind") == "sequence":
            return expand_ranges(segment["ranges"], self.data["bars"])
        order = expand_playback_order(max(map(int, self.data["bars"])), self.data["navigation"])
        return [item for item in order if segment["start_bar"] <= item["source_bar"] <= segment["end_bar"]]

    def navigation_covered(self, start, end):
        scopes = self.data.get("navigation_scopes", [])
        return any(scope["start_bar"] <= start and end <= scope["end_bar"] for scope in scopes)

    def set_navigation(self, markers, start=None, end=None):
        maximum = max(map(int, self.data["bars"]), default=0)
        whole = start is None and end is None
        start, end = (1, maximum) if whole else (start, end)
        if not start or not end or start > end or start < 1 or end > maximum:
            raise ContractError("請填有效的反覆核對範圍")
        if not whole and any(str(bar) not in self.data["bars"] for bar in range(start, end + 1)):
            raise ContractError("反覆核對範圍內有尚未登記的小節")
        for marker in markers:
            first, last = marker_range(marker)
            if first is None or last is None or first < start or last > end:
                raise ContractError("反覆記號超出核對範圍；請擴大範圍以涵蓋完整反覆與所有結尾")
        # 範圍中的起點、終點、結尾須能獨立配對，不能靠區域外的記號蒙混通過。
        expand_playback_order(maximum, markers)
        for existing in self.data["navigation"]:
            first, last = marker_range(existing)
            if not whole and max(first, start) <= min(last, end) and (first < start or last > end):
                raise ContractError("已有跨出此範圍的反覆／跳轉；請擴大核對範圍，避免刪掉部分路線")
        combined = [] if whole else [deepcopy(m) for m in self.data["navigation"]
                                      if marker_range(m)[1] < start or marker_range(m)[0] > end]
        combined.extend(deepcopy(markers))
        order = expand_playback_order(maximum, combined)
        scopes = merge_scopes(([] if whole else self.data["navigation_scopes"]) + [{"start_bar": start, "end_bar": end}])
        self.data.update(navigation=combined, navigation_draft=deepcopy(markers), navigation_scopes=scopes,
                         navigation_draft_scope={"start_bar": start, "end_bar": end},
                         navigation_confirmed=any(s["start_bar"] == 1 and s["end_bar"] >= maximum for s in scopes))
        self.save()
        return [item for item in order if start <= item["source_bar"] <= end]

    def suggested_navigation(self, start=None, end=None):
        result, seen = [], set()
        active_runs = {r["run_id"] for r in self.data["regions"].values() if r["status"] in {"succeeded", "empty"}}
        for candidate in self.data["navigation_candidates"]:
            if candidate["run_id"] not in active_runs:
                continue
            marker = candidate["marker"]
            if start is not None and not start <= marker["start_bar"] <= end:
                continue
            if marker["kind"] == "ending":
                value = {"type": "volta", "start_bar": marker["start_bar"], "end_bar": marker["end_bar"] or marker["start_bar"], "passes": marker["passes"]}
            elif marker["kind"] in {"repeat_start", "repeat_end"}:
                value = {"type": marker["kind"], "bar": marker["start_bar"]}
                if marker["kind"] == "repeat_end":
                    value["times"] = marker["times"] or 2
            else:
                continue
            key = json.dumps(value, sort_keys=True)
            if key not in seen:
                seen.add(key)
                result.append(value)
        return result

    def training_payload(self):
        """建立通過完整匯出檢查的快照，供 JSON 與表格共用，不寫入專案。"""
        segments = self.data["segments"]
        if not segments:
            raise ContractError("先建立至少一個訓練段落")
        sequences = [s for s in segments if s.get("kind") == "sequence"]
        for segment in sequences:
            expand_ranges(segment["ranges"], self.data["bars"])
        ordinary_bars = {b for s in segments if s.get("kind") != "sequence" for b in segment_bars(s)}
        bars = sorted({b for s in segments for b in segment_bars(s)})
        problems = []
        for segment in sequences:
            if not segment.get("sequence_acknowledged"):
                problems.append(f"段落「{segment['name']}」尚未核對自訂演奏順序")
        for bar in bars:
            settings = self.data["bars"][str(bar)]
            region = self.data["regions"][settings["region_id"]]
            if region["status"] != "succeeded":
                problems.append(f"小節 {bar}：來源尚未成功辨識（{region['status']}）")
            meter, tempo = self.bar_setting(bar)
            if not meter or not tempo:
                problems.append(f"小節 {bar}：拍號或速度未填妥")
            entries = self.entries(bar)
            if not entries:
                problems.append(f"小節 {bar}：沒有敲擊或已核對休止的證據")
            if not any(e["status"] == "accepted" for e in entries):
                problems.append(f"小節 {bar}：至少須接受敲擊或休止證據，不可全部排除")
            for entry in entries:
                if entry["status"] == "pending":
                    problems.append(f"小節 {bar}：仍有未核對觀察")
                elif entry["status"] == "accepted":
                    problems.extend(f"小節 {bar}：{issue}" for issue in self.issues(entry))
            # 同聲部不能同時確認全休止與敲擊。
            rest_staff = {e["value"]["staff_label"] for e in entries if e["status"] == "accepted" and e["value"]["kind"] == "full_rest"}
            if any(e["status"] == "accepted" and e["value"]["kind"] == "note" and e["value"]["staff_label"] in rest_staff for e in entries):
                problems.append(f"小節 {bar}：同聲部同時有整小節休止與敲擊")
        if any(not self.navigation_covered(bar, bar) for bar in ordinary_bars):
            problems.append("段落範圍尚未確認反覆記號；沒有反覆也需確認『無反覆』")
        active_runs = {r["run_id"] for r in self.data["regions"].values() if r["status"] in {"succeeded", "empty"}}
        for candidate in self.data["navigation_candidates"]:
            marker = candidate["marker"]
            if (candidate["run_id"] in active_runs and marker["kind"] == "other" and marker["start_bar"] in ordinary_bars
                    and not evidence_resolved(marker, self.data["navigation"])):
                problems.append(f"小節 {marker['start_bar']}：有尚未支援的跳轉記號，需另行處理後才能匯出：{marker['raw_text']}")
        if problems:
            raise ContractError("\n".join(dict.fromkeys(problems))[:6500])
        from .timeline import build_plan
        groups = []
        for bar in sorted(ordinary_bars):
            if not groups or bar != groups[-1][-1] + 1:
                groups.append([bar])
            else:
                groups[-1].append(bar)
        # 相鄰段落仍可有跨界延音；只檢查實際被匯出的連續範圍。
        for group in groups:
            plan = build_plan(self, group[0], group[-1], include_pending=False, max_duration=None)
            blockers = plan["blocking_warnings"] if self.data["review_mode"] == "rhythm" else plan["warnings"]
            if blockers:
                raise ContractError("\n".join(blockers))
        for segment in sequences:
            order = [item["source_bar"] for item in self.segment_order(segment)]
            plan = build_plan(self, min(order), max(order), include_pending=False, max_duration=None, source_order=order)
            blockers = plan["blocking_warnings"] if self.data["review_mode"] == "rhythm" else plan["warnings"]
            if blockers:
                raise ContractError("\n".join(blockers))
        output_segments = deepcopy(segments)
        for segment in output_segments:
            stats = segment_statistics(self, segment)
            segment.update(event_count=stats["event_count"], verified_silent=stats["verified_silent"])
        if sequences:
            order = []
            for segment in output_segments:
                segment["playback_order"] = self.segment_order(segment)
                order.extend({**item, "segment_id": segment["segment_id"]} for item in segment["playback_order"])
        else:
            order = expand_playback_order(max(map(int, self.data["bars"])), self.data["navigation"])
        output = {"format": "musicai-reviewed-training", "schema_version": "1.0", "ppq": PPQ,
                  "review_id": self.data["review_id"], "title": self.data["title"], "exported_at": now(),
                  "source_package_id": self.data["source_package_id"], "segments": output_segments,
                  "instruments": deepcopy(self.data["instruments"]), "navigation": deepcopy(self.data["navigation"]),
                  "navigation_scopes": deepcopy(self.data["navigation_scopes"]),
                  "playback_order": [item for item in order if item["source_bar"] in bars], "bars": [],
                  "excluded_bar_numbers": sorted(set(map(int, self.data["bars"])) - set(bars))}
        if sequences:
            output["playback_order_mode"] = "segments"
        output["annotation_policy"] = {"mode": self.data["review_mode"], "unknown_fields": "preserve_and_mask",
                                       "training_requirement": "Only use fields with training_mask=true; null is not false."}
        for bar in bars:
            meter, tempo = self.bar_setting(bar)
            entries = self.entries(bar)
            output["bars"].append({"number": bar, "time_signature": meter, "tempo_quarter": tempo,
                "source": deepcopy(self.data["bars"][str(bar)]),
                "events": [{"event_id": e["id"], "observation_id": e["original"]["observation_id"] if e["original"] else None,
                            "run_id": e["run_id"], **{key: deepcopy(value) for key, value in e["value"].items()
                              if key not in {"review_status", "uncertainties", "onset_tick_in_bar", "duration_ticks"}},
                            "review_status": "accepted", "training_mask": training_mask(self, e["value"]),
                            "attack": e["value"].get("tie") in {"none", "start"} if e["value"].get("tie") in {"none", "start", "continue", "stop"} else None}
                           for e in entries if e["status"] == "accepted" and e["value"]["kind"] == "note"],
                "rest_evidence": [e["id"] for e in entries if e["status"] == "accepted" and e["value"]["kind"] in {"rest", "full_rest"}],
                "rest_observations": [{"evidence_id": e["id"], "kind": e["value"]["kind"], "scope": "source_staff",
                    "timing_known": e["value"]["kind"] == "full_rest" or (e["value"].get("onset") is not None and e["value"].get("duration") is not None),
                    "training_mask": {"interval": e["value"]["kind"] == "full_rest" or (e["value"].get("onset") is not None and e["value"].get("duration") is not None)},
                    **{key: deepcopy(e["value"].get(key)) for key in ("staff_label", "instrument_text", "instrument_id", "onset", "duration", "source_asset_id", "source_bbox")}}
                    for e in entries if e["status"] == "accepted" and e["value"]["kind"] in {"rest", "full_rest"}],
                "rejected_observation_ids": [e["id"] for e in entries if e["status"] == "rejected"]})
        return output

    def export_target(self, path):
        """所有衍生格式共用的來源／專案覆寫保護。"""
        target = Path(path).resolve()
        if target == (self.root / MANIFEST) or target.is_relative_to(self.root / "assets"):
            raise ContractError("匯出位置不可覆蓋校對專案或音效／原圖")
        source_path = Path(self.data["source_path"])
        if target == source_path or (source_path.name == SOURCE_MANIFEST and target.is_relative_to(source_path.parent)):
            raise ContractError("不可覆蓋辨識來源")
        return target

    def export_training(self, path):
        target = self.export_target(path)
        output = self.training_payload()
        atomic_json(target, output)
        return {"bars": len(output["bars"]), "events": sum(len(b["events"]) for b in output["bars"]), "segments": len(output["segments"])}

    def export_tables(self, path):
        from .table_export import export_tables
        target = self.export_target(path)
        if target.suffix.lower() not in {".xlsx", ".zip"}:
            raise ContractError("請選擇 Excel .xlsx 或 CSV 表格壓縮包 .zip")
        return export_tables(self.training_payload(), target)
