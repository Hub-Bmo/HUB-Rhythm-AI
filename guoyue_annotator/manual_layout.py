"""人工覆寫樂譜頁面的譜表與小節分區。

這個模組不含 UI，也不會修改傳入的分析結果。第一次人工覆寫時會
保留原始 ``systems`` 於 ``automatic_systems``，供使用者後續還原。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import math
from numbers import Integral, Real


MANUAL_LAYOUT_ORIGIN = "manual_layout"
_DEFAULT_PAGE_INSET = 0.05
_LABEL_GUTTER_FRACTION = 0.07
_MINIMUM_EXISTING_MEASURE_COVERAGE = 0.55
_MINIMUM_MEASURE_WIDTH = 0.000001


class ManualLayoutError(ValueError):
    """人工版面設定無法在不猜測的情況下套用。"""


def _counts(value: object) -> list[int]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ManualLayoutError("counts 必須是 1..64 正整數序列")
    result: list[int] = []
    for index, count in enumerate(value, start=1):
        if isinstance(count, bool) or not isinstance(count, Integral):
            raise ManualLayoutError(
                f"counts 第 {index} 項必須是 1..64 的正整數"
            )
        normalized = int(count)
        if not 1 <= normalized <= 64:
            raise ManualLayoutError(
                f"counts 第 {index} 項必須介於 1 與 64 之間"
            )
        result.append(normalized)
    if not result:
        raise ManualLayoutError("counts 不能是空序列")
    return result


def _bbox(value: object) -> dict[str, float] | None:
    """讀取並夾到正規化圖片範圍；無法形成正面積時回傳 None。"""

    if not isinstance(value, Mapping):
        return None
    coordinates: dict[str, float] = {}
    for key in ("x", "y", "width", "height"):
        coordinate = value.get(key)
        if (
            isinstance(coordinate, bool)
            or not isinstance(coordinate, Real)
            or not math.isfinite(float(coordinate))
        ):
            return None
        coordinates[key] = float(coordinate)
    if coordinates["width"] <= 0 or coordinates["height"] <= 0:
        return None
    left = max(0.0, coordinates["x"])
    top = max(0.0, coordinates["y"])
    right = min(1.0, coordinates["x"] + coordinates["width"])
    bottom = min(1.0, coordinates["y"] + coordinates["height"])
    if right <= left or bottom <= top:
        return None
    return _box_from_edges(left, top, right, bottom)


def _box_from_edges(
    left: float, top: float, right: float, bottom: float
) -> dict[str, float]:
    """用共用的圓整邊界建立合法且可重現的 bbox。"""

    left = round(max(0.0, min(1.0, left)), 6)
    top = round(max(0.0, min(1.0, top)), 6)
    right = round(max(left, min(1.0, right)), 6)
    bottom = round(max(top, min(1.0, bottom)), 6)
    if right <= left or bottom <= top:
        raise ManualLayoutError("版面區域必須具有正寬度與正高度")
    return {
        "x": left,
        "y": top,
        "width": round(right - left, 6),
        "height": round(bottom - top, 6),
    }


def _union(boxes: Sequence[Mapping[str, float]]) -> dict[str, float]:
    if not boxes:
        raise ManualLayoutError("無法合併空白的版面區域")
    return _box_from_edges(
        min(float(item["x"]) for item in boxes),
        min(float(item["y"]) for item in boxes),
        max(float(item["x"]) + float(item["width"]) for item in boxes),
        max(float(item["y"]) + float(item["height"]) for item in boxes),
    )


def _validated_systems(value: object) -> list[dict]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ManualLayoutError("analysis.systems 必須是清單")
    result: list[dict] = []
    for index, system in enumerate(value, start=1):
        if not isinstance(system, Mapping):
            raise ManualLayoutError(f"第 {index} 個 system 格式無效")
        system_copy = deepcopy(dict(system))
        if _bbox(system_copy.get("bbox")) is None:
            raise ManualLayoutError(f"第 {index} 個 system 沒有合法 bbox")
        result.append(system_copy)
    return result


def _fallback_system_bbox(analysis: Mapping[str, object]) -> dict[str, float]:
    content = analysis.get("content_region")
    if isinstance(content, Mapping):
        content_bbox = _bbox(content.get("bbox"))
        if content_bbox is not None:
            return content_bbox
    inset = _DEFAULT_PAGE_INSET
    return _box_from_edges(inset, inset, 1.0 - inset, 1.0 - inset)


def _split_vertically(
    bbox: Mapping[str, float], count: int
) -> list[dict[str, float]]:
    left = float(bbox["x"])
    top = float(bbox["y"])
    right = left + float(bbox["width"])
    bottom = top + float(bbox["height"])
    return [
        _box_from_edges(
            left,
            top + (bottom - top) * index / count,
            right,
            top + (bottom - top) * (index + 1) / count,
        )
        for index in range(count)
    ]


def _measure_boxes(systems: Sequence[Mapping[str, object]]) -> list[dict[str, float]]:
    boxes: list[dict[str, float]] = []
    for system in systems:
        measures = system.get("measure_regions")
        if not isinstance(measures, Sequence) or isinstance(measures, (str, bytes)):
            continue
        for measure in measures:
            if not isinstance(measure, Mapping):
                continue
            normalized = _bbox(measure.get("bbox"))
            if normalized is not None:
                boxes.append(normalized)
    return boxes


def _measure_span(
    system_bbox: Mapping[str, float], source_systems: Sequence[Mapping[str, object]]
) -> tuple[float, float]:
    system_left = float(system_bbox["x"])
    system_right = system_left + float(system_bbox["width"])
    existing = _measure_boxes(source_systems)
    if existing:
        union = _union(existing)
        measure_left = max(system_left, float(union["x"]))
        measure_right = min(
            system_right, float(union["x"]) + float(union["width"])
        )
        coverage = (measure_right - measure_left) / float(system_bbox["width"])
        if coverage >= _MINIMUM_EXISTING_MEASURE_COVERAGE:
            return measure_left, measure_right
    # 沒有可靠的既有小節區域時，保留左側樂器／聲部名稱空間。
    return (
        system_left + float(system_bbox["width"]) * _LABEL_GUTTER_FRACTION,
        system_right,
    )


def _manual_system(
    system_index: int,
    count: int,
    system_bbox: Mapping[str, float],
    source_systems: Sequence[Mapping[str, object]],
    next_measure_number: int,
) -> tuple[dict, int]:
    left, right = _measure_span(system_bbox, source_systems)
    top = float(system_bbox["y"])
    bottom = top + float(system_bbox["height"])
    span = right - left
    if span <= 0:
        raise ManualLayoutError("無法在 system 內建立小節區域")

    measures: list[dict] = []
    for local_index in range(count):
        measure_left = left + span * local_index / count
        measure_right = left + span * (local_index + 1) / count
        measures.append(
            {
                "measure_id": f"MEASURE-MANUAL-{next_measure_number:04d}",
                "bbox": _box_from_edges(
                    measure_left, top, measure_right, bottom
                ),
                "confidence": 1.0,
                "origin": MANUAL_LAYOUT_ORIGIN,
            }
        )
        next_measure_number += 1

    return (
        {
            "system_id": f"SYS-MANUAL-{system_index:03d}",
            "bbox": deepcopy(dict(system_bbox)),
            "confidence": 1.0,
            "origin": MANUAL_LAYOUT_ORIGIN,
            "barlines": [],
            "measure_regions": measures,
        },
        next_measure_number,
    )


def apply_manual_measure_counts(
    analysis: Mapping[str, object], counts: Sequence[int]
) -> dict:
    """回傳人工重分小節後的新 analysis，不修改傳入值。

    ``counts`` 與原始 system 數相同時，會逐一覆寫各 system。
    單一 count 可將原始 systems 合併成一個 system；若輸入的行數
    與自動結果不同，則會在 content region 內平均估計各行位置。
    """

    if not isinstance(analysis, Mapping):
        raise ManualLayoutError("analysis 必須是物件")
    normalized_counts = _counts(counts)
    result = deepcopy(dict(analysis))

    if "automatic_systems" in result:
        automatic_value = result.get("automatic_systems")
        if not isinstance(automatic_value, list):
            raise ManualLayoutError("automatic_systems 備份格式無效")
        source_systems = _validated_systems(automatic_value)
    else:
        current_value = result.get("systems", [])
        source_systems = _validated_systems(current_value)
        # 儲存第一次的原始值，後續覆寫不會汙染這份備份。
        result["automatic_systems"] = deepcopy(source_systems)

    system_groups: list[tuple[dict[str, float], list[dict]]]
    if source_systems and len(normalized_counts) == len(source_systems):
        system_groups = []
        for system in source_systems:
            # _validated_systems 已確認此 bbox 存在。
            normalized_bbox = _bbox(system.get("bbox"))
            assert normalized_bbox is not None
            system_groups.append((normalized_bbox, [system]))
    elif len(normalized_counts) == 1:
        valid_boxes = [
            normalized
            for system in source_systems
            if (normalized := _bbox(system.get("bbox"))) is not None
        ]
        combined = _union(valid_boxes) if valid_boxes else _fallback_system_bbox(result)
        system_groups = [(combined, source_systems)]
    else:
        # 自動分析完全漏掉譜表，或把多行誤認成不同數量時，仍允許
        # 使用者輸入正確行數。這些位置只是清楚標示的平均估計，之後
        # 必須回到預覽核對，不能冒充精確的自動偵測框。
        content_bbox = _fallback_system_bbox(result)
        if result.get("content_region") is None and source_systems:
            source_boxes = [
                normalized
                for system in source_systems
                if (normalized := _bbox(system.get("bbox"))) is not None
            ]
            if source_boxes:
                content_bbox = _union(source_boxes)
        system_groups = [
            (system_bbox, source_systems)
            for system_bbox in _split_vertically(
                content_bbox, len(normalized_counts)
            )
        ]

    manual_systems: list[dict] = []
    next_measure_number = 1
    for system_index, (count, group) in enumerate(
        zip(normalized_counts, system_groups), start=1
    ):
        system, next_measure_number = _manual_system(
            system_index,
            count,
            group[0],
            group[1],
            next_measure_number,
        )
        manual_systems.append(system)

    result["systems"] = manual_systems
    result["manual_override"] = {
        "origin": MANUAL_LAYOUT_ORIGIN,
        "measure_counts": normalized_counts,
        "system_count": len(manual_systems),
    }
    return result


def restore_automatic_layout(analysis: Mapping[str, object]) -> dict:
    """從首次人工覆寫所儲存的備份還原 systems。"""

    if not isinstance(analysis, Mapping):
        raise ManualLayoutError("analysis 必須是物件")
    backup = analysis.get("automatic_systems")
    if not isinstance(backup, list):
        raise ManualLayoutError(
            "找不到 automatic_systems 備份，無法還原自動版面"
        )
    # 先驗證備份，避免將損壞的區域寫回正式 systems。
    restored_systems = _validated_systems(backup)
    result = deepcopy(dict(analysis))
    result["systems"] = deepcopy(restored_systems)
    result.pop("manual_override", None)
    return result


def _system_index(
    systems: Sequence[Mapping[str, object]], selector: int | str
) -> int:
    """Resolve a zero-based index or an exact ``system_id``."""

    if isinstance(selector, bool):
        raise ManualLayoutError("system 必須是 0-based 索引或 system_id")
    if isinstance(selector, Integral):
        index = int(selector)
        if not 0 <= index < len(systems):
            raise ManualLayoutError(
                f"system 索引 {index} 超出範圍（共有 {len(systems)} 個 system）"
            )
        return index
    if isinstance(selector, str):
        matches = [
            index
            for index, system in enumerate(systems)
            if system.get("system_id") == selector
        ]
        if not matches:
            raise ManualLayoutError(f"找不到 system_id：{selector}")
        if len(matches) > 1:
            raise ManualLayoutError(f"system_id 不唯一：{selector}")
        return matches[0]
    raise ManualLayoutError("system 必須是 0-based 索引或 system_id")


def _finite_image_x(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ManualLayoutError("x 必須是 0..1 的正規化圖片座標")
    x = float(value)
    if not math.isfinite(x) or not 0.0 <= x <= 1.0:
        raise ManualLayoutError("x 必須是 0..1 的有限正規化圖片座標")
    # All stored geometry uses six decimals.  Validate the value that will
    # actually be persisted so rounding cannot silently collapse a measure.
    return round(x, 6)


def _strict_normalized_bbox(value: object) -> dict[str, float]:
    """Return a canonical bbox only when every edge is already on the page.

    ``_bbox`` deliberately clips detector output because the automatic
    analysis can extend a fraction beyond the photographed page.  A user edit
    must be different: silently clipping a dragged blue frame would make the
    saved geometry disagree with what the user chose.  Keep this stricter
    validator local to manual edits.
    """

    if not isinstance(value, Mapping):
        raise ManualLayoutError("bbox 必須是含 x、y、width、height 的物件")
    coordinates: dict[str, float] = {}
    for key in ("x", "y", "width", "height"):
        coordinate = value.get(key)
        if (
            isinstance(coordinate, bool)
            or not isinstance(coordinate, Real)
            or not math.isfinite(float(coordinate))
        ):
            raise ManualLayoutError(
                f"bbox.{key} 必須是有限的正規化圖片座標"
            )
        coordinates[key] = float(coordinate)

    left = coordinates["x"]
    top = coordinates["y"]
    width = coordinates["width"]
    height = coordinates["height"]
    right = left + width
    bottom = top + height
    if width <= 0 or height <= 0:
        raise ManualLayoutError("bbox 必須具有正寬度與正高度")
    if left < 0 or top < 0 or right > 1 or bottom > 1:
        raise ManualLayoutError("bbox 必須完整位於 0..1 的正規化圖片範圍內")
    return _box_from_edges(left, top, right, bottom)


def _measure_regions(
    system: Mapping[str, object],
    system_index: int,
    *,
    require_nonempty: bool = True,
) -> list[dict]:
    value = system.get("measure_regions")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ManualLayoutError(
            f"第 {system_index + 1} 個 system 的 measure_regions 必須是清單"
        )
    if require_nonempty and not value:
        raise ManualLayoutError(
            f"第 {system_index + 1} 個 system 沒有可調整的小節"
        )
    measures: list[dict] = []
    for measure_index, measure in enumerate(value, start=1):
        if not isinstance(measure, Mapping):
            raise ManualLayoutError(
                f"第 {system_index + 1} 個 system 的第 {measure_index} 小節格式無效"
            )
        copied = deepcopy(dict(measure))
        if _bbox(copied.get("bbox")) is None:
            raise ManualLayoutError(
                f"第 {system_index + 1} 個 system 的第 {measure_index} 小節沒有合法 bbox"
            )
        measures.append(copied)
    return measures


def _horizontal_edges(measure: Mapping[str, object]) -> tuple[float, float]:
    bbox = _bbox(measure.get("bbox"))
    assert bbox is not None
    left = float(bbox["x"])
    return left, round(left + float(bbox["width"]), 6)


def _replace_horizontal_edges(
    measure: dict, left: float, right: float
) -> None:
    left = round(left, 6)
    right = round(right, 6)
    width = round(right - left, 6)
    if width < _MINIMUM_MEASURE_WIDTH:
        raise ManualLayoutError("調整後的小節寬度必須至少為 0.000001")
    original = measure.get("bbox")
    if not isinstance(original, Mapping):  # Already checked; keep the error explicit.
        raise ManualLayoutError("小節沒有合法 bbox")
    updated_bbox = deepcopy(dict(original))
    updated_bbox["x"] = left
    updated_bbox["width"] = width
    measure["bbox"] = updated_bbox
    measure["origin"] = MANUAL_LAYOUT_ORIGIN


def adjust_measure_boundary(
    analysis: Mapping[str, object],
    system: int | str,
    boundary_index: int,
    x: Real,
) -> dict:
    """Move one vertical measure boundary and return a new analysis.

    ``system`` accepts a zero-based system index or an exact ``system_id``.
    ``boundary_index`` is in ``0..measure_count``: zero and measure_count
    move the outer edges, while an inner boundary resizes both adjacent
    measures to the same normalized image ``x``.  The input is never mutated.
    """

    if not isinstance(analysis, Mapping):
        raise ManualLayoutError("analysis 必須是物件")
    systems = _validated_systems(analysis.get("systems", []))
    if not systems:
        raise ManualLayoutError("analysis 沒有可調整的 system")
    selected_index = _system_index(systems, system)
    selected = systems[selected_index]
    measures = _measure_regions(selected, selected_index)

    if isinstance(boundary_index, bool) or not isinstance(boundary_index, Integral):
        raise ManualLayoutError("boundary_index 必須是整數")
    boundary = int(boundary_index)
    if not 0 <= boundary <= len(measures):
        raise ManualLayoutError(
            f"boundary_index {boundary} 超出範圍（應為 0..{len(measures)}）"
        )
    normalized_x = _finite_image_x(x)

    system_bbox = _bbox(selected.get("bbox"))
    assert system_bbox is not None
    system_left = float(system_bbox["x"])
    system_right = round(system_left + float(system_bbox["width"]), 6)
    if normalized_x < system_left or normalized_x > system_right:
        raise ManualLayoutError(
            f"x={normalized_x:g} 必須位於 system 範圍 {system_left:g}..{system_right:g} 內"
        )

    if boundary == 0:
        _, first_right = _horizontal_edges(measures[0])
        if round(first_right - normalized_x, 6) < _MINIMUM_MEASURE_WIDTH:
            raise ManualLayoutError("左外界不能越過第一小節的右界")
        _replace_horizontal_edges(measures[0], normalized_x, first_right)
    elif boundary == len(measures):
        last_left, _ = _horizontal_edges(measures[-1])
        if round(normalized_x - last_left, 6) < _MINIMUM_MEASURE_WIDTH:
            raise ManualLayoutError("右外界不能越過最後小節的左界")
        _replace_horizontal_edges(measures[-1], last_left, normalized_x)
    else:
        left_edge, _ = _horizontal_edges(measures[boundary - 1])
        _, right_edge = _horizontal_edges(measures[boundary])
        if round(normalized_x - left_edge, 6) < _MINIMUM_MEASURE_WIDTH:
            raise ManualLayoutError("小節線不能越過左側相鄰小節的左界")
        if round(right_edge - normalized_x, 6) < _MINIMUM_MEASURE_WIDTH:
            raise ManualLayoutError("小節線不能越過右側相鄰小節的右界")
        _replace_horizontal_edges(
            measures[boundary - 1], left_edge, normalized_x
        )
        _replace_horizontal_edges(measures[boundary], normalized_x, right_edge)

    # A system is the unit consumed by downstream segment/crop generation.
    # Keep one provenance throughout it after a manual boundary edit; mixing
    # detected and manual origins in the same row would split source refs even
    # though only its geometry changed.  Old barline links no longer describe
    # the edited positions, so remove only those stale derived references.
    for measure in measures:
        measure["origin"] = MANUAL_LAYOUT_ORIGIN
        measure.pop("left_barline_id", None)
        measure.pop("right_barline_id", None)

    result = deepcopy(dict(analysis))
    if "automatic_systems" in result:
        if not isinstance(result.get("automatic_systems"), list):
            raise ManualLayoutError("automatic_systems 備份格式無效")
        # Validate without replacing the user's stable first backup.
        _validated_systems(result["automatic_systems"])
    else:
        result["automatic_systems"] = deepcopy(systems)

    selected["measure_regions"] = measures
    selected["barlines"] = []
    selected["origin"] = MANUAL_LAYOUT_ORIGIN
    systems[selected_index] = selected
    result["systems"] = systems

    existing_override = result.get("manual_override")
    if existing_override is None:
        manual_override: dict = {}
    elif isinstance(existing_override, Mapping):
        manual_override = deepcopy(dict(existing_override))
    else:
        raise ManualLayoutError("manual_override 格式無效")
    manual_override["origin"] = MANUAL_LAYOUT_ORIGIN
    manual_override["boundary_adjusted"] = True
    result["manual_override"] = manual_override
    return result


def adjust_system_bbox(
    analysis: Mapping[str, object],
    system: int | str,
    bbox: Mapping[str, object],
) -> dict:
    """Move or resize one system (the blue frame) without mutating input.

    The edited frame must keep every orange measure horizontally inside it.
    Its vertical midpoint may not cross an adjacent system's midpoint, which
    preserves the page's established top-to-bottom system order while still
    allowing neighbouring frames to overlap when the source analysis already
    requires that.  Every measure in the row inherits the system's new
    vertical extent; its horizontal geometry is retained verbatim.
    """

    if not isinstance(analysis, Mapping):
        raise ManualLayoutError("analysis 必須是物件")
    systems = _validated_systems(analysis.get("systems", []))
    if not systems:
        raise ManualLayoutError("analysis 沒有可調整的 system")
    selected_index = _system_index(systems, system)
    selected = systems[selected_index]
    measures = _measure_regions(selected, selected_index, require_nonempty=False)
    normalized_bbox = _strict_normalized_bbox(bbox)

    measure_boxes = []
    for measure in measures:
        measure_bbox = _bbox(measure.get("bbox"))
        assert measure_bbox is not None
        measure_boxes.append(measure_bbox)
    system_left = float(normalized_bbox["x"])
    system_right = round(system_left + float(normalized_bbox["width"]), 6)
    if measure_boxes:
        measure_left = min(float(item["x"]) for item in measure_boxes)
        measure_right = max(
            float(item["x"]) + float(item["width"])
            for item in measure_boxes
        )
        if system_left > round(measure_left, 6) or system_right < round(
            measure_right, 6
        ):
            raise ManualLayoutError("藍色 system 框的左右邊界必須包含所有小節")

    new_midpoint = float(normalized_bbox["y"]) + float(
        normalized_bbox["height"]
    ) / 2
    if selected_index > 0:
        previous_bbox = _bbox(systems[selected_index - 1].get("bbox"))
        assert previous_bbox is not None
        previous_midpoint = float(previous_bbox["y"]) + float(
            previous_bbox["height"]
        ) / 2
        if new_midpoint <= previous_midpoint:
            raise ManualLayoutError("system 不能越過上一行的垂直順序")
    if selected_index + 1 < len(systems):
        next_bbox = _bbox(systems[selected_index + 1].get("bbox"))
        assert next_bbox is not None
        next_midpoint = float(next_bbox["y"]) + float(next_bbox["height"]) / 2
        if new_midpoint >= next_midpoint:
            raise ManualLayoutError("system 不能越過下一行的垂直順序")

    new_y = float(normalized_bbox["y"])
    new_height = float(normalized_bbox["height"])
    for measure in measures:
        original_bbox = measure.get("bbox")
        assert isinstance(original_bbox, Mapping)
        updated_bbox = deepcopy(dict(original_bbox))
        updated_bbox["y"] = new_y
        updated_bbox["height"] = new_height
        measure["bbox"] = updated_bbox
        measure["origin"] = MANUAL_LAYOUT_ORIGIN
        measure.pop("left_barline_id", None)
        measure.pop("right_barline_id", None)

    result = deepcopy(dict(analysis))
    if "automatic_systems" in result:
        if not isinstance(result.get("automatic_systems"), list):
            raise ManualLayoutError("automatic_systems 備份格式無效")
        # Validate but never replace the stable first automatic snapshot.
        _validated_systems(result["automatic_systems"])
    else:
        result["automatic_systems"] = deepcopy(systems)

    selected["bbox"] = normalized_bbox
    selected["measure_regions"] = measures
    selected["barlines"] = []
    selected["origin"] = MANUAL_LAYOUT_ORIGIN
    systems[selected_index] = selected
    result["systems"] = systems

    existing_override = result.get("manual_override")
    if existing_override is None:
        manual_override: dict = {}
    elif isinstance(existing_override, Mapping):
        manual_override = deepcopy(dict(existing_override))
    else:
        raise ManualLayoutError("manual_override 格式無效")
    manual_override["origin"] = MANUAL_LAYOUT_ORIGIN
    manual_override["system_bbox_adjusted"] = True
    result["manual_override"] = manual_override
    return result


__all__ = [
    "MANUAL_LAYOUT_ORIGIN",
    "ManualLayoutError",
    "adjust_measure_boundary",
    "adjust_system_bbox",
    "apply_manual_measure_counts",
    "restore_automatic_layout",
]
