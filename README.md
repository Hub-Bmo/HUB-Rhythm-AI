# MusicAI：GMD → 國樂 Transformer 研究交接

這個資料夾是一份獨立的研究快照，整理於 **2026-10-30**。包含實際訓練資料、三版 Transformer 模型、續訓狀態、設定、逐輪 loss、來源影像與校對快照，以及可重跑的程式。原始位置的檔案均保留。

老師或其他 AI 請先讀 [AI_HANDOFF.md](AI_HANDOFF.md)，實際操作見 [重現指南](docs/REPRODUCE.md)，訓練過程整理見 [過程文件](docs/TRAINING_PROCESS.md)。

## 三個正式模型版本

| 交接版本 | 用途 | 最佳模型位置 | 驗證 loss |
| --- | --- | --- | ---: |
| V1 | GMD Transformer 基礎模型 | `outputs/gmd_transformer_v1/gmd_transformer_pretrained.pt` | 0.692777 |
| V2 | 保留國樂樂器分類的第一次微調 | `outputs/guoyue_transformer_ft_v1/guoyue_transformer_finetuned_v1.pt` | 0.406496 |
| V3 | 國樂樂器對應 GMD 類別的第二次微調 | `outputs/guoyue_gmd12_ft_v1/guoyue_gmd12_finetuned_v1.pt` | 0.367123 |

V2、V3 **各自從同一個 V1 開始**，不是 V1 → V2 → V3 的連續微調。舊 LSTM 另放在 `legacy/lstm/`，不與三版 Transformer 混用。

以上 loss 的資料及目標分類不同，不能把三個數字直接當作音樂品質排名。V3 的粗分類樂器與時值較好，時間間隔預測卻下降；詳見 [實驗結果](docs/EXPERIMENTS.md)。

## 需要的資料在哪裡

| 資料 | 位置 |
| --- | --- |
| 原始 GMD MIDI、info.csv、原授權 | `data/gmd/raw_midi/` |
| 實際 GMD 中間事件 CSV，無損壓縮 | `data/gmd/intermediate/gmd_events.csv.gz` |
| 實際訓練的 GMD token、133-token 詞彙表 | `projects/gmd_transformer_v1/data/` |
| 六首國樂已整理 JSON | `outputs/六份樂譜_20261029_TOKEN版/訓練JSON/` |
| 六首原始國樂 token、84-token 詞彙與規則 | `outputs/六份樂譜_20261029_TOKEN版/TOKEN/` |
| 樂器／欄位規則工作庫 | `outputs/六份樂譜_20261029_TOKEN版/TOKEN工作庫/` |
| V2 實際訓練資料與 200-token 詞彙表 | `projects/guoyue_transformer_ft_v1/data/` |
| V3 對應後的新 token 與完整對照表 | `outputs/guoyue_gmd12_tokens_v1/` |
| 原始譜面照片、辨識與校對專案快照 | `projects/*.score-recognition/`、`projects/*.score-review/` |
| 各版模型、last.pt、loss 歷程、設定、log、當時程式快照 | `outputs/*_v1/` 的模型目錄 |

保留 `projects/` 與 `outputs/` 的命名，是為了對應既有資料格式及程式。檔案清單與 SHA256 在 `package_manifest.json`，複製來源對照在 `provenance/`。

## 最快確認可以接手

建議 Python 3.13，先安裝對應 CPU 或 NVIDIA CUDA 的 PyTorch，再安裝 `requirements-preprocess.txt`。詳細命令見 [重現指南](docs/REPRODUCE.md)。

```text
python reproduce.py doctor
python reproduce.py verify --models
python reproduce.py test
python reproduce.py evaluate
```

先用已保存的 checkpoint 即可檢查結果，不需要重新訓練。若要測試三階段流程：

```text
python reproduce.py train --stage all --smoke --run-name smoke_check
```

正式重跑：

```text
python reproduce.py train --stage all --device cuda --run-name my_reproduction
```

新結果一律寫到 `reproduced_runs/`，不覆蓋保存的模型。沒有 NVIDIA GPU 可改 `--device cpu`；運行時間與數值可能不同。

## 上傳 GitHub

這份本機交接包尚未上傳，也未包含 `.git` 歷史或執行環境。請依 [GitHub 上傳說明](docs/GITHUB_UPLOAD.md) 建立 repository。模型與 GMD token 超過網頁單檔上傳限制，應使用 Git 工具推送。此包沒有 Git LFS 指標檔；下載後保留實際權重檔。

GMD 的原始 CC BY 4.0 授權與引用資料已附。六首國樂譜面與其資料未被套用新的開放授權，來源與授權範圍見 [資料來源](docs/DATA_SOURCES_AND_LICENSES.md)。
