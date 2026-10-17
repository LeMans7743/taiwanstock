# 台股 AI 多因子 × 國內外新聞趨勢分析儀表板

**網站：https://lemans7743.github.io/taiwanstock/**

每個交易日台灣時間 21:30 由 GitHub Actions 自動抓取全市場資料、計算評分、呼叫 Gemini AI 解讀新聞，結果寫入 `data/stocks_data.json`，並自動重新部署網站。

## 分析流程

1. **股票池**：上市成交金額前 250 名＋上櫃前 120 名＋核心追蹤清單（約 370 檔）
2. **量化評分**（每檔都算）
   | 維度 | 預設權重 | 依據 |
   |---|---|---|
   | 基本面 | 30% | 月營收年增／月增／累計年增、毛利率、營益率、EPS、本益比 |
   | 籌碼面 | 30% | 外資／投信連買賣天數、法人 5 日買超占成交量、融資增減、券資比、千張大戶週變化、散戶人數變化 |
   | 技術面 | 20% | 月線／季線、季線斜率、20 日新高、爆量、RSI、季線乖離 |
   | 新聞面 | 20% | 深度分析名單才有（見下） |
3. **深度分析**（量化前 30 名＋核心清單）：收集 Google News（國內＋國際）、鉅亨網、公開資訊觀測站重大訊息，由 Gemini 逐則評分新聞、寫出趨勢研判、利多與風險
4. **大盤研判**：加權／櫃買／費半／NASDAQ／匯率／外資台指期未平倉／融資餘額＋宏觀新聞，由 Gemini 寫出大盤解讀、熱門題材與風險

網頁上可拖曳調整權重；沒有新聞分數的股票會以其餘三個維度計算總分。

## Gemini API 金鑰

到 https://aistudio.google.com/apikey 申請免費金鑰，放到 repo 的 **Settings → Secrets and variables → Actions → New repository secret**，名稱 `GEMINI_API_KEY`。

- 未設定金鑰時會自動改用關鍵字詞庫評分，網站照常更新。
- 想換模型：在同一頁的 **Variables** 新增 `GEMINI_MODEL`（預設 `gemini-3.8-flash`）。

## 本機執行

```bash
pip install -r requirements.txt
python update_data.py
python -m http.server 8000
```

打開 http://localhost:8000 。本機要用 AI 時，先設定環境變數 `GEMINI_API_KEY`。

## 程式結構

| 檔案 | 用途 |
|---|---|
| `sources.py` | 各資料來源擷取（證交所、櫃買、集保、期交所、yfinance、新聞） |
| `scoring.py` | 基本面／籌碼面／技術面評分規則 |
| `ai.py` | Gemini 新聞評分與解讀、關鍵字詞庫備援 |
| `update_data.py` | 主流程；股票池大小與核心清單在檔案上方設定 |
| `data/tdcc_history.json` | 集保大戶每週資料（計算週變化用，自動維護） |

> 分數與 AI 解讀為自動化計算結果，僅供研究參考，不構成任何投資建議。
