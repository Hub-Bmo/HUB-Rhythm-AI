"""節奏優先核對、可訓練欄位與明確名稱修正；不捏造未知標註。"""
from collections import Counter
import re

from score_recognizer.contract import ContractError

REVIEW_MODES = {"節奏優先（未知細節可保留）": "rhythm", "完整標註（所有細節需確認）": "complete"}


def training_mask(doc, value):
    ins = doc.instrument(value.get("instrument_id"))
    pitches = value.get("pitches", [])
    return {"instrument": bool(ins), "onset": value.get("onset") is not None,
            "duration": value.get("duration") is not None,
            "technique": bool(doc.technique(value.get("instrument_id"), value.get("technique_id"))),
            "pitches_midi": bool(pitches) and all(p.get("midi") is not None for p in pitches),
            "pitches_degree": bool(pitches) and all(p.get("degree") in range(1, 8) and p.get("octave_shift") is not None for p in pitches),
            "is_roll": type(value.get("is_roll")) is bool,
            "is_accent": type(value.get("is_accent")) is bool,
            "is_ornament": type(value.get("is_ornament")) is bool,
            "tie": value.get("tie") in {"none", "start", "continue", "stop"},
            "attack": value.get("tie") in {"none", "start", "continue", "stop"},
            "dynamic": value.get("dynamic") is not None, "slur": value.get("slur") is not None}


def inferred_profile(name):
    # 只處理可獨立識別的名稱；組合聲部／模型的描述性暫名留待核對。
    if any(c in name for c in "／/、，,\n疑列"):
        return None
    if name == "定音鼓":
        return "timpani"
    if "鈸" in name:
        return "cymbal"
    if "鑼" in name:
        return "small_gong" if "小" in name else "large_gong"
    if "鈴" in name or "鐘" in name or "響盞" in name:
        return "bell"
    if "鼓" in name:
        return "drum"
    return None


def apply_name_rules(doc, value, rules=None):
    """僅套用使用者建立的全名規則；原始 AI 回應另存於 original。"""
    name = value.get("instrument_text") or value.get("staff_label")
    rules = rules if rules is not None else doc.data.get("name_corrections", {})
    rule = rules.get(name)
    if not rule:
        changed = False
        for key in ("instrument_text", "staff_label"):
            text = value.get(key)
            if not text:
                continue
            parts = re.split(r"([／/、，,\n])", text)
            for index in range(0, len(parts), 2):
                token = parts[index].strip()
                if token in rules and rules[token]["name"] != token:
                    parts[index] = rules[token]["name"]
            corrected = "".join(parts)
            if corrected != text:
                value[key] = corrected
                changed = True
        return changed
    value["instrument_text"] = rule["name"]
    if value.get("staff_label") == name:
        value["staff_label"] = rule["name"]
    if value.get("instrument_id") != rule["instrument_id"]:
        value["technique_id"] = None
    value["instrument_id"] = rule["instrument_id"]
    return True


def correct_name(doc, raw_name, name, profile):
    if not raw_name.strip() or not name.strip():
        raise ContractError("請選原名稱並填正確名稱")
    if raw_name == name:
        raise ContractError("原名稱與正確名稱相同；請使用『依名稱整理樂器』")
    ins = next((i for i in doc.data["instruments"] if i["name"] == name.strip()), None)
    if not ins:
        ins = doc.add_instrument(name.strip(), profile, name.strip() == "定音鼓")
    rules = {raw_name: {"name": name.strip(), "instrument_id": ins["instrument_id"]}}
    changed = 0
    for entry in doc.entries():
        if apply_name_rules(doc, entry["value"], rules):
            if entry["status"] != "rejected":
                entry.update(status="pending", acknowledged=False)
            changed += 1
    doc.data["name_corrections"].update(rules)
    # 舊規則指向的名稱再次修正時，後續匯入直接使用最後的正式名稱。
    for rule in doc.data["name_corrections"].values():
        if rule["name"] == raw_name:
            rule.update(rules[raw_name])
    if raw_name in doc.data["score_sounds"] and name.strip() not in doc.data["score_sounds"]:
        doc.data["score_sounds"][name.strip()] = doc.data["score_sounds"][raw_name]
    doc.save()
    return changed


def organize_instruments(doc):
    """為原文單一樂器建立獨立身分；不把它改名為泛稱鼓／鈸，也不猜技法。"""
    changed, created = 0, 0
    for entry in doc.entries():
        if entry["status"] == "rejected" or entry["value"]["kind"] not in {"note", "rest", "full_rest"}:
            continue
        value = entry["value"]
        name = (value.get("instrument_text") or value["staff_label"]).strip()
        previous = doc.instrument(value.get("instrument_id"))
        if previous and (previous["name"] == name or previous["name"] not in {"鼓", "鈸", "大鑼", "小鑼", "響盞", "排鼓"}):
            continue
        ins = next((i for i in doc.data["instruments"] if i["name"] == name), None)
        profile = inferred_profile(name)
        if not ins and profile:
            ins = doc.add_instrument(name, previous["profile"] if previous else profile, name == "定音鼓")
            created += 1
        if ins:
            old_stroke = doc.technique(value.get("instrument_id"), value.get("technique_id"))
            stroke = None
            if old_stroke:
                stroke = next((s for s in ins["strokes"] if s["name"] == old_stroke["name"]), None)
                if not stroke:
                    stroke = doc.add_technique(ins["instrument_id"], old_stroke["name"])
                if old_stroke.get("sample") and not stroke.get("sample"):
                    stroke["sample"] = old_stroke["sample"]
            value.update(instrument_id=ins["instrument_id"], technique_id=stroke["technique_id"] if stroke else None)
            entry.update(status="pending", acknowledged=False)
            changed += 1
            doc.data["name_corrections"][name] = {"name": name, "instrument_id": ins["instrument_id"]}
    doc.save()
    return {"mapped": changed, "created": created}


def review_summary(doc, bars):
    rows, ready, optional = [], [], Counter()
    bars = set(bars)
    present = {e["value"]["bar"] for e in doc.entries() if e["status"] != "rejected"}
    for bar in sorted(bars):
        source = doc.data["bars"].get(str(bar))
        if source and bar not in present:
            rows.append({"bar": bar, "entry_id": None, "issues": ["此小節尚無可用辨識資料或全部已排除"]})
    for entry in doc.entries():
        value = entry["value"]
        if value["bar"] not in bars or entry["status"] == "rejected":
            continue
        problems = list(doc.issue_messages(entry))
        source = doc.data["bars"][str(value["bar"])]
        if doc.data["regions"][source["region_id"]]["status"] != "succeeded":
            problems.append("來源尚未成功辨識")
        if not doc.bar_setting(value["bar"])[1]:
            problems.append("缺少速度")
        if problems:
            rows.append({"bar": value["bar"], "entry_id": entry["id"], "issues": problems})
        elif entry["status"] == "pending":
            ready.append(entry["id"])
        for issue in doc.optional_issues(entry):
            optional[issue] += 1
    return {"ready": ready, "problems": rows, "optional": dict(optional)}
