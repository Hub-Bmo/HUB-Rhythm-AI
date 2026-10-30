"""Launch the local tokenizer; optionally import sources or export headlessly."""
import argparse
from pathlib import Path

from score_tokenizer.core import Store


def main():
    parser = argparse.ArgumentParser(description="MusicAI 國樂 token 化工具")
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parent / "projects" / "tokenizer-workspace")
    parser.add_argument("--import", dest="sources", nargs="*", default=[])
    parser.add_argument("--export", type=Path, help="以目前保存的設定匯出全部已匯入作品，不啟動視窗")
    parser.add_argument("--page", choices=("pieces", "mapping", "settings", "preview"), default="pieces", help="開啟指定頁籤")
    args = parser.parse_args()
    if args.export:
        store = Store(args.workspace)
        for source in args.sources:
            store.import_file(source)
        folder, manifest = store.export(list(store.data["pieces"]), args.export)
        print(f"{folder}\n{manifest['sequence_count']} sequences / {manifest['token_count']} tokens")
    else:
        from score_tokenizer.app import run
        run(args.workspace, args.sources, args.page)


if __name__ == "__main__":
    main()
