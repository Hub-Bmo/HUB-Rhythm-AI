"""Cross-project rules for mapping printed symbols to known strokes.

The recognition provider is intentionally not allowed to extend the instrument
library.  A rule in this file only connects an observed, human-confirmed symbol
to an existing ``instrument_id`` / ``stroke_id`` pair.  IDs therefore remain
stable even when the display names in the instrument library are edited.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata
from typing import Mapping
from uuid import uuid4

from .instrument_library import (
    UNKNOWN_ID,
    get_instrument,
    get_stroke,
    validate_library,
)
from .recognition_contract import NOTATION_PROFILES
from .recognition import review_candidate


SYMBOL_MAPPING_SCHEMA_VERSION = "1.0"
SYMBOL_MAPPING_PATH_ENV = "GUOYUE_ANNOTATOR_SYMBOL_MAPPING_PATH"
DEFAULT_SYMBOL_MAPPING_FILENAME = "symbol-mappings.json"
MAPPABLE_NOTATION_PROFILES = frozenset(NOTATION_PROFILES - {"auto"})


class SymbolMappingError(ValueError):
    """A mapping rule or mapping file is invalid."""


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_observed_symbol(value: object) -> str:
    """Return the stable comparison key used for printed symbols.

    NFKC makes full-width digits and Latin characters comparable, ``casefold``
    handles case-insensitive textual symbols, and whitespace is collapsed.  We
    deliberately do not guess that different punctuation marks mean the same
    musical symbol; such aliases should be saved as separate confirmed rules.
    """

    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(normalized.strip().casefold().split())


def default_symbol_mapping_path() -> Path:
    """Return the AppData/config path shared by all projects."""

    configured = os.environ.get(SYMBOL_MAPPING_PATH_ENV)
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "win32":
        root = Path(os.environ.get("APPDATA") or Path.home())
        return root / "GuoyueRhythmAnnotator" / DEFAULT_SYMBOL_MAPPING_FILENAME
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "GuoyueRhythmAnnotator"
            / DEFAULT_SYMBOL_MAPPING_FILENAME
        )
    root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return root / "guoyue-rhythm-annotator" / DEFAULT_SYMBOL_MAPPING_FILENAME


def create_symbol_mappings() -> dict:
    timestamp = _now_iso()
    return {
        "schema_version": SYMBOL_MAPPING_SCHEMA_VERSION,
        "mapping_id": "global",
        "revision": 1,
        "created_at": timestamp,
        "updated_at": timestamp,
        "rules": [],
    }


def _profile(value: object) -> str:
    profile = normalize_observed_symbol(value)
    if profile not in MAPPABLE_NOTATION_PROFILES:
        allowed = "、".join(sorted(MAPPABLE_NOTATION_PROFILES))
        raise SymbolMappingError(f"譜面 profile 必須是：{allowed}")
    return profile


def _symbol(value: object, *, notation_profile: str | None = None) -> tuple[str, str]:
    if not isinstance(value, str):
        raise SymbolMappingError("辨識符號必須是文字")
    observed = value.strip()
    if len(observed) > 512:
        raise SymbolMappingError("辨識符號原始文字不可超過 512 個字元")
    normalized = normalize_observed_symbol(observed)
    if not normalized:
        raise SymbolMappingError("辨識符號不可空白")
    if len(normalized) > 128:
        raise SymbolMappingError("辨識符號不可超過 128 個字元")
    if notation_profile == "numbered" and _is_numbered_rest_symbol(normalized):
        raise SymbolMappingError("數字譜 0 是休止，不能對應成敲擊法")
    return observed, normalized


def _is_numbered_rest_symbol(value: str) -> bool:
    """Match a standalone zero with notation marks, but not prose or ``60``."""

    if re.findall(r"\d", value) != ["0"]:
        return False
    return not any(character.isalpha() for character in value)


def _validate_target(
    library: Mapping[str, object], instrument_id: object, stroke_id: object
) -> tuple[str, str]:
    validate_library(library)
    instrument = str(instrument_id or "").strip()
    stroke = str(stroke_id or "").strip()
    if not instrument or instrument == UNKNOWN_ID or get_instrument(library, instrument) is None:
        raise SymbolMappingError(f"對應規則引用不存在的樂器：{instrument or '<empty>'}")
    if not stroke or stroke == UNKNOWN_ID or get_stroke(library, instrument, stroke) is None:
        raise SymbolMappingError(
            f"對應規則的擊法不屬於指定樂器：{stroke or '<empty>'}"
        )
    return instrument, stroke


def _rule_key(rule: Mapping[str, object]) -> tuple[str, str, str]:
    return (
        str(rule.get("notation_profile") or ""),
        str(rule.get("instrument_id") or ""),
        str(rule.get("normalized_symbol") or ""),
    )


def validate_symbol_mappings(
    mappings: Mapping[str, object],
    library: Mapping[str, object] | None = None,
) -> None:
    """Validate file shape, unique IDs/keys, and optionally current library IDs."""

    if not isinstance(mappings, Mapping):
        raise SymbolMappingError("符號對應設定必須是 JSON object")
    if mappings.get("schema_version") != SYMBOL_MAPPING_SCHEMA_VERSION:
        raise SymbolMappingError(
            f"不支援的符號對應版本：{mappings.get('schema_version')!r}"
        )
    if not isinstance(mappings.get("mapping_id"), str) or not mappings.get("mapping_id"):
        raise SymbolMappingError("符號對應設定缺少 mapping_id")
    revision = mappings.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise SymbolMappingError("符號對應 revision 必須是正整數")
    rules = mappings.get("rules")
    if not isinstance(rules, list):
        raise SymbolMappingError("符號對應設定缺少 rules 陣列")
    if library is not None:
        validate_library(library)

    ids: set[str] = set()
    keys: set[tuple[str, str, str]] = set()
    for index, rule in enumerate(rules):
        context = f"rule[{index}]"
        if not isinstance(rule, Mapping):
            raise SymbolMappingError(f"{context} 必須是 JSON object")
        rule_id = rule.get("rule_id")
        if not isinstance(rule_id, str) or not rule_id.startswith("MAP-"):
            raise SymbolMappingError(f"{context} 含有無效的 rule_id")
        if rule_id in ids:
            raise SymbolMappingError(f"重複的 rule_id：{rule_id}")
        ids.add(rule_id)
        if not isinstance(rule.get("enabled", True), bool):
            raise SymbolMappingError(f"{context} 的 enabled 必須是布林值")

        profile = _profile(rule.get("notation_profile"))
        observed, normalized = _symbol(
            rule.get("observed_symbol"), notation_profile=profile
        )
        del observed
        if rule.get("normalized_symbol") != normalized:
            raise SymbolMappingError(f"{context} 的 normalized_symbol 與原始符號不一致")
        instrument_id = str(rule.get("instrument_id") or "").strip()
        stroke_id = str(rule.get("stroke_id") or "").strip()
        if library is not None:
            instrument_id, stroke_id = _validate_target(
                library, instrument_id, stroke_id
            )
        elif not instrument_id.startswith("ins_") or not stroke_id.startswith("stk_"):
            raise SymbolMappingError(f"{context} 含有無效的樂器或擊法 ID")

        key = (profile, instrument_id, normalized)
        if key in keys:
            raise SymbolMappingError(
                f"重複的符號對應規則：{profile} / {instrument_id} / {normalized}"
            )
        keys.add(key)


def _touch(mappings: dict) -> None:
    mappings["revision"] = int(mappings.get("revision", 0)) + 1
    mappings["updated_at"] = _now_iso()


def find_mapping_rule(
    mappings: Mapping[str, object],
    *,
    notation_profile: object,
    instrument_id: object,
    observed_symbol: object,
    include_disabled: bool = False,
) -> dict | None:
    """Return the one exact composite-key match, without guessing aliases."""

    try:
        profile = _profile(notation_profile)
        _observed, normalized = _symbol(
            observed_symbol, notation_profile=profile
        )
    except SymbolMappingError:
        return None
    key = (profile, str(instrument_id or "").strip(), normalized)
    matches = [
        rule
        for rule in mappings.get("rules", [])  # type: ignore[union-attr]
        if isinstance(rule, dict)
        and (include_disabled or rule.get("enabled", True) is True)
        and _rule_key(rule) == key
    ]
    return matches[0] if len(matches) == 1 else None


def upsert_mapping_rule(
    mappings: dict,
    library: Mapping[str, object],
    *,
    notation_profile: object,
    instrument_id: object,
    observed_symbol: object,
    stroke_id: object,
) -> dict:
    """Create or update one composite-key rule while preserving its ID."""

    validate_symbol_mappings(mappings, library)
    profile = _profile(notation_profile)
    observed, normalized = _symbol(observed_symbol, notation_profile=profile)
    instrument, stroke = _validate_target(library, instrument_id, stroke_id)
    existing = find_mapping_rule(
        mappings,
        notation_profile=profile,
        instrument_id=instrument,
        observed_symbol=observed,
        include_disabled=True,
    )
    if existing is not None:
        changed = (
            existing.get("stroke_id") != stroke
            or existing.get("observed_symbol") != observed
            or existing.get("enabled", True) is not True
        )
        if changed:
            existing["stroke_id"] = stroke
            existing["observed_symbol"] = observed
            existing["normalized_symbol"] = normalized
            existing["enabled"] = True
            existing["updated_at"] = _now_iso()
            _touch(mappings)
            validate_symbol_mappings(mappings, library)
        return existing

    timestamp = _now_iso()
    rule = {
        "rule_id": f"MAP-{uuid4().hex}",
        "notation_profile": profile,
        "instrument_id": instrument,
        "observed_symbol": observed,
        "normalized_symbol": normalized,
        "stroke_id": stroke,
        "enabled": True,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    mappings.setdefault("rules", []).append(rule)
    _touch(mappings)
    validate_symbol_mappings(mappings, library)
    return rule


def update_mapping_rule(
    mappings: dict,
    library: Mapping[str, object],
    rule_id: str,
    *,
    notation_profile: object | None = None,
    instrument_id: object | None = None,
    observed_symbol: object | None = None,
    stroke_id: object | None = None,
    enabled: bool | None = None,
) -> dict:
    """Edit a rule by stable ID, rejecting a duplicate composite key."""

    validate_symbol_mappings(mappings, library)
    matches = [
        rule
        for rule in mappings.get("rules", [])
        if isinstance(rule, dict) and rule.get("rule_id") == rule_id
    ]
    if len(matches) != 1:
        raise SymbolMappingError(f"找不到符號對應規則：{rule_id}")
    rule = matches[0]
    profile = _profile(
        rule["notation_profile"] if notation_profile is None else notation_profile
    )
    observed, normalized = _symbol(
        rule["observed_symbol"] if observed_symbol is None else observed_symbol,
        notation_profile=profile,
    )
    instrument, stroke = _validate_target(
        library,
        rule["instrument_id"] if instrument_id is None else instrument_id,
        rule["stroke_id"] if stroke_id is None else stroke_id,
    )
    key = (profile, instrument, normalized)
    if any(
        isinstance(other, Mapping)
        and other.get("rule_id") != rule_id
        and _rule_key(other) == key
        for other in mappings.get("rules", [])
    ):
        raise SymbolMappingError("相同譜面、樂器與符號的對應規則已存在")

    replacement = {
        **rule,
        "notation_profile": profile,
        "instrument_id": instrument,
        "observed_symbol": observed,
        "normalized_symbol": normalized,
        "stroke_id": stroke,
        "enabled": rule.get("enabled", True) if enabled is None else enabled,
    }
    if not isinstance(replacement["enabled"], bool):
        raise SymbolMappingError("enabled 必須是布林值")
    if all(rule.get(key_name) == replacement.get(key_name) for key_name in replacement):
        return rule
    rule.update(replacement)
    rule["updated_at"] = _now_iso()
    _touch(mappings)
    validate_symbol_mappings(mappings, library)
    return rule


def remove_mapping_rule(mappings: dict, library: Mapping[str, object], rule_id: str) -> bool:
    validate_symbol_mappings(mappings, library)
    rules = mappings.get("rules", [])
    next_rules = [
        rule
        for rule in rules
        if not (isinstance(rule, Mapping) and rule.get("rule_id") == rule_id)
    ]
    if len(next_rules) == len(rules):
        return False
    mappings["rules"] = next_rules
    _touch(mappings)
    validate_symbol_mappings(mappings, library)
    return True


def candidate_observed_symbol(candidate: Mapping[str, object]) -> str | None:
    """Return the symbol used for a rule, preferring human-corrected data."""

    data = candidate.get("data")
    if candidate.get("review_status") == "corrected":
        review = candidate.get("review")
        if isinstance(review, Mapping) and isinstance(review.get("corrected_data"), Mapping):
            data = review["corrected_data"]
    if isinstance(data, Mapping):
        raw_symbol = data.get("raw_symbol")
        if isinstance(raw_symbol, str) and normalize_observed_symbol(raw_symbol):
            return raw_symbol
    evidence = candidate.get("source_evidence")
    if isinstance(evidence, Mapping):
        observed_text = evidence.get("observed_text")
        if isinstance(observed_text, str) and normalize_observed_symbol(observed_text):
            return observed_text
    return None


def mapping_suggestion_for_candidate(
    candidate: Mapping[str, object],
    *,
    notation_profile: object,
    mappings: Mapping[str, object],
    library: Mapping[str, object],
) -> dict | None:
    """Return a non-mutating correction suggestion for one event candidate.

    This function never changes ``review_status`` and never edits the candidate.
    Callers can show the returned suggestion in the review UI, then explicitly
    pass it to :func:`apply_mapping_rule_to_candidate` after user confirmation.
    """

    validate_symbol_mappings(mappings, library)
    if candidate.get("kind") != "event":
        return None

    data = candidate.get("data")
    if candidate.get("review_status") == "corrected":
        review = candidate.get("review")
        if isinstance(review, Mapping) and isinstance(review.get("corrected_data"), Mapping):
            data = review["corrected_data"]
    if not isinstance(data, Mapping):
        return None

    instrument_id = str(data.get("instrument_id") or "").strip()
    if get_instrument(library, instrument_id) is None:
        return None
    observed = candidate_observed_symbol(candidate)
    if observed is None:
        return None
    rule = find_mapping_rule(
        mappings,
        notation_profile=notation_profile,
        instrument_id=instrument_id,
        observed_symbol=observed,
    )
    if rule is None:
        return None
    stroke_id = str(rule.get("stroke_id") or "")
    if get_stroke(library, instrument_id, stroke_id) is None:
        return None
    current_stroke = str(data.get("stroke_id") or "").strip()
    # A provider can return a valid but semantically wrong stroke.  The
    # human-confirmed rule is still shown as a suggestion in that case; this
    # function never mutates or accepts the candidate.  When both already
    # agree there is nothing to correct.
    if current_stroke == stroke_id:
        return None
    corrected_data = deepcopy(dict(data))
    corrected_data["stroke_id"] = stroke_id
    return {
        "rule_id": rule["rule_id"],
        "notation_profile": str(rule["notation_profile"]),
        "instrument_id": instrument_id,
        "observed_symbol": observed,
        "normalized_symbol": str(rule["normalized_symbol"]),
        "stroke_id": stroke_id,
        "corrected_data": corrected_data,
    }


def apply_mapping_rule_to_candidate(
    candidate: Mapping[str, object],
    *,
    notation_profile: object,
    mappings: Mapping[str, object],
    library: Mapping[str, object],
    note: str = "",
    reviewed_at: str | None = None,
) -> dict:
    """Explicitly accept a mapping suggestion as a corrected candidate.

    The provider's original ``data`` remains untouched for audit history;
    mapped data is stored in ``review.corrected_data`` using the same semantics
    as any other human correction.  A non-match returns an equivalent copy.
    """

    # Global rules are offered to fresh candidates.  Already accepted,
    # corrected, or rejected records are audit history and must only change via
    # a separate, deliberate re-review workflow.
    if candidate.get("review_status", "pending") != "pending":
        return deepcopy(dict(candidate))

    suggestion = mapping_suggestion_for_candidate(
        candidate,
        notation_profile=notation_profile,
        mappings=mappings,
        library=library,
    )
    if suggestion is None:
        return deepcopy(dict(candidate))
    rule_note = f"依全域符號對應規則 {suggestion['rule_id']} 修正擊法"
    clean_note = str(note).strip()
    result = review_candidate(
        candidate,
        "corrected",
        corrected_data=suggestion["corrected_data"],
        note=f"{clean_note}\n{rule_note}" if clean_note else rule_note,
        reviewed_at=reviewed_at,
    )
    result["applied_mapping_rule_id"] = suggestion["rule_id"]
    return result


def save_symbol_mappings(
    mappings: Mapping[str, object],
    path: str | Path,
    library: Mapping[str, object],
) -> Path:
    """Validate and atomically persist global mapping rules."""

    validate_symbol_mappings(mappings, library)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(mappings, ensure_ascii=False, indent=2) + "\n"
    descriptor = -1
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, destination)
        temporary_name = None
    except OSError as error:
        raise SymbolMappingError(f"無法儲存符號對應設定：{destination}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass
    return destination


def load_symbol_mappings(
    path: str | Path, library: Mapping[str, object]
) -> dict:
    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except UnicodeError as error:
        raise SymbolMappingError(f"符號對應設定不是有效的 UTF-8：{source}") from error
    except json.JSONDecodeError as error:
        raise SymbolMappingError(f"無法解析符號對應 JSON：{source}") from error
    except OSError as error:
        raise SymbolMappingError(f"無法讀取符號對應設定：{source}") from error
    validate_symbol_mappings(raw, library)
    return raw


class SymbolMappingStore:
    """Path wrapper for a cross-project mapping JSON file."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else default_symbol_mapping_path()

    def load(
        self,
        library: Mapping[str, object],
        *,
        create_if_missing: bool = True,
    ) -> dict:
        if self.path.exists():
            return load_symbol_mappings(self.path, library)
        if not create_if_missing:
            raise FileNotFoundError(self.path)
        mappings = create_symbol_mappings()
        self.save(mappings, library)
        return mappings

    def save(
        self, mappings: Mapping[str, object], library: Mapping[str, object]
    ) -> Path:
        return save_symbol_mappings(mappings, self.path, library)


__all__ = [
    "DEFAULT_SYMBOL_MAPPING_FILENAME",
    "MAPPABLE_NOTATION_PROFILES",
    "SYMBOL_MAPPING_PATH_ENV",
    "SYMBOL_MAPPING_SCHEMA_VERSION",
    "SymbolMappingError",
    "SymbolMappingStore",
    "apply_mapping_rule_to_candidate",
    "candidate_observed_symbol",
    "create_symbol_mappings",
    "default_symbol_mapping_path",
    "find_mapping_rule",
    "load_symbol_mappings",
    "mapping_suggestion_for_candidate",
    "normalize_observed_symbol",
    "remove_mapping_rule",
    "save_symbol_mappings",
    "update_mapping_rule",
    "upsert_mapping_rule",
    "validate_symbol_mappings",
]
