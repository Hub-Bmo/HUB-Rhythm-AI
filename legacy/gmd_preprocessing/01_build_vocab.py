from pathlib import Path
import json
from collections import Counter


INPUT_JSONL = Path("gmd_sequences_v2.jsonl")
OUTPUT_VOCAB = Path("vocab.json")


SPECIAL_TOKENS = [
    "<PAD>",
    "<UNK>",
]


def main():

    counter = Counter()
    segment_count = 0

    print("讀取：", INPUT_JSONL)

    with open(INPUT_JSONL, "r", encoding="utf-8") as f:

        for line in f:
            line = line.strip()

            if not line:
                continue

            item = json.loads(line)

            tokens = item["tokens"]

            counter.update(tokens)
            segment_count += 1


    # 先放特殊 token
    vocab = {}

    for token in SPECIAL_TOKENS:
        vocab[token] = len(vocab)


    # 其餘 token 依出現頻率排序
    for token, count in counter.most_common():

        if token not in vocab:
            vocab[token] = len(vocab)


    # 存 vocab
    with open(
        OUTPUT_VOCAB,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            vocab,
            f,
            ensure_ascii=False,
            indent=2
        )


    print()
    print("============================")
    print("Vocabulary 建立完成")
    print("Segment 數：", segment_count)
    print("Vocabulary 大小：", len(vocab))
    print("輸出：", OUTPUT_VOCAB)
    print("============================")


    print()
    print("最常出現的 30 個 token：")

    for token, count in counter.most_common(30):

        print(
            f"{token:30s}",
            count
        )


    print()
    print("前 30 個 vocab ID：")

    for token, idx in list(vocab.items())[:30]:

        print(
            idx,
            token
        )


if __name__ == "__main__":
    main()