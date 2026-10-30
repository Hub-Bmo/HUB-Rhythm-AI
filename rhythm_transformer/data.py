"""Keep the existing LSTM split and exact 512-token window construction."""
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path

import torch
from torch.nn import functional as F
from torch.utils.data import Dataset


def file_hash(path):
    digest = sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_data(sequence_path, vocab_path):
    vocab = json.loads(Path(vocab_path).read_text(encoding="utf-8-sig"))
    if not isinstance(vocab, dict) or any(type(v) is not int for v in vocab.values()) or sorted(vocab.values()) != list(range(len(vocab))):
        raise ValueError("Expected original contiguous token-to-ID vocabulary")
    for required in ("<PAD>", "<UNK>", "<BOS>", "<EOS>", "<HIT_SET>", "<END_HIT_SET>"):
        if required not in vocab:
            raise ValueError(f"Missing required token {required}")
    splits = {"train": [], "validation": [], "test": []}
    seen_ids, seen_files, seen_sequences, seen_groups = {}, {}, {}, {}
    counters, masked_counts = Counter(), Counter()
    for line in Path(sequence_path).read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        split = {"val": "validation", "valid": "validation"}.get(str(item.get("split", "")).lower(), str(item.get("split", "")).lower())
        if split not in splits:
            raise ValueError(f"Missing or unknown split for {item.get('segment_id')}")
        tokens = item["tokens"]
        if len(tokens) < 2 or tokens[0] != "<BOS>" or tokens[-1] != "<EOS>":
            raise ValueError("Source sequence must have BOS/EOS and at least two tokens")
        unknown = set(tokens) - vocab.keys()
        if unknown:
            raise ValueError(f"Unmapped tokens, refusing silent UNK substitution: {unknown}")
        if "<PAD>" in tokens:
            raise ValueError("PAD may only be introduced by right-padding batches")
        mask = item.get("loss_mask", [1] * len(tokens))
        if len(mask) != len(tokens) or any(type(x) is not int or x not in (0, 1) for x in mask):
            raise ValueError("loss_mask must contain one integer 0/1 per token")
        if any(t.endswith("_UNKNOWN>") and known for t, known in zip(tokens, mask)):
            raise ValueError("Unknown field tokens must have loss_mask=0")
        for registry, key in ((seen_ids, item.get("segment_id")), (seen_files, item.get("midi_filename")),
                              (seen_groups, item.get("group_id")),
                              (seen_sequences, sha256(json.dumps(tokens).encode()).hexdigest())):
            if key is not None:
                if key in registry and registry[key] != split:
                    raise ValueError(f"Cross-split duplicate source or sequence: {key}")
                registry[key] = split
        ids = [vocab[token] for token in tokens]
        splits[split].append({"ids": ids, "loss_mask": mask, "segment_id": item.get("segment_id"),
            "midi_filename": item.get("midi_filename"), "group_id": item.get("group_id"), "title": item.get("title")})
        counters[split] += len(ids)
        masked_counts[split] += len(mask) - sum(mask)
    if not splits["train"] or not splits["validation"]:
        raise ValueError("Train and validation splits must both be nonempty")
    metadata = {"vocab_size": len(vocab), "sequence_sha256": file_hash(sequence_path), "vocab_sha256": file_hash(vocab_path),
                "split_counts": {k: len(v) for k, v in splits.items()}, "token_counts": dict(counters),
                "ppq": 960, "window_policy": "legacy-lstm: chunks of 512 tokens; x=chunk[:-1], y=chunk[1:], step=512",
                "quantization": "existing source tokens unchanged; source conversion specifies PPQ960 / 1/16 grid"}
    metadata["masked_token_counts"] = dict(masked_counts)
    manifest = Path(sequence_path).parent / "dataset_manifest.json"
    if manifest.exists():
        metadata["dataset_manifest"] = json.loads(manifest.read_text(encoding="utf-8-sig"))
        metadata["quantization"] = "prepared source time tokens preserved exactly; no requantization"
    return vocab, splits, metadata


class TokenDataset(Dataset):
    def __init__(self, sequences, window_length=512, return_mask=False):
        self.samples = []
        self.masks = []
        self.return_mask = return_mask
        for item in sequences:
            seq = item["ids"] if isinstance(item, dict) else item
            mask = item.get("loss_mask", [1] * len(seq)) if isinstance(item, dict) else [1] * len(seq)
            if len(mask) != len(seq):
                raise ValueError("Sequence and mask lengths differ")
            for start in range(0, len(seq) - 1, window_length):
                chunk = seq[start:start + window_length]
                if len(chunk) >= 2:
                    self.samples.append(torch.tensor(chunk, dtype=torch.long))
                    self.masks.append(torch.tensor(mask[start:start + window_length], dtype=torch.bool))

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, index):
        seq = self.samples[index]
        if self.return_mask:
            return seq[:-1], seq[1:], self.masks[index][1:]
        return seq[:-1], seq[1:]


def collate(batch, pad_id):
    width = max(len(item[0]) for item in batch)
    xs = torch.full((len(batch), width), pad_id, dtype=torch.long)
    ys = xs.clone()
    masks = torch.zeros_like(xs, dtype=torch.bool)
    masked = len(batch[0]) == 3
    for index, item in enumerate(batch):
        x, y = item[:2]
        xs[index, :len(x)] = x
        ys[index, :len(y)] = y
        if masked:
            masks[index, :len(y)] = item[2]
    return (xs, ys, masks) if masked else (xs, ys)


def masked_loss_sum(logits, targets, target_mask, pad_id):
    """Mask target positions, independently of the causal attention operation."""
    valid = target_mask.bool() & targets.ne(pad_id)
    per_token = F.cross_entropy(logits.flatten(0, 1), targets.flatten(), ignore_index=pad_id, reduction="none").view_as(targets)
    return (per_token * valid).sum(), valid.sum()
