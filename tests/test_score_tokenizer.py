"""Behavioral checks for normalization, reproducibility and musical timing."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from score_tokenizer.core import FIELDS, Store, TokenizerError, validate_source


def event(instrument="定音鼓", onset=0, *, roll=None, attack=True, duration=1):
    return {"event_id": f"e-{instrument}-{onset}", "kind": "note", "instrument_text": instrument,
            "instrument_id": "source-" + instrument, "technique_id": None, "technique_text": None,
            "onset": {"n": onset, "d": 1}, "duration": {"n": duration, "d": 1},
            "pitches": [], "is_roll": roll, "is_accent": None, "is_ornament": None,
            "tie": "none" if attack else "continue", "attack": attack, "dynamic": None,
            "raw_symbol": "原譜標記", "training_mask": {"instrument": True, "onset": True,
            "duration": True, "technique": False, "pitches_midi": False, "pitches_degree": False,
            "is_roll": roll is not None, "is_accent": False, "is_ornament": False,
            "tie": True, "attack": attack is not None, "dynamic": False, "slur": False}}


def source(title="測試曲"):
    return {"format": "musicai-reviewed-training", "schema_version": "1.0", "ppq": 960,
            "title": title, "source_package_id": "package-test", "instruments": [],
            "segments": [{"segment_id": "seg", "name": "段一", "start_bar": 1, "end_bar": 3,
                "playback_order": [{"source_bar": 1}, {"source_bar": 2}, {"source_bar": 1}, {"source_bar": 3}]}],
            "bars": [{"number": 1, "time_signature": "4/4", "tempo_quarter": 120,
                      "events": [event(roll=True), event("小鑼", roll=False)]},
                     {"number": 2, "time_signature": "3/4", "tempo_quarter": 120, "events": []},
                     {"number": 3, "time_signature": "2/4", "tempo_quarter": 120, "events": [event(onset=1)]}]}


class TokenizerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "library")

    def import_data(self, data=None, filename="piece.json"):
        path = self.root / filename
        path.write_text(json.dumps(data or source(), ensure_ascii=False), encoding="utf-8")
        piece_id, added = self.store.import_file(path)
        return piece_id, path

    def map_instruments(self, piece_id):
        for row in self.store.inventory([piece_id]):
            if row["field"] == "instrument":
                concept = self.store.add_concept("instrument", row["source"])
                self.store.set_rule("instrument", row["source"], "", concept["id"], scope="global")

    def test_missing_mapping_blocks_only_enabled_fields(self):
        data = source()
        data["bars"][0]["events"][0]["technique_text"] = "特殊記法 A"
        piece_id, _ = self.import_data(data)
        with self.assertRaisesRegex(TokenizerError, "待確認"):
            self.store.convert([piece_id])
        self.map_instruments(piece_id)
        result = self.store.convert([piece_id])
        self.assertTrue(result["sequences"])
        self.store.profile["fields"]["technique"] = True
        with self.assertRaisesRegex(TokenizerError, "特殊記法 A"):
            self.store.convert([piece_id])

    def test_optional_fields_toggle_without_losing_source_or_rules(self):
        piece_id, path = self.import_data()
        original = path.read_bytes()
        self.map_instruments(piece_id)
        off = self.store.convert([piece_id])["sequences"][0]
        self.assertFalse(any(t.startswith("<ROLL") for t in off["tokens"]))
        self.store.clone_profile("含輪奏版")
        self.store.profile["fields"]["is_roll"] = True
        self.store.save()
        on = self.store.convert([piece_id])["sequences"][0]
        self.assertTrue(any(t.startswith("<ROLL:") for t in on["tokens"]))
        unknown_positions = [i for i, t in enumerate(on["tokens"]) if t == "<ROLL_UNKNOWN>"]
        self.assertTrue(unknown_positions)
        self.assertTrue(all(on["loss_mask"][i] == 0 for i in unknown_positions))
        known_false = next(c["id"] for c in self.store.data["concepts"]["is_roll"] if c["label"] == "無輪奏")
        self.assertTrue(any(t == f"<ROLL:{known_false}>" and mask == 1 for t, mask in zip(on["tokens"], on["loss_mask"])))
        loaded = Store(self.store.root)
        self.assertTrue(loaded.profile["fields"]["is_roll"])
        self.assertFalse(loaded.data["profiles"]["basic"]["fields"]["is_roll"])
        self.assertEqual(original, path.read_bytes())
        self.assertEqual(loaded.data["pieces"][piece_id]["payload"], source())

    def test_repeats_silence_and_variable_meter_keep_exact_time(self):
        piece_id, _ = self.import_data()
        self.map_instruments(piece_id)
        sequence = self.store.convert([piece_id])["sequences"][0]
        self.assertEqual(sequence["source_bars"], [1, 2, 1, 3])
        self.assertEqual(sequence["total_ticks"], (4 + 3 + 4 + 2) * 960)
        self.assertEqual(sequence["tokens"].count("<HIT_SET>"), 3)
        self.assertEqual(sequence["tokens"].count("<EVENT>"), 5)
        self.assertEqual(sequence["tokens"].count("<METER_3/4>"), 1)
        shifts = [int(t[len("<TIME_SHIFT_"):-1]) for t in sequence["tokens"] if t.startswith("<TIME_SHIFT_")]
        self.assertEqual(sum(shifts), sequence["total_ticks"])
        self.assertIn(2880, shifts)  # Empty 3/4 bar remains in the sequence.
        self.assertEqual(len(sequence["tokens"]), len(sequence["loss_mask"]))
        self.assertEqual(len(sequence["tokens"]), len(sequence["provenance"]))

    def test_projection_preserves_identity_multiplicity_and_other_profile(self):
        data = source()
        data["bars"][0]["events"] = [event("大鑼"), event("小鑼")]
        piece_id, _ = self.import_data(data)
        self.map_instruments(piece_id)
        concepts = {c["label"]: c["id"] for c in self.store.data["concepts"]["instrument"]}
        self.store.clone_profile("合併鑼")
        self.store.set_projection("instrument", concepts["大鑼"], concepts["小鑼"])
        result = self.store.convert([piece_id])
        self.assertTrue(result["collisions"])
        self.assertEqual(result["sequences"][0]["tokens"].count("<EVENT>"), 5)
        target, _ = self.store.resolve("instrument", "大鑼", "", piece_id)
        self.assertEqual(target, concepts["大鑼"])
        self.store.data["active_profile"] = "basic"
        self.assertEqual(self.store.project("instrument", concepts["大鑼"]), concepts["大鑼"])

    def test_global_alias_reused_but_pitch_local_rule_not_guessed(self):
        first, _ = self.import_data()
        concept = self.store.add_concept("instrument", "定音鼓")
        self.store.set_rule("instrument", "Timpani", "", concept["id"], scope="global")
        pitch = self.store.add_concept("pitch", "低音鼓位")
        self.store.set_rule("pitch", "譜面:A", "定音鼓", pitch["id"], scope="piece", piece_id=first)
        second_data = source("另一首")
        second_data["source_package_id"] = "other-package"
        second, _ = self.import_data(second_data, "other.json")
        self.assertEqual(self.store.resolve("instrument", "TIMPANI", "", second)[0], concept["id"])
        self.assertIsNone(self.store.resolve("pitch", "譜面:A", "定音鼓", second)[0])
        self.assertIsNone(self.store.resolve("pitch", "譜面:1", "定音鼓", first)[0])

    def test_piece_rule_has_priority_and_projection_cycles_rejected(self):
        piece_id, _ = self.import_data()
        first = self.store.add_concept("instrument", "小鑼")
        second = self.store.add_concept("instrument", "大鑼")
        self.store.set_rule("instrument", "鑼", "", first["id"], scope="global")
        self.store.set_rule("instrument", "鑼", "", second["id"], scope="piece", piece_id=piece_id)
        self.assertEqual(self.store.resolve("instrument", "鑼", "", piece_id), (second["id"], "本作品規則"))
        self.store.set_projection("instrument", first["id"], second["id"])
        with self.assertRaisesRegex(TokenizerError, "循環"):
            self.store.set_projection("instrument", second["id"], first["id"])
        self.assertEqual(self.store.project("instrument", first["id"]), second["id"])

    def test_exports_keep_ids_stable_and_include_reproduction_inputs(self):
        piece_id, path = self.import_data()
        self.map_instruments(piece_id)
        folder, manifest = self.store.export([piece_id], self.root / "exports")
        first_vocab = json.loads((folder / "vocab.json").read_text(encoding="utf-8"))
        self.store.profile["fields"]["is_roll"] = True
        second_folder, second_manifest = self.store.export([piece_id], self.root / "exports")
        next_vocab = json.loads((second_folder / "vocab.json").read_text(encoding="utf-8"))
        for token, value in first_vocab["token_to_id"].items():
            self.assertEqual(next_vocab["token_to_id"][token], value)
        self.assertNotEqual(folder, second_folder)
        self.assertEqual(manifest["ppq"], 960)
        self.assertIn("loss_mask[1:]", manifest["loss_mask_contract"])
        self.assertTrue((folder / "rules.json").exists())
        self.assertTrue((folder / "report.json").exists())
        sequence = json.loads((folder / "sequences.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual([first_vocab["tokens"][i] for i in sequence["token_ids"]], sequence["tokens"])
        self.assertEqual(sequence["group_id"], "package-test")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), source())

    def test_known_tie_continuation_never_becomes_new_attack(self):
        data = source()
        data["bars"][2]["events"][0] = event(onset=1, attack=False)
        piece_id, _ = self.import_data(data)
        self.map_instruments(piece_id)
        sequence = self.store.convert([piece_id])["sequences"][0]
        self.assertIn("<ATTACK_NO>", sequence["tokens"])
        self.assertFalse(any(t.startswith("<TIE:") for t in sequence["tokens"]))

    def test_no_rounding_unknown_onset_or_missing_repeat_route(self):
        data = source()
        data["bars"][0]["events"][0]["onset"] = {"n": 1, "d": 7}
        with self.assertRaisesRegex(TokenizerError, "精確"):
            validate_source(data)
        data = source()
        data["bars"][0]["events"][0]["training_mask"]["onset"] = False
        with self.assertRaisesRegex(TokenizerError, "未知起點"):
            validate_source(data)
        data = source()
        data["segments"][0]["kind"] = "sequence"
        del data["segments"][0]["playback_order"]
        with self.assertRaisesRegex(TokenizerError, "缺少 playback_order"):
            validate_source(data)

    def test_import_deduplicates_by_content_and_snapshots_source(self):
        piece_id, path = self.import_data()
        duplicate, new = self.store.import_file(path)
        self.assertEqual(duplicate, piece_id)
        self.assertFalse(new)
        path.write_text("{}", encoding="utf-8")
        self.assertEqual(Store(self.store.root).data["pieces"][piece_id]["payload"], source())

    def test_stale_window_cannot_overwrite_saved_mapping(self):
        self.store.save()
        other = Store(self.store.root)
        self.store.add_concept("instrument", "大鑼")
        self.store.save()
        other.add_concept("instrument", "小鑼")
        with self.assertRaisesRegex(TokenizerError, "另一個視窗"):
            other.save()
        self.assertEqual(Store(self.store.root).data["concepts"]["instrument"][0]["label"], "大鑼")

    def test_rename_keeps_identity_and_token_ids(self):
        piece_id, _ = self.import_data()
        self.map_instruments(piece_id)
        folder, _ = self.store.export([piece_id], self.root / "exports")
        before = self.store.convert([piece_id])["sequences"][0]["tokens"]
        concept = self.store.data["concepts"]["instrument"][0]
        self.store.rename_concept("instrument", concept["id"], "定音鼓（統一名稱）")
        after = self.store.convert([piece_id])["sequences"][0]["tokens"]
        self.assertEqual(before, after)
        self.assertTrue((folder / "manifest.json").exists())

    def test_real_jchw_order_and_full_conversion(self):
        path = Path(__file__).resolve().parents[1] / "projects" / "sheetData" / "JCHW.json"
        if not path.exists():
            path = Path(__file__).resolve().parents[1] / "outputs" / "六份樂譜_20261029_TOKEN版" / "訓練JSON" / "節慶歡舞.json"
        if not path.exists():
            self.skipTest("local user sample not available")
        piece_id, _ = self.store.import_file(path)
        self.map_instruments(piece_id)
        result = self.store.convert([piece_id])
        self.assertEqual(len(result["sequences"]), 13)
        self.assertEqual(sum(len(s["source_bars"]) for s in result["sequences"]), 197)
        custom = next(s for s in result["sequences"] if s["segment_name"] == "SEG15")
        self.assertEqual(custom["source_bars"], [153, 154, *range(1, 9), *range(1, 8), *range(9, 18)])
        for sequence in result["sequences"]:
            shifts = [int(t[len("<TIME_SHIFT_"):-1]) for t in sequence["tokens"] if t.startswith("<TIME_SHIFT_")]
            self.assertEqual(sum(shifts), sequence["total_ticks"])


if __name__ == "__main__":
    unittest.main()
