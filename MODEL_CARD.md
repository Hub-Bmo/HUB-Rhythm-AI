# Model card

**用途：**打擊樂符號序列研究、GMD到少量國樂資料的遷移實驗、資料表示比較。模型輸出token，不直接產生真實樂器音訊。

**架構：**4層Pre-LayerNorm causal Transformer，維度256、8heads、FF1024、context512，dropout0.1。V1為133-token，V2/V3為200-token。PyTorch權重檔含設定、詞彙表、最佳epoch、資料metadata及優化器／亂數狀態。

**資料：**GMD1150序列與六首國樂資料。GMD依原有train／validation／test；國樂五首train、一首黃土情validation，沒有獨立test。來源與授權見docs/DATA_SOURCES_AND_LICENSES.md。

**評估：**teacher-forced next-token交叉熵與欄位正確率；V1另有生成樣本。V2/V3未做自由生成／聽覺品質評估。整體loss包含大量結構與常見標記，不等於作曲品質。

**限制：**資料少、風格與編制涵蓋有限；四首含暫行整理；未知欄位遮罩；音高／力度等部分欄位未訓練。V3合併樂器會丟失細分類別。上下文512只保留局部序列，沒有整曲結構保證。

**存取：**使用`torch.load(..., weights_only=True, map_location="cpu")`及本包model.py載入。V1可用generate.py；V2/V3不能直接使用GMD舊語法生成器。

**重現：**已保存原checkpoint與輸入hash，可核對原檔；跨硬體重新訓練不保證逐bit一致。新實驗輸出至reproduced_runs，不覆蓋歷史版本。
