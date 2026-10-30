from pathlib import Path
import pandas as pd
import mido

GMD_ROOT = Path("groove")
INFO_CSV = GMD_ROOT / "info.csv"
OUTPUT_CSV = Path("gmd_events.csv")

TARGET_PPQ = 960
GRID_DIVISION = 16   # 先統一量化到 1/16


GM_DRUM_MAP = {

    # -----------------
    # Kick
    # -----------------
    35: "kick",
    36: "kick",


    # -----------------
    # Snare
    # -----------------
    37: "snare",
    38: "snare",
    39: "clap",
    40: "snare",


    # -----------------
    # Toms
    # -----------------
    41: "tom_low",
43: "tom_low",
58: "tom_low",


    45: "tom_mid",
    47: "tom_mid",

    48: "tom_high",
    50: "tom_high",


    # -----------------
    # Hi-hat
    # -----------------
    22: "hihat_closed",
    42: "hihat_closed",

    44: "hihat_pedal",

    26: "hihat_open",
    46: "hihat_open",


    # -----------------
    # Cymbals
    # -----------------
    49: "crash",
    52: "crash",
    55: "splash",
    57: "crash",

    51: "ride",
    53: "ride_bell",
    59: "ride",


    # -----------------
    # Others
    # -----------------
    54: "tambourine",
    56: "cowbell",
}

def note_to_stroke(note):
    return GM_DRUM_MAP.get(note, f"midi_note_{note}")


def normalize_tick(source_tick, source_ppq):
    return round(source_tick * TARGET_PPQ / source_ppq)


def quantize_tick(raw_tick, denominator=16):
    """
    raw_tick 已經是 TARGET_PPQ=960 座標。
    denominator=16 代表量化到 1/16 音符。
    """
    grid_size = TARGET_PPQ * 4 / denominator
    return int(round(raw_tick / grid_size) * grid_size)


def tick_to_bar_beat(tick, numerator, denominator):
    beat_length = TARGET_PPQ * 4 / denominator
    bar_length = beat_length * numerator

    bar = int(tick // bar_length) + 1
    tick_in_bar = tick % bar_length
    beat = tick_in_bar / beat_length + 1

    return bar, round(beat, 4)


def get_grid_index(tick, numerator, denominator, grid_division=16):
    beat_length = TARGET_PPQ * 4 / denominator
    bar_length = beat_length * numerator
    grid_size = TARGET_PPQ * 4 / grid_division

    tick_in_bar = tick % bar_length
    return int(round(tick_in_bar / grid_size))


def parse_midi(midi_path, metadata, segment_id):
    midi = mido.MidiFile(midi_path)
    source_ppq = midi.ticks_per_beat

    numerator = 4
    denominator = 4

    events = []
    event_counter = 1

    for track_index, track in enumerate(midi.tracks):
        absolute_tick = 0

        for msg in track:
            absolute_tick += msg.time

            if msg.type == "time_signature":
                numerator = msg.numerator
                denominator = msg.denominator

            if msg.type == "note_on" and msg.velocity > 0:

                source_tick = absolute_tick

                raw_tick = normalize_tick(
                    source_tick,
                    source_ppq
                )

                tick = quantize_tick(
                    raw_tick,
                    GRID_DIVISION
                )

                bar, beat = tick_to_bar_beat(
                    tick,
                    numerator,
                    denominator
                )

                grid_index = get_grid_index(
                    tick,
                    numerator,
                    denominator,
                    GRID_DIVISION
                )

                stroke = note_to_stroke(msg.note)

                event_id = f"{segment_id}-E{event_counter:06d}"
                track_id = f"{segment_id}-T{track_index + 1:02d}"
                take_id = f"{track_id}-K001"

                events.append({
                    "event_id": event_id,
                    "source": "gmd",

                    "segment_id": segment_id,
                    "track_id": track_id,
                    "take_id": take_id,

                    "instrument": "drum_kit",

                    "bar": bar,
                    "beat": beat,

                    "tick": tick,
                    "raw_tick": raw_tick,
                    "source_tick": source_tick,
                    "source_ppq": source_ppq,

                    "onset_ms": None,

                    "stroke": stroke,

                    "is_accent": None,
                    "is_ornament": None,
                    "is_roll": None,

                    "duration": None,

                    "velocity": msg.velocity,
                    "key_code": msg.note,

                    "input_group": "midi",

                    "quantization": f"1/{GRID_DIVISION}",
                    "grid_index": grid_index,

                    "input_method": "midi",

                    # GMD metadata
                    "drummer": metadata["drummer"],
                    "session": metadata["session"],
                    "style": metadata["style"],
                    "bpm": metadata["bpm"],
                    "beat_type": metadata["beat_type"],
                    "time_signature_original": metadata["time_signature"],
                    "split": metadata["split"],
                    "midi_filename": metadata["midi_filename"],
                })

                event_counter += 1

    return events


def main():
    info = pd.read_csv(INFO_CSV)

    all_events = []

    for index, row in info.iterrows():

        midi_path = GMD_ROOT / row["midi_filename"]

        if not midi_path.exists():
            print("找不到 MIDI：", midi_path)
            continue

        segment_id = f"GMD{index + 1:06d}"

        print(
            f"[{index + 1}/{len(info)}]",
            row["midi_filename"]
        )

        try:
            events = parse_midi(
                midi_path,
                row,
                segment_id
            )

            all_events.extend(events)

        except Exception as e:
            print("轉換失敗：", midi_path)
            print("錯誤：", e)

    df = pd.DataFrame(all_events)

    df.to_csv(
        OUTPUT_CSV,
        index=False,
        encoding="utf-8-sig"
    )

    print()
    print("================================")
    print("轉換完成")
    print("MIDI 筆數：", len(info))
    print("Event 總數：", len(df))
    print("輸出檔案：", OUTPUT_CSV)
    print("================================")


if __name__ == "__main__":
    main()