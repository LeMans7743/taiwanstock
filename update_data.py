"""台股多因子 × 國內外新聞趨勢 — 每日資料更新

流程：
1. 全市場（上市 + 上櫃）依成交金額選出流動性足夠的股票池
2. 每檔計算基本面（月營收、毛利率、EPS、本益比）、籌碼面（法人連買、融資融券、千張大戶）、技術面
3. 量化分數前段班 + 核心追蹤清單 → 收集國內外新聞（Google News、鉅亨網、重大訊息）→ Gemini AI 評分與解讀
4. 大盤數據 + 宏觀新聞 → Gemini AI 大盤研判
輸出 data/stocks_data.json；集保大戶歷史存於 data/tdcc_history.json（計算週變化用）
"""
import datetime
import json
import os

import pandas as pd

import ai
import scoring
import sources as src
from sources import log

BASE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(BASE, "data", "stocks_data.json")
TDCC_PATH = os.path.join(BASE, "data", "tdcc_history.json")

TOP_TWSE = 250        # 上市取成交金額前 N 名
TOP_TPEX = 120        # 上櫃取成交金額前 N 名
DEEP_TOP = 30         # 量化分數前 N 名做新聞 + AI 深度分析
AI_BATCH = 5          # 每次 AI 呼叫分析幾檔（節省免費額度）
QUANT_WEIGHTS = {"fundamental": 30, "chips": 30, "technical": 20}

# 核心追蹤清單：一定納入深度分析，並搜尋英文國際新聞
CORE = {
    "2330": "TSMC", "2317": "Foxconn Hon Hai", "2454": "MediaTek", "2382": "Quanta Computer",
    "2308": "Delta Electronics Taiwan", "3711": "ASE Technology", "3017": "Asia Vital Components",
    "2303": "United Microelectronics UMC", "2412": "Chunghwa Telecom", "2881": "Fubon Financial",
    "3231": "Wistron", "6669": "Wiwynn", "2345": "Accton Technology", "3008": "Largan Precision",
    "1519": "Fortune Electric Taiwan", "2603": "Evergreen Marine",
}

MACRO_TOPICS = [
    {"category": "🇺🇸 美股科技與 AI 供應鏈", "google": [("NVIDIA OR hyperscaler capex OR AI chips", "國際"),
                                                    ("semiconductor stocks Nasdaq", "國際")]},
    {"category": "🏦 全球總經與聯準會", "google": [("Federal Reserve OR CPI inflation OR Treasury yields", "國際"),
                                              ("tariffs Taiwan OR dollar index", "國際")]},
    {"category": "🇹🇼 台股資金與產業動態", "google": [("台股 外資 OR 月營收 OR 法說會", "國內")], "cnyes": True},
]


def load_tdcc_history():
    try:
        with open(TDCC_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_tdcc_history(hist, date, data, codes):
    if not date or not data:
        return hist
    hist[date] = {c: [data[c]["big1000"], data[c]["big400"], data[c]["retailPeople"]] for c in codes if c in data}
    for old in sorted(hist)[:-8]:  # 保留 8 週
        del hist[old]
    with open(TDCC_PATH, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, separators=(",", ":"))
    return hist


def append_latest_bar(df, quote, d):
    """yfinance 當日資料常延遲，以交易所收盤行情補上最新一根"""
    if df is None or not quote or df.index[-1].date() >= d:
        return df
    row = pd.DataFrame([{"Open": quote["open"], "High": quote["high"], "Low": quote["low"],
                         "Close": quote["close"], "Volume": quote["volume"]}], index=pd.DatetimeIndex([pd.Timestamp(d)]))
    return pd.concat([df, row])


def quant_total(scores):
    total = sum(QUANT_WEIGHTS.values())
    return round(sum(scores[k] * w for k, w in QUANT_WEIGHTS.items()) / total, 1)


def main():
    log(f"啟動台股自動分析引擎（AI：{'Gemini ' + ai.MODEL if ai.enabled() else '關鍵字詞庫'}）")

    # ---------- 1. 交易日、法人、行情 ----------
    dates, inst = src.fetch_institutional_history()
    if not dates:
        raise SystemExit("❌ 取不到任何交易日資料，終止")
    d = dates[0]
    log(f"法人資料 {len(dates)} 個交易日，最新 {d}")
    quotes = src.fetch_quotes(d)
    log(f"當日行情 {len(quotes)} 檔")

    by_value = sorted(quotes.items(), key=lambda kv: kv[1]["value"], reverse=True)
    universe = [c for c, q in by_value if q["market"] == "上市"][:TOP_TWSE]
    universe += [c for c, q in by_value if q["market"] == "上櫃"][:TOP_TPEX]
    universe += [c for c in CORE if c in quotes and c not in universe]
    log(f"股票池 {len(universe)} 檔")

    # ---------- 2. 基本面、籌碼、K 線 ----------
    valuation = src.fetch_valuation()
    revenue = src.fetch_revenue()
    profit = src.fetch_profitability()
    margin, market_margin = src.fetch_margin(d)
    tdcc_date, tdcc = src.fetch_tdcc()
    tdcc_hist = load_tdcc_history()
    prev_weeks = sorted(k for k in tdcc_hist if k != tdcc_date)
    tdcc_prev = tdcc_hist[prev_weeks[-1]] if prev_weeks else {}
    save_tdcc_history(tdcc_hist, tdcc_date, tdcc, universe)
    log(f"本益比 {len(valuation)}、月營收 {len(revenue)}、營益 {len(profit)}、融資券 {len(margin)}、集保 {len(tdcc)}（{tdcc_date}）")

    histories = src.download_histories({c: quotes[c]["market"] for c in universe})
    log(f"K 線 {len(histories)} 檔")

    stocks = []
    for code in universe:
        q = quotes[code]
        df = append_latest_bar(histories.get(code), q, d)
        if df is None or len(df) < 40:
            continue
        tech = scoring.technical(df)
        rev, prof, val = revenue.get(code, {}), profit.get(code, {}), valuation.get(code, {})
        f_score, f_notes = scoring.fundamental(rev, prof, val)
        c_score, inst_desc, chip_detail = scoring.chips(inst.get(code, []), tech["avgVolLots"], margin.get(code),
                                                        tdcc.get(code), tdcc_prev.get(code))
        scores = {"fundamental": f_score, "chips": c_score, "technical": tech["score"]}
        stocks.append({
            "code": code, "name": q["name"], "market": q["market"], "sector": rev.get("industry") or "其他",
            "close": tech["close"], "changePct": tech["changePct"], "valueYi": round(q["value"] / 1e8, 1),
            "revYoY": rev.get("revYoY"), "revMoM": rev.get("revMoM"), "revCumYoY": rev.get("revCumYoY"),
            "revMonth": rev.get("revMonth"),
            "grossMargin": prof.get("grossMargin"), "opMargin": prof.get("opMargin"), "eps": prof.get("eps"),
            "finPeriod": prof.get("period"),
            "pe": val.get("pe"), "pb": val.get("pb"), "dividendYield": val.get("yield"),
            "instDays": inst_desc, "chips": chip_detail,
            "tech": {k: tech[k] for k in ("ma20", "ma60", "rsi", "ret20", "bias60", "volumeLots")},
            "signals": f_notes + tech["signals"], "spark": tech["spark"],
            "scores": scores, "quantScore": quant_total(scores),
            "sentimentScore": None, "news": [], "ai": None, "deep": False,
        })
    stocks.sort(key=lambda s: s["quantScore"], reverse=True)
    log(f"完成量化評分 {len(stocks)} 檔")

    # ---------- 3. 大盤（先做，確保 AI 額度優先用在大盤研判） ----------
    market = {"taiex": src.fetch_taiex(d) or src.yf_last("^TWII"), "otc": src.fetch_otc_index(),
              "sox": src.yf_last("^SOX"), "nasdaq": src.yf_last("^IXIC"), "usdtwd": src.yf_last("TWD=X"),
              "futuresOI": src.fetch_futures_oi(), **market_margin}

    # 外資現貨買賣超金額（張數 × 最新收盤價估算，單位：億元）
    def foreign_yi(days):
        return round(sum(h["foreign"] * 1000 * quotes[c]["close"] for c in inst if c in quotes
                         for h in inst[c][:days]) / 1e8, 1)
    market["foreignTodayYi"] = foreign_yi(1)
    market["foreign5dYi"] = foreign_yi(5)
    macro = []
    for t in MACRO_TOPICS:
        items = []
        for query, region in t["google"]:
            items += src.google_news(query, region, limit=6)
        if t.get("cnyes"):
            items += src.cnyes_category("tw_stock", limit=8)
        macro.append({"category": t["category"], "news": src.dedupe_news(items)[:10]})
    top_brief = [{"code": s["code"], "name": s["name"], "sector": s["sector"], "quant": s["quantScore"],
                  "signals": s["signals"]} for s in stocks[:15]]
    mkt_ai = ai.analyze_market(market, macro, top_brief)
    log(f"大盤分析完成（{mkt_ai.get('engine')}）")

    # ---------- 4. 個股深度分析：新聞 + AI（每次呼叫分析 AI_BATCH 檔） ----------
    material = src.fetch_material_news()
    deep = [s for s in stocks if s["code"] in CORE]
    deep += [s for s in stocks[:DEEP_TOP] if s["code"] not in CORE]
    batch = []
    for i, s in enumerate(deep, 1):
        news = src.google_news(s["name"], "國內", limit=5) + src.cnyes_news(s["name"], limit=5)
        news += material.get(s["code"], [])[:3]
        if s["code"] in CORE:
            news += src.google_news(CORE[s["code"]], "國際", limit=5)
        news = src.dedupe_news(news)[:12]
        facts = {
            "收盤": s["close"], "漲跌%": s["changePct"], "20日報酬%": s["tech"]["ret20"], "季線乖離%": s["tech"]["bias60"],
            "RSI": s["tech"]["rsi"], "技術訊號": s["signals"], "月營收年增%": s["revYoY"], "累計營收年增%": s["revCumYoY"],
            "毛利率%": s["grossMargin"], "營益率%": s["opMargin"], "EPS(今年累計)": s["eps"], "本益比": s["pe"],
            "法人": s["instDays"], "外資5日張數": s["chips"].get("foreign5d"), "投信5日張數": s["chips"].get("trust5d"),
            "融資增減張數": s["chips"].get("marginChg"), "千張大戶%": s["chips"].get("big1000"),
            "千張大戶週變化": s["chips"].get("big1000Chg"),
        }
        batch.append(({"code": s["code"], "name": s["name"], "sector": s["sector"], "facts": facts}, news))
        if len(batch) == AI_BATCH or i == len(deep):
            log(f"深度分析 {i}/{len(deep)}（{', '.join(b[0]['name'] for b in batch)}）")
            results = ai.analyze_stocks(batch)
            for st, _ in batch:
                target = next(x for x in deep if x["code"] == st["code"])
                news_out, result = results[st["code"]]
                target["news"] = news_out
                target["sentimentScore"] = result["sentiment"]
                target["scores"]["news"] = int(scoring.clamp(60 + result["sentiment"] * 38, 20, 99))
                target["deep"] = True
                if result.get("engine") == "gemini":
                    target["ai"] = {k: result.get(k) for k in ("outlook", "summary", "bullPoints", "risks")}
            batch = []

    payload = {
        "updatedAt": datetime.datetime.now(src.TW_TZ).strftime("%Y-%m-%d %H:%M"),
        "dataDate": f"{d:%Y-%m-%d}",
        "tdccDate": tdcc_date,
        "ai": ai.status(),
        "market": market,
        "marketAI": mkt_ai,
        "globalSentiment": mkt_ai["globalSentiment"],
        "localSentiment": mkt_ai["localSentiment"],
        "macro": macro,
        "stocks": stocks,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    log(f"✅ 已更新 {OUT_PATH}（{len(stocks)} 檔，深度分析 {len(deep)} 檔，AI 呼叫成功 {ai.stats['ok']} 次）")


if __name__ == "__main__":
    main()
