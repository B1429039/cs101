# LINE 校園失物招領 Bot

## 啟動

Windows 請使用同一個 Python 安裝依賴與啟動程式。若 `python` 指到 MSYS2，請改用安裝套件的 Python：

```powershell
$py = "$env:LOCALAPPDATA\Python\pythoncore-3.15-arm64\python.exe"
& $py -m pip install -r requirements.txt
& $py app.py
```

啟動後先確認 `http://127.0.0.1:5000/` 能開啟。
也可以開啟 `http://127.0.0.1:5000/health`，應看到 `"ok": true`。

## LINE Developers 設定

1. Messaging API 的 Webhook URL 設為 `https://你的公開網址/callback`。
2. 開啟 **Use webhook**。
3. `.env` 的 `CHANNEL_SECRET` 必須是 Basic settings 的 Channel secret。
4. `.env` 的 `CHANNEL_ACCESS_TOKEN` 必須是 Messaging API 發行的長期 token。
5. 如果要在群組測試，設定 `REPLY_IN_GROUP=1`；預設值 `0` 會在群組中收到訊息但不回覆。

`PUBLIC_BASE_URL` 必須是目前仍在運作的 HTTPS 公開網址。若使用 ngrok，重開 tunnel 後網址可能改變，LINE Developers 也要同步更新。

## 目前功能

- `/` 是完整的校園失物招領首頁：統計卡片、最新物品卡片、搜尋、遺失／拾獲篩選與手機版版面。
- `/lost-found` 是回報入口頁，串接圖書館、學務處與網站服務。
- 輸入「失物招領」、「連結」或「選單」會直接回傳包含圖書館、學務處、我們的網頁的 Link Tree 卡片。
- 包含遺失或拾獲關鍵字的訊息會記錄到 SQLite。
- 失物文字後 3 分鐘內傳送的圖片會附加到該筆資料。
- `/lost-found` 顯示入口頁，`/api/posts` 顯示 JSON 資料。

## 金鑰安全

`.env` 不應提交到 Git。若金鑰曾貼到聊天、截圖或公開儲存庫，請在 LINE Developers 與 CGU Gateway 立即撤銷並重新建立，再更新本機 `.env`。
