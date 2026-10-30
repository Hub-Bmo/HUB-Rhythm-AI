# 實驗版本與結果

```text
GMD MIDI → 既有事件CSV → GMD token
                          ↓
                   V1 GMD Transformer
                    ┌─────┴─────┐
                    ↓           ↓
          V2 國樂32類微調   V3 功能映射GMD類別微調
```

V2與V3是獨立分支。更早的LSTM只當歷史基準，checkpoint在legacy/lstm/，沒有拿LSTM權重初始化Transformer。

| 項目 | V1 | V2 | V3 |
| --- | --- | --- | --- |
| 資料 | GMD | 五首國樂train，一首validation | 與V2相同片段，替換樂器token |
| 詞彙表大小 | 133 | 200 | 200 |
| 參數量 | 3,358,853 | 3,393,224 | 3,393,224 |
| Epoch／最佳epoch | 30／30 | 60／60 | 60／60 |
| Batch | 8 | 8 | 8 |
| 最高learning rate | 0.0003 | 0.00003 | 0.00003 |
| Warmup updates | 100 | 20 | 20 |
| Validation loss | 0.692777 | 0.406496 | 0.367123 |
| Test loss | 0.690338 | 沒有獨立test集 | 沒有獨立test集 |

每版目錄內的config.json、history.json、summary.json、split_manifest.json及train-*.log是實際保存紀錄，code/是當時程式快照。新的重現入口使用根目錄的可攜程式，歷史code/保留作查核。

## V2／V3的可比結果

| 驗證指標 | V2 | V3 |
| --- | ---: | ---: |
| 同一GMD分類尺度的樂器正確率 | 64.42% | 70.22% |
| 時間間隔正確率 | 80.51% | 76.14% |
| 時值正確率 | 58.24% | 60.86% |

驗證有6,483個已知目標，其中534個樂器目標、549個時間間隔目標、534個時值目標。共同樂器指標將V2各INST及STROKE輸出機率加總到同樣GMD類別，其他token歸為無效輸出類。

結果呈現取捨：功能映射改善樂器粗分類與時值，但時間間隔下降。目標壓縮也降低任務難度；不能只看整體loss排名。單seed與單首validation不提供穩健的音樂泛化結論。

## 已做與尚未做

- 已檢查causal attention、padding、未知欄位梯度遮罩、詞彙擴充、作品切分、source hash、模型重載與續訓。
- V1已有8及32小節生成樣本與MIDI／合成音色WAV。
- V2/V3尚未完成對應新版語法的自由生成與真實國樂音色試聽評估。
- 未重做原譜的人工作業；四首含暫行整理，保留其不確定狀態。
- 沒有從頭訓練國樂模型的對照，也沒有證明GMD預訓練對所有國樂作品都有助益。

## 歷史LSTM

保存的checkpoint顯示vocab133、embedding256、hidden512、兩層，最佳epoch9、validation loss0.73646149。原始訓練腳本與模型保存，但未找到原始完整逐epoch log，不能補造缺失曲線。
