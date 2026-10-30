"""比較音樂內容，而非圖片框的位置；差異不是對錯判定。"""
from fractions import Fraction
from itertools import zip_longest
import json

FIELDS = ("聲部", "樂器", "種類", "第幾拍", "時值", "音高", "擊法文字", "輪奏", "重音", "裝飾音", "延音線", "連奏", "力度", "原符號")
KINDS = {"note": "敲擊", "rest": "休止", "unreadable": "未辨明", "full_rest": "整小節休止", "unclear": "聲部未辨明"}


def time_text(value, onset=False):
    if value is None:
        return "未知"
    try:
        number = Fraction(value["n"], value["d"])
    except ZeroDivisionError:
        return f"無效（{value['n']}/{value['d']}）"
    return str(number + 1 if onset else number)


def flag(value):
    return {True: "是", False: "否", None: "未知"}[value]


def pitch_text(pitches):
    values = []
    for p in pitches:
        actual = f"{p.get('step') or '?'}{p['octave'] if p.get('octave') is not None else '?'}" if p.get("step") or p.get("octave") is not None else "實音未知"
        parts = [actual]
        if p.get("midi") is not None:
            parts.append(f"MIDI {p['midi']}")
        if p.get("degree") is not None:
            parts.append(f"音級 {p['degree']}")
        parts.append("八度點 " + (str(p["octave_shift"]) if p.get("octave_shift") is not None else "未知"))
        parts.append("升降 " + (p["accidental"] or "無記號" if p.get("accidental") is not None else "未知"))
        if p.get("clef"):
            parts.append("譜號 " + p["clef"])
        values.append("／".join(parts))
    return " | ".join(sorted(values)) if values else "無音高"


def event_rows(result):
    if not result:
        return []
    rows = []
    for measure in result["measures"]:
        for part in measure["parts"]:
            base = {"聲部": part["staff_label"].strip(), "樂器": (part["instrument_text"] or part["staff_label"]).strip()}
            if not part["events"]:
                rows.append({**base, "種類": KINDS.get(part["status"], "聲部未列事件"),
                             "第幾拍": "整小節" if part["status"] == "full_rest" else "未知", "時值": "整小節" if part["status"] == "full_rest" else "未知",
                             "證據": part["bbox"], "原符號": "", "疑問": "；".join(part["uncertainties"])})
            for e in part["events"]:
                rows.append({**base, "樂器": (e["instrument_text"] or base["樂器"]).strip(), "種類": KINDS[e["kind"]],
                             "第幾拍": time_text(e["onset"], True), "時值": time_text(e["duration"]),
                             "音高": pitch_text(e["pitches"]),
                             "音高基準": json.dumps(sorted(json.dumps({k: p[k] for k in p if k != "written_text"}, ensure_ascii=False, sort_keys=True) for p in e["pitches"])),
                             "擊法文字": e["technique_text"] or "未標示",
                             "輪奏": flag(e["is_roll"]), "重音": flag(e["is_accent"]), "裝飾音": flag(e["is_ornament"]),
                             "延音線": {"none": "無", "start": "開始", "continue": "延續", "stop": "結束", "unknown": "未知"}[e["tie"]], "連奏": e["slur"] or "未標示", "力度": e["dynamic"] or "未標示",
                             "原符號": e["raw_symbol"], "證據": e["bbox"],
                             "疑問": "；".join(part["uncertainties"] + e["uncertainties"] + measure["uncertainties"])})
    def order(row):
        try:
            onset = Fraction(row["第幾拍"])
        except ValueError:
            onset = Fraction(-1)
        return (row["聲部"], row["樂器"], onset, row["種類"], signature(row))
    return sorted(rows, key=order)


def signature(row):
    return tuple(field_value(row, k) for k in FIELDS)


def field_value(row, key):
    return row.get("音高基準", row.get(key, "")) if key == "音高" else row.get(key, "")


def differences(left_result, right_result):
    """先移除完全相同項，避免一個漏音使整列音符錯位；剩餘僅為候選配對。"""
    left, right = event_rows(left_result), event_rows(right_result)
    right_positions = {}
    for index, row in enumerate(right):
        right_positions.setdefault(signature(row), []).append(index)
    matched_right, same, remaining_left = set(), [], []
    for index, row in enumerate(left):
        candidates = right_positions.get(signature(row), [])
        if candidates:
            other = candidates.pop(0)
            matched_right.add(other)
            same.append({"state": "相同", "left": index, "right": other, "fields": []})
        else:
            remaining_left.append(index)
    remaining_right = [i for i in range(len(right)) if i not in matched_right]
    changed = []
    # 只在同一聲部內按時間順序配對；無法確定實際對應時仍可回原圖檢查。
    staffs = sorted({left[i]["聲部"] for i in remaining_left} | {right[i]["聲部"] for i in remaining_right})
    for staff in staffs:
        a = [i for i in remaining_left if left[i]["聲部"] == staff]
        b = [i for i in remaining_right if right[i]["聲部"] == staff]
        for i, j in zip_longest(a, b):
            keys = [k for k in FIELDS if (field_value(left[i], k) if i is not None else None) != (field_value(right[j], k) if j is not None else None)]
            changed.append({"state": "僅右側" if i is None else "僅左側" if j is None else "不同（候選配對）",
                            "left": i, "right": j, "fields": keys})
    metadata = []
    if left_result and right_result:
        for key, name in (("notation", "譜種"), ("navigation", "反覆記號")):
            if key == "navigation":
                clean = lambda result: sorted(json.dumps({k: v for k, v in marker.items() if k not in {"bbox", "uncertainties"}}, sort_keys=True, ensure_ascii=False) for marker in result[key])
                a, b = clean(left_result), clean(right_result)
            else:
                a, b = left_result[key], right_result[key]
            if a != b:
                metadata.append(name)
        for key, name in (("time_signature", "拍號"), ("key_signature", "調號"), ("tempo_quarter", "速度")):
            if left_result["measures"][0][key] != right_result["measures"][0][key]:
                metadata.append(name)
    return {"left": left, "right": right, "rows": changed + same, "same": len(same), "different": len(changed), "metadata": metadata}


def readable_row(row):
    if row is None:
        return "—"
    detail = "、".join(k for k in ("輪奏", "重音", "裝飾音") if row.get(k) == "是")
    return f"{row['樂器']}｜{row['種類']}｜第 {row['第幾拍']} 拍｜時值 {row['時值']}" + (f"｜{detail}" if detail else "")


def export_report(project, directory):
    """輸出每個模型的事件與用量，CSV 使用 UTF-8 BOM 方便 Excel 開啟。"""
    import csv
    from pathlib import Path
    from score_recognizer.workspace import atomic_json
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    runs, events, diffs = [], [], []
    from itertools import combinations
    for case in project.data["cases"]:
        common = {"比較編號": case["case_id"], "頁碼": case["page"], "小節": case["bar"], "輸入指紋": case["input_sha256"]}
        for run in [*case.get("previous_attempts", []), *case["runs"]]:
            usage = run.get("usage") or {}
            runs.append({**common, "工作編號": run["run_id"], "模型": run["model"], "狀態": run["status"],
                         "先前嘗試": "是" if run in case.get("previous_attempts", []) else "否", "秒數": run.get("elapsed_seconds"),
                         "輸入tokens": usage.get("input_tokens"), "輸出tokens": usage.get("output_tokens"),
                         "估算USD（未扣快取）": run.get("cost_usd"), "錯誤": run.get("error"),
                         "人工判定": json.dumps(case.get("judgment"), ensure_ascii=False) if case.get("judgment") else ""})
            for row in event_rows(run.get("result")):
                events.append({**common, "工作編號": run["run_id"], "模型": run["model"],
                               "先前嘗試": "是" if run in case.get("previous_attempts", []) else "否",
                               **{k: row.get(k, "") for k in (*FIELDS, "疑問")}})
        for a, b in combinations(case["runs"], 2):
            if not a.get("result") or not b.get("result"):
                continue
            report = differences(a["result"], b["result"])
            diffs.append({**common, "左模型": a["model"], "右模型": b["model"], "相同事件數": report["same"],
                          "差異列數（非錯誤數）": report["different"], "其他差異": "、".join(report["metadata"])})
    def write(name, rows, headers):
        with (directory / name).open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else headers)
            writer.writeheader()
            # 原譜文字可能以公式起始；CSV 僅供檢視，JSON 保留原值。
            for row in rows:
                writer.writerow({k: "'" + v if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@")) else v for k, v in row.items()})
    write("模型用量.csv", runs, ["比較編號", "頁碼", "小節", "模型", "狀態"])
    write("各模型事件.csv", events, ["比較編號", "頁碼", "小節", "模型", *FIELDS])
    write("差異摘要.csv", diffs, ["比較編號", "頁碼", "小節", "左模型", "右模型", "差異列數（非錯誤數）"])
    atomic_json(directory / "比較紀錄.json", project.data)
    (directory / "說明.txt").write_text("差異不代表錯誤；相同不代表正確。配對僅供定位。秒數包含該次 API 往返；成本依 2026-10-28 一般單價粗估，未扣快取或計加價，非帳單。空白用量不是零費用。人工判定不等於已建立正確答案。\n", encoding="utf-8")
    return directory
