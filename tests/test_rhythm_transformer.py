import json
from pathlib import Path
import tempfile
import unittest

import torch
from torch.nn import functional as F

from rhythm_transformer.data import TokenDataset, collate, masked_loss_sum, load_data
from rhythm_transformer.generate import allowed_tokens, parse_events
from rhythm_transformer.model import ModelConfig, RhythmTransformer
from rhythm_transformer.train import atomic_checkpoint, learning_rate


class TransformerTests(unittest.TestCase):
    def setUp(self):
        torch.set_num_threads(2)
        torch.manual_seed(42)
        self.model = RhythmTransformer(ModelConfig(vocab_size=20, context_length=32,
            d_model=32, n_layers=2, n_heads=4, d_ff=64, dropout=0.0)).eval()

    def test_future_tokens_cannot_affect_earlier_logits(self):
        first = torch.tensor([[1, 2, 3, 4, 5, 6]])
        second = torch.tensor([[1, 2, 3, 9, 8, 7]])
        with torch.no_grad():
            torch.testing.assert_close(self.model(first)[:, :3], self.model(second)[:, :3])
            torch.testing.assert_close(self.model(first[:, :3]), self.model(first)[:, :3])

    def test_right_padding_does_not_change_valid_logits_or_loss(self):
        ids = torch.tensor([[1, 2, 3]])
        padded = torch.tensor([[1, 2, 3, 0, 0]])
        with torch.no_grad():
            short = self.model(ids)
            long = self.model(padded)
        torch.testing.assert_close(short, long[:, :3])
        loss = F.cross_entropy(short.flatten(0, 1), torch.tensor([2, 3, 4]))
        padded_loss = F.cross_entropy(long.flatten(0, 1), torch.tensor([2, 3, 4, 0, 0]), ignore_index=0)
        torch.testing.assert_close(loss, padded_loss)
        masked_sum, count = masked_loss_sum(long, torch.tensor([[2, 3, 4, 0, 0]]), torch.ones((1, 5), dtype=torch.bool), 0)
        torch.testing.assert_close(masked_sum / count, padded_loss)

    def test_dataset_matches_existing_lstm_windows(self):
        data = TokenDataset([list(range(1030))])
        self.assertEqual(len(data), 3)
        self.assertEqual(data[0][0].tolist(), list(range(511)))
        self.assertEqual(data[0][1].tolist(), list(range(1, 512)))
        self.assertEqual(data[1][0][0].item(), 512)
        self.assertEqual(data[2][1].tolist(), list(range(1025, 1030)))
        x, y = collate([data[0], data[2]], pad_id=0)
        self.assertEqual(x.shape, (2, 511))
        self.assertTrue((y[1, 5:] == 0).all())

    def test_expansion_preserves_all_old_logits(self):
        expanded = self.model.expanded(25).eval()
        ids = torch.tensor([[1, 2, 3]])
        with torch.no_grad():
            torch.testing.assert_close(self.model(ids), expanded(ids)[:, :, :20])
        self.assertEqual(expanded.config.vocab_size, 25)

    def test_checkpoint_can_load_safely_and_restore_model(self):
        directory = Path(__file__).resolve().parents[1] / ".verification-temp"
        directory.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as temp:
            path = Path(temp) / "model.pt"
            atomic_checkpoint(path, {"model": self.model.state_dict(), "rng": torch.get_rng_state()})
            checkpoint = torch.load(path, weights_only=True)
            other = RhythmTransformer(self.model.config).eval()
            other.load_state_dict(checkpoint["model"])
            ids = torch.tensor([[1, 2, 3]])
            with torch.no_grad():
                torch.testing.assert_close(self.model(ids), other(ids))

    def test_generation_grammar_preserves_simultaneity_and_positive_progress(self):
        tokens = ["<BOS>", "<TIME_SHIFT_0>", "<HIT_SET>", "<STROKE_kick>", "<VEL_4>",
                  "<STROKE_snare>", "<VEL_8>", "<END_HIT_SET>", "<TIME_SHIFT_240>",
                  "<HIT_SET>", "<STROKE_snare>", "<VEL_3>", "<END_HIT_SET>", "<EOS>"]
        events = parse_events(tokens)
        self.assertEqual([e["tick"] for e in events], [0, 0, 240])
        vocab = {t: i for i, t in enumerate(dict.fromkeys(tokens))}
        self.assertNotIn("<TIME_SHIFT_0>", allowed_tokens(vocab, "time"))
        self.assertIn("<TIME_SHIFT_0>", allowed_tokens(vocab, "time", first=True))
        self.assertEqual(allowed_tokens(vocab, "next", hit_count=16), ["<END_HIT_SET>"])
        with self.assertRaises(ValueError):
            parse_events(["<HIT_SET>", "<STROKE_kick>", "<END_HIT_SET>"])

    def test_schedule_warms_up_and_decays(self):
        self.assertLess(learning_rate(0, 1000, 100, .001), learning_rate(99, 1000, 100, .001))
        self.assertGreater(learning_rate(100, 1000, 100, .001), learning_rate(999, 1000, 100, .001))

    def test_unknown_target_and_padding_have_zero_loss_gradient(self):
        logits = torch.randn(1, 4, 20, requires_grad=True)
        targets = torch.tensor([[2, 3, 4, 0]])
        mask = torch.tensor([[True, False, True, True]])
        loss_sum, count = masked_loss_sum(logits, targets, mask, 0)
        expected = F.cross_entropy(logits[0, [0, 2]], targets[0, [0, 2]], reduction="sum")
        torch.testing.assert_close(loss_sum, expected)
        self.assertEqual(int(count), 2)
        (loss_sum / count).backward()
        self.assertEqual(float(logits.grad[0, 1].abs().sum()), 0.0)
        self.assertEqual(float(logits.grad[0, 3].abs().sum()), 0.0)
        self.assertGreater(float(logits.grad[0, 0].abs().sum()), 0)

    def test_target_masks_shift_with_targets_and_padding_is_unknown(self):
        dataset = TokenDataset([{"ids": [1, 2, 3, 4], "loss_mask": [1, 0, 1, 1]},
                                {"ids": [1, 2], "loss_mask": [1, 1]}], return_mask=True)
        self.assertEqual(dataset[0][2].tolist(), [False, True, True])
        x, y, mask = collate([dataset[0], dataset[1]], pad_id=0)
        self.assertEqual(mask.tolist(), [[False, True, True], [True, False, False]])

    def test_prepared_six_works_preserve_masks_and_base_ids(self):
        root = Path(__file__).resolve().parents[1]
        directory = root / "projects/guoyue_transformer_ft_v1/data"
        if not (directory / "sequences.jsonl").exists():
            self.skipTest("local fine-tuning dataset unavailable")
        vocab, splits, metadata = load_data(directory / "sequences.jsonl", directory / "vocab.json")
        base = json.loads((root / "projects/gmd_transformer_v1/data/vocab.json").read_text(encoding="utf-8"))
        self.assertTrue(all(vocab[token] == index for token, index in base.items()))
        self.assertEqual(len(vocab), 200)
        self.assertEqual({row["title"] for row in splits["validation"]}, {"黃土情"})
        self.assertEqual(len({row["title"] for row in splits["train"]}), 5)
        self.assertFalse({row["group_id"] for row in splits["train"]} & {row["group_id"] for row in splits["validation"]})
        report = metadata["dataset_manifest"]
        source = Path(report["source_folder"]) / "sequences.jsonl"
        original = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
        lookup = {(row["piece_id"], row["segment_id"]): row for row in original}
        for line in (directory / "sequences.jsonl").read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            old = lookup[(row["piece_id"], row["source_segment_id"])]
            self.assertEqual(row["tokens"], old["tokens"])
            self.assertEqual(row["loss_mask"], old["loss_mask"])
        self.assertEqual(len(report["removed_cross_split_duplicates"]), 2)


if __name__ == "__main__":
    unittest.main()
