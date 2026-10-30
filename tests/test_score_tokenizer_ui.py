import tempfile
from pathlib import Path
import tkinter as tk
import unittest

from score_tokenizer.app import TokenizerApp
from score_tokenizer.core import Store
from tests.test_score_tokenizer import source
import json


class TokenizerUITests(unittest.TestCase):
    def test_import_map_toggle_profile_preview_and_reload(self):
        with tempfile.TemporaryDirectory() as temp:
            root = tk.Tk()
            root.withdraw()
            try:
                app = TokenizerApp(root, Path(temp) / "library")
                path = Path(temp) / "sample.json"
                path.write_text(json.dumps(source(), ensure_ascii=False), encoding="utf-8")
                app.import_files([path])
                root.update()
                self.assertEqual(len(app.selected_piece_ids()), 1)
                app.mapping_tree.selection_set(app.mapping_tree.get_children())
                app.confirm_instrument_names()
                app.preview()
                self.assertIn("<樂器:定音鼓>", app.preview_box.get("1.0", "end"))
                app.store.clone_profile("含輪奏")
                app.refresh_settings()
                app.field_vars["is_roll"].set(True)
                app.save_fields()
                app.preview()
                self.assertIn("<輪奏:有輪奏>", app.preview_box.get("1.0", "end"))
                self.assertTrue(Store(app.store.root).profile["fields"]["is_roll"])
                self.assertFalse(Store(app.store.root).data["profiles"]["basic"]["fields"]["is_roll"])
                self.assertEqual(len(app.tabs.tabs()), 4)
            finally:
                root.destroy()


if __name__ == "__main__":
    unittest.main()
