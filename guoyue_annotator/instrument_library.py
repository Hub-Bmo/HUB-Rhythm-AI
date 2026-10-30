"""跨專案的樂器與擊法主庫。

這個模組故意只使用一般 ``dict`` / ``list``，讓主庫與專案快照都能
直接寫入 JSON。顯示名稱和別名可以改變，``instrument_id`` 與
``stroke_id`` 則在建立後保持不變。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import unicodedata
from typing import Iterable, Mapping
from uuid import UUID, uuid4, uuid5

from .models import BRUSH_COLORS, INSTRUMENT_PRESETS


LIBRARY_SCHEMA_VERSION = "1.0"
PROJECT_SNAPSHOT_KEY = "instrument_library_snapshot"
UNKNOWN_ID = "unknown"
LIBRARY_PATH_ENV = "GUOYUE_ANNOTATOR_LIBRARY_PATH"
DEFAULT_LIBRARY_FILENAME = "instrument-library.json"
DEFAULT_LIBRARY_CONTENT_REVISION = 2

# 這個 namespace 是檔案格式的一部分，不可更換；預設項目因此可在
# 不同電腦上獲得相同的穩定 ID。
_DEFAULT_ID_NAMESPACE = UUID("1d4651ab-29eb-4ee4-9294-c59620156f81")


class InstrumentLibraryError(ValueError):
    """樂器庫格式或操作錯誤。"""


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _lookup_key(value: object) -> str:
    """將人類輸入轉成別名比對鍵，但不改變實際儲存的文字。"""
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(normalized.strip().casefold().split())


def _stable_id(prefix: str, seed: str) -> str:
    return f"{prefix}_{uuid5(_DEFAULT_ID_NAMESPACE, seed).hex}"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _clean_aliases(values: Iterable[object], *reserved: object) -> list[str]:
    reserved_keys = {_lookup_key(value) for value in reserved}
    result: list[str] = []
    used = set(reserved_keys)
    for value in values:
        alias = str(value).strip()
        key = _lookup_key(alias)
        if not key or key in used:
            continue
        used.add(key)
        result.append(alias)
    return result


def default_library_path() -> Path:
    """取得全域主庫的預設位置。

    測試或可攜式執行可以傳入 ``InstrumentLibraryStore(path)``，或設定
    ``GUOYUE_ANNOTATOR_LIBRARY_PATH``，不必觸碰使用者真正的全域資料。
    """
    configured = os.environ.get(LIBRARY_PATH_ENV)
    if configured:
        return Path(configured).expanduser()

    if sys.platform == "win32":
        root = Path(os.environ.get("APPDATA") or Path.home())
        return root / "GuoyueRhythmAnnotator" / DEFAULT_LIBRARY_FILENAME
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "GuoyueRhythmAnnotator"
            / DEFAULT_LIBRARY_FILENAME
        )
    root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return root / "guoyue-rhythm-annotator" / DEFAULT_LIBRARY_FILENAME


def create_default_library(
    presets: Mapping[str, Mapping[str, object]] | None = None,
) -> dict:
    """將現有 ``INSTRUMENT_PRESETS`` 轉成有穩定 ID 的預設主庫。"""
    preset_source = presets if presets is not None else INSTRUMENT_PRESETS
    created_at = _now_iso()
    instruments: list[dict] = []
    for instrument_name, raw_preset in preset_source.items():
        name = str(instrument_name).strip()
        instrument_id = _stable_id("ins", f"preset-instrument:{_lookup_key(name)}")
        strokes: list[dict] = []
        raw_brushes = raw_preset.get("brushes", [])
        for index, raw_brush in enumerate(raw_brushes):
            if isinstance(raw_brush, Mapping):
                code = str(raw_brush.get("code") or f"stroke_{index + 1}").strip()
                label = str(raw_brush.get("label") or code).strip()
                color = str(
                    raw_brush.get("color")
                    or BRUSH_COLORS[index % len(BRUSH_COLORS)]
                )
            else:
                try:
                    code, label = raw_brush  # type: ignore[misc]
                except (TypeError, ValueError) as error:
                    raise InstrumentLibraryError(
                        f"樂器「{name}」的第 {index + 1} 個擊法格式無效"
                    ) from error
                code = str(code).strip()
                label = str(label).strip()
                color = BRUSH_COLORS[index % len(BRUSH_COLORS)]
            stroke_id = _stable_id(
                "stk", f"preset-stroke:{instrument_id}:{_lookup_key(code)}"
            )
            strokes.append(
                {
                    "stroke_id": stroke_id,
                    "code": code,
                    "name": label or code,
                    "aliases": [],
                    "color": color,
                }
            )
        instruments.append(
            {
                "instrument_id": instrument_id,
                "name": name,
                "aliases": [],
                "sound_profile": str(raw_preset.get("sound_profile") or "bell"),
                "strokes": strokes,
            }
        )

    library = {
        "schema_version": LIBRARY_SCHEMA_VERSION,
        "library_id": "global",
        "revision": 1,
        "default_content_revision": DEFAULT_LIBRARY_CONTENT_REVISION,
        "created_at": created_at,
        "updated_at": created_at,
        "instruments": instruments,
    }
    validate_library(library)
    return library


# 較明確的轉換器名稱，便於匯入其他預設集。
library_from_presets = create_default_library


def validate_library(library: Mapping[str, object], *, snapshot: bool = False) -> None:
    """驗證主庫或專案快照；發現重複 ID 時直接拒絕載入。"""
    if not isinstance(library, Mapping):
        raise InstrumentLibraryError("樂器庫必須是 JSON object")
    if library.get("schema_version") != LIBRARY_SCHEMA_VERSION:
        raise InstrumentLibraryError(
            f"不支援的樂器庫版本：{library.get('schema_version')!r}"
        )
    if not snapshot:
        if not isinstance(library.get("library_id"), str):
            raise InstrumentLibraryError("樂器庫缺少 library_id")
        revision = library.get("revision")
        if not isinstance(revision, int) or revision < 1:
            raise InstrumentLibraryError("樂器庫 revision 必須是正整數")

    instruments = library.get("instruments")
    if not isinstance(instruments, list):
        raise InstrumentLibraryError("樂器庫缺少 instruments 陣列")

    instrument_ids: set[str] = set()
    stroke_ids: set[str] = set()
    instrument_lookup: dict[str, str] = {}
    for instrument in instruments:
        if not isinstance(instrument, Mapping):
            raise InstrumentLibraryError("樂器資料必須是 JSON object")
        instrument_id = instrument.get("instrument_id")
        if not isinstance(instrument_id, str) or not instrument_id.startswith("ins_"):
            raise InstrumentLibraryError(f"無效的 instrument_id：{instrument_id!r}")
        if instrument_id in instrument_ids:
            raise InstrumentLibraryError(f"重複的 instrument_id：{instrument_id}")
        instrument_ids.add(instrument_id)

        name = instrument.get("name")
        if not isinstance(name, str) or not name.strip():
            raise InstrumentLibraryError(f"{instrument_id} 缺少樂器名稱")
        aliases = instrument.get("aliases", [])
        if not isinstance(aliases, list) or not all(
            isinstance(alias, str) for alias in aliases
        ):
            raise InstrumentLibraryError(f"{instrument_id} 的 aliases 格式無效")
        for label in (name, *aliases):
            key = _lookup_key(label)
            owner = instrument_lookup.get(key)
            if owner is not None and owner != instrument_id:
                raise InstrumentLibraryError(
                    f"樂器名稱或別名重複：{label!r} 同時屬於 {owner} 與 {instrument_id}"
                )
            instrument_lookup[key] = instrument_id

        strokes = instrument.get("strokes")
        if not isinstance(strokes, list):
            raise InstrumentLibraryError(f"{instrument_id} 缺少 strokes 陣列")
        stroke_lookup: dict[str, str] = {}
        for stroke in strokes:
            if not isinstance(stroke, Mapping):
                raise InstrumentLibraryError(f"{instrument_id} 含有無效的擊法")
            stroke_id = stroke.get("stroke_id")
            if not isinstance(stroke_id, str) or not stroke_id.startswith("stk_"):
                raise InstrumentLibraryError(f"無效的 stroke_id：{stroke_id!r}")
            if stroke_id in stroke_ids:
                raise InstrumentLibraryError(f"重複的 stroke_id：{stroke_id}")
            stroke_ids.add(stroke_id)
            code = stroke.get("code")
            stroke_name = stroke.get("name")
            if not isinstance(code, str) or not code.strip():
                raise InstrumentLibraryError(f"{stroke_id} 缺少 code")
            if not isinstance(stroke_name, str) or not stroke_name.strip():
                raise InstrumentLibraryError(f"{stroke_id} 缺少擊法名稱")
            stroke_aliases = stroke.get("aliases", [])
            if not isinstance(stroke_aliases, list) or not all(
                isinstance(alias, str) for alias in stroke_aliases
            ):
                raise InstrumentLibraryError(f"{stroke_id} 的 aliases 格式無效")
            for label in (code, stroke_name, *stroke_aliases):
                key = _lookup_key(label)
                owner = stroke_lookup.get(key)
                if owner is not None and owner != stroke_id:
                    raise InstrumentLibraryError(
                        f"{instrument_id} 的擊法名稱或別名重複：{label!r}"
                    )
                stroke_lookup[key] = stroke_id


def _touch(library: dict) -> None:
    library["revision"] = int(library.get("revision", 0)) + 1
    library["updated_at"] = _now_iso()


def get_instrument(library: Mapping[str, object], instrument_id: str) -> dict | None:
    for instrument in library.get("instruments", []):  # type: ignore[union-attr]
        if instrument.get("instrument_id") == instrument_id:
            return instrument
    return None


def get_stroke(
    library: Mapping[str, object], instrument_id: str, stroke_id: str
) -> dict | None:
    instrument = get_instrument(library, instrument_id)
    if instrument is None:
        return None
    return next(
        (stroke for stroke in instrument.get("strokes", []) if stroke.get("stroke_id") == stroke_id),
        None,
    )


def resolve_instrument(library: Mapping[str, object], reference: object) -> str:
    """只回傳已存在的 instrument_id；無法唯一對應時回傳 unknown。"""
    if reference is None:
        return UNKNOWN_ID
    text = str(reference).strip()
    if not text:
        return UNKNOWN_ID
    instruments = library.get("instruments", [])
    exact = [
        instrument
        for instrument in instruments  # type: ignore[union-attr]
        if instrument.get("instrument_id") == text
    ]
    if len(exact) == 1:
        return exact[0]["instrument_id"]

    key = _lookup_key(text)
    matches: list[str] = []
    for instrument in instruments:  # type: ignore[union-attr]
        labels = [instrument.get("name", ""), *instrument.get("aliases", [])]
        if key in {_lookup_key(label) for label in labels}:
            matches.append(instrument["instrument_id"])
    return matches[0] if len(set(matches)) == 1 else UNKNOWN_ID


def resolve_stroke(
    library: Mapping[str, object], instrument_reference: object, reference: object
) -> str:
    """在指定樂器內對應擊法，不會在別的樂器中猜測。"""
    instrument_id = resolve_instrument(library, instrument_reference)
    instrument = get_instrument(library, instrument_id)
    if instrument is None or reference is None:
        return UNKNOWN_ID
    text = str(reference).strip()
    if not text:
        return UNKNOWN_ID
    exact = [
        stroke
        for stroke in instrument.get("strokes", [])
        if stroke.get("stroke_id") == text
    ]
    if len(exact) == 1:
        return exact[0]["stroke_id"]

    key = _lookup_key(text)
    matches: list[str] = []
    for stroke in instrument.get("strokes", []):
        labels = [
            stroke.get("code", ""),
            stroke.get("name", ""),
            *stroke.get("aliases", []),
        ]
        if key in {_lookup_key(label) for label in labels}:
            matches.append(stroke["stroke_id"])
    return matches[0] if len(set(matches)) == 1 else UNKNOWN_ID


def resolve_ai_candidate(
    library: Mapping[str, object],
    instrument_reference: object,
    stroke_reference: object | None = None,
) -> dict:
    """
    將 AI 的名稱或 ID 回應收旂為庫內 ID。

    此函式永遠不會新增項目。AI 即使回傳看似合法的 ``ins_*`` /
    ``stk_*``，只要不存在於當前主庫或快照，結果仍然是 ``unknown``。
    """
    instrument_id = resolve_instrument(library, instrument_reference)
    if instrument_id == UNKNOWN_ID:
        return {
            "status": "unknown_instrument",
            "instrument_id": UNKNOWN_ID,
            "stroke_id": UNKNOWN_ID,
        }
    if stroke_reference is None or not str(stroke_reference).strip():
        return {
            "status": "resolved_instrument",
            "instrument_id": instrument_id,
            "stroke_id": UNKNOWN_ID,
        }
    stroke_id = resolve_stroke(library, instrument_id, stroke_reference)
    if stroke_id == UNKNOWN_ID:
        return {
            "status": "unknown_stroke",
            "instrument_id": instrument_id,
            "stroke_id": UNKNOWN_ID,
        }
    return {
        "status": "resolved",
        "instrument_id": instrument_id,
        "stroke_id": stroke_id,
    }


def _ensure_unique_instrument_labels(
    library: Mapping[str, object], labels: Iterable[object], excluding: str | None = None
) -> None:
    for label in labels:
        resolved = resolve_instrument(library, label)
        if resolved != UNKNOWN_ID and resolved != excluding:
            raise InstrumentLibraryError(f"樂器名稱或別名已存在：{label!r}")


def add_instrument(
    library: dict,
    name: str,
    *,
    sound_profile: str = "bell",
    aliases: Iterable[str] = (),
) -> dict:
    """新增樂器；系統產生 ID，後續改名不會改變它。"""
    clean_name = str(name).strip()
    if not clean_name:
        raise InstrumentLibraryError("樂器名稱不可空白")
    clean_aliases = _clean_aliases(aliases, clean_name)
    _ensure_unique_instrument_labels(library, [clean_name, *clean_aliases])
    instrument = {
        "instrument_id": _new_id("ins"),
        "name": clean_name,
        "aliases": clean_aliases,
        "sound_profile": str(sound_profile or "bell"),
        "strokes": [],
    }
    library.setdefault("instruments", []).append(instrument)
    _touch(library)
    validate_library(library)
    return instrument


def update_instrument(
    library: dict,
    instrument_id: str,
    *,
    name: str | None = None,
    sound_profile: str | None = None,
    aliases: Iterable[str] | None = None,
) -> dict:
    """更新可編輯欄位；``instrument_id`` 不在可編輯範圍內。"""
    instrument = get_instrument(library, instrument_id)
    if instrument is None:
        raise InstrumentLibraryError(f"找不到樂器：{instrument_id}")
    next_name = str(name).strip() if name is not None else instrument["name"]
    if not next_name:
        raise InstrumentLibraryError("樂器名稱不可空白")
    alias_source = instrument.get("aliases", []) if aliases is None else aliases
    next_aliases = _clean_aliases(alias_source, next_name)
    _ensure_unique_instrument_labels(
        library, [next_name, *next_aliases], excluding=instrument_id
    )
    instrument["name"] = next_name
    instrument["aliases"] = next_aliases
    if sound_profile is not None:
        instrument["sound_profile"] = str(sound_profile or "bell")
    _touch(library)
    validate_library(library)
    return instrument


def add_instrument_alias(library: dict, instrument_id: str, alias: str) -> dict:
    instrument = get_instrument(library, instrument_id)
    if instrument is None:
        raise InstrumentLibraryError(f"找不到樂器：{instrument_id}")
    aliases = [*instrument.get("aliases", []), alias]
    return update_instrument(library, instrument_id, aliases=aliases)


def _ensure_unique_stroke_labels(
    instrument: Mapping[str, object],
    labels: Iterable[object],
    excluding: str | None = None,
) -> None:
    for label in labels:
        key = _lookup_key(label)
        for stroke in instrument.get("strokes", []):  # type: ignore[union-attr]
            if stroke.get("stroke_id") == excluding:
                continue
            existing = [
                stroke.get("code", ""),
                stroke.get("name", ""),
                *stroke.get("aliases", []),
            ]
            if key and key in {_lookup_key(value) for value in existing}:
                raise InstrumentLibraryError(f"擊法代碼、名稱或別名已存在：{label!r}")


def add_stroke(
    library: dict,
    instrument_id: str,
    code: str,
    name: str,
    *,
    aliases: Iterable[str] = (),
    color: str | None = None,
) -> dict:
    """新增擊法；每個 stroke_id 在整個主庫內唯一。"""
    instrument = get_instrument(library, instrument_id)
    if instrument is None:
        raise InstrumentLibraryError(f"找不到樂器：{instrument_id}")
    clean_code = str(code).strip()
    clean_name = str(name).strip()
    if not clean_code or not clean_name:
        raise InstrumentLibraryError("擊法 code 與名稱不可空白")
    clean_aliases = _clean_aliases(aliases, clean_code, clean_name)
    _ensure_unique_stroke_labels(
        instrument, [clean_code, clean_name, *clean_aliases]
    )
    index = len(instrument.get("strokes", []))
    stroke = {
        "stroke_id": _new_id("stk"),
        "code": clean_code,
        "name": clean_name,
        "aliases": clean_aliases,
        "color": str(color or BRUSH_COLORS[index % len(BRUSH_COLORS)]),
    }
    instrument.setdefault("strokes", []).append(stroke)
    _touch(library)
    validate_library(library)
    return stroke


def update_stroke(
    library: dict,
    instrument_id: str,
    stroke_id: str,
    *,
    code: str | None = None,
    name: str | None = None,
    aliases: Iterable[str] | None = None,
    color: str | None = None,
) -> dict:
    """更新可編輯欄位；``stroke_id`` 不在可編輯範圍內。"""
    instrument = get_instrument(library, instrument_id)
    stroke = get_stroke(library, instrument_id, stroke_id)
    if instrument is None or stroke is None:
        raise InstrumentLibraryError(f"找不到擊法：{stroke_id}")
    next_code = str(code).strip() if code is not None else stroke["code"]
    next_name = str(name).strip() if name is not None else stroke["name"]
    if not next_code or not next_name:
        raise InstrumentLibraryError("擊法 code 與名稱不可空白")
    alias_source = stroke.get("aliases", []) if aliases is None else aliases
    next_aliases = _clean_aliases(alias_source, next_code, next_name)
    _ensure_unique_stroke_labels(
        instrument,
        [next_code, next_name, *next_aliases],
        excluding=stroke_id,
    )
    stroke["code"] = next_code
    stroke["name"] = next_name
    stroke["aliases"] = next_aliases
    if color is not None:
        stroke["color"] = str(color)
    _touch(library)
    validate_library(library)
    return stroke


def add_stroke_alias(
    library: dict, instrument_id: str, stroke_id: str, alias: str
) -> dict:
    stroke = get_stroke(library, instrument_id, stroke_id)
    if stroke is None:
        raise InstrumentLibraryError(f"找不到擊法：{stroke_id}")
    aliases = [*stroke.get("aliases", []), alias]
    return update_stroke(
        library, instrument_id, stroke_id, aliases=aliases
    )


def create_project_snapshot(
    library: Mapping[str, object], instrument_ids: Iterable[str] | None = None
) -> dict:
    """
    建立與主庫分離的專案快照。

    預設複製全部樂器；傳入 ID 時只收錄專案真正使用的樂器。
    """
    validate_library(library)
    requested = None if instrument_ids is None else list(dict.fromkeys(instrument_ids))
    if requested is None:
        instruments = deepcopy(library.get("instruments", []))
    else:
        missing = [value for value in requested if get_instrument(library, value) is None]
        if missing:
            raise InstrumentLibraryError(
                f"快照含有不存在的樂器 ID：{', '.join(missing)}"
            )
        instruments = [
            deepcopy(get_instrument(library, value)) for value in requested
        ]
    snapshot = {
        "schema_version": LIBRARY_SCHEMA_VERSION,
        "source_library_id": library.get("library_id", "global"),
        "source_revision": library.get("revision", 1),
        "captured_at": _now_iso(),
        "instruments": instruments,
    }
    validate_library(snapshot, snapshot=True)
    return snapshot


def collect_project_instrument_ids(
    project: Mapping[str, object], library: Mapping[str, object]
) -> list[str]:
    """依軌道的 ID，或舊版 instrument 名稱，收集專案使用的樂器。"""
    result: list[str] = []
    for segment in project.get("segments", []):  # type: ignore[union-attr]
        for track in segment.get("tracks", []):
            instrument_id = resolve_instrument(
                library, track.get("instrument_id") or track.get("instrument")
            )
            if instrument_id != UNKNOWN_ID and instrument_id not in result:
                result.append(instrument_id)
    return result


def attach_project_snapshot(
    project: dict,
    library: Mapping[str, object],
    instrument_ids: Iterable[str] | None = None,
) -> dict:
    """將獨立快照寫入專案，回傳的就是專案內部那份資料。"""
    selected = (
        collect_project_instrument_ids(project, library)
        if instrument_ids is None
        else list(instrument_ids)
    )
    snapshot = create_project_snapshot(library, selected)
    project[PROJECT_SNAPSHOT_KEY] = snapshot
    return snapshot


def get_project_snapshot(project: Mapping[str, object]) -> dict | None:
    snapshot = project.get(PROJECT_SNAPSHOT_KEY)
    if snapshot is None:
        return None
    if not isinstance(snapshot, dict):
        raise InstrumentLibraryError("專案的樂器庫快照格式無效")
    validate_library(snapshot, snapshot=True)
    return snapshot


def bind_project_to_library(project: dict, library: Mapping[str, object]) -> dict:
    """
    將舊專案的 instrument/stroke 名稱加上穩定 ID。

    原有 ``instrument`` 和 ``stroke`` 字串保留，因此舊版播放與匯出仍可
    工作。找不到時只標示 ``unknown``，不會自動汙染主庫。
    """
    summary = {
        "tracks_resolved": 0,
        "tracks_unknown": 0,
        "strokes_resolved": 0,
        "strokes_unknown": 0,
    }
    for segment in project.get("segments", []):
        for track in segment.get("tracks", []):
            instrument_reference = track.get("instrument_id")
            instrument_id = resolve_instrument(library, instrument_reference)
            if instrument_id == UNKNOWN_ID:
                instrument_id = resolve_instrument(library, track.get("instrument"))
            track["instrument_id"] = instrument_id
            if instrument_id == UNKNOWN_ID:
                summary["tracks_unknown"] += 1
            else:
                summary["tracks_resolved"] += 1

            for brush in track.get("brushes", []):
                stroke_id = resolve_stroke(
                    library,
                    instrument_id,
                    brush.get("stroke_id") or brush.get("code") or brush.get("label"),
                )
                brush["stroke_id"] = stroke_id
                key = "strokes_resolved" if stroke_id != UNKNOWN_ID else "strokes_unknown"
                summary[key] += 1

            for take in track.get("takes", []):
                for event in take.get("events", []):
                    stroke_id = resolve_stroke(
                        library,
                        instrument_id,
                        event.get("stroke_id") or event.get("stroke"),
                    )
                    event["stroke_id"] = stroke_id
    return summary


def save_library(library: Mapping[str, object], path: str | Path) -> Path:
    """以 UTF-8 JSON 原子性地儲存主庫。"""
    validate_library(library)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(library, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def load_library(path: str | Path) -> dict:
    source = Path(path)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise InstrumentLibraryError(f"無法解析樂器庫 JSON：{source}") from error
    validate_library(data)
    return data


def upgrade_default_library_content(library: dict) -> bool:
    """Non-destructively add newly shipped strokes to matching built-ins."""

    try:
        current_revision = int(library.get("default_content_revision", 1))
    except (TypeError, ValueError):
        current_revision = 1
    if current_revision >= DEFAULT_LIBRARY_CONTENT_REVISION:
        return False

    defaults = create_default_library()
    for default_instrument in defaults.get("instruments", []):
        if not isinstance(default_instrument, Mapping):
            continue
        instrument_id = str(default_instrument.get("instrument_id") or "")
        current = get_instrument(library, instrument_id)
        if current is None:
            # A missing instrument may have been intentionally removed.  Only
            # enrich built-ins that are still present.
            continue
        existing_strokes = [
            stroke
            for stroke in current.get("strokes", [])
            if isinstance(stroke, Mapping)
        ]
        existing_ids = {
            str(stroke.get("stroke_id") or "") for stroke in existing_strokes
        }
        existing_codes = {
            _lookup_key(stroke.get("code")) for stroke in existing_strokes
        }
        for default_stroke in default_instrument.get("strokes", []):
            if not isinstance(default_stroke, Mapping):
                continue
            stroke_id = str(default_stroke.get("stroke_id") or "")
            stroke_code = _lookup_key(default_stroke.get("code"))
            # A custom stroke using the new built-in code takes precedence;
            # never create duplicate controls or overwrite its display data.
            if stroke_id in existing_ids or stroke_code in existing_codes:
                continue
            current.setdefault("strokes", []).append(
                deepcopy(dict(default_stroke))
            )
            existing_ids.add(stroke_id)
            existing_codes.add(stroke_code)

    library["default_content_revision"] = DEFAULT_LIBRARY_CONTENT_REVISION
    _touch(library)
    validate_library(library)
    return True


class InstrumentLibraryStore:
    """主庫 JSON 的路徑封裝；可在測試時指向暫存目錄。"""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else default_library_path()

    def load(self, *, create_if_missing: bool = True) -> dict:
        if self.path.exists():
            library = load_library(self.path)
            if upgrade_default_library_content(library):
                self.save(library)
            return library
        if not create_if_missing:
            raise FileNotFoundError(self.path)
        library = create_default_library()
        self.save(library)
        return library

    def save(self, library: Mapping[str, object]) -> Path:
        return save_library(library, self.path)

