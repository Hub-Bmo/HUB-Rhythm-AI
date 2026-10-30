# Token與資料契約

## GMD基礎資料

`projects/gmd_transformer_v1/data/gmd_sequences_v2.jsonl`是一行一個完整來源序列，保留segment_id、MIDI來源、split、風格、速度、拍號、事件數、token文字。

```text
<BOS> <SOURCE_GMD> <TS_4-4> <BPM_138>
<TIME_SHIFT_0> <HIT_SET>
<STROKE_kick> <VEL_4> <STROKE_snare> <VEL_6>
<END_HIT_SET> ... <EOS>
```

詞彙表是token→整數ID，133項。原MIDI轉PPQ960並量化1/16，velocity分8級。不同樂器在同tick內按固定次序排列。訓練沒有把note-off或鼓聲尾音當成旋律時值。

目前12種stroke是kick、snare、tom_high/mid/low、hihat_closed/open/pedal、ride、ride_bell、crash、splash。snare合併了原小鼓不同敲擊來源；splash沿用原轉換命名，對應的note55在GMD Roland表中為Crash1鈸邊。

## 國樂原始校對JSON

格式`musicai-reviewed-training`1.0。包含原始小節、反覆路線、事件、樂器／擊法表與training_mask。時間分數以四分音符為單位，乘以960取得tick。紙面原始小節與演奏後重複引用不是同一個計數。

原始圖片、辨識專案與校對專案保存來源，不表示其中所有資料已納入訓練；實際輸出以六首固定的訓練JSON及TOKEN包為準。

## 國樂token

```text
<BOS> <SEGMENT_START>
<BAR> <METER_4/4> <TIME_SHIFT_0> <HIT_SET>
<EVENT> <INST:C00006> <ROLL:C00001> <ACCENT:C00002>
<ORNAMENT:C00002> <TIE:C00001> <ATTACK_YES> <DURATION_1440> <END_EVENT>
<END_HIT_SET> ... <END_BAR> <SEGMENT_END> <EOS>
```

此處C00006在instrument分類代表定音鼓；C00001在roll分類代表有輪奏。各欄位分別有自己的分類表，不能只看C編號而忽略prefix。

同一時間的事件合成HIT_SET，每個EVENT仍保留自己的屬性。每小節結尾的TIME_SHIFT保留尾端空白時間，全休止小節也保留。續接事件用ATTACK_NO，不當成新的敲擊。

來源token套件的84-token數字ID、V1的133-tokenID與V2/V3的200-tokenID不同；一律用同一套件的vocab.json解碼。V2將67個新token追加到原GMD133項之後。V3僅將INST token替換為已存在的STROKE token，詞彙表仍固定200項以控制實驗。

## 遮罩

每個token有一個loss_mask。只有已知且可用於訓練的欄位才有mask=1，未知欄位的UNKNOWN token為0。

```text
inputs      = token_ids[:-1]
targets     = token_ids[1:]
target_mask = loss_mask[1:]
```

loss_mask不是attention mask。固定的causal attention仍阻止看未來；右側padding與未知目標不進入loss。

本批選填欄位：時值、輪奏、重音、裝飾音、延音。速度、音高、細部擊法、力度與圓滑線關閉。這些原始資訊若存在仍保留於來源JSON，不可推定模型已學會關閉欄位。

## 切分與視窗

國樂依group_id整首切分。五首train，黃土情validation。原本132段中有兩個228-token訓練側片段與驗證側完全相同，排除後130段用於實驗；完整132段仍保存。

每512token一片，輸入／目標最多511個。這是承接LSTM的視窗政策，不是音樂樂句標註；視窗可在EVENT或HIT_SET中途開始，完整來源序列不被改寫。
