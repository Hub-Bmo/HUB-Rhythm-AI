"""將紙面事件排成試聽時間；不改寫原事件、不把輪奏拆入訓練資料。"""
from __future__ import annotations

from copy import deepcopy
from fractions import Fraction

from .navigation import expand_playback_order
from score_recognizer.contract import ContractError, fraction, meter_length


def bar_order(doc, start, end, repeats=False):
    if start > end or any(str(b) not in doc.data["bars"] for b in range(start, end + 1)):
        raise ContractError("播放範圍必須是已登記、連續的小節")
    if not repeats:
        return list(range(start, end + 1))
    if not doc.navigation_covered(start, end):
        raise ContractError("請先在『段落與反覆』確認此播放範圍的反覆記號")
    maximum = max(map(int, doc.data["bars"]))
    order = expand_playback_order(maximum, doc.data["navigation"])
    return [item["source_bar"] for item in order if start <= item["source_bar"] <= end]


def relative_preview_pitches(pitches):
    """僅供試聽：以 C3 作簡譜 1，不將未知八度／音高寫回文件。"""
    result = []
    accidentals = {None: 0, "": 0, "natural": 0, "♮": 0, "sharp": 1, "#": 1, "♯": 1,
                   "flat": -1, "b": -1, "♭": -1}
    for pitch in pitches:
        if pitch.get("midi") is not None:
            result.append(pitch["midi"])
            continue
        degree, octave = pitch.get("degree"), pitch.get("octave_shift")
        if degree not in range(1, 8) or octave is None or pitch.get("accidental") not in accidentals:
            return []
        value = 48 + (0, 2, 4, 5, 7, 9, 11)[degree - 1] + octave * 12 + accidentals[pitch.get("accidental")]
        if not 0 <= value <= 127:
            return []
        result.append(value)
    # 已知與未知實際音高混合時，不能將不同基準當成相對和弦。
    known = [p.get("midi") is not None for p in pitches]
    return [] if any(known) and not all(known) else result


def tie_pitch_signature(value):
    """延音比對使用保存的音高資訊，不使用可能全為空的試聽音高。"""
    result = []
    for pitch in value.get("pitches", []):
        if pitch.get("midi") is not None:
            result.append(("midi", pitch["midi"]))
        elif pitch.get("step") is not None:
            result.append(("written", pitch.get("step"), pitch.get("octave"), pitch.get("accidental")))
        elif pitch.get("degree") is not None:
            result.append(("degree", pitch.get("degree"), pitch.get("octave_shift"),
                           pitch.get("accidental"), value.get("key_signature")))
        else:
            # 節奏優先模式保留未知音高遮罩；不能讓非結構化描述決定延音。
            result.append(("unknown",))
    return tuple(result)


def tie_location(doc, entry):
    value = entry["value"]
    instrument = doc.instrument(value.get("instrument_id"))
    name = (instrument or {}).get("name") or value.get("instrument_text") or value.get("staff_label") or "未對應樂器"
    beat = str(fraction(value["onset"]) + 1)
    return f"{name}，第 {beat} 拍；{entry['id']}"


def build_plan(doc, start, end, *, include_pending=True, repeats=False, metronome=False, instrument_id=None, max_duration=600, relative_pitch=False, source_order=None):
    if source_order is None:
        order = bar_order(doc, start, end, repeats)
    else:
        order = list(source_order)
        if not order or len(order) > 10000 or any(type(b) is not int or str(b) not in doc.data["bars"] for b in order):
            raise ContractError("自訂段落必須只引用已登記的小節，且順序不可空白")
    notes, warnings, positions = [], [], []
    blocking = []
    cursor = 0.0
    # 延音須有同樂器、擊法、音高，且實際時間相接。反覆跳轉不會任意接線。
    tied = {}
    previous_bar = None
    for bar in order:
        if previous_bar is not None and bar != previous_bar + 1:
            if tied:
                warnings.append(f"小節 {previous_bar}：延音線遇到反覆／跳轉，請核對連接")
                blocking.append(warnings[-1])
            tied.clear()
        previous_bar = bar
        meter, tempo = doc.bar_setting(bar)
        if not meter or not tempo:
            raise ContractError(f"小節 {bar} 尚未填拍號或速度，請先填共通設定或本小節設定")
        length = meter_length(meter)
        beat_seconds = 60 / tempo
        positions.append({"bar": bar, "start": cursor, "duration": float(length) * beat_seconds})
        region = doc.data["regions"][doc.data["bars"][str(bar)]["region_id"]]
        if region["status"] != "succeeded":
            warnings.append(f"小節 {bar} 的來源為 {region['status']}；本次未辨識內容以靜音留位")
        entries = sorted(doc.entries(bar), key=lambda e: fraction(e["value"]["onset"]) if e["value"].get("onset") is not None else Fraction(1000000))
        for entry in entries:
            if entry["status"] == "rejected" or (not include_pending and entry["status"] != "accepted"):
                continue
            value = entry["value"]
            if instrument_id is not None and value.get("instrument_id") != instrument_id:
                continue
            if value["kind"] in {"rest", "full_rest"}:
                continue
            if value["kind"] != "note":
                warnings.append(f"小節 {bar}：未讀清楚的符號無法試聽（{entry['id']}）")
                continue
            try:
                onset, duration = fraction(value.get("onset")), fraction(value.get("duration"), positive=True)
                if onset >= length or onset + duration > length:
                    raise ContractError("超出小節")
            except (ContractError, TypeError, KeyError):
                warnings.append(f"小節 {bar}：起點或時值未知／超出小節，略過試聽（{entry['id']}）")
                continue
            ins = doc.instrument(value.get("instrument_id"))
            stroke = doc.technique(value.get("instrument_id"), value.get("technique_id"))
            selected_profile = doc.score_sound(value)
            if not ins:
                warnings.append(f"小節 {bar}：{value.get('instrument_text') or value.get('staff_label')} 尚未對應樂器／擊法，" + ("使用本譜選定音色試聽" if selected_profile else "暫用提示音"))
            elif not stroke:
                warnings.append(f"小節 {bar}：{ins['name']} 擊法尚未對應，使用該樂器的一般校對音效")
            pitches = [p.get("midi") for p in value.get("pitches", [])]
            pitch_missing = any(p is None for p in pitches) or ((ins or {}).get("pitched") and not pitches)
            relative = relative_preview_pitches(value.get("pitches", [])) if pitch_missing and relative_pitch else []
            if relative:
                warnings.append(f"小節 {bar}：實際音高未確認，以 1=C3 試聽相對音高；不會填入訓練資料")
            elif pitch_missing:
                warnings.append(f"小節 {bar}：實際音高未知，" + ("僅以選定音色試聽" if selected_profile else "暫用無音高提示音") + f"（{entry['id']}）")
            pitches = relative or ([p for p in pitches if p is not None] if not pitch_missing else [])
            if any(value.get(f) is None for f in ("is_roll", "is_accent", "is_ornament")):
                warnings.append(f"小節 {bar}：演奏記號未知，暫依已知部分試聽（{entry['id']}）")
            if value.get("tie") == "unknown":
                warnings.append(f"小節 {bar}：延音線尚未確認（{entry['id']}）")
            key = (value.get("instrument_id") or value.get("instrument_text") or value.get("staff_label"),
                   value.get("technique_id"), tie_pitch_signature(value))
            start_sec, duration_sec = cursor + float(onset) * beat_seconds, float(duration) * beat_seconds
            if value.get("tie") in {"continue", "stop"}:
                prior = tied.get(key)
                if prior is not None and abs(notes[prior]["start"] + notes[prior]["duration"] - start_sec) < 1e-6:
                    notes[prior]["duration"] += duration_sec
                    if value["tie"] == "stop":
                        del tied[key]
                    continue
                warnings.append(f"小節 {bar}：延音線缺少相接的起點，暫重新起音（{tie_location(doc, entry)}）")
                blocking.append(warnings[-1])
            profile = ins["profile"] if ins and (not pitch_missing or relative) else ("bell" if pitches else "metronome")
            notes.append({"entry_id": entry["id"], "bar": bar, "start": start_sec, "duration": duration_sec,
                          "profile": selected_profile or profile,
                          "sample": stroke.get("sample") if stroke and not pitch_missing and selected_profile is None else None,
                          "relative_pitch_preview": bool(relative),
                          "pitches": pitches, "is_roll": value.get("is_roll") is True,
                          "is_accent": value.get("is_accent") is True, "is_ornament": value.get("is_ornament") is True,
                          "dynamic": value.get("dynamic"), "beat_seconds": beat_seconds})
            if value.get("tie") in {"start", "continue"}:
                if key in tied:
                    warnings.append(f"小節 {bar}：同音延音線重複起點（{tie_location(doc, entry)}）")
                    blocking.append(warnings[-1])
                tied[key] = len(notes) - 1
        if metronome:
            for beat in range(int(length) + 1):
                if Fraction(beat) >= length:
                    break
                notes.append({"entry_id": "metronome", "bar": bar, "start": cursor + beat * beat_seconds,
                              "duration": .05, "profile": "metronome", "sample": None, "pitches": [],
                              "is_roll": False, "is_accent": beat == 0, "is_ornament": False, "dynamic": "p", "beat_seconds": beat_seconds})
        cursor += float(length) * beat_seconds
        if max_duration is not None and cursor > max_duration:
            raise ContractError("一次試聽最多 10 分鐘，請縮小小節範圍")
    for index in tied.values():
        entry = doc.entry(notes[index]["entry_id"])
        warnings.append(f"小節 {notes[index]['bar']}：延音線尚未閉合，請檢查下一小節或擴大播放範圍（{tie_location(doc, entry)}）")
        blocking.append(warnings[-1])
    return {"notes": sorted(notes, key=lambda n: n["start"]), "bars": positions,
            "duration": cursor, "warnings": list(dict.fromkeys(warnings)),
            "includes_pending": include_pending, "order": order, "blocking_warnings": list(dict.fromkeys(blocking))}
