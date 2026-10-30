from pathlib import Path
import pandas as pd
import numpy as np


INPUT_CSV = Path("gmd_events.csv")
FEATURE_OUTPUT = Path("gmd_features.csv")
REVIEW_OUTPUT = Path("gmd_review_candidates.csv")


def add_velocity_features(df):
    # 同一 segment + stroke 內比較 velocity
    group_cols = ["segment_id", "stroke"]

    df["velocity_mean"] = (
        df.groupby(group_cols)["velocity"]
        .transform("mean")
    )

    df["velocity_std"] = (
        df.groupby(group_cols)["velocity"]
        .transform("std")
    )

    df["velocity_z"] = (
        (df["velocity"] - df["velocity_mean"])
        / df["velocity_std"]
    )

    df["velocity_z"] = (
        df["velocity_z"]
        .replace([np.inf, -np.inf], np.nan)
        .fillna(0)
    )

    return df


def add_timing_features(df):
    # 同一 MIDI / stroke 依 raw_tick 排序
    df = df.sort_values(
        ["segment_id", "stroke", "raw_tick"]
    ).copy()

    df["prev_same_stroke_tick"] = (
        df.groupby(["segment_id", "stroke"])["raw_tick"]
        .shift(1)
    )

    df["next_same_stroke_tick"] = (
        df.groupby(["segment_id", "stroke"])["raw_tick"]
        .shift(-1)
    )

    df["ioi_prev_same_stroke"] = (
        df["raw_tick"] - df["prev_same_stroke_tick"]
    )

    df["ioi_next_same_stroke"] = (
        df["next_same_stroke_tick"] - df["raw_tick"]
    )

    # 與量化格點的偏移
    df["grid_offset"] = (
        df["raw_tick"] - df["tick"]
    )

    df["abs_grid_offset"] = (
        df["grid_offset"].abs()
    )

    return df


def add_simultaneous_count(df):
    # 同一個量化 tick 上有幾個事件
    df["simultaneous_count"] = (
        df.groupby(["segment_id", "tick"])["event_id"]
        .transform("count")
    )

    return df


def add_local_density(df, window=240):
    """
    計算同一 segment 中，
    每個事件前後 ±window tick 內共有幾個事件。
    """

    densities = []

    for segment_id, group in df.groupby("segment_id"):

        group = group.sort_values("raw_tick")

        ticks = group["raw_tick"].to_numpy()

        for tick in ticks:
            count = np.sum(
                (ticks >= tick - window)
                & (ticks <= tick + window)
            )

            densities.append((group.index[len(densities) if False else 0], count))

    # 上面不方便直接對 index，改用逐列方式
    density_series = pd.Series(index=df.index, dtype=float)

    for segment_id, group in df.groupby("segment_id"):
        ticks = group["raw_tick"].to_numpy()
        indices = group.index.to_numpy()

        for idx, tick in zip(indices, ticks):
            density_series.loc[idx] = np.sum(
                (ticks >= tick - window)
                & (ticks <= tick + window)
            )

    df["local_density_240"] = density_series.astype(int)

    return df


def add_candidate_flags(df):
    """
    第一版候選規則：
    只是方便人工找案例，不代表 ground truth。
    """

    # 重音候選：
    # 同一 segment / stroke 中 velocity 明顯較高
    df["accent_candidate"] = (
        df["velocity_z"] >= 1.5
    )

    # 滾奏候選：
    # 同一 stroke 前後 IOI 很短
    df["roll_candidate"] = (
        (
            df["ioi_prev_same_stroke"].notna()
            & (df["ioi_prev_same_stroke"] <= 240)
        )
        |
        (
            df["ioi_next_same_stroke"].notna()
            & (df["ioi_next_same_stroke"] <= 240)
        )
    )

    # 裝飾音候選：
    # velocity 偏低、靠近同一 stroke 的下一擊，
    # 且離量化格點有明顯偏移
    df["ornament_candidate"] = (
        (df["velocity_z"] <= -0.8)
        & (
            df["ioi_next_same_stroke"].notna()
            & (df["ioi_next_same_stroke"] <= 240)
        )
        & (df["abs_grid_offset"] >= 60)
    )

    return df


def build_review_table(df):
    review_rows = []

    for _, row in df.iterrows():

        candidate_types = []

        if row["accent_candidate"]:
            candidate_types.append("accent")

        if row["roll_candidate"]:
            candidate_types.append("roll")

        if row["ornament_candidate"]:
            candidate_types.append("ornament")

        if not candidate_types:
            continue

        for candidate_type in candidate_types:

            review_rows.append({
                "candidate_type": candidate_type,

                "event_id": row["event_id"],
                "segment_id": row["segment_id"],

                "midi_filename": row["midi_filename"],

                "style": row["style"],
                "bpm": row["bpm"],
                "beat_type": row["beat_type"],
                "time_signature": row["time_signature_original"],

                "bar": row["bar"],
                "beat": row["beat"],

                "source_tick": row["source_tick"],
                "raw_tick": row["raw_tick"],
                "tick": row["tick"],

                "stroke": row["stroke"],
                "velocity": row["velocity"],
                "velocity_z": round(row["velocity_z"], 3),

                "ioi_prev_same_stroke":
                    row["ioi_prev_same_stroke"],

                "ioi_next_same_stroke":
                    row["ioi_next_same_stroke"],

                "grid_offset":
                    row["grid_offset"],

                "simultaneous_count":
                    row["simultaneous_count"],

                "local_density_240":
                    row["local_density_240"],
            })

    return pd.DataFrame(review_rows)


def print_summary(df):
    print()
    print("========== 基本統計 ==========")

    print("Event 總數：", len(df))
    print("Segment 數：", df["segment_id"].nunique())

    print()
    print("拍號：")
    print(
        df["time_signature_original"]
        .value_counts()
        .head(20)
    )

    print()
    print("Stroke：")
    print(
        df["stroke"]
        .value_counts()
        .head(30)
    )

    print()
    print("PPQ：")
    print(
        df["source_ppq"]
        .value_counts()
    )

    print()
    print("Accent candidates：",
          df["accent_candidate"].sum())

    print("Roll candidates：",
          df["roll_candidate"].sum())

    print("Ornament candidates：",
          df["ornament_candidate"].sum())


def main():

    print("讀取：", INPUT_CSV)

    df = pd.read_csv(INPUT_CSV)

    df = add_velocity_features(df)
    df = add_timing_features(df)
    df = add_simultaneous_count(df)
    df = add_local_density(df)
    df = add_candidate_flags(df)

    # 存完整 feature table
    df.to_csv(
        FEATURE_OUTPUT,
        index=False,
        encoding="utf-8-sig"
    )

    # 建人工檢查表
    review_df = build_review_table(df)

    review_df.to_csv(
        REVIEW_OUTPUT,
        index=False,
        encoding="utf-8-sig"
    )

    print_summary(df)

    print()
    print("完成")
    print("完整特徵：", FEATURE_OUTPUT)
    print("人工檢查：", REVIEW_OUTPUT)


if __name__ == "__main__":
    main()