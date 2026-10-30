"""Lossless ID alignment of reviewed guoyue tokens to the pretrained vocabulary."""
from collections import Counter
from copy import deepcopy
from hashlib import sha256
import argparse
import json
from pathlib import Path
import shutil

from .data import file_hash, load_data


def fingerprint(tokens):
    return sha256(json.dumps(tokens).encode()).hexdigest()


def prepare(source, base_vocab_path, output, validation_title="黃土情"):
    source, output = Path(source).resolve(), Path(output).resolve()
    if (output / "sequences.jsonl").exists():
        raise ValueError("Prepared dataset already exists; use a new folder to preserve the first experiment")
    vocab_data = json.loads((source / "vocab.json").read_text(encoding="utf-8-sig"))
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8-sig"))
    rules = json.loads((source / "rules.json").read_text(encoding="utf-8-sig"))
    original_tokens = vocab_data["tokens"]
    original_ids = vocab_data["token_to_id"]
    if original_ids != {token: index for index, token in enumerate(original_tokens)}:
        raise ValueError("Source vocabulary IDs are inconsistent")
    canonical = lambda obj: json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if sha256(canonical(original_tokens).encode()).hexdigest() != manifest["vocab_sha256"]:
        raise ValueError("Source manifest vocabulary hash differs")
    if sha256(canonical(rules).encode()).hexdigest() != manifest["rules_sha256"]:
        raise ValueError("Source manifest rules hash differs")
    if manifest["ppq"] != 960:
        raise ValueError("Expected PPQ960; refusing a silent time rescale")
    rows = [json.loads(line) for line in (source / "sequences.jsonl").read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    for row in rows:
        tokens, mask, ids = row["tokens"], row["loss_mask"], row["token_ids"]
        if not row.get("group_id") or not row.get("piece_id"):
            raise ValueError("Piece/group identity is required for split safety")
        if len(tokens) != len(mask) or len(tokens) != len(ids):
            raise ValueError("Source sequence, ID and mask lengths differ")
        if any(type(x) is not int or x not in (0, 1) for x in mask):
            raise ValueError("Invalid source loss mask")
        if any(original_ids.get(token) != token_id for token, token_id in zip(tokens, ids)):
            raise ValueError("Source token IDs do not match the source vocabulary")
        if any(token.endswith("_UNKNOWN>") and known for token, known in zip(tokens, mask)):
            raise ValueError("Unknown field was marked as a known target")
        if tokens[:2] != ["<BOS>", "<SEGMENT_START>"] or tokens[-2:] != ["<SEGMENT_END>", "<EOS>"]:
            raise ValueError("Expected complete guoyue-v1 segments")
    if len(rows) != manifest["sequence_count"] or sum(len(r["tokens"]) for r in rows) != manifest["token_count"]:
        raise ValueError("Source sequence/token count differs from manifest")
    validation_groups = {row["group_id"] for row in rows if row["title"] == validation_title}
    if len(validation_groups) != 1:
        raise ValueError("Validation title must select exactly one complete work/group")
    validation = [r for r in rows if r["group_id"] in validation_groups]
    train = [r for r in rows if r["group_id"] not in validation_groups]
    validation_fingerprints = {fingerprint(r["tokens"]) for r in validation}
    removed = [r for r in train if fingerprint(r["tokens"]) in validation_fingerprints]
    train = [r for r in train if fingerprint(r["tokens"]) not in validation_fingerprints]
    if not train or not validation:
        raise ValueError("Need nonempty train and validation splits")
    unified = json.loads(Path(base_vocab_path).read_text(encoding="utf-8-sig"))
    if sorted(unified.values()) != list(range(len(unified))):
        raise ValueError("Base vocabulary is not contiguous")
    base_size = len(unified)
    for token in original_tokens:
        if token not in unified:
            unified[token] = len(unified)
    prepared = []
    for split, sequences in (("train", train), ("validation", validation)):
        for original in sequences:
            row = deepcopy(original)
            row["source_segment_id"] = row["segment_id"]
            row["segment_id"] = row["piece_id"] + "::" + row["segment_id"]
            row["split"] = split
            row["token_ids"] = [unified[token] for token in row["tokens"]]
            if row["tokens"] != original["tokens"] or row["loss_mask"] != original["loss_mask"]:
                raise AssertionError("Adapter modified token semantics or masks")
            prepared.append(row)
    output.mkdir(parents=True, exist_ok=True)
    (output / "vocab.json").write_text(json.dumps(unified, ensure_ascii=False, indent=2), encoding="utf-8")
    with (output / "sequences.jsonl").open("w", encoding="utf-8") as stream:
        for row in prepared:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    for name in ("vocab.json", "rules.json", "manifest.json", "report.json"):
        shutil.copyfile(source / name, output / ("source_" + name))
    report = {"token_format": "musicai-token-sequences-v1", "adapter": "append-only-vocabulary-alignment-v1",
        "ppq": 960, "source_folder": str(source), "source_hashes": {n: file_hash(source / n) for n in ("sequences.jsonl", "vocab.json", "rules.json", "manifest.json")},
        "base_vocab_sha256": file_hash(base_vocab_path), "base_vocab_size": base_size, "unified_vocab_size": len(unified),
        "new_tokens": len(unified) - base_size, "source_sequence_count": len(rows), "source_token_count": sum(len(r["tokens"]) for r in rows),
        "source_masked_tokens": sum(len(r["loss_mask"]) - sum(r["loss_mask"]) for r in rows),
        "split_policy": "5 complete works train / 1 complete work validation; no independent test set",
        "validation_title": validation_title,
        "split_counts": {split: sum(r["split"] == split for r in prepared) for split in ("train", "validation")},
        "split_token_counts": {split: sum(len(r["tokens"]) for r in prepared if r["split"] == split) for split in ("train", "validation")},
        "split_titles": {split: sorted({r["title"] for r in prepared if r["split"] == split}) for split in ("train", "validation")},
        "removed_cross_split_duplicates": [{"title": r["title"], "segment_id": r["segment_id"], "tokens": len(r["tokens"])} for r in removed],
        "concepts": vocab_data["concepts"], "profile": manifest["profile"],
        "local_to_unified_id": {str(index): unified[token] for index, token in enumerate(original_tokens)},
        "tokens_and_masks_preserved": True,
        "limitations": ["Only six works; exploratory first fine-tuning run.", "First four scores contain provisional excerpt preparation.",
                         "No pitch, fine technique, dynamics or tempo tokens in this selected profile."]}
    (output / "dataset_manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    # Exercise the same strict loader that the actual trainer will use.
    load_data(output / "sequences.jsonl", output / "vocab.json")
    return report


def main():
    p = argparse.ArgumentParser(description="Align reviewed guoyue token IDs to the GMD Transformer")
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--base-vocab", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--validation-title", default="黃土情")
    args = p.parse_args()
    report = prepare(args.source, args.base_vocab, args.output, args.validation_title)
    print(json.dumps({k: v for k, v in report.items() if k not in {"concepts", "profile", "local_to_unified_id"}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
