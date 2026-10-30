# GMD Transformer v1：訓練與交接

本次基礎模型使用既有 GMD token，沒有重新轉換 MIDI，也沒有改寫原始資料夾中的檔案或 LSTM 模型。

## 本次已完成結果

2026 年 10 月 29 日上午 06:36（台灣時間）完成正式 30 epochs。最佳模型為第 30 輪，validation loss **0.692777**、保留測試集 loss **0.690338**，正式訓練與最後評估約 149 秒。這是完整 GMD 訓練，不是 smoke test。

已重新載入保存的最佳 checkpoint，並核對 8 小節生成樣本的群組、MIDI 時間與 WAV 非靜音輸出。7 項模型／資料測試通過，另驗證 checkpoint 可從上一輪續訓。來源 JSONL／vocab 與本次快照的內容雜湊相同。

這是 GMD 基礎模型，尚未做本次國樂資料微調。Loss 是 token 預測指標，音樂品質仍需試聽評估。

## 資料與模型位置

- 原始資料：`C:/Users/Habu_/OneDrive/桌面/AI HIVE/midiConvert/1001_toJSONL_v2/`
- 唯讀來源的訓練快照：`projects/gmd_transformer_v1/data/`
- 正式訓練輸出：`outputs/gmd_transformer_v1/`
- `outputs/gmd_transformer_smoke/` 只用來驗證訓練流程，不是正式成果。

使用原始 `gmd_sequences_v2.jsonl` 與 `vocab.json`：133 個 token、1,150 筆序列、1,698,507 tokens。原始 split 保持 train 897、validation 124、test 129。訓練／驗證／測試視窗分別為 3,297／417／412。

來源轉換程式已把原 MIDI PPQ 換算為 960，再量化為 1/16。因此這份 token 的 `TIME_SHIFT_240` 是四分音符的四分之一，而不是使用原 MIDI 的 PPQ 480 來解讀。來源 token 保持原狀；微小時間偏移已不在這版量化 token 中。

為與既有 LSTM 比較，沿用其精確視窗方式：每 512 個原 token 一片，input 為前 511 個、target 為後 511 個；不足的最後一片以 PAD 靠右補齊。這也沿用舊版在視窗接點略去一個跨視窗預測的行為，並未偷偷更換取樣法。視窗可能落在 HIT_SET 內部，作為上下文切片使用；完整原始序列仍保留完整群組，生成器則限制合法群組語法。

## 模型與訓練

- 自回歸、Pre-LayerNorm Transformer；causal self-attention 不看未來。
- 4 層、維度 256、8 個注意力頭、前饋層 1024、上下文上限 512、Dropout 0.1。
- 約 336 萬參數；輸入 embedding 與輸出 head 分開，可擴充國樂 token。
- RTX 4060 Ti、PyTorch 2.14.1+cu130，BF16 混合精度。
- Batch 8；AdamW，最高學習率 0.0003，warmup 100 updates 後 cosine 衰減。
- 最多 30 epochs，連續 6 epochs 未改善 validation loss 則提前停止。
- validation 用於選最佳模型；test 只在最後以最佳模型評估一次，不參與挑選。
- 不跨來源重新隨機切分，並檢查不同 split 是否有相同來源或完全相同 token 序列。

與 LSTM 比較時，資料與視窗相同，但模型、優化器設定與學習率排程不同；不能將差異全部歸因於架構。

## 起來後先看哪些檔案

1. `outputs/gmd_transformer_v1/summary.json`：只有完整完成最後評估後才產生；包含最佳 epoch、validation loss、test loss。
2. `outputs/gmd_transformer_v1/gmd_transformer_pretrained.pt`：validation 表現最佳的模型，後續國樂微調從這份開始。
3. `outputs/gmd_transformer_v1/last.pt`：最後一次進度，含優化器、亂數狀態、epoch／batch，可從中斷處續跑。
4. `outputs/gmd_transformer_v1/history.json`：每輪 train／validation loss。
5. `outputs/gmd_transformer_v1/sample.wav` 與 `sample.mid`：最佳模型的固定種子 8 小節生成樣本。WAV 使用簡單合成鼓音，只供核對節奏，音質不代表模型能力。

`status.json` 記錄目前階段、epoch、最佳 loss 與最近更新時間；`launch.json` 記錄背景程序與 log 位置。若沒有 summary，請查看 status、`error.txt` 或 `error-*.log`。`config.json`、`split_manifest.json` 與 `code/` 保存設定、來源切分與本次程式快照。

儲存採暫存檔完成後替換正式檔，每 100 updates 及每 epoch 保存 last.pt，較佳 validation 結果另外更新最佳模型。訓練期間暫時阻止自動休眠，程序結束後解除；手動休眠或關機仍會中斷訓練。

## 中斷後續跑

雙擊根目錄的 `繼續GMD_Transformer訓練.bat`。不要在同一工作已運行時再啟動，啟動器會檢查既有程序。

或在專案目錄使用已驗證的 GPU Python：

```powershell
& "$env:USERPROFILE/AppData/Local/Programs/Python/Python313/python.exe" -u -m rhythm_transformer.train --run outputs/gmd_transformer_v1 --resume outputs/gmd_transformer_v1/last.pt
```

resume 保持相同資料、詞彙表、batch size、seed 與學習率設定。若要在原 30 epochs 之後繼續，可另外指定 `--epochs 40`；此時學習率排程也會依新的總長度計算，應視為續作實驗並記錄。新實驗請使用新的 run 資料夾，避免混淆。

## 再生成節奏

```powershell
& "$env:USERPROFILE/AppData/Local/Programs/Python/Python313/python.exe" -m rhythm_transformer.generate --checkpoint outputs/gmd_transformer_v1/gmd_transformer_pretrained.pt --output outputs/gmd_transformer_v1/sample32 --bars 32 --bpm 138 --temperature 0.85 --top-k 12 --seed 456
```

同一條序列持續往後生成，超過上下文長度時保留最後 512 tokens，不每 8 小節重新起始。這能延續局部節奏，但不表示記住整首長期結構。生成器限制 TIME_SHIFT／HIT_SET／STROKE／VEL 語法，避免未完成群組與零時間無限迴圈。未命名的 midi_note_22／26 保留原 MIDI note number，沒有強行改成其他樂器。

## 下一步國樂微調

這份最佳 checkpoint 可作為 Transformer 初始化。訓練器已有 `--init-from`，並能在保留所有 GMD token ID 的前提下附加新 token；舊 embedding 與輸出權重會複製，新 token 的對應權重重新初始化。

```powershell
& "$env:USERPROFILE/AppData/Local/Programs/Python/Python313/python.exe" -m rhythm_transformer.train --init-from outputs/gmd_transformer_v1/gmd_transformer_pretrained.pt --data PATH_TO_COMPATIBLE_SEQUENCES.jsonl --vocab PATH_TO_APPEND_ONLY_VOCAB.json --run outputs/guoyue_transformer_ft_v1 --lr 0.00003 --epochs 20
```

這個指令中的路徑須換成整理好的實際資料。國樂資料必須先接軌原 GMD grammar／PPQ，並含明確 train／validation split；不能直接把新 token 工具的獨立 `INST:C...`／`EVENT` 格式當成已相容的資料。下一步要處理的是共用詞彙與輸出格式轉換，原始國樂樂器不需對應成西式鼓組。

作品少時先按作品切分；同一作品的反覆、重疊視窗、來源版本不得同時進入 train／validation。單首或數個片段的微調只可作為概念驗證，不能用來宣稱國樂泛化效果。

## 實作驗證

已加入 causal future-leakage、右側 padding、原 LSTM 視窗相容、模型安全儲存與重載、詞彙擴充保留權重、生成群組與時間進展等測試；另以真實 GMD 跑 GPU 小批次 smoke test，確認訓練、驗證、測試與 checkpoint 可運作。

官方 API 參考：[PyTorch causal SDPA](https://docs.pytorch.org/docs/2.14/generated/torch.nn.functional.scaled_dot_product_attention.html)、[混合精度](https://docs.pytorch.org/docs/2.14/notes/amp_examples.html)。
