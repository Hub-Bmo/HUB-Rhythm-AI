from pathlib import Path
import json
import pandas as pd


INPUT_CSV = Path("gmd_events.csv")
OUTPUT_JSONL = Path("gmd_sequences_v2.jsonl")


# --------------------------------------------------
# Velocity 分成 8 級
# --------------------------------------------------

def velocity_to_bin(v):
    v = int(v)

    if v <= 16:
        return 1
    elif v <= 32:
        return 2
    elif v <= 48:
        return 3
    elif v <= 64:
        return 4
    elif v <= 80:
        return 5
    elif v <= 96:
        return 6
    elif v <= 112:
        return 7
    else:
        return 8


def sanitize_token(text):
    return str(text).strip().replace(" ", "_")


# --------------------------------------------------
# 固定鼓組排序
#
# 目的：
# 同一 tick 即使 MIDI 原始事件順序不同，
# 轉換後仍保持一致。
# --------------------------------------------------

STROKE_ORDER = {
    "kick": 10,

    "snare": 20,
    "side_stick": 21,
    "clap": 22,

    "tom_low": 30,
    "tom_mid": 31,
    "tom_high": 32,

    "hihat_pedal": 40,
    "hihat_closed": 41,
    "hihat_open": 42,

    "ride": 50,
    "ride_bell": 51,

    "crash": 60,
    "splash": 61,
    "china": 62,

    "tambourine": 70,
    "cowbell": 71,
}


def stroke_sort_key(stroke):
    """
    未知 stroke 自動排到最後。
    """
    return (
        STROKE_ORDER.get(stroke, 999),
        stroke
    )


# --------------------------------------------------
# 單一 segment → tokens
# --------------------------------------------------

def segment_to_tokens(group):

    group = group.copy()

    # 確保 tick 為整數
    group["tick"] = group["tick"].astype(int)

    first = group.iloc[0]

    tokens = []

    # ==================================================
    # Metadata
    # ==================================================

    tokens.append("<BOS>")
    tokens.append("<SOURCE_GMD>")

    time_signature = sanitize_token(
        first["time_signature_original"]
    )

    tokens.append(
        f"<TS_{time_signature}>"
    )

    bpm = int(round(float(first["bpm"])))

    tokens.append(
        f"<BPM_{bpm}>"
    )

    # ==================================================
    # 將同一 tick 的事件合成 HIT_SET
    # ==================================================

    tick_groups = []

    for tick, hit_group in group.groupby(
        "tick",
        sort=True
    ):

        hits = []

        for _, row in hit_group.iterrows():

            stroke = sanitize_token(
                row["stroke"]
            )

            vel_bin = velocity_to_bin(
                row["velocity"]
            )

            hits.append({
                "stroke": stroke,
                "velocity_bin": vel_bin,
                "event_id": row["event_id"]
            })

        # 固定同時事件的樂器順序
        hits.sort(
            key=lambda x:
            stroke_sort_key(x["stroke"])
        )

        tick_groups.append({
            "tick": int(tick),
            "hits": hits
        })

    # ==================================================
    # TIME_SHIFT + HIT_SET
    # ==================================================

    previous_tick = 0

    for i, tick_group in enumerate(tick_groups):

        current_tick = tick_group["tick"]

        if i == 0:
            # 第一個事件相對於 segment 開始
            time_shift = current_tick
        else:
            time_shift = (
                current_tick - previous_tick
            )

        tokens.append(
            f"<TIME_SHIFT_{time_shift}>"
        )

        tokens.append("<HIT_SET>")

        for hit in tick_group["hits"]:

            tokens.append(
                f"<STROKE_{hit['stroke']}>"
            )

            tokens.append(
                f"<VEL_{hit['velocity_bin']}>"
            )

        tokens.append("<END_HIT_SET>")

        previous_tick = current_tick

    tokens.append("<EOS>")

    return tokens, tick_groups


# --------------------------------------------------
# 主程式
# --------------------------------------------------

def main():

    print("讀取：", INPUT_CSV)

    df = pd.read_csv(INPUT_CSV)

    output_rows = []

    grouped = df.groupby(
        "segment_id",
        sort=False
    )

    total = grouped.ngroups

    for i, (segment_id, group) in enumerate(
        grouped,
        start=1
    ):

        tokens, tick_groups = (
            segment_to_tokens(group)
        )

        first = group.iloc[0]

        item = {
            "segment_id":
                segment_id,

            "source":
                "gmd",

            "split":
                first["split"],

            "style":
                first["style"],

            "bpm":
                int(
                    round(
                        float(first["bpm"])
                    )
                ),

            "time_signature":
                first[
                    "time_signature_original"
                ],

            "beat_type":
                first["beat_type"],

            "midi_filename":
                first["midi_filename"],

            # Event 原始數量
            "event_count":
                len(group),

            # 有多少個不同時間點
            "hit_set_count":
                len(tick_groups),

            "token_count":
                len(tokens),

            "tokens":
                tokens
        }

        output_rows.append(item)

        if (
            i <= 5
            or i % 100 == 0
            or i == total
        ):

            print(
                f"[{i}/{total}]",
                segment_id,
                "| events =",
                len(group),
                "| hit_sets =",
                len(tick_groups),
                "| tokens =",
                len(tokens)
            )

    # ==================================================
    # 寫 JSONL
    # ==================================================

    with open(
        OUTPUT_JSONL,
        "w",
        encoding="utf-8"
    ) as f:

        for item in output_rows:

            f.write(
                json.dumps(
                    item,
                    ensure_ascii=False
                )
                + "\n"
            )

    print()
    print("================================")
    print("轉換完成")
    print("Segment 數：", len(output_rows))
    print("輸出：", OUTPUT_JSONL)
    print("================================")

    # ==================================================
    # 顯示第一筆
    # ==================================================

    if output_rows:

        first = output_rows[0]

        print()
        print("第一筆 sequence：")
        print(
            "segment_id:",
            first["segment_id"]
        )

        print(
            "MIDI:",
            first["midi_filename"]
        )

        print(
            "event_count:",
            first["event_count"]
        )

        print(
            "hit_set_count:",
            first["hit_set_count"]
        )

        print()

        print(
            " ".join(
                first["tokens"][:100]
            )
        )


if __name__ == "__main__":
    main()