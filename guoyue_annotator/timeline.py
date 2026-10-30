"""音樂時間座標的換算。

全專案固定使用 PPQ=960。tick 是訓練資料的正式時間座標；bar 與 beat
都是從 tick 推導出來，避免三個欄位彼此矛盾。
"""

from __future__ import annotations


PPQ = 960

QUANTIZATION_TICKS = {
    "1/4": PPQ,
    "1/8": PPQ // 2,
    "1/16": PPQ // 4,
    "1/32": PPQ // 8,
    "1/8 triplet": PPQ // 3,
    "1/16 triplet": PPQ // 6,
}


def parse_time_signature(value: str) -> tuple[int, int]:
    """把 ``4/4`` 轉成 ``(4, 4)``，並拒絕不合理的拍號。"""
    try:
        numerator_text, denominator_text = value.strip().split("/", 1)
        numerator = int(numerator_text)
        denominator = int(denominator_text)
    except (AttributeError, ValueError) as exc:
        raise ValueError("拍號格式應為 4/4、3/4 或 6/8") from exc
    if numerator <= 0 or denominator <= 0:
        raise ValueError("拍號的分子與分母必須大於 0")
    if denominator not in {1, 2, 4, 8, 16, 32}:
        raise ValueError("目前支援的拍號分母為 1、2、4、8、16、32")
    return numerator, denominator


def ticks_per_beat(time_signature: str, ppq: int = PPQ) -> int:
    """傳回拍號中一拍所包含的 tick 數。"""
    _, denominator = parse_time_signature(time_signature)
    return ppq * 4 // denominator


def ticks_per_bar(time_signature: str, ppq: int = PPQ) -> int:
    numerator, _ = parse_time_signature(time_signature)
    return numerator * ticks_per_beat(time_signature, ppq)


def segment_ticks(time_signature: str, bars: int, ppq: int = PPQ) -> int:
    if bars <= 0:
        raise ValueError("小節數必須大於 0")
    return ticks_per_bar(time_signature, ppq) * bars


def quantize_tick(raw_tick: float, grid_ticks: int) -> int:
    """將原始 tick 四捨五入到最近的格線；結果不小於 0。"""
    if grid_ticks <= 0:
        raise ValueError("量化格線必須大於 0")
    return max(0, int(raw_tick / grid_ticks + 0.5) * grid_ticks)


def tick_to_bar_beat(
    tick: int, time_signature: str, ppq: int = PPQ
) -> tuple[int, float]:
    """將絕對 tick 轉成從 1 開始的 bar 與 beat。"""
    tick = max(0, int(tick))
    per_bar = ticks_per_bar(time_signature, ppq)
    per_beat = ticks_per_beat(time_signature, ppq)
    bar = tick // per_bar + 1
    beat = (tick % per_bar) / per_beat + 1
    return bar, round(beat, 6)


def tick_to_seconds(tick: int, tempo: float, ppq: int = PPQ) -> float:
    if tempo <= 0:
        raise ValueError("BPM 必須大於 0")
    return tick / ppq * 60.0 / tempo


def seconds_to_tick(seconds: float, tempo: float, ppq: int = PPQ) -> float:
    if tempo <= 0:
        raise ValueError("BPM 必須大於 0")
    return seconds * tempo / 60.0 * ppq


def refresh_event_position(event: dict, time_signature: str) -> None:
    """依事件的正式 tick，重新寫入 bar 與 beat。"""
    event["bar"], event["beat"] = tick_to_bar_beat(
        int(event["tick"]), time_signature
    )

