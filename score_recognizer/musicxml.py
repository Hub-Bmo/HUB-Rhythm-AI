"""MusicXML 轉成既有候選資料；不把本機 OMR 的輸出視為人工核對。"""
from copy import deepcopy
from collections import Counter
from fractions import Fraction
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile

from .contract import ContractError, meter_length, validate_result


def read_score(path):
    path = Path(path)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            container = ET.fromstring(archive.read("META-INF/container.xml"))
            roots = [e.get("full-path") for e in container.iter() if e.tag.split("}")[-1] == "rootfile"]
            if not roots or not roots[0]:
                raise ContractError("MusicXML 壓縮檔沒有主文件")
            info = archive.getinfo(roots[0])
            if info.file_size > 50_000_000:
                raise ContractError("MusicXML 文件過大")
            raw = archive.read(info)
    else:
        if path.stat().st_size > 50_000_000:
            raise ContractError("MusicXML 文件過大")
        raw = path.read_bytes()
    root = ET.fromstring(raw)
    for element in root.iter():
        element.tag = element.tag.split("}")[-1]
    if root.tag != "score-partwise":
        raise ContractError("目前只支援 score-partwise MusicXML，請由 Audiveris 匯出此格式")
    return root


def rat(value):
    return {"n": value.numerator, "d": value.denominator} if value is not None else None


def measure_box(region, index, row, rows):
    left, right = region["boundaries"][index:index+2]
    return {"x": left, "y": row/rows, "width": right-left, "height": 1/rows}


def _number(element, name, default=None):
    value = element.findtext(name)
    return Fraction(value) if value is not None else default


def _pitch(note, clef):
    pitch = note.find("pitch")
    if pitch is None:
        return []
    step, octave = pitch.findtext("step"), pitch.findtext("octave")
    alter = pitch.findtext("alter") or "0"
    if step not in {"C","D","E","F","G","A","B"} or octave is None:
        return []
    semitones = Fraction(alter)
    value = (int(octave)+1)*12 + {"C":0,"D":2,"E":4,"F":5,"G":7,"A":9,"B":11}[step] + semitones
    midi = int(value) if value.denominator == 1 and 0 <= value <= 127 else None
    accidental = note.findtext("accidental") or ({"1":"sharp","-1":"flat","0":"natural"}.get(alter) if semitones else None)
    return [{"degree":None,"octave_shift":None,"accidental":accidental,"step":step,
             "octave":int(octave),"midi":midi,"clef":clef,"written_text":f"{step}{alter if semitones else ''}{octave}"}]


def import_musicxml(path, region):
    root = read_score(path)
    parts = root.findall("part")
    if not parts:
        raise ContractError("OMR 沒有輸出任何聲部，請檢查框選範圍與五線譜是否清楚")
    counts = [len(p.findall("measure")) for p in parts]
    if any(n != region["bar_count"] for n in counts):
        raise ContractError(f"OMR 找到的各聲部小節數為 {counts}，登記為 {region['bar_count']}。已保留 MusicXML；請先檢查小節分界、是否混入別的系統，不能自動猜測對齊。")
    definitions = {p.get("id"):p for p in root.findall("./part-list/score-part")}
    name_counts=Counter(p.findtext("part-name") for p in definitions.values())
    staves = {p.get("id"): max([int(s.text) for s in p.findall("./measure/attributes/staves")] or [1]) for p in parts}
    total_rows = sum(staves.values())
    result = {"notation":"staff","measures":[{"index":i,"time_signature":None,"key_signature":None,
              "tempo_quarter":None,"parts":[],"uncertainties":[]} for i in range(region["bar_count"])],
              "navigation":[],"warnings":["OMR 候選均待核對；結構檢查無法保證音符、漏音或符號辨識正確。"]}
    flags = {i:set() for i in range(region["bar_count"])}
    row_offset = 0
    for part in parts:
        pid = part.get("id")
        definition = definitions.get(pid)
        name = definition.findtext("part-name") if definition is not None else None
        generic=not name or bool(re.fullmatch(r"(?:Part|Voice|P|聲部)[ ._\d-]*",name,re.I))
        instruments = {e.get("id"): e.findtext("instrument-name") for e in definition.findall("score-instrument")} if definition is not None else {}
        count = staves[pid]
        divisions, current_meter, current_key = None, region.get("time_signature") or None, region.get("key_signature") or None
        clefs = {}
        open_endings = {}
        for index, measure in enumerate(part.findall("measure")):
            target = result["measures"][index]
            attributes = measure.find("attributes")
            if attributes is not None:
                divisions = _number(attributes,"divisions",divisions)
                if divisions is not None and divisions <= 0:
                    raise ContractError("MusicXML divisions 必須大於零")
                time = attributes.find("time")
                if time is not None:
                    top, bottom = time.findtext("beats"), time.findtext("beat-type")
                    try:
                        current_meter = f"{sum(int(n) for n in top.split('+'))}/{int(bottom)}"
                        meter_length(current_meter)
                    except (ValueError,AttributeError,TypeError,ContractError):
                        current_meter = None
                        flags[index].add("複合或未知拍號需核對")
                if attributes.find("key/fifths") is not None:
                    current_key = f"fifths={attributes.findtext('key/fifths')} {attributes.findtext('key/mode') or ''}".strip()
                transpose=attributes.find("transpose")
                if transpose is not None and any(Fraction(transpose.findtext(k) or "0") != 0 for k in ("chromatic","octave-change")):
                    raise ContractError("此 MusicXML 含移調記譜，目前未換算實音；請另行核對，或改用 OpenAI，不能把書寫音高當成實際音高。")
                for clef in attributes.findall("clef"):
                    clefs[clef.get("number","1")] = (clef.findtext("sign") or "") + (clef.findtext("line") or "")
                if attributes.find("measure-style/multiple-rest") is not None:
                    flags[index].add("有多小節休止，需核對原譜小節編號與範圍")
            for key,value in (("time_signature",current_meter),("key_signature",current_key)):
                if target[key] and value and target[key] != value:
                    flags[index].add(f"不同聲部的 {key} 不一致")
                elif value:
                    target[key] = value
            rows = []
            for staff in range(1,count+1):
                label = (name or "未辨明聲部") + (f"｜{pid}" if generic or name_counts[name]>1 else "") + (f"／第{staff}譜表" if count > 1 else "")
                rows.append({"staff_label":label,"instrument_text":None if generic else name,"status":"unclear",
                             "bbox":measure_box(region,index,row_offset+staff-1,total_rows),"events":[],"uncertainties":[]})
            if generic:
                flags[index].add("OMR 未能確認樂器名稱，請用原譜核對；不按聲部次序猜樂器")
            cursor, previous_onset = Fraction(0), {}
            active_dynamic = None
            voice_ends = {}
            voice_intervals = {}
            for element in measure:
                if element.tag in {"backup","forward"}:
                    if divisions is None:
                        raise ContractError("MusicXML 未提供 divisions，無法換算時間")
                    movement = _number(element,"duration",Fraction(0))/divisions
                    cursor += -movement if element.tag == "backup" else movement
                    if cursor < 0:
                        flags[index].add("聲部游標退到小節之前，時間位置需核對")
                    continue
                if element.tag == "direction":
                    dynamics = element.find("direction-type/dynamics")
                    if dynamics is not None and len(dynamics):
                        active_dynamic = dynamics[0].tag
                    sound = element.find("sound")
                    if sound is not None and sound.get("tempo"):
                        target["tempo_quarter"] = float(sound.get("tempo"))
                    metronome = element.find("direction-type/metronome")
                    if target["tempo_quarter"] is None and metronome is not None:
                        beats = metronome.findtext("per-minute")
                        unit = metronome.findtext("beat-unit")
                        units = {"whole":4,"half":2,"quarter":1,"eighth":.5,"16th":.25}
                        if beats and unit in units:
                            try:
                                dotted = len(metronome.findall("beat-unit-dot"))
                                target["tempo_quarter"] = float(beats)*units[unit]*sum(.5**i for i in range(dotted+1))
                            except ValueError:
                                flags[index].add("速度文字無法換算")
                    words = " ".join(e.text or "" for e in element.findall("direction-type/words"))
                    if words or element.find("direction-type/wedge") is not None or any(element.find(f"direction-type/{kind}") is not None for kind in ("coda","segno")) or (sound is not None and any(k in sound.attrib for k in ("dacapo","dalsegno","tocoda","fine"))):
                        marker = {"kind":"other","start_bar":region["start_bar"]+index,"end_bar":None,"passes":[],"times":None,
                                  "raw_text":words or ET.tostring(element,encoding="unicode"),"bbox":deepcopy(rows[0]["bbox"]),
                                  "uncertainties":["演奏指示／跳轉由 MusicXML 保留，需核對適用範圍"]}
                        result["navigation"].append(marker)
                        flags[index].add("有文字、漸強弱或跳轉指示需核對")
                    continue
                if element.tag != "note":
                    continue
                if divisions is None:
                    raise ContractError("MusicXML 未提供 divisions，無法換算音符時值")
                staff = int(element.findtext("staff") or "1")
                if not 1 <= staff <= count:
                    raise ContractError("MusicXML 譜表編號超出聲部定義")
                row = rows[staff-1]
                voice = (staff,element.findtext("voice") or "1")
                chord = element.find("chord") is not None
                onset = previous_onset.get(voice,cursor) if chord else cursor
                duration = _number(element,"duration")
                duration = duration/divisions if duration is not None else None
                grace = element.find("grace") is not None
                if not chord and not grace:
                    previous_onset[voice] = onset
                    if duration is not None:
                        cursor += duration
                        voice_ends[voice] = max(voice_ends.get(voice,Fraction(0)),cursor)
                        voice_intervals.setdefault(voice,[]).append((onset,cursor))
                if duration is None or duration <= 0:
                    duration = None
                    flags[index].add("裝飾音或音符缺少可確定時值")
                rest = element.find("rest") is not None
                note_ins = element.find("instrument")
                instrument = instruments.get(note_ins.get("id")) if note_ins is not None else name
                if generic and instrument==name:
                    instrument=None
                issues = []
                length = meter_length(current_meter)
                if onset < 0 or (length is not None and (onset >= length or duration is not None and onset+duration > length)):
                    issues.append("OMR 時間超出拍號，保留音符但清空不可靠起點／時值")
                    onset_value, duration_value = None,None
                    flags[index].add("音符起點或時值超出小節")
                else:
                    onset_value, duration_value = onset,duration
                ties = {e.get("type") for e in element.findall("tie")+element.findall("notations/tied")}
                tie = "continue" if {"start","stop"} <= ties else "start" if "start" in ties else "stop" if "stop" in ties else "none"
                slurs = [e.get("type", "unknown") for e in element.findall("notations/slur")]
                tremolo = element.find("notations/ornaments/tremolo")
                other_ornaments = [e for e in element.findall("notations/ornaments/*") if e.tag != "tremolo"]
                techniques = [e.tag for e in element.findall("notations/articulations/*")+element.findall("notations/technical/*")]
                if element.find("notations/fermata") is not None:
                    techniques.append("fermata")
                    flags[index].add("有延長記號，實際速度／停留時間需核對")
                if tremolo is not None:
                    techniques.append(f"tremolo:{tremolo.get('type','single')}:{tremolo.text or ''}")
                if element.find("unpitched") is not None:
                    pitches = []
                else:
                    pitches = _pitch(element,clefs.get(str(staff)))
                if not rest and not pitches and element.find("unpitched") is None:
                    issues.append("無法取得音高；不自動當成無音高樂器")
                    flags[index].add("音高無法確認")
                raw_symbol = "休止" if rest else " ".join(p["written_text"] for p in pitches) or element.findtext("notehead") or "無音高音符"
                event = {"kind":"rest" if rest else "note","raw_symbol":raw_symbol,
                         "instrument_text":instrument,"technique_text":",".join(techniques) or None,"onset":rat(onset_value),"duration":rat(duration_value),
                         "pitches":pitches,"is_roll":True if tremolo is not None else None,"is_accent":True if element.find("notations/articulations/accent") is not None or element.find("notations/articulations/strong-accent") is not None else None,
                         "is_ornament":True if grace or other_ornaments else None,"tie":tie,"slur":",".join(slurs) or None,
                         "dynamic":active_dynamic,"bbox":deepcopy(row["bbox"]),"uncertainties":issues+["OMR 音符位置僅定位至聲部小節；技法等未見標記保留未知，仍需看原圖"]}
                # chord 標記才合併，避免不同聲部的同時音被誤當和弦或重複刪除。
                if chord and row["events"] and row["events"][-1]["kind"] == "note" and row["events"][-1]["onset"] == event["onset"] and row["events"][-1]["duration"] == event["duration"] and row["events"][-1]["instrument_text"] == instrument:
                    row["events"][-1]["pitches"].extend(pitches)
                else:
                    row["events"].append(event)
                if grace or other_ornaments or element.find("notations/tuplet") is not None or tremolo is not None:
                    flags[index].add("裝飾音、連音或輪奏需對照原圖")
            length = meter_length(current_meter)
            if length is not None and any(end != length for end in voice_ends.values()) and measure.get("implicit") != "yes":
                flags[index].add("聲部拍數未填滿／超出拍號，可能漏音、休止或錯辨")
            for intervals in voice_intervals.values():
                covered=Fraction(0)
                for start,end in sorted(intervals):
                    if start != covered:
                        flags[index].add("同一聲部有時間空缺或重疊，請核對休止與節奏")
                    covered=max(covered,end)
            if not current_meter:
                flags[index].add("拍號尚未確認，無法檢查小節拍數")
            for row in rows:
                events = row["events"]
                full_rest = bool(events) and all(e["kind"] == "rest" for e in events) and all(e["onset"] is not None and e["duration"] is not None for e in events)
                if full_rest and length is not None:
                    intervals = sorted((Fraction(e["onset"]["n"],e["onset"]["d"]),Fraction(e["duration"]["n"],e["duration"]["d"])) for e in events)
                    covered = Fraction(0)
                    for start,duration in intervals:
                        if start > covered:
                            break
                        covered = max(covered,start+duration)
                    full_rest = covered == length
                else:
                    full_rest = False
                row["status"] = "full_rest" if full_rest else "events" if events else "unclear"
                if full_rest:
                    row["events"] = []
                if not events:
                    flags[index].add("聲部沒有音符或休止，不能直接當成整小節休止")
                row["uncertainties"] = ["OMR 的樂器名稱、譜表與符號仍待核對"]
                target["parts"].append(row)
            for barline in measure.findall("barline"):
                marker_box = deepcopy(rows[0]["bbox"])
                repeat = barline.find("repeat")
                if repeat is not None:
                    flags[index].add("有反覆記號，需核對跨頁起訖與演奏次數")
                    start = repeat.get("direction") == "forward"
                    result["navigation"].append({"kind":"repeat_start" if start else "repeat_end","start_bar":region["start_bar"]+index,"end_bar":None,
                        "passes":[],"times":int(repeat.get("times") or "2") if not start else None,"raw_text":ET.tostring(barline,encoding="unicode"),"bbox":marker_box,"uncertainties":["需與跨頁反覆／結尾配對"]})
                ending = barline.find("ending")
                if ending is not None:
                    flags[index].add("有第幾次結尾，需核對跨頁起訖與適用次數")
                    passes = [int(n) for n in re.findall(r"\d+",ending.get("number", ""))]
                    signature = tuple(passes)
                    if ending.get("type") == "start":
                        open_endings[signature] = region["start_bar"]+index
                    elif ending.get("type") in {"stop","discontinue"}:
                        start = open_endings.pop(signature,None)
                        result["navigation"].append({"kind":"ending","start_bar":start or region["start_bar"]+index,"end_bar":region["start_bar"]+index,"passes":passes,"times":None,
                            "raw_text":ET.tostring(ending,encoding="unicode"),"bbox":marker_box,"uncertainties":[] if start else ["結尾起點可能在其他範圍，尚未配對"]})
            if measure.find("attributes/measure-style/measure-repeat") is not None or measure.find("attributes/measure-style/slash") is not None:
                flags[index].add("小節重複／斜線記譜尚未展開，不可當成完整敲擊事件")
        for passes,start in open_endings.items():
            result["navigation"].append({"kind":"ending","start_bar":start,"end_bar":None,"passes":list(passes),"times":None,"raw_text":"MusicXML ending start","bbox":measure_box(region,start-region["start_bar"],row_offset,total_rows),"uncertainties":["結尾終點尚未配對，可能跨範圍"]})
        row_offset += count
    # 同一總譜的各聲部會重複回報導覽；保留一份，疑問合併。
    markers = {}
    for marker in result["navigation"]:
        key = (marker["kind"],marker["start_bar"],marker["end_bar"],tuple(marker["passes"]),marker["times"],marker["raw_text"] if marker["kind"] == "other" else "")
        markers.setdefault(key,marker)
    result["navigation"] = list(markers.values())
    review = [{"index":i,"bar":region["start_bar"]+i,"reasons":sorted(reasons)} for i,reasons in flags.items() if reasons]
    for item in review:
        result["measures"][item["index"]]["uncertainties"].extend(item["reasons"])
    validate_result(result,region)
    return result, review
