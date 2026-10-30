# 資料來源與授權範圍

## GMD

Groove MIDI Dataset由Google Magenta釋出。本包保存原MIDI、info.csv、原README與LICENSE，另外保留實際處理後的token與事件CSV壓縮副本。GMD官方以CC BY 4.0提供：[官方來源與授權說明](https://magenta.withgoogle.com/datasets/groove)。

引用：Jon Gillick, Adam Roberts, Jesse Engel, Douglas Eck, David Bamman. *Learning to Groove with Inverse Sequence Transformations*. ICML 2019.

```bibtex
@inproceedings{groove2019,
  author = {Gillick, Jon and Roberts, Adam and Engel, Jesse and Eck, Douglas and Bamman, David},
  title = {Learning to Groove with Inverse Sequence Transformations},
  booktitle = {International Conference on Machine Learning},
  year = {2019}
}
```

本專案的轉換包含PPQ960、1/16量化、樂器合併、力度8級、HIT_SET及時間差token；原MIDI保留未改動。note55在本轉換命名為splash，但GMD Roland對照為Crash1鈸邊，不可逕自視為獨立Splash樂器。

## 國樂

天下無雙、闖將令、黃土情、黃土高坡、節慶歡舞、海神音詩，均為使用者提供或在此專案整理的譜面／辨識／校對資料。

原圖與專案快照保存在projects，實際訓練來源另有固定的JSON、tokens、規則與工作庫。前四首含暫行整理；本包沒有把未確認欄位提升為人工確認。

本包沒有收到六首作品各自可公開散布的授權文件，也沒有將GMD的CC BY套用到國樂資料。研究交接與公開repository的資料授權範圍應分別確認。

## 程式與模型

保存本專案程式與模型供使用者及老師研究交接。未替整個repo新增MIT／Apache等授權，也未改寫既有第三方授權。PyTorch、NumPy、Pandas、mido及選用套件仍依各自授權提供；本包僅列依賴，不複製套件環境或二進位runtime。
