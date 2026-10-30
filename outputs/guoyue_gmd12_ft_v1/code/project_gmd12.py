"""Audited functional projection of 32 guoyue instruments to existing GMD strokes.

Only instrument tokens change. Repeated simultaneous events are never deduplicated.
The full 200-token vocabulary is retained to control model size/initialization.
"""
from collections import Counter
from copy import deepcopy
from hashlib import sha256
import argparse
import json
from pathlib import Path

from .data import file_hash, load_data
from .prepare_finetune import fingerprint


# This is an experimental function/timbre hypothesis, not an identity assertion.
MAPPING = {
    "小鈸": ("splash", "短促、明亮的金屬標點，對應本 GMD 中標為 splash 的鈸邊分類", "中"),
    "小鑼": ("ride_bell", "清楚、集中的金屬點音與句讀功能", "中"),
    "大鑼": ("crash", "較大幅度的金屬重點與段落強調", "中"),
    "小鼓": ("snare", "清晰鼓擊、節奏推進及加花功能", "中"),
    "大鼓": ("kick", "低音節奏支點與重拍功能", "高"),
    "定音鼓": ("tom_mid", "有共鳴的鼓聲與鼓句功能；本批音高欄位關閉，暫固定中音 tom", "中"),
    "大鈸": ("crash", "強調重點、爆發性金屬聲與收句功能", "高"),
    "方響": ("ride_bell", "金屬定音點擊的集中起音；此對應不保留音高差異", "低"),
    "木魚": ("hihat_closed", "短音、規律拍點與細分節奏功能，依用途而非材質對應", "中"),
    "鈴鼓": ("hihat_open", "帶鈴片的噪音質地與較鬆散、帶尾音的節奏鋪陳", "中"),
    "低音鼓": ("kick", "低音節奏支點與重拍功能", "高"),
    "軍鈸": ("crash", "鈸類重點與句讀；未另外假設實際尺寸", "中"),
    "小堂鼓": ("tom_high", "相對小型鼓的鼓句與較高音域位置", "中"),
    "大堂鼓": ("tom_low", "較低音域的共鳴鼓聲與鼓句；與 kick 的短低音支點分開", "中"),
    "大鈸（獅鈸）": ("crash", "大型對鈸的重點與段落強調功能", "高"),
    "吊鈸": ("ride", "持續金屬色彩、節拍鋪陳及延續聲響；輪奏欄位另外保留", "中"),
    "碰鈴": ("ride_bell", "清亮、集中的鐘鈴類點音", "中"),
    "低音鑼": ("crash", "依鑼類金屬重點與長共鳴用途對應；低音差異在此合併", "中"),
    "大銅磐": ("crash", "暫依銅製打擊名稱與共鳴標點用途分類，未核實實物與擊法", "低"),
    "低音堂鼓": ("tom_low", "明確的低音域鼓句與共鳴鼓聲", "高"),
    "班鼓": ("snare", "暫依班／板鼓類領奏、密集節奏與清楚起音功能分類", "中"),
    "子弟鑼": ("crash", "專有鑼種暫依金屬句讀用途分類，未由名稱推定尺寸音高", "低"),
    "高音堂鼓": ("tom_high", "明確的高音域鼓句與共鳴鼓聲", "高"),
    "最下方鑼聲部": ("crash", "只確認鑼類標點用途；聲部位置不視為尺寸或音高證據", "低"),
    "沙鎚": ("hihat_closed", "短促沙聲、規律細分與節奏鋪陳功能", "中"),
    "沙槌": ("hihat_closed", "依沙聲細分用途分類；保留原名在對照表，與沙鎚共用目標", "中"),
    "風鈴": ("ride_bell", "暫依鐘鈴類金屬亮點分類，不能重現風鈴群音與滑奏", "低"),
    "風鑼": ("crash", "金屬共鳴、段落色彩與強調功能", "中"),
    "鈴鑼": ("ride_bell", "暫依名稱採鐘鈴／鑼類集中的金屬點音，待實際音色核對", "低"),
    "三角鐵": ("ride_bell", "清亮高頻金屬點音與拍點功能", "中"),
    "低音大鑼": ("crash", "大型鑼的段落重點與長共鳴；此對應不保留低音差異", "中"),
    "文鑼": ("ride_bell", "暫採較收斂的鑼類句讀／點音功能，專名含義仍待實聽核對", "低"),
}

TARGET_LABELS = {"kick": "低音大鼓", "snare": "小鼓／軍鼓", "tom_high": "高音通鼓",
    "tom_mid": "中音通鼓", "tom_low": "低音通鼓", "hihat_closed": "閉合踩鈸",
    "hihat_open": "開放踩鈸", "hihat_pedal": "腳踩踩鈸", "ride": "Ride 鈸",
    "ride_bell": "Ride 鈸鐘", "crash": "Crash 鈸", "splash": "GMD splash 命名分類（鈸邊）"}


def mapping_rows(concepts, counts=None):
    counts = counts or {}
    result = []
    for concept in concepts:
        name = concept["label"]
        if name not in MAPPING:
            raise ValueError(f"No explicit mapping for instrument {name}")
        stroke, reason, confidence = MAPPING[name]
        token = f"<INST:{concept['id']}>"
        result.append({"source_name": name, "source_token": token, "target_token": f"<STROKE_{stroke}>",
            "target_name": TARGET_LABELS[stroke], "reason": reason, "confidence": confidence,
            "source_occurrences": counts.get(token, 0)})
    return result


def project_row(original, token_map, vocab):
    row = deepcopy(original)
    row["tokens"] = [token_map.get(token, token) for token in original["tokens"]]
    row["token_ids"] = [vocab[token] for token in row["tokens"]]
    return row


def project(source, original_package, output):
    source, original_package, output = Path(source).resolve(), Path(original_package).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError("Output folder already exists; use a new location")
    manifest = json.loads((source / "dataset_manifest.json").read_text(encoding="utf-8"))
    vocab = json.loads((source / "vocab.json").read_text(encoding="utf-8"))
    prepared = [json.loads(line) for line in (source / "sequences.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    full_source = [json.loads(line) for line in (original_package / "sequences.jsonl").read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    counts = Counter(token for row in full_source for token in row["tokens"] if token.startswith("<INST:"))
    table = mapping_rows(manifest["concepts"]["instrument"], counts)
    token_map = {row["source_token"]: row["target_token"] for row in table}
    actual_instruments = set(counts)
    if actual_instruments - token_map.keys() or any(t not in vocab for t in token_map.values()):
        raise ValueError("Instrument coverage or existing GMD vocabulary check failed")
    result = [project_row(row, token_map, vocab) for row in prepared]
    full = [project_row(row, token_map, vocab) for row in full_source]
    # Projection can create new exact duplicates even when original identities differed.
    validation_hashes = {fingerprint(row["tokens"]) for row in result if row["split"] == "validation"}
    new_duplicates = [row for row in result if row["split"] == "train" and fingerprint(row["tokens"]) in validation_hashes]
    if new_duplicates:
        raise ValueError("Projection created cross-split duplicates; a paired filtered control is needed: " +
                         json.dumps([{k:r[k] for k in ('title','segment_id')} for r in new_duplicates], ensure_ascii=False))
    output.mkdir(parents=True, exist_ok=False)
    for name, rows in (("sequences.jsonl", result), ("sequences_all.jsonl", full)):
        with (output / name).open("w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    (output / "vocab.json").write_text(json.dumps(vocab, ensure_ascii=False, indent=2), encoding="utf-8")
    mapping = {"version": "gmd12-functional-v1", "type": "functional_and_timbre_experiment",
        "identity_claim": False, "mappings": table, "token_map": token_map,
        "unused_targets": [f"<STROKE_{s}>" for s in TARGET_LABELS if f"<STROKE_{s}>" not in token_map.values()]}
    (output / "instrument_mapping.json").write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    projected_manifest = deepcopy(manifest)
    projected_manifest.update({"adapter": "guoyue-to-existing-gmd12-functional-v1", "instrument_projection": mapping,
        "control_dataset": str(source), "control_hashes": {name:file_hash(source / name) for name in ('sequences.jsonl','vocab.json','dataset_manifest.json')},
        "projection_source_hashes": {name:file_hash(original_package / name) for name in ('sequences.jsonl','vocab.json','rules.json','manifest.json')},
        "source_instrument_class_count": len(table), "projected_used_instrument_class_count": len(set(token_map.values())),
        "instrument_token_prefix": "STROKE", "vocabulary_policy": "Keep exact original 200 IDs including unused INST classes to control initialization and model size",
        "noninstrument_tokens_and_masks_preserved": True, "training_rows_and_order_preserved": True,
        "new_cross_split_duplicates": 0,
        "source_instrument_token_count": sum(counts.values()),
        "prepared_instrument_token_count": sum(token.startswith('<STROKE_') for row in result for token in row['tokens']),
        "projected_target_counts_all_sequences": dict(Counter(token for row in full for token in row["tokens"] if token.startswith("<STROKE_")))})
    (output / "dataset_manifest.json").write_text(json.dumps(projected_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    text = "# 國樂 → GMD 12 類功能對照表\n\n這是使用者授權的功能／近似音色實驗，不代表兩種樂器相同。僅依本批來源名稱與一般節奏角色判斷，未聆聽或重新辨識原譜。\n\n"
    text += f"本批有 {len(table)} 個國樂分類，對應後使用 {len(set(token_map.values()))} 個 GMD 分類；腳踩踩鈸未使用。定音鼓暫固定 tom_mid，因本次音高欄位關閉。GMD 的 splash 命名沿用原轉換器，其來源為 Crash 1 鈸邊分類。\n\n"
    text += "| 國樂來源 | 原 token | GMD 目標 | 目標 token | 全部132段出現次數 | 判斷把握 | 理由 |\n| --- | --- | --- | --- | ---: | --- | --- |\n"
    for row in table:
        text += f"| {row['source_name']} | `{row['source_token']}` | {row['target_name']} | `{row['target_token']}` | {row['source_occurrences']} | {row['confidence']} | {row['reason']} |\n"
    text += "\n## 轉換保留內容\n\n原始132段全部轉換保存為 sequences_all.jsonl；沿用上次實驗篩選及切分的130段保存為 sequences.jsonl，用於本次訓練。只有樂器 token 與其數字 ID 改變；原始標記、來源位置、時間、事件順序、事件數、輪奏、重音、裝飾音、延音、時值及 loss_mask 保留。相同時間合併成同一類的多個事件仍全部保留。\n\n保留和對照模型完全相同的200-token詞彙表與ID，因此GMD原有的STROKE權重可以直接沿用；舊INST token仍在詞彙表中，但不作為本次訓練輸入或目標。\n"
    (output / "樂器對照表.md").write_text(text, encoding="utf-8")
    load_data(output / "sequences.jsonl", output / "vocab.json")
    return projected_manifest


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--original-package", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    m = project(args.source, args.original_package, args.output)
    print(json.dumps({k:m[k] for k in ('source_instrument_class_count','projected_used_instrument_class_count','split_counts',
        'prepared_instrument_token_count','source_instrument_token_count','new_cross_split_duplicates')},ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
