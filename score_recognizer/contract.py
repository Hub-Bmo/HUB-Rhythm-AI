"""辨識交換契約：紙面小節、原始觀察、未知值與證據互相獨立。"""

from __future__ import annotations

from fractions import Fraction
import math

PPQ = 960
FORMAT = "musicai-recognition-package"
SCHEMA_VERSION = "1.0"
NOTATIONS = {"auto": "自動", "numbered": "簡譜", "staff": "五線譜", "gongjing": "鑼鼓經"}


def obj(**properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def nullable(kind):
    return {"type": [kind, "null"]}


def array(item):
    return {"type": "array", "items": item}


TEXT = {"type": "string"}
BOOL = {"type": "boolean"}
BOX = obj(x={"type": "number"}, y={"type": "number"},
          width={"type": "number"}, height={"type": "number"})
RATIONAL = obj(n={"type": "integer"}, d={"type": "integer"})
PITCH = obj(
    degree=nullable("integer"), octave_shift=nullable("integer"),
    accidental=nullable("string"), step=nullable("string"), octave=nullable("integer"),
    midi=nullable("integer"), clef=nullable("string"), written_text=TEXT,
)
EVENT = obj(
    kind={"type": "string", "enum": ["note", "rest", "unreadable"]},
    raw_symbol=TEXT, instrument_text=nullable("string"), technique_text=nullable("string"),
    onset={"anyOf": [RATIONAL, {"type": "null"}]},
    duration={"anyOf": [RATIONAL, {"type": "null"}]},
    pitches=array(PITCH), is_roll=nullable("boolean"), is_accent=nullable("boolean"),
    is_ornament=nullable("boolean"),
    tie={"type": "string", "enum": ["none", "start", "continue", "stop", "unknown"]},
    slur=nullable("string"), dynamic=nullable("string"),
    bbox=BOX, uncertainties=array(TEXT),
)
PART = obj(
    staff_label=TEXT, instrument_text=nullable("string"),
    status={"type": "string", "enum": ["events", "full_rest", "unclear"]},
    bbox=BOX, events=array(EVENT), uncertainties=array(TEXT),
)
MEASURE = obj(
    index={"type": "integer"}, time_signature=nullable("string"),
    key_signature=nullable("string"), tempo_quarter=nullable("number"),
    parts=array(PART), uncertainties=array(TEXT),
)
MARKER = obj(
    kind={"type": "string", "enum": ["repeat_start", "repeat_end", "ending", "other"]},
    start_bar={"type": "integer"}, end_bar=nullable("integer"),
    passes=array({"type": "integer"}), times=nullable("integer"),
    raw_text=TEXT, bbox=BOX, uncertainties=array(TEXT),
)
RESPONSE_SCHEMA = obj(
    notation={"type": "string", "enum": list(NOTATIONS)},
    measures=array(MEASURE), navigation=array(MARKER), warnings=array(TEXT),
)


class ContractError(ValueError):
    pass


def check_shape(value, schema, path="result"):
    """本機也驗證嚴格 schema；離線測試與匯入不依賴遠端保證。"""
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            try:
                check_shape(value, option, path)
                return
            except ContractError:
                pass
        raise ContractError(f"{path} 的格式不符")
    kinds = schema["type"]
    kinds = [kinds] if isinstance(kinds, str) else kinds
    valid = {
        "null": value is None, "string": isinstance(value, str),
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(value),
        "object": isinstance(value, dict), "array": isinstance(value, list),
    }
    if not any(valid[kind] for kind in kinds):
        raise ContractError(f"{path} 的型別不符")
    if "enum" in schema and value not in schema["enum"]:
        raise ContractError(f"{path} 的選項無效")
    if isinstance(value, dict) and "properties" in schema:
        if set(value) != set(schema["properties"]):
            raise ContractError(f"{path} 的欄位缺漏或有多餘欄位")
        for key, child in schema["properties"].items():
            check_shape(value[key], child, f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for index, child in enumerate(value):
            check_shape(child, schema["items"], f"{path}[{index}]")


def bbox(value):
    check_shape(value, BOX, "bbox")
    x, y, w, h = (value[key] for key in ("x", "y", "width", "height"))
    if min(x, y) < 0 or min(w, h) <= 0 or x + w > 1.00000001 or y + h > 1.00000001:
        raise ContractError("框選範圍超出圖片或沒有面積")
    return value


def fraction(value, *, positive=False):
    check_shape(value, RATIONAL, "time")
    if value["d"] <= 0 or value["n"] < 0 or (positive and value["n"] == 0):
        raise ContractError("時間分數必須非負，分母必須大於零；時值必須大於零")
    return Fraction(value["n"], value["d"])


def ticks(value):
    if value is None:
        return None
    result = fraction(value) * PPQ
    return int(result) if result.denominator == 1 else None


def meter_length(text):
    if not text:
        return None
    try:
        top, bottom = (int(part) for part in text.split("/"))
        if top <= 0 or bottom <= 0 or bottom & (bottom - 1):
            raise ValueError
        return Fraction(top * 4, bottom)
    except (ValueError, AttributeError) as error:
        raise ContractError("拍號請填 4/4、3/4、6/8 等格式，未知可留白") from error


def validate_result(result, region):
    check_shape(result, RESPONSE_SCHEMA)
    expected = set(range(region["bar_count"]))
    indexes = [measure["index"] for measure in result["measures"]]
    if len(indexes) != len(expected) or set(indexes) != expected:
        raise ContractError("辨識回應的小節數不完整或重複；原始回應已保留，可重試")
    for measure in result["measures"]:
        length = meter_length(measure["time_signature"] or region.get("time_signature"))
        if measure["tempo_quarter"] is not None and measure["tempo_quarter"] <= 0:
            raise ContractError("速度必須大於零")
        for part in measure["parts"]:
            bbox(part["bbox"])
            if part["status"] == "full_rest" and part["events"]:
                raise ContractError("全小節休止不可同時含事件")
            for event in part["events"]:
                bbox(event["bbox"])
                onset = fraction(event["onset"]) if event["onset"] is not None else None
                duration = fraction(event["duration"], positive=True) if event["duration"] is not None else None
                if onset is not None and length is not None:
                    if onset >= length or (duration is not None and onset + duration > length):
                        raise ContractError("事件時間超出小節；跨小節延音須分開並記錄 tie")
                for pitch in event["pitches"]:
                    if pitch["degree"] is not None and not 1 <= pitch["degree"] <= 7:
                        raise ContractError("簡譜音級必須為 1–7，0 應記為休止")
                    if pitch["midi"] is not None and not 0 <= pitch["midi"] <= 127:
                        raise ContractError("MIDI 音高超出範圍")
    for marker in result["navigation"]:
        bbox(marker["bbox"])
        if marker["start_bar"] < 1 or (marker["end_bar"] is not None and marker["end_bar"] < marker["start_bar"]):
            raise ContractError("反覆記號的小節範圍無效")
        if any(value < 1 for value in marker["passes"]):
            raise ContractError("結尾的演奏次數必須大於零")
    return result


def image_bbox(crop_bbox, source_bbox):
    return {
        "x": source_bbox["x"] + crop_bbox["x"] * source_bbox["width"],
        "y": source_bbox["y"] + crop_bbox["y"] * source_bbox["height"],
        "width": crop_bbox["width"] * source_bbox["width"],
        "height": crop_bbox["height"] * source_bbox["height"],
    }
