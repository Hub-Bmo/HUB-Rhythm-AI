from collections import Counter
import json
from pathlib import Path
import unittest

from rhythm_transformer.data import load_data
from rhythm_transformer.project_gmd12 import MAPPING, mapping_rows, project_row
from rhythm_transformer.compare_gmd12 import instrument_groups
import torch


class GMD12ProjectionTests(unittest.TestCase):
    def test_unknown_instrument_requires_an_explicit_decision(self):
        with self.assertRaisesRegex(ValueError, "No explicit mapping"):
            mapping_rows([{"id": "C99999", "label": "未見過的新樂器"}])

    def test_projection_preserves_duplicate_events_and_unknown_masks(self):
        tokens = ["<HIT_SET>", "<EVENT>", "<INST:A>", "<ROLL_UNKNOWN>", "<END_EVENT>",
                  "<EVENT>", "<INST:B>", "<ROLL_UNKNOWN>", "<END_EVENT>", "<END_HIT_SET>"]
        original = {"tokens": tokens, "loss_mask": [1,1,1,0,1,1,1,0,1,1], "token_ids": list(range(10)), "source_bars": [1]}
        mapping = {"<INST:A>": "<STROKE_crash>", "<INST:B>": "<STROKE_crash>"}
        vocab = {token: index for index, token in enumerate(dict.fromkeys(tokens + ["<STROKE_crash>"]))}
        result = project_row(original, mapping, vocab)
        self.assertEqual(result["tokens"].count("<STROKE_crash>"), 2)
        self.assertEqual(result["tokens"].count("<EVENT>"), 2)
        self.assertEqual(result["loss_mask"], original["loss_mask"])
        self.assertEqual(original["tokens"], tokens)
        self.assertEqual(result["source_bars"], [1])

    def test_common_instrument_metric_adds_alias_probabilities_and_keeps_invalid_mass(self):
        vocab = {"<INST:A>":0, "<INST:B>":1, "<STROKE_crash>":2, "<STROKE_snare>":3, "<END_EVENT>":4}
        mapping = {"<INST:A>":"<STROKE_crash>", "<INST:B>":"<STROKE_crash>"}
        classes, groups, token_classes = instrument_groups(vocab, mapping)
        self.assertEqual(sorted(i for group in groups for i in group), list(range(5)))
        self.assertEqual(token_classes[0],token_classes[2])
        probabilities = torch.tensor([0.3,0.3,0.0,0.35,0.05])
        summed = torch.tensor([probabilities[group].sum() for group in groups])
        self.assertAlmostEqual(float(summed.sum()),1.0,places=6)
        self.assertEqual(classes[int(summed.argmax())],"<STROKE_crash>")
        self.assertAlmostEqual(float(summed[-1]),0.05,places=6)

    def test_real_projection_changes_only_instruments_and_ids(self):
        root = Path(__file__).resolve().parents[1]
        output = root / "outputs/guoyue_gmd12_tokens_v1"
        if not output.exists():
            self.skipTest("local experiment dataset not available")
        control = root / "projects/guoyue_transformer_ft_v1/data"
        parse = lambda path: [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        before, after = parse(control / "sequences.jsonl"), parse(output / "sequences.jsonl")
        vocab = json.loads((output / "vocab.json").read_text(encoding="utf-8"))
        self.assertEqual(vocab, json.loads((control / "vocab.json").read_text(encoding="utf-8")))
        table = json.loads((output / "instrument_mapping.json").read_text(encoding="utf-8"))
        mapping = table["token_map"]
        self.assertEqual(len(table["mappings"]), 32)
        self.assertEqual(len(before), len(after))
        for old, new in zip(before, after):
            for field, value in old.items():
                if field not in {"tokens", "token_ids"}:
                    self.assertEqual(value, new[field])
            self.assertEqual(new["tokens"], [mapping.get(t, t) for t in old["tokens"]])
            self.assertFalse(any(t.startswith("<INST:") for t in new["tokens"]))
            self.assertEqual(new["token_ids"], [vocab[t] for t in new["tokens"]])
        _, splits, _ = load_data(output / "sequences.jsonl", output / "vocab.json")
        self.assertEqual(len(splits["train"]), 100)
        self.assertEqual(len(splits["validation"]), 30)
        full = parse(output / "sequences_all.jsonl")
        self.assertEqual(len(full), 132)
        self.assertEqual(sum(t.startswith("<STROKE_") for row in full for t in row["tokens"]), 5316)
        self.assertEqual(sum(len(row["loss_mask"]) - sum(row["loss_mask"]) for row in full), 3157)
