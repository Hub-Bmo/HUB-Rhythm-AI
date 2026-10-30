# 重現與使用指南

## 1. 安裝環境

原實驗：Windows、Python3.13.13、PyTorch2.14.1+cu130、NumPy2.5.3、Pandas3.0.6、mido1.3.3，RTX4060Ti，BF16。每個run的config.json保存原始版本與參數。

建議使用Python3.13，建立獨立虛擬環境。CPU／CUDA請按PyTorch官方選擇器選擇相符版本：[官方安裝說明](https://pytorch.org/get-started/locally/)。

Windows範例：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install torch==2.14.1 --index-url https://download.pytorch.org/whl/cpu
python -m pip install -r requirements-preprocess.txt
```

上面是CPU環境。NVIDIA電腦先依官方指令安裝相符的CUDA wheel，再安裝requirements-preprocess.txt。Mac可使用CPU流程；這個訓練器目前沒有MPS選項。原始3.13環境沒有被複製進此包，避免不同平台無法使用。

只有譜面辨識／校對工具需要額外的 `requirements-annotation.txt`。訓練、token轉換及驗證不用OpenAI API key，也不呼叫付費服務。

## 2. 不重訓，先確認資料與模型

```text
python reproduce.py doctor
python reproduce.py verify --models
python reproduce.py test
python reproduce.py evaluate
```

verify核對SHA256、模型結構、詞彙表與資料hash；evaluate重新計算V2/V3的驗證及共同分類比較，寫到verification/，不修改歷史結果。

## 3. 重建資料

```text
python reproduce.py rebuild-data --stage all
```

預設由保存的、可無損解壓的GMD事件CSV重建GMD tokens；國樂則由保存的完整工作庫及校對資料重建132段tokens，再重建V2切分與V3映射。程式逐項比較token、ID、mask、順序與來源資訊。結果寫在work/，摘要在verification/latest_rebuild.json。

若要連原GMD MIDI也從頭處理：

```text
python reproduce.py rebuild-data --stage all --from-midi
```

中間CSV會暫時解壓或重建到work/，約110MB，沒有覆寫保存資料。原始轉換腳本保存在legacy/gmd_preprocessing/。若重建與保存tokens不一致，程式會停止並保留log；不要默默替換實際訓練資料。

國樂的原始圖片、辨識結果及校對快照都保留，但圖片辨識／人工校對本身不是可保證完全一致的自動程序。

辨識／校對工具原始碼是選用的來源工具，未在本包跨平台重新驗證全部介面。歷史專案中的原圖來源路徑可能需要重新連結，OMR也需要另外安裝；本包已驗證的主流程是token重建、模型載入、訓練、續訓及評估。

## 4. 重跑訓練

先跑完整流程的小型檢查：

```text
python reproduce.py train --stage all --smoke --run-name smoke_check
```

每個階段只跑兩個batch與一輪，結果標記smoke_test，不能當正式模型。V2/V3都使用本次smoke V1作為起點。

正式重跑三階段：

```text
python reproduce.py train --stage all --device cuda --run-name my_run
```

沒有CUDA可用 `--device cpu`。all會新訓練V1，再各自從該V1訓練V2/V3；不會把V2拿去繼續訓練V3。

只重跑國樂實驗，使用保存的V1：

```text
python reproduce.py train --stage v2 --device cuda --run-name new_identity_run
python reproduce.py train --stage v3 --device cuda --run-name new_mapping_run
```

設定來自experiments.json；新輸出在reproduced_runs/指定名稱/v1、v2、v3。console log、history、config、best checkpoint及last.pt都會保存。

中斷後，同一run-name及stage加 `--resume`；要追加輪數可指定 `--epochs`。例如之前的smoke_check已跑完一輪，測試追加到第二輪：

```text
python reproduce.py train --stage v2 --smoke --run-name smoke_check --resume --epochs 2
```

更改總輪數會改變學習率排程，應當作續作實驗記錄。不要對保存的歷史outputs直接續訓。

若要接著訓練**已完成的正式V2**，先將`outputs/guoyue_transformer_ft_v1/`整個複製為`reproduced_runs/continue_v2/v2/`，連同最佳模型、last.pt、history及vocab保留，再執行：

```text
python reproduce.py train --stage v2 --device cuda --run-name continue_v2 --resume --epochs 90
```

這會由原第60輪繼續，結果寫在新資料夾。V1或V3同理，複製對應正式模型目錄，並使用相符的stage與資料；CPU可改為`--device cpu`。如果是增加新作品，還需要先檢查詞彙、資料切分與遮罩，不能直接套用舊的續訓資料檢查。

## 5. V1生成節奏

```text
python -m rhythm_transformer.generate --checkpoint outputs/gmd_transformer_v1/gmd_transformer_pretrained.pt --output reproduced_runs/listen/sample --bars 8 --bpm 138 --device cpu
```

會輸出JSON、token文字、MIDI與簡單合成鼓音WAV。GPU可改為cuda。已有8小節與32小節樣本在outputs/gmd_transformer_v1/。

這個生成器只驗證過V1的GMD語法。V2/V3保留新的國樂EVENT／BAR格式，目前尚未完成對應自由生成與真實音色播放，請勿直接用舊生成器宣稱國樂生成效果。

## 重現層級

1. 已訓練模型與實際輸入：可比對SHA256，得到原始保存檔。
2. 資料轉換：核對完整token及遮罩；不同系統換行可能使檔案hash不同，但語意比較仍會檢查每筆。
3. 重新訓練：保留seed與設定，驗證是否得到相近結果。浮點、GPU與套件差異可能造成權重不同，不承諾跨硬體逐bit相同。
