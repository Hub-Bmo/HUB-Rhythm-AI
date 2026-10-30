# GitHub 上傳方式

這個資料夾是可上傳的本機交接包，目前沒有建立repository、沒有遠端URL，也尚未上傳。

GitHub一般Git檔案超過50MiB會警告，超過100MiB會阻擋；網頁上傳單檔上限為25MiB。[官方大小限制](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)

本包模型約40–41MB、GMD JSONL約27.5MB，因此應使用Git工具推送，網頁拖曳上傳不適合。原超過100MiB的CSV已保存成可還原的gzip副本，解壓後的中間檔只在忽略的work/中生成。

本包沒有啟用Git LFS，所以以GitHub下載ZIP或一般clone取得的是實際模型檔；不需要另外抓取LFS指標。`.gitattributes`保留所有檔案的原始換行，以維持SHA256。

在GitHub建立老師可存取的repository後，使用GitHub Desktop或Git CLI提交此資料夾。CLI範例，將YOUR_REPOSITORY_URL換成真實網址：

```text
git init -b main
git add .
git commit -m "Add MusicAI research handoff and reproducible experiments"
git remote add origin YOUR_REPOSITORY_URL
git push -u origin main
```

`.gitignore`只排除執行環境、暫存與新實驗；保存的data、projects與outputs都會納入。老師下載後先看README.md與AI_HANDOFF.md。

資料來源與各自授權在DATA_SOURCES_AND_LICENSES.md，並未替國樂樂譜或整個程式專案擅自指定新的開放授權。
