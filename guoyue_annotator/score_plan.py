"""整份樂譜的頁面順序、跨頁來源區域與建議分段。

這個模組只處理可 JSON 序列化的 dict/list，不依賴 Tk 或影像套件。
因此版面分析、縮圖排序與專案儲存可共用同一份資料規則。

新欄位都是附加欄位：舊專案中沒有 ``page_sequence`` 或
``source_ref.sequence`` 時，儲存層會依現有 list 順序補齊，不會刪除不認識的欄位。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from numbers import Integral, Real
from typing import Iterable, Mapping, MutableMapping
from uuid import uuid4


PAGE_SEQUENCE_SOURCES = frozenset({"import", "manual", "detected"})
SUGGESTED_SEGMENT_SIZES = frozenset({4, 8})
SOURCE_REF_ORIGINS = frozenset(
    {"manual", "manual_layout", "detected", "ai", "legacy"}
)
SOURCE_REF_REVIEW_STATUSES = frozenset({"pending", "confirmed", "rejected"})


class ScorePlanError(ValueError):
    """整份樂譜的頁面或小節規劃無法安全建立。"""


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _positive_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool):
        raise ScorePlanError(f"{field_name} 必須是正整數")
    if isinstance(value, Integral):
        result = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        result = int(value.strip())
    else:
        raise ScorePlanError(f"{field_name} 必須是正整數")
    if result <= 0:
        raise ScorePlanError(f"{field_name} 必須是正整數")
    return result


def _sequence_or_none(value: object) -> int | None:
    try:
        return _positive_integer(value, "sequence")
    except ScorePlanError:
        return None


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScorePlanError(f"{field_name} 不可為空")
    return value.strip()


def _normalized_bbox(bbox: Mapping[str, Real]) -> dict[str, float]:
    if not isinstance(bbox, Mapping):
        raise ScorePlanError("bbox 必須是物件")
    keys = ("x", "y", "width", "height")
    if any(
        key not in bbox
        or isinstance(bbox[key], bool)
        or not isinstance(bbox[key], Real)
        for key in keys
    ):
        raise ScorePlanError("bbox 必須包含數值 x、y、width、height")
    result = {key: float(bbox[key]) for key in keys}
    if result["x"] < 0 or result["y"] < 0:
        raise ScorePlanError("bbox 位置不可為負")
    if result["width"] <= 0 or result["height"] <= 0:
        raise ScorePlanError("bbox 寬高必須大於 0")
    tolerance = 1e-9
    if result["x"] + result["width"] > 1.0 + tolerance:
        raise ScorePlanError("bbox 水平範圍超出照片")
    if result["y"] + result["height"] > 1.0 + tolerance:
        raise ScorePlanError("bbox 垂直範圍超出照片")
    return result


def migrate_page_metadata_in_place(assets: list[object]) -> None:
    """為舊照片資產補上穩定頁面順序。

    既有且不重複的正整數會保留。缺值、無效值或重複值依 list
    順序接在已用最大號後面，避免新匯入頁面插到已人工排序頁面之間。
    """

    used: set[int] = set()
    needs_sequence: list[MutableMapping[str, object]] = []
    for asset in assets:
        if not isinstance(asset, MutableMapping):
            continue
        sequence = _sequence_or_none(asset.get("page_sequence"))
        if sequence is None or sequence in used:
            needs_sequence.append(asset)
        else:
            asset["page_sequence"] = sequence
            used.add(sequence)
        asset.setdefault("printed_page_number", None)
        source = asset.get("page_sequence_source")
        if not isinstance(source, str) or not source.strip():
            asset["page_sequence_source"] = "import"

    next_sequence = max(used, default=0) + 1
    for asset in needs_sequence:
        while next_sequence in used:
            next_sequence += 1
        asset["page_sequence"] = next_sequence
        used.add(next_sequence)
        next_sequence += 1


def ordered_page_assets(
    assets: Iterable[Mapping[str, object]],
) -> list[dict[str, object]]:
    """依 ``page_sequence`` 回傳獨立副本；無效序號穩定排在最後。"""

    indexed = [(index, deepcopy(dict(asset))) for index, asset in enumerate(assets)]
    indexed.sort(
        key=lambda pair: (
            _sequence_or_none(pair[1].get("page_sequence")) is None,
            _sequence_or_none(pair[1].get("page_sequence")) or 0,
            pair[0],
        )
    )
    return [asset for _index, asset in indexed]


def reorder_page_assets(
    assets: Iterable[Mapping[str, object]],
    ordered_asset_ids: Iterable[str],
    *,
    source: str = "manual",
) -> list[dict[str, object]]:
    """依人工確認順序回傳新資產清單，不修改傳入物件。

    ``ordered_asset_ids`` 必須每張照片剛好出現一次，才不會在跨頁
    Segment 中靜默漏頁或重複頁。
    """

    records = [deepcopy(dict(asset)) for asset in assets]
    by_id: dict[str, dict[str, object]] = {}
    for asset in records:
        asset_id = _required_text(asset.get("asset_id"), "asset.asset_id")
        if asset_id in by_id:
            raise ScorePlanError(f"重複的 asset_id：{asset_id}")
        by_id[asset_id] = asset

    requested = [_required_text(value, "ordered_asset_ids") for value in ordered_asset_ids]
    if len(requested) != len(set(requested)):
        raise ScorePlanError("ordered_asset_ids 不可含重複頁面")
    missing = [asset_id for asset_id in by_id if asset_id not in requested]
    unknown = [asset_id for asset_id in requested if asset_id not in by_id]
    if missing or unknown:
        details = []
        if missing:
            details.append(f"缺少 {missing}")
        if unknown:
            details.append(f"不明 {unknown}")
        raise ScorePlanError("ordered_asset_ids 必須是全部頁面的排列：" + "；".join(details))

    sequence_source = _required_text(source, "source")
    result: list[dict[str, object]] = []
    for sequence, asset_id in enumerate(requested, start=1):
        asset = by_id[asset_id]
        asset["page_sequence"] = sequence
        asset["page_sequence_source"] = sequence_source
        asset.setdefault("printed_page_number", None)
        result.append(asset)
    return result


def migrate_source_refs_in_place(
    source_refs: list[object],
    *,
    legacy_start_bar: object = "",
    legacy_end_bar: object = "",
) -> None:
    """依舊 list 順序補上 source-ref sequence 及小節範圍欄位。"""

    used: set[int] = set()
    needs_sequence: list[MutableMapping[str, object]] = []
    for source_ref in source_refs:
        if not isinstance(source_ref, MutableMapping):
            continue
        sequence = _sequence_or_none(source_ref.get("sequence"))
        if sequence is None or sequence in used:
            needs_sequence.append(source_ref)
        else:
            source_ref["sequence"] = sequence
            used.add(sequence)
        source_ref.setdefault("source_start_bar", legacy_start_bar)
        source_ref.setdefault("source_end_bar", legacy_end_bar)
        # Segment-local coordinates cannot always be reconstructed from old
        # projects: older refs often repeated the whole Segment's global range
        # on every photographed page.  Preserve that evidence and leave the
        # local mapping explicitly unknown until a user/layout pass confirms it.
        source_ref.setdefault("segment_start_bar", None)
        source_ref.setdefault("segment_end_bar", None)
        source_ref.setdefault("system_id", None)
        source_ref.setdefault("origin", "legacy")
        source_ref.setdefault("review_status", "confirmed")

    next_sequence = max(used, default=0) + 1
    for source_ref in needs_sequence:
        while next_sequence in used:
            next_sequence += 1
        source_ref["sequence"] = next_sequence
        used.add(next_sequence)
        next_sequence += 1


def new_source_ref(
    asset_id: str,
    bbox: Mapping[str, Real],
    *,
    sequence: int,
    source_start_bar: int,
    source_end_bar: int,
    segment_start_bar: int | None = None,
    segment_end_bar: int | None = None,
    system_id: str | None = None,
    origin: str = "manual",
    review_status: str = "confirmed",
    region_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, object]:
    """建立一個可排序的跨頁 Segment 來源區域。"""

    start_bar = _positive_integer(source_start_bar, "source_start_bar")
    end_bar = _positive_integer(source_end_bar, "source_end_bar")
    if end_bar < start_bar:
        raise ScorePlanError("source_end_bar 不可早於 source_start_bar")
    if (segment_start_bar is None) != (segment_end_bar is None):
        raise ScorePlanError(
            "segment_start_bar 與 segment_end_bar 必須同時提供或同時留空"
        )
    local_start: int | None = None
    local_end: int | None = None
    if segment_start_bar is not None and segment_end_bar is not None:
        local_start = _positive_integer(segment_start_bar, "segment_start_bar")
        local_end = _positive_integer(segment_end_bar, "segment_end_bar")
        if local_end < local_start:
            raise ScorePlanError("segment_end_bar 不可早於 segment_start_bar")
        if local_end - local_start != end_bar - start_bar:
            raise ScorePlanError("Segment 局部範圍與原譜小節數不一致")
    normalized_region_id = (
        f"REG-{uuid4().hex[:16]}"
        if region_id is None
        else _required_text(region_id, "region_id")
    )
    return {
        "region_id": normalized_region_id,
        "asset_id": _required_text(asset_id, "asset_id"),
        "bbox": _normalized_bbox(bbox),
        "sequence": _positive_integer(sequence, "sequence"),
        "source_start_bar": start_bar,
        "source_end_bar": end_bar,
        "segment_start_bar": local_start,
        "segment_end_bar": local_end,
        "system_id": None if system_id is None else _required_text(system_id, "system_id"),
        "origin": _required_text(origin, "origin"),
        "review_status": _required_text(review_status, "review_status"),
        "created_at": created_at or _now_iso(),
    }


def ordered_source_refs(
    source_refs: Iterable[Mapping[str, object]],
) -> list[dict[str, object]]:
    """依跨頁順序回傳獨立副本；舊 ref 依原 list 順序排在最後。"""

    indexed = [(index, deepcopy(dict(source_ref))) for index, source_ref in enumerate(source_refs)]
    indexed.sort(
        key=lambda pair: (
            _sequence_or_none(pair[1].get("sequence")) is None,
            _sequence_or_none(pair[1].get("sequence")) or 0,
            pair[0],
        )
    )
    return [source_ref for _index, source_ref in indexed]


def with_segment_source_refs(
    segment: Mapping[str, object],
    source_refs: Iterable[Mapping[str, object]],
    *,
    require_contiguous: bool = True,
) -> dict[str, object]:
    """回傳連結多個跨頁區域的 Segment 副本。

    新連結會依 ``sequence`` 排序後重新編為 1..N。預設要求每個
    ref 的小節範圍剛好連續，因為靜默允許漏小節會讓 AI 結果無法
    確定前後位置。
    """

    refs = ordered_source_refs(source_refs)
    if not refs:
        raise ScorePlanError("Segment 至少需要一個 source_ref")

    seen_regions: set[str] = set()
    previous_end: int | None = None
    previous_local_end = 0
    for sequence, source_ref in enumerate(refs, start=1):
        region_id = _required_text(source_ref.get("region_id"), "source_ref.region_id")
        _required_text(source_ref.get("asset_id"), "source_ref.asset_id")
        try:
            source_ref["bbox"] = _normalized_bbox(source_ref.get("bbox"))
        except (TypeError, ScorePlanError) as error:
            raise ScorePlanError(f"source_ref {region_id} 的 bbox 無效：{error}") from error
        if region_id in seen_regions:
            raise ScorePlanError(f"重複的 region_id：{region_id}")
        seen_regions.add(region_id)
        start_bar = _positive_integer(
            source_ref.get("source_start_bar"), "source_ref.source_start_bar"
        )
        end_bar = _positive_integer(
            source_ref.get("source_end_bar"), "source_ref.source_end_bar"
        )
        if end_bar < start_bar:
            raise ScorePlanError("source_ref 的結束小節不可早於起始小節")
        if previous_end is not None:
            if start_bar <= previous_end:
                raise ScorePlanError("source_ref 的小節範圍重疊或前後順序錯誤")
            if require_contiguous and start_bar != previous_end + 1:
                raise ScorePlanError(
                    f"source_ref 在小節 {previous_end} 與 {start_bar} 之間有缺口"
                )
        local_start_value = source_ref.get("segment_start_bar")
        local_end_value = source_ref.get("segment_end_bar")
        if local_start_value is None and local_end_value is None:
            local_start = previous_local_end + 1
            local_end = local_start + (end_bar - start_bar)
        elif local_start_value is None or local_end_value is None:
            raise ScorePlanError(
                "source_ref.segment_start_bar 與 segment_end_bar 必須同時提供"
            )
        else:
            local_start = _positive_integer(
                local_start_value, "source_ref.segment_start_bar"
            )
            local_end = _positive_integer(
                local_end_value, "source_ref.segment_end_bar"
            )
            if local_end < local_start:
                raise ScorePlanError("source_ref 的 Segment 結束小節早於起始小節")
            if local_end - local_start != end_bar - start_bar:
                raise ScorePlanError("source_ref 的 Segment/原譜小節數不一致")
        if local_start != previous_local_end + 1:
            raise ScorePlanError(
                f"source_ref 的 Segment 局部小節在 {previous_local_end} 與 {local_start} 之間不連續"
            )
        source_ref["sequence"] = sequence
        source_ref["source_start_bar"] = start_bar
        source_ref["source_end_bar"] = end_bar
        source_ref["segment_start_bar"] = local_start
        source_ref["segment_end_bar"] = local_end
        system_id = source_ref.get("system_id")
        source_ref["system_id"] = (
            None if system_id is None else _required_text(system_id, "source_ref.system_id")
        )
        source_ref["origin"] = _required_text(
            source_ref.get("origin", "manual"), "source_ref.origin"
        )
        source_ref["review_status"] = _required_text(
            source_ref.get("review_status", "confirmed"), "source_ref.review_status"
        )
        previous_end = end_bar
        previous_local_end = local_end

    result = deepcopy(dict(segment))
    result["source_refs"] = refs
    result["source_start_bar"] = refs[0]["source_start_bar"]
    result["source_end_bar"] = refs[-1]["source_end_bar"]
    return result


def _repeat_interval(block: object) -> tuple[int, int]:
    if isinstance(block, Mapping):
        start_value = block.get("start_bar", block.get("source_start_bar"))
        end_value = block.get("end_bar", block.get("source_end_bar"))
    elif isinstance(block, (tuple, list)) and len(block) == 2:
        start_value, end_value = block
    else:
        raise ScorePlanError(
            "repeat block 必須是 (start, end) 或含 start_bar/end_bar 的物件"
        )
    start_bar = _positive_integer(start_value, "repeat.start_bar")
    end_bar = _positive_integer(end_value, "repeat.end_bar")
    if end_bar < start_bar:
        raise ScorePlanError("repeat.end_bar 不可早於 repeat.start_bar")
    return start_bar, end_bar


def _merged_repeat_intervals(
    repeat_blocks: Iterable[object],
    *,
    start_bar: int,
    end_bar: int,
) -> list[tuple[int, int]]:
    intervals = sorted(_repeat_interval(block) for block in repeat_blocks)
    for repeat_start, repeat_end in intervals:
        if repeat_start < start_bar or repeat_end > end_bar:
            raise ScorePlanError(
                f"反覆區間 {repeat_start}-{repeat_end} 超出規劃範圍 {start_bar}-{end_bar}"
            )
    merged: list[tuple[int, int]] = []
    for repeat_start, repeat_end in intervals:
        if not merged or repeat_start > merged[-1][1]:
            merged.append((repeat_start, repeat_end))
            continue
        previous_start, previous_end = merged[-1]
        merged[-1] = (previous_start, max(previous_end, repeat_end))
    return merged


def suggest_segment_ranges(
    start_bar: int,
    end_bar: int,
    *,
    target_bars: int = 4,
    repeat_blocks: Iterable[object] = (),
) -> list[dict[str, object]]:
    """依連續小節建議 4 或 8 小節分段，不在反覆區間中間下刀。

    當預設邊界落在反覆區間內，該段會向後延伸到反覆結束，即使
    超過 4/8 小節也不拆開反覆。這是「建議」而非音樂語意判斷。
    """

    first = _positive_integer(start_bar, "start_bar")
    last = _positive_integer(end_bar, "end_bar")
    if last < first:
        raise ScorePlanError("end_bar 不可早於 start_bar")
    target = _positive_integer(target_bars, "target_bars")
    if target not in SUGGESTED_SEGMENT_SIZES:
        raise ScorePlanError("target_bars 目前只支援 4 或 8")
    repeats = _merged_repeat_intervals(
        repeat_blocks, start_bar=first, end_bar=last
    )

    suggestions: list[dict[str, object]] = []
    cursor = first
    while cursor <= last:
        nominal_end = min(cursor + target - 1, last)
        adjusted_end = nominal_end
        # 延伸後可能又落入另一個重疊區間，因此迭代到邊界穩定。
        while True:
            containing = [
                repeat_end
                for repeat_start, repeat_end in repeats
                if repeat_start <= adjusted_end < repeat_end
            ]
            if not containing:
                break
            extended = max(containing)
            if extended == adjusted_end:
                break
            adjusted_end = extended
        adjusted_end = min(adjusted_end, last)

        contains_repeat = any(
            repeat_start <= adjusted_end and repeat_end >= cursor
            for repeat_start, repeat_end in repeats
        )
        if adjusted_end > nominal_end:
            reason = "repeat_preserved"
        elif adjusted_end - cursor + 1 < target:
            reason = "remainder"
        else:
            reason = "regular"
        suggestions.append({
            "start_bar": cursor,
            "end_bar": adjusted_end,
            "bar_count": adjusted_end - cursor + 1,
            "target_bars": target,
            "reason": reason,
            "contains_repeat": contains_repeat,
        })
        cursor = adjusted_end + 1
    return suggestions


__all__ = [
    "PAGE_SEQUENCE_SOURCES",
    "SOURCE_REF_ORIGINS",
    "SOURCE_REF_REVIEW_STATUSES",
    "SUGGESTED_SEGMENT_SIZES",
    "ScorePlanError",
    "migrate_page_metadata_in_place",
    "migrate_source_refs_in_place",
    "new_source_ref",
    "ordered_page_assets",
    "ordered_source_refs",
    "reorder_page_assets",
    "suggest_segment_ranges",
    "with_segment_source_refs",
]
