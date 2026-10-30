"""已核對訓練快照的閱讀用表格。僅使用標準函式庫，無 Excel 安裝需求。"""
from __future__ import annotations

from collections import Counter
import csv
from dataclasses import dataclass
from fractions import Fraction
from io import BytesIO, StringIO
import json
from pathlib import Path
import re
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from score_recognizer.contract import ContractError
from score_recognizer.workspace import atomic_bytes
from .segments import segment_bars


@dataclass
class Table:
    name: str
    headers: list[str]
    rows: list[list]


def rational(value):
    return Fraction(value["n"], value["d"]) if value is not None else None


def flag(value):
    return "未知" if value is None else ("是" if value else "否")


def literal(value):
    return "未知" if value is None else str(value)


def packed(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


EVENT_HEADERS = ["原譜小節", "頁碼", "樂器", "來源聲部", "第幾拍（從1起）", "時值（四分音符=1）",
                 "拍點精確分數", "時值精確分數", "擊法", "實際MIDI音高", "譜面音高文字",
                 "輪奏", "重音", "裝飾音", "延音線", "新起音", "力度", "連奏線", "原符號",
                 "拍號", "四分音符每分鐘", "所屬段落", "樂器ID", "擊法ID", "事件ID",
                 "來源觀察ID", "來源辨識ID", "原譜樂器文字", "原譜擊法文字", "音高完整資料",
                 "訓練欄位遮罩", "來源圖片ID", "來源位置框"]


def make_tables(payload):
    """單一表格模型，CSV、產品 XLSX 與檢查工具共用相同值。"""
    if payload.get("format") != "musicai-reviewed-training":
        raise ContractError("表格來源必須是已核對訓練資料")
    bars = {b["number"]: b for b in payload["bars"]}
    instruments = {i["instrument_id"]: i for i in payload["instruments"]}
    techniques = {s["technique_id"]: s["name"] for i in instruments.values() for s in i.get("strokes", [])}
    membership = {}
    orders = {}
    for segment in payload["segments"]:
        selected = segment_bars(segment)
        for bar in selected:
            membership.setdefault(bar, []).append(segment["name"])
        orders[segment["segment_id"]] = segment.get("playback_order")
        if orders[segment["segment_id"]] is None:
            orders[segment["segment_id"]] = [ref for ref in payload["playback_order"] if ref["source_bar"] in selected]

    original_rows, event_rows = [], {}
    for number, bar in sorted(bars.items()):
        for event in sorted(bar["events"], key=lambda e: (rational(e.get("onset")) or Fraction(0), e.get("staff_label", ""), e["event_id"])):
            onset, duration = rational(event.get("onset")), rational(event.get("duration"))
            beat = onset + 1 if onset is not None else None
            ins = instruments.get(event.get("instrument_id"), {})
            pitches = event.get("pitches", [])
            midi = "; ".join(literal(p.get("midi")) for p in pitches) if pitches else "不含音高"
            row = [number, bar.get("source", {}).get("page", ""), ins.get("name", "未知"),
                   event.get("staff_label", ""), float(beat) if beat is not None else None,
                   float(duration) if duration is not None else None, literal(beat), literal(duration),
                   techniques.get(event.get("technique_id"), "未知"), midi,
                   "; ".join(p.get("written_text", "") for p in pitches),
                   flag(event.get("is_roll")), flag(event.get("is_accent")), flag(event.get("is_ornament")),
                   {"none": "無", "start": "開始", "continue": "延續", "stop": "結束"}.get(event.get("tie"), "未知"),
                   flag(event.get("attack")), literal(event.get("dynamic")), literal(event.get("slur")),
                   event.get("raw_symbol", ""), bar["time_signature"], bar["tempo_quarter"],
                   "、".join(membership.get(number, [])), event.get("instrument_id"), event.get("technique_id"),
                   event["event_id"], event.get("observation_id"), event.get("run_id"),
                   event.get("instrument_text"), event.get("technique_text"), packed(pitches),
                   packed(event.get("training_mask", {})), event.get("source_asset_id"), packed(event.get("source_bbox"))]
            original_rows.append(row)
            event_rows[event["event_id"]] = row

    segments, played, route, rests = [], [], [], []
    for segment in payload["segments"]:
        order = orders[segment["segment_id"]]
        confirmed, unknown, continued = 0, 0, 0
        counts = Counter()
        for position, ref in enumerate(order, 1):
            number = ref["source_bar"]
            if number not in bars:
                raise ContractError(f"段落演奏順序引用了未匯出的小節 {number}")
            counts[number] += 1
            prefix = [segment["name"], position, counts[number], ref.get("pass_index"), ref.get("sequence_step"), ref.get("route_phase")]
            route.append([segment["name"], position, number, bars[number].get("source", {}).get("page", ""),
                          counts[number], ref.get("pass_index"), ref.get("sequence_step"), ref.get("route_phase"),
                          len(bars[number]["events"]), segment["segment_id"]])
            for event in bars[number]["events"]:
                attack = event.get("attack")
                confirmed += attack is True
                unknown += attack is None
                continued += attack is False
                played.append(prefix + event_rows[event["event_id"]])
        ranges = segment.get("ranges") or [{"start_bar": segment["start_bar"], "end_bar": segment["end_bar"]}]
        written = " → ".join(str(r["start_bar"]) if r["start_bar"] == r["end_bar"] else f"{r['start_bar']}–{r['end_bar']}" for r in ranges)
        segments.append([segment["name"], "自訂順序" if segment.get("kind") == "sequence" else "一般段落",
                         written, len(segment_bars(segment)), len(order), confirmed + unknown, confirmed, unknown,
                         continued, flag(segment.get("verified_silent")), segment["segment_id"]])

    for number, bar in sorted(bars.items()):
        for rest in bar.get("rest_observations", []):
            onset, duration = rational(rest.get("onset")), rational(rest.get("duration"))
            rests.append([number, bar.get("source", {}).get("page", ""), rest.get("staff_label"),
                          rest.get("instrument_text"), "整小節休止" if rest["kind"] == "full_rest" else "休止",
                          float(onset + 1) if onset is not None else None,
                          float(duration) if duration is not None else None,
                          literal(onset + 1 if onset is not None else None), literal(duration), flag(rest.get("timing_known")),
                          bar["time_signature"], "、".join(membership.get(number, [])), rest["evidence_id"],
                          rest.get("instrument_id"), packed(rest.get("training_mask", {})),
                          rest.get("source_asset_id"), packed(rest.get("source_bbox"))])

    notes = [
        ["作品", payload["title"]], ["匯出時間", payload["exported_at"]],
        ["資料識別碼", payload["review_id"]], ["來源識別碼", payload.get("source_package_id")],
        ["用途", "這是核對資料的閱讀與統計快照；修改表格不會回寫校對專案。正式訓練保留 JSON。"],
        ["原譜事件", "每個原譜事件只列一次；所屬段落可有多個。延音延續也保留為事件。"],
        ["演奏事件", "依每段演奏順序重列事件，反覆與跳轉會增加列數；包含延音延續。"],
        ["段落擊打事件數", "確認新起音加起音未知的候選；延音延續不計。輪奏及和弦各算一筆，不是實際敲擊下數。"],
        ["原譜與段落統計", "同一原譜事件可被多段引用，段落加總不可當成原譜事件總數。"],
        ["拍點與時值", "第幾拍從 1 起，以四分音符為 1 單位，與校對介面相同；6/8 亦非以附點四分音符計數。"],
        ["精確分數", "精確欄為文字，避免被 Excel 當成日期；數值欄方便統計，三連音等請以分數為準。"],
        ["未知與否", "未知不是否或零；技法、音高、旗標與訓練欄位遮罩沿用 JSON，不補猜。"],
        ["休止紀錄", "證據針對來源聲部；未定位休止保留空白。整小節休止範圍依拍號，不能把缺資料當全休止。"],
        ["全段確認無聲", "只有 verified_silent=true 才顯示是；單看擊打事件數為 0 不足以證明。"],
        ["演奏順序", "段內演奏小節序從 1 起；同小節第幾次是該段內出現次數，與反覆遍次不同。"],
        ["路線階段", "0 首次、1 返回後、2 Coda 尾奏；未適用保持空白。自訂順序不猜反覆遍次。"],
        ["CSV", "壓縮包內每表一個 CSV，UTF-8 BOM 可直接用 Excel 開啟；分數／拍號文字及可能被當公式的文字前加單引號，保護原意。"],
    ]
    return [Table("段落統計", ["段落", "類型", "登記範圍／自訂順序", "原譜小節數", "演奏小節數",
                  "擊打事件數（含起音待確認）", "確認新起音", "起音待確認", "延音延續事件數", "全段確認無聲", "段落ID"], segments),
            Table("原譜事件", EVENT_HEADERS, original_rows),
            Table("演奏事件", ["段落", "段內演奏小節序", "同小節第幾次", "反覆遍次", "自訂範圍序", "路線階段"] + EVENT_HEADERS, played),
            Table("休止紀錄", ["原譜小節", "頁碼", "來源聲部", "原譜樂器文字", "種類", "第幾拍（從1起）",
                  "時值（四分音符=1）", "拍點精確分數", "時值精確分數", "休止範圍已知", "拍號", "所屬段落",
                  "證據ID", "樂器ID", "訓練欄位遮罩", "來源圖片ID", "來源位置框"], rests),
            Table("演奏順序", ["段落", "段內演奏小節序", "原譜小節", "頁碼", "同小節第幾次", "反覆遍次",
                  "自訂範圍序", "路線階段", "本次事件列數", "段落ID"], route),
            Table("欄位說明", ["項目", "說明"], notes)]


def column_name(number):
    name = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        name = chr(65 + remainder) + name
    return name


def column_width(header, rows, index):
    if header == "說明":
        return 105
    def length(value):
        return sum(2 if ord(ch) > 255 else 1 for ch in str(value or ""))
    return min(48, max(14, length(header) + 2, *(length(r[index]) + 2 for r in rows[:80])))


XMLNS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
ET.register_namespace("", XMLNS)


def xml_bytes(root):
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def xlsx_bytes(tables):
    """小型、具型別的 OpenXML 寫出器；字串一律 inlineStr，原符號不當公式。"""
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>' + ''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, len(tables) + 1)) + '</Types>')
        archive.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        workbook = ET.Element("workbook", xmlns=XMLNS, attrib={"xmlns:r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"})
        sheets = ET.SubElement(workbook, "sheets")
        for i, table in enumerate(tables, 1):
            ET.SubElement(sheets, "sheet", name=table.name, sheetId=str(i), attrib={"r:id": f"rId{i}"})
        archive.writestr("xl/workbook.xml", xml_bytes(workbook))
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + ''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(tables) + 1)) + '<Relationship Id="styles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        archive.writestr("xl/styles.xml", '''<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="1"><numFmt numFmtId="164" formatCode="0.######"/></numFmts><fonts count="2"><font><sz val="11"/><name val="Arial"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Arial"/></font></fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF304866"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"><alignment vertical="center"/></xf><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf><xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>''')
        for i, table in enumerate(tables, 1):
            if len(table.rows) > 1048575 or len(table.headers) > 16384:
                raise ContractError("表格超過 Excel 單張工作表容量，請縮小段落範圍後匯出")
            sheet = ET.Element("worksheet", xmlns=XMLNS)
            ET.SubElement(sheet, "dimension", ref=f"A1:{column_name(len(table.headers))}{len(table.rows) + 1}")
            view = ET.SubElement(ET.SubElement(sheet, "sheetViews"), "sheetView", workbookViewId="0", showGridLines="0")
            ET.SubElement(view, "pane", xSplit="2", ySplit="1", topLeftCell="C2", activePane="bottomRight", state="frozen")
            ET.SubElement(sheet, "sheetFormatPr", defaultRowHeight="23")
            cols = ET.SubElement(sheet, "cols")
            for index, header in enumerate(table.headers):
                ET.SubElement(cols, "col", min=str(index + 1), max=str(index + 1), width=str(column_width(header, table.rows, index)), customWidth="1")
            data = ET.SubElement(sheet, "sheetData")
            for row_index, values in enumerate([table.headers, *table.rows], 1):
                row = ET.SubElement(data, "row", r=str(row_index), **({"ht": "36", "customHeight": "1"} if row_index == 1 else {}))
                for col, value in enumerate(values, 1):
                    if value is None:
                        continue
                    cell = ET.SubElement(row, "c", r=f"{column_name(col)}{row_index}", s="1" if row_index == 1 else "0")
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        if isinstance(value, float) and not value.is_integer():
                            cell.set("s", "2")
                        ET.SubElement(cell, "v").text = str(value)
                    else:
                        text = str(value)
                        if len(text) > 32767:
                            raise ContractError("表格有超過 Excel 儲存格上限的文字，請改用 CSV 匯出")
                        cell.set("t", "inlineStr")
                        ET.SubElement(ET.SubElement(cell, "is"), "t", {"{http://www.w3.org/XML/1998/namespace}space": "preserve"}).text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
            if table.rows:
                ET.SubElement(sheet, "autoFilter", ref=f"A1:{column_name(len(table.headers))}{len(table.rows) + 1}")
            archive.writestr(f"xl/worksheets/sheet{i}.xml", xml_bytes(sheet))
    return stream.getvalue()


def csv_zip_bytes(tables):
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        for table in tables:
            text = StringIO(newline="")
            writer = csv.writer(text)
            for row in [table.headers, *table.rows]:
                writer.writerow(["'" + value if isinstance(value, str) and (
                    value.lstrip().startswith(("=", "+", "-", "@")) or re.fullmatch(r"\d+/\d+", value))
                    else value for value in row])
            archive.writestr(table.name + ".csv", text.getvalue().encode("utf-8-sig"))
    return stream.getvalue()


def export_tables(payload, path):
    path = Path(path)
    tables = make_tables(payload)
    if path.suffix.lower() == ".xlsx":
        data = xlsx_bytes(tables)
    elif path.suffix.lower() == ".zip":
        data = csv_zip_bytes(tables)
    else:
        raise ContractError("請選擇 Excel .xlsx 或 CSV 表格壓縮包 .zip")
    atomic_bytes(path, data)
    return {"segments": len(tables[0].rows), "events": len(tables[1].rows), "performed_events": len(tables[2].rows),
            "bars": len(payload["bars"]), "tables": len(tables)}
