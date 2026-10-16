# 台股 AI 多因子 × 國內外新聞趨勢分析儀表板

每個交易日 16:30（台灣時間）由 GitHub Actions 自動抓取資料、計算評分，結果寫入 `data/stocks_data.json`，再由 GitHub Pages 上的 `index.html` 呈現。

## 評分模型（預設權重，可在網頁上拖曳調整）

| 維度 | 權重 | 計算依據 |
|---|---|---|
| 基本面 | 30% | 最新月營收年增率、累計營收年增率、本益比（證交所 OpenAPI） |
| 籌碼面 | 30% | 外資／投信連買賣天數、近 5 日買賣超張數（證交所 T86） |
| 技術面 | 20% | 月線／季線位置、季線斜率、20 日新高、爆量、RSI（yfinance 還原股價） |
| 新聞面 | 20% | Google News RSS 國內外新聞標題，以正負向詞庫計算情緒分數 |

## 本機執行

```bash
pip install -r requirements.txt
python update_data.py
python -m http.server 8000
```

然後打開 http://localhost:8000 。直接雙擊 `index.html` 會因瀏覽器限制讀不到 JSON，只會顯示示範資料。

## 修改追蹤清單

編輯 `update_data.py` 最上方的 `WATCHLIST`（目前僅支援上市股票）。

## 部署

1. 推上 GitHub 後，到 **Settings → Pages**，Branch 選 `main`、資料夾 `/ (root)`。
2. 到 **Actions → Daily TWSE & News Auto Analyzer → Run workflow** 手動跑第一次。

> 分數為規則化計算結果，僅供研究參考，不構成任何投資建議。
