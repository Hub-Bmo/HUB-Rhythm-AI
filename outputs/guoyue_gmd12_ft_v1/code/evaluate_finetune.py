"""Reload a fine-tuned checkpoint and report loss by target field."""
import argparse
from collections import defaultdict
from functools import partial
import json
from pathlib import Path

import torch
from torch.nn import functional as F
from torch.utils.data import DataLoader

from .data import TokenDataset, collate, file_hash, load_data
from .model import ModelConfig, RhythmTransformer


def category(token):
    if token.startswith("<STROKE_"):
        return "instrument"
    for prefix, label in (("<INST:", "instrument"), ("<TIME_SHIFT_", "time_shift"),
        ("<DURATION_", "duration"), ("<ROLL:", "roll"), ("<ACCENT:", "accent"),
        ("<ORNAMENT:", "ornament"), ("<TIE:", "tie"), ("<ATTACK_", "attack")):
        if token.startswith(prefix):
            return label
    return "structure"


@torch.inference_mode()
def evaluate(checkpoint_path, data_dir, device="cuda"):
    checkpoint_path, data_dir = Path(checkpoint_path), Path(data_dir)
    torch.set_num_threads(4)
    checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=True)
    vocab, splits, metadata = load_data(data_dir / "sequences.jsonl", data_dir / "vocab.json")
    if vocab != checkpoint["vocab"] or metadata["sequence_sha256"] != checkpoint["data_metadata"]["sequence_sha256"]:
        raise ValueError("Checkpoint and evaluation data differ")
    model = RhythmTransformer(ModelConfig(**checkpoint["model_config"])).to(device).eval()
    model.load_state_dict(checkpoint["model_state_dict"])
    tokens = {index: token for token, index in vocab.items()}
    loader = DataLoader(TokenDataset(splits["validation"], return_mask=True), batch_size=8,
                        collate_fn=partial(collate, pad_id=model.config.pad_id))
    stats = defaultdict(lambda: {"count": 0, "loss_sum": 0.0, "correct": 0})
    dtype = torch.bfloat16 if device == "cuda" and torch.cuda.is_bf16_supported() else torch.float16
    from contextlib import nullcontext
    for x, y, mask in loader:
        x, y, mask = x.to(device), y.to(device), mask.to(device)
        with torch.autocast("cuda", dtype=dtype) if device == "cuda" else nullcontext():
            logits = model(x)
            losses = F.cross_entropy(logits.flatten(0, 1), y.flatten(), reduction="none").view_as(y)
        valid = mask & y.ne(model.config.pad_id)
        expected = y[valid].cpu().tolist()
        observed = logits.argmax(-1)[valid].cpu().tolist()
        values = losses[valid].float().cpu().tolist()
        for target, prediction, loss in zip(expected, observed, values):
            item = stats[category(tokens[target])]
            item["count"] += 1
            item["loss_sum"] += loss
            item["correct"] += int(target == prediction)
    base_path = Path(checkpoint["training_args"]["init_from"])
    base = torch.load(base_path, map_location="cpu", weights_only=True)
    base_preserved = all(vocab[token] == index for token, index in base["vocab"].items())
    changed_backbone = [name for name, value in base["model_state_dict"].items()
                        if name.startswith("blocks.") and not torch.equal(value, checkpoint["model_state_dict"][name])]
    source_report = metadata["dataset_manifest"]
    source = Path(source_report["source_folder"])
    unchanged = all(file_hash(source / name) == digest for name, digest in source_report["source_hashes"].items())
    if not base_preserved or not changed_backbone or not unchanged:
        raise AssertionError("Vocabulary, fine-tuning update or source integrity verification failed")
    total = sum(v["count"] for v in stats.values())
    result = {"checkpoint": str(checkpoint_path.resolve()), "checkpoint_sha256": file_hash(checkpoint_path),
        "best_epoch": checkpoint["epoch"], "validation_loss_reloaded": sum(v["loss_sum"] for v in stats.values()) / total,
        "known_validation_targets": total,
        "fields": {name: {"count": v["count"], "loss": v["loss_sum"] / v["count"],
                          "top1_accuracy": v["correct"] / v["count"]} for name, v in stats.items()},
        "source_files_unchanged": unchanged, "original_gmd_token_ids_preserved": base_preserved,
        "updated_backbone_tensor_count": len(changed_backbone), "base_checkpoint_sha256": file_hash(base_path),
        "interpretation": "Teacher-forced validation on one held-out work; not listening quality, free generation, or an independent test result."}
    (checkpoint_path.parent / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--checkpoint", type=Path, required=True)
    p.add_argument("--data-dir", type=Path, required=True)
    p.add_argument("--device", choices=("cpu", "cuda"), default="cuda")
    args = p.parse_args()
    print(json.dumps(evaluate(args.checkpoint, args.data_dir, args.device), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
