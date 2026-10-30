"""Lossless source snapshots, explicit mapping rules and reproducible token exports.

Only Python's standard library is required. Source IDs are never interpreted as
cross-piece identities. A projection belongs to an export profile, not a source.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from fractions import Fraction
from hashlib import sha256
import json
from pathlib import Path
import unicodedata
from uuid import uuid4


FIELDS = {
    "instrument": "樂器", "technique": "擊法／音色變體", "pitch": "音高／鼓位",
    "dynamic": "力度", "is_roll": "輪奏", "is_accent": "重音",
    "is_ornament": "裝飾音", "tie": "延音線", "slur": "圓滑線",
}
OPTIONAL = {"duration": "時值", **{k: v for k, v in FIELDS.items() if k != "instrument"}, "tempo": "速度"}
FLAGS = {"is_roll", "is_accent", "is_ornament"}
PREFIX = {"instrument": "INST", "technique": "TECH", "pitch": "PITCH", "dynamic": "DYN",
          "is_roll": "ROLL", "is_accent": "ACCENT", "is_ornament": "ORNAMENT", "tie": "TIE", "slur": "SLUR"}
PPQ = 960


class TokenizerError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def normalized(value):
    return " ".join(unicodedata.normalize("NFKC", str(value)).strip().casefold().split())


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink()


def rational(value):
    if not isinstance(value, dict) or type(value.get("n")) is not int or type(value.get("d")) is not int:
        raise TokenizerError("時間必須是整數分子／分母")
    if value["n"] < 0 or value["d"] <= 0:
        raise TokenizerError("時間分子不可負，分母須大於零")
    return Fraction(value["n"], value["d"])


def ticks(value):
    result = value * PPQ
    if result.denominator != 1:
        raise TokenizerError(f"時間 {value} 無法精確表示為 PPQ {PPQ}；未自動四捨五入")
    return int(result)


def meter_ticks(meter):
    try:
        top, bottom = map(int, meter.split("/"))
        if top <= 0 or bottom <= 0 or bottom & (bottom - 1):
            raise ValueError
        return ticks(Fraction(top * 4, bottom))
    except (ValueError, AttributeError, ZeroDivisionError) as exc:
        raise TokenizerError(f"拍號無效：{meter}") from exc


def validate_source(data):
    if not isinstance(data, dict) or data.get("format") != "musicai-reviewed-training" or data.get("schema_version") != "1.0":
        raise TokenizerError("目前接受 musicai-reviewed-training 1.0 的已校對 JSON")
    if type(data.get("ppq")) is not int or data["ppq"] <= 0:
        raise TokenizerError("來源 PPQ 必須是正整數")
    if not data.get("segments") or not isinstance(data.get("bars"), list):
        raise TokenizerError("來源需要段落與小節資料")
    numbers = [bar.get("number") for bar in data["bars"]]
    if any(type(n) is not int or n < 1 for n in numbers) or len(set(numbers)) != len(numbers):
        raise TokenizerError("來源小節編號必須是唯一的正整數")
    for bar in data["bars"]:
        length = meter_ticks(bar.get("time_signature"))
        if not isinstance(bar.get("events"), list):
            raise TokenizerError(f"小節 {bar['number']} 缺少事件陣列")
        for event in bar["events"]:
            if not isinstance(event, dict) or event.get("kind", "note") != "note":
                raise TokenizerError("events 只接受音符事件；休止請保存在休止證據欄位")
            if event.get("review_status", "accepted") != "accepted":
                raise TokenizerError("來源包含尚未接受的事件，請先完成校對")
            mask = event.get("training_mask")
            if not isinstance(mask, dict):
                raise TokenizerError(f"小節 {bar['number']} 缺少 training_mask")
            if mask.get("onset") is not True:
                raise TokenizerError(f"小節 {bar['number']} 有未知起點，須先回校對程式確認")
            onset = ticks(rational(event.get("onset")))
            if onset >= length:
                raise TokenizerError(f"小節 {bar['number']} 有超出範圍的起點")
            if mask.get("duration") is True:
                duration = ticks(rational(event.get("duration")))
                if duration <= 0 or onset + duration > length:
                    raise TokenizerError(f"小節 {bar['number']} 的時值為零或超出範圍")
    segment_ids = [s.get("segment_id") for s in data["segments"]]
    if any(not isinstance(s, str) or not s for s in segment_ids) or len(set(segment_ids)) != len(segment_ids):
        raise TokenizerError("段落需要唯一的 segment_id")
    for segment in data["segments"]:
        playback_order(data, segment)


def playback_order(data, segment):
    order = segment.get("playback_order")
    if order is None:
        if segment.get("kind") == "sequence":
            raise TokenizerError("自訂順序段落缺少 playback_order，未猜測演奏路線")
        first, last = segment.get("start_bar"), segment.get("end_bar")
        if type(first) is not int or type(last) is not int or first > last:
            raise TokenizerError("段落範圍無效")
        order = [item for item in data.get("playback_order", []) if first <= item.get("source_bar", -1) <= last]
    if not isinstance(order, list) or not order or len(order) > 10000:
        raise TokenizerError("段落演奏順序不可空白，且不得超過 10,000 小節")
    available = {bar["number"] for bar in data["bars"]}
    for item in order:
        if not isinstance(item, dict) or item.get("source_bar") not in available:
            raise TokenizerError("演奏順序引用了未匯出的小節")
    return order


def observations(data):
    instruments = {i["instrument_id"]: i for i in data.get("instruments", [])}
    for bar in data["bars"]:
        for index, event in enumerate(bar["events"]):
            ins = instruments.get(event.get("instrument_id"), {})
            instrument = event.get("instrument_text") or ins.get("name") or event.get("staff_label") or "未辨明樂器"
            mask = event["training_mask"]
            values = {"instrument": instrument}
            stroke = next((s for s in ins.get("strokes", []) if s.get("technique_id") == event.get("technique_id")), {})
            values["technique"] = stroke.get("name") if mask.get("technique") is True else None
            values["technique"] = values["technique"] or event.get("technique_text")
            pitch_items = event.get("pitches") or []
            if pitch_items:
                if mask.get("pitches_midi") is True and all(type(p.get("midi")) is int for p in pitch_items):
                    values["pitch"] = "MIDI:" + ",".join(str(p["midi"]) for p in pitch_items)
                elif mask.get("pitches_degree") is True:
                    values["pitch"] = "音級:" + canonical([{k: p.get(k) for k in ("degree", "octave_shift", "accidental")} for p in pitch_items])
                else:
                    texts = [p.get("written_text", "") for p in pitch_items]
                    values["pitch"] = "譜面:" + canonical(texts) if any(texts) else None
            for field in FLAGS:
                values[field] = str(event[field]).lower() if mask.get(field) is True and type(event.get(field)) is bool else None
            values["tie"] = event.get("tie") if mask.get("tie") is True and event.get("tie") != "unknown" else None
            values["dynamic"] = event.get("dynamic")
            values["slur"] = event.get("slur") if mask.get("slur") is True else None
            yield bar, event, instrument, values, index


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / "tokenizer-library.json"
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if self.data.get("schema_version") != "1.0" or self.data.get("format") != "musicai-tokenizer-library":
                raise TokenizerError("不支援的 token 工作庫版本")
            self._saved_revision = self.data["revision"]
        else:
            self._saved_revision = None
            self.data = {"format": "musicai-tokenizer-library", "schema_version": "1.0", "revision": 0,
                         "pieces": {}, "concepts": {field: [] for field in FIELDS}, "rules": [],
                         "profiles": {}, "active_profile": "basic", "vocabulary": ["<PAD>", "<BOS>", "<EOS>"]}
            self.data["profiles"]["basic"] = self.new_profile("純節奏版（輪奏等先關閉）")
            for field in FLAGS:
                for value, label in (("true", "有" + FIELDS[field]), ("false", "無" + FIELDS[field])):
                    concept = self.add_concept(field, label)
                    self.set_rule(field, value, "", concept["id"], scope="global")
            for value, label in (("none", "無延音線"), ("start", "延音開始"), ("continue", "延音延續"), ("stop", "延音結束")):
                concept = self.add_concept("tie", label)
                self.set_rule("tie", value, "", concept["id"], scope="global")

    @staticmethod
    def new_profile(name):
        return {"name": name, "fields": {field: field in {"duration", "tempo"} for field in OPTIONAL},
                "projections": {field: {} for field in FIELDS}}

    @property
    def profile(self):
        return self.data["profiles"][self.data["active_profile"]]

    def save(self):
        if self.path.exists():
            disk_revision = json.loads(self.path.read_text(encoding="utf-8-sig"))["revision"]
            if disk_revision != self._saved_revision:
                raise TokenizerError("工作庫已由另一個視窗更新；請重新開啟工作庫後再修改，避免覆蓋他人的設定")
        elif self._saved_revision is not None:
            raise TokenizerError("工作庫檔案已被移走，請重新開啟工作庫")
        self.data["revision"] = (self._saved_revision or 0) + 1
        atomic_json(self.path, self.data)
        self._saved_revision = self.data["revision"]

    def import_file(self, path):
        path = Path(path).resolve()
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        validate_source(data)
        digest = sha256(canonical(data).encode("utf-8")).hexdigest()
        piece_id = "piece_" + digest[:20]
        existed = piece_id in self.data["pieces"]
        if not existed:
            self.data["pieces"][piece_id] = {"id": piece_id, "title": data.get("title") or path.stem,
                "source_path": str(path), "sha256": digest, "payload": data}
            self.save()
        return piece_id, not existed

    def add_concept(self, field, label):
        if field not in FIELDS or not str(label).strip():
            raise TokenizerError("請選擇欄位並輸入標準名稱")
        for concept in self.data["concepts"][field]:
            if normalized(concept["label"]) == normalized(label):
                return concept
        concept = {"id": f"C{len(self.data['concepts'][field]) + 1:05d}", "label": str(label).strip()}
        self.data["concepts"][field].append(concept)
        return concept

    def concept(self, field, concept_id):
        found = next((c for c in self.data["concepts"][field] if c["id"] == concept_id), None)
        if found is None:
            raise TokenizerError(f"找不到標準分類：{field}/{concept_id}")
        return found

    def rename_concept(self, field, concept_id, label):
        if not str(label).strip():
            raise TokenizerError("標準分類名稱不可空白")
        if any(c["id"] != concept_id and normalized(c["label"]) == normalized(label) for c in self.data["concepts"][field]):
            raise TokenizerError("已有相同名稱的分類；要合併請使用輸出合併設定")
        self.concept(field, concept_id)["label"] = str(label).strip()

    def set_rule(self, field, source, instrument, target, *, scope="piece", piece_id=None):
        self.concept(field, target)
        if scope not in {"piece", "global"} or (scope == "piece" and piece_id not in self.data["pieces"]):
            raise TokenizerError("對應規則範圍無效")
        context = "" if field in FLAGS or field in {"instrument", "tie"} else normalized(instrument)
        key = (field, normalized(source), context, scope, piece_id if scope == "piece" else None)
        rule = {"field": key[0], "source": key[1], "instrument": key[2], "scope": key[3], "piece_id": key[4], "target": target}
        self.data["rules"] = [r for r in self.data["rules"] if (r["field"], r["source"], r["instrument"], r["scope"], r["piece_id"]) != key]
        self.data["rules"].append(rule)

    def resolve(self, field, source, instrument, piece_id):
        if source is None:
            return None, "未知"
        context = "" if field in FLAGS or field in {"instrument", "tie"} else normalized(instrument)
        matching = [r for r in self.data["rules"] if r["field"] == field and r["source"] == normalized(source)
                    and r["instrument"] == context and (r["scope"] == "global" or r["piece_id"] == piece_id)]
        local = [r for r in matching if r["scope"] == "piece"]
        chosen = local or matching
        targets = {r["target"] for r in chosen}
        if len(targets) > 1:
            raise TokenizerError(f"對應規則衝突：{source}")
        return (next(iter(targets)), "本作品規則" if local else "共用規則") if targets else (None, "待確認")

    def project(self, field, concept_id, profile=None):
        profile = profile or self.profile
        seen = set()
        while concept_id in profile["projections"][field]:
            if concept_id in seen:
                raise TokenizerError("訓練合併設定形成循環")
            seen.add(concept_id)
            concept_id = profile["projections"][field][concept_id]
        self.concept(field, concept_id)
        return concept_id

    def set_projection(self, field, source_id, target_id):
        self.concept(field, source_id)
        self.concept(field, target_id)
        previous = deepcopy(self.profile["projections"][field])
        if source_id == target_id:
            self.profile["projections"][field].pop(source_id, None)
        else:
            self.profile["projections"][field][source_id] = target_id
        try:
            for concept in self.data["concepts"][field]:
                self.project(field, concept["id"])
        except Exception:
            self.profile["projections"][field] = previous
            raise

    def clone_profile(self, name):
        if not name.strip():
            raise TokenizerError("請填設定名稱")
        if any(normalized(p["name"]) == normalized(name) for p in self.data["profiles"].values()):
            raise TokenizerError("已有同名輸出設定，請使用不同名稱")
        profile_id = "profile_" + uuid4().hex[:10]
        self.data["profiles"][profile_id] = deepcopy(self.profile)
        self.data["profiles"][profile_id]["name"] = name.strip()
        self.data["active_profile"] = profile_id
        self.save()
        return profile_id

    def inventory(self, piece_ids=None):
        result = {}
        for piece_id in piece_ids if piece_ids is not None else self.data["pieces"]:
            piece = self.data["pieces"][piece_id]
            for bar, event, instrument, values, index in observations(piece["payload"]):
                for field, source in values.items():
                    if source is None:
                        continue
                    key = (piece_id, field, source, instrument if field != "instrument" else "")
                    if key not in result:
                        target, reason = self.resolve(field, source, instrument, piece_id)
                        result[key] = {"piece_id": piece_id, "field": field, "source": source, "instrument": instrument,
                            "target": target, "reason": reason, "count": 0, "bars": set(), "example": event.get("raw_symbol", "")}
                    result[key]["count"] += 1
                    result[key]["bars"].add(bar["number"])
        return list(result.values())

    def convert(self, piece_ids, *, preview=False):
        if not piece_ids:
            raise TokenizerError("請至少選擇一首作品")
        profile = deepcopy(self.profile)
        enabled = {"instrument"} | {f for f in FIELDS if profile["fields"].get(f)}
        unresolved = [row for row in self.inventory(piece_ids) if row["field"] in enabled and row["target"] is None]
        if unresolved:
            sample = "\n".join(f"{self.data['pieces'][r['piece_id']]['title']}｜{FIELDS[r['field']]}｜{r['source']}" for r in unresolved[:12])
            raise TokenizerError(f"已啟用欄位仍有 {len(unresolved)} 項待確認對應。\n{sample}\n可先關閉尚未處理的選填欄位。")
        sequences, warnings, collisions = [], [], []
        for piece_id in piece_ids:
            piece = self.data["pieces"][piece_id]
            data = piece["payload"]
            bar_map = {bar["number"]: bar for bar in data["bars"]}
            event_values = {(bar["number"], index): (event, instrument, values) for bar, event, instrument, values, index in observations(data)}
            for segment in data["segments"]:
                tokens, loss_mask, provenance = [], [], []

                def emit(token, known=True, origin=None):
                    tokens.append(token)
                    loss_mask.append(int(known))
                    provenance.append(origin)

                emit("<BOS>")
                emit("<SEGMENT_START>")
                cursor = 0
                source_bars, timeline = [], []
                for position, order_item in enumerate(playback_order(data, segment)):
                    number = order_item["source_bar"]
                    source_bars.append(number)
                    bar = bar_map[number]
                    origin = {"piece_id": piece_id, "source_bar": number, "playback_bar": position + 1,
                              "pass_index": order_item.get("pass_index", 1)}
                    length = meter_ticks(bar["time_signature"])
                    timeline.append({**origin, "start_tick": cursor, "length_ticks": length, "tempo_quarter": bar.get("tempo_quarter")})
                    emit("<BAR>", origin=origin)
                    emit(f"<METER_{bar['time_signature']}>", origin=origin)
                    if profile["fields"].get("tempo"):
                        tempo = bar.get("tempo_quarter")
                        if type(tempo) not in {int, float} or not 0 < tempo < 1000:
                            raise TokenizerError(f"小節 {number} 速度無效；可先關閉速度欄位")
                        emit(f"<TEMPO_{float(tempo):g}>", origin=origin)
                    groups = {}
                    for index, event in enumerate(bar["events"]):
                        onset = ticks(rational(event["onset"]))
                        groups.setdefault(onset, []).append((index, event))
                    local_cursor = 0
                    for onset, events in sorted(groups.items()):
                        emit(f"<TIME_SHIFT_{onset - local_cursor}>", origin=origin)
                        emit("<HIT_SET>", origin=origin)
                        prepared = []
                        for index, event in events:
                            _, instrument, values = event_values[(number, index)]
                            attrs = []
                            for field in FIELDS:
                                if field not in enabled:
                                    continue
                                target, reason = self.resolve(field, values.get(field), instrument, piece_id)
                                projected = self.project(field, target, profile) if target else None
                                token = f"<{PREFIX[field]}:{projected}>" if projected else f"<{PREFIX[field]}_UNKNOWN>"
                                attrs.append((token, projected is not None))
                            mask = event["training_mask"]
                            attack = event.get("attack") if mask.get("attack") is True else None
                            # A known continuation is never silently changed into a fresh hit.
                            if attack is None and mask.get("tie") is True:
                                attack = event.get("tie") in {"none", "start"} if event.get("tie") in {"none", "start", "continue", "stop"} else None
                            attrs.append(("<ATTACK_UNKNOWN>" if attack is None else ("<ATTACK_YES>" if attack else "<ATTACK_NO>"), attack is not None))
                            if profile["fields"].get("duration"):
                                duration = ticks(rational(event["duration"])) if mask.get("duration") is True and event.get("duration") is not None else None
                                attrs.append((f"<DURATION_{duration}>" if duration is not None else "<DURATION_UNKNOWN>", duration is not None))
                            prepared.append((attrs, index, event))
                        prepared.sort(key=lambda item: tuple(t for t, known in item[0]))
                        counts = Counter(tuple(t for t, known in item[0]) for item in prepared)
                        for signature, count in counts.items():
                            if count > 1:
                                collisions.append({**origin, "segment_id": segment["segment_id"], "tick": cursor + onset,
                                                   "event_tokens": list(signature), "count": count, "action": "preserve_all"})
                        for attrs, index, event in prepared:
                            event_origin = {**origin, "event_id": event.get("event_id"), "event_index": index,
                                            "tick": cursor + onset, "raw_symbol": event.get("raw_symbol")}
                            emit("<EVENT>", origin=event_origin)
                            for token, known in attrs:
                                emit(token, known, event_origin)
                            emit("<END_EVENT>", origin=event_origin)
                        emit("<END_HIT_SET>", origin=origin)
                        local_cursor = onset
                    # Preserve leading, internal and trailing silence, including empty bars.
                    emit(f"<TIME_SHIFT_{length - local_cursor}>", origin=origin)
                    emit("<END_BAR>", origin=origin)
                    cursor += length
                emit("<SEGMENT_END>")
                emit("<EOS>")
                sequences.append({"piece_id": piece_id, "title": piece["title"], "segment_id": segment["segment_id"],
                    "segment_name": segment.get("name", ""), "group_id": data.get("source_package_id") or data.get("review_id") or piece_id,
                    "source_bars": source_bars, "total_ticks": cursor, "timeline": timeline,
                    "tokens": tokens, "loss_mask": loss_mask, "provenance": provenance})
        if collisions:
            warnings.append(f"{len(collisions)} 組同時事件轉換後相同；全部保留，未自動去重。")
        warnings.append("本格式為國樂 tokenizer v1，尚未驗證與既有 GMD vocab／tokenizer 相容；接入基礎模型前須對齊。")
        warnings.append("同作品、同來源套件及重複引用小節必須留在同一資料切分；此工具不自動切訓練／驗證集。")
        if not profile["fields"].get("is_roll"):
            warnings.append("輪奏欄位已關閉：只保留原譜事件，不展開成連打；此版本不包含輪奏的演奏資訊。")
        return {"sequences": sequences, "warnings": warnings, "collisions": collisions, "profile": profile,
                "unknown_tokens": sum(sum(1 - value for value in s["loss_mask"]) for s in sequences)}

    def export(self, piece_ids, parent):
        result = self.convert(piece_ids)
        parent = Path(parent).resolve()
        # Dedicated new output folders prevent accidental source/workspace overwrites.
        output_name = datetime.now().strftime("tokens-%Y%m%d-%H%M%S-") + uuid4().hex[:6]
        folder = parent / (".pending-" + output_name)
        folder.mkdir(parents=True, exist_ok=False)
        vocab = list(self.data["vocabulary"])
        ids = {token: index for index, token in enumerate(vocab)}
        for sequence in result["sequences"]:
            for token in sequence["tokens"]:
                if token not in ids:
                    ids[token] = len(vocab)
                    vocab.append(token)
            sequence["token_ids"] = [ids[t] for t in sequence["tokens"]]
        self.data["vocabulary"] = vocab
        self.save()
        rule_snapshot = {"concepts": deepcopy(self.data["concepts"]), "rules": deepcopy(self.data["rules"]),
                         "profile": result["profile"], "profile_id": self.data["active_profile"]}
        source_info = [{k: v for k, v in self.data["pieces"][p].items() if k != "payload"} for p in piece_ids]
        manifest = {"format": "musicai-token-sequences", "schema_version": "1.0", "tokenizer_version": "0.1.0",
                    "created_at": datetime.now(timezone.utc).isoformat(), "ppq": PPQ, "source_time_unit": "quarter_note_fraction",
                    "library_revision": self.data["revision"], "profile": result["profile"], "sources": source_info,
                    "rules_sha256": sha256(canonical(rule_snapshot).encode("utf-8")).hexdigest(),
                    "vocab_sha256": sha256(canonical(vocab).encode("utf-8")).hexdigest(),
                    "sequence_count": len(result["sequences"]), "token_count": sum(len(s["tokens"]) for s in result["sequences"]),
                    "vocabulary_size": len(vocab), "unknown_tokens": result["unknown_tokens"],
                    "warnings": result["warnings"], "loss_mask_contract": "loss_mask[i] applies to token_ids[i]. For next-token training use targets=ids[1:] and target_mask=loss_mask[1:]. Do not use this as an attention mask.",
                    "compatibility": "Independent guoyue-v1 grammar; not yet aligned to existing GMD vocabulary."}
        atomic_json(folder / "vocab.json", {"tokens": vocab, "token_to_id": ids, "concepts": self.data["concepts"]})
        atomic_json(folder / "rules.json", rule_snapshot)
        atomic_json(folder / "report.json", {"warnings": result["warnings"], "collisions": result["collisions"]})
        with (folder / "sequences.jsonl").open("w", encoding="utf-8") as stream:
            for sequence in result["sequences"]:
                stream.write(json.dumps(sequence, ensure_ascii=False, allow_nan=False) + "\n")
        with (folder / "preview.txt").open("w", encoding="utf-8") as stream:
            stream.write(self.preview_text(result))
        # A final directory only appears after every file has been written.
        atomic_json(folder / "manifest.json", manifest)
        completed = parent / output_name
        folder.rename(completed)
        folder = completed
        return folder, manifest

    def preview_text(self, result, limit=None):
        labels = {f"<{PREFIX[f]}:{c['id']}>": f"<{FIELDS[f]}:{c['label']}>" for f in FIELDS for c in self.data["concepts"][f]}
        lines = [f"輸出設定：{result['profile']['name']}", "固定：PPQ 960／時間位置／拍號／小節／段落／同時事件／起音狀態",
                 "選填：" + "、".join(OPTIONAL[f] for f, enabled in result["profile"]["fields"].items() if enabled),
                 f"段落 {len(result['sequences'])}；未知欄位 token {result['unknown_tokens']}（loss_mask=0）", *result["warnings"], ""]
        for sequence in result["sequences"]:
            lines.append(f"{sequence['title']}｜{sequence['segment_name']}｜{len(sequence['source_bars'])} 演奏小節｜{len(sequence['tokens'])} tokens")
            shown = sequence["tokens"][:limit] if limit else sequence["tokens"]
            for token in shown:
                lines.append(labels.get(token, token))
            if limit and len(sequence["tokens"]) > limit:
                lines.append("……預覽截斷，正式匯出會包含完整資料。")
            lines.append("")
        return "\n".join(lines)
