"""台股多因子 × 國內外新聞趨勢 — 每日資料更新腳本

資料來源（皆免費、免 API Key）：
- 證交所 OpenAPI：本益比/殖利率 (BWIBBU_ALL)、上市公司月營收 (t187ap05_L)
- 證交所三大法人買賣超日報 (T86)
- yfinance：日 K 線（均線、量能、RSI）、加權指數、費城半導體
- Google News RSS：國內 / 國際新聞，以關鍵字詞庫計算情緒分數

輸出：data/stocks_data.json（供 index.html 讀取）
任何單一資料源失敗都不會中斷，該欄位會以 null 輸出。
"""
import datetime
import io
import json
import os
import time
import urllib.parse

import feedparser
import pandas as pd
import requests
import yfinance as yf

HEADERS = {"User-Agent": "Mozilla/5.0"}
TW_TZ = datetime.timezone(datetime.timedelta(hours=8))
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "stocks_data.json")

# 追蹤清單：代號、名稱、產業、國際/國內新聞關鍵字
WATCHLIST = [
    {"code": "2330", "name": "台積電", "sector": "晶圓與先進封裝", "us_kw": "TSMC", "tw_kw": "台積電"},
    {"code": "3711", "name": "日月光投控", "sector": "晶圓與先進封裝", "us_kw": "ASE Technology packaging", "tw_kw": "日月光投控"},
    {"code": "2317", "name": "鴻海", "sector": "AI伺服器與散熱", "us_kw": "Foxconn AI server", "tw_kw": "鴻海 AI"},
    {"code": "2382", "name": "廣達", "sector": "AI伺服器與散熱", "us_kw": "Quanta AI server", "tw_kw": "廣達 伺服器"},
    {"code": "3017", "name": "奇鋐", "sector": "AI伺服器與散熱", "us_kw": "AI server liquid cooling", "tw_kw": "奇鋐"},
    {"code": "2454", "name": "聯發科", "sector": "IC設計與邊緣AI", "us_kw": "MediaTek", "tw_kw": "聯發科"},
    {"code": "2308", "name": "台達電", "sector": "電源與重電綠能", "us_kw": "Delta Electronics data center power", "tw_kw": "台達電"},
    {"code": "1519", "name": "華城", "sector": "電源與重電綠能", "us_kw": "Taiwan power grid transformer", "tw_kw": "華城 重電"},
]

# 宏觀新聞牆的三個類別
MACRO_TOPICS = [
    {"category": "🇺🇸 國際科技與美股財報", "region": "國際", "query": "NVIDIA OR hyperscaler capex OR semiconductor stocks"},
    {"category": "🏦 全球總經與聯準會", "region": "國際", "query": "Federal Reserve rates OR CPI inflation OR dollar index"},
    {"category": "🇹🇼 台灣月營收與法說會", "region": "國內", "query": "台股 月營收 OR 法說會 OR 外資"},
]

POS_WORDS = ["創高", "新高", "大增", "買超", "上修", "強勁", "滿載", "利多", "突破", "成長", "優於預期", "看好", "調升", "增溫", "暢旺",
             "surge", "beat", "bullish", "record", "upgrade", "raise", "strong", "soar", "rally", "growth", "jump", "gain"]
NEG_WORDS = ["下修", "砍單", "賣超", "衰退", "跌破", "利空", "不如預期", "延後", "匯損", "下滑", "重挫", "調降", "疲弱", "虧損",
             "drop", "cut", "bearish", "miss", "downgrade", "plunge", "fall", "slump", "weak", "decline", "tariff", "loss"]


def log(msg):
    print(f"[{datetime.datetime.now(TW_TZ):%H:%M:%S}] {msg}", flush=True)


def to_num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def round2(v):
    n = to_num(v)
    return round(n, 2) if n is not None else None


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def get_json(url, **kw):
    res = requests.get(url, headers=HEADERS, timeout=30, **kw)
    res.raise_for_status()
    return res.json()


# ==========================================
# 1. 證交所：本益比 / 殖利率 / 月營收
# ==========================================
def fetch_valuation():
    try:
        rows = get_json("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL")
        return {r["Code"]: {"pe": to_num(r.get("PEratio")), "yield": to_num(r.get("DividendYield")),
                            "pb": to_num(r.get("PBratio"))} for r in rows}
    except Exception as e:
        log(f"⚠️ 本益比資料失敗：{e}")
        return {}


def fetch_monthly_revenue():
    try:
        rows = get_json("https://openapi.twse.com.tw/v1/opendata/t187ap05_L")
        out = {}
        for r in rows:
            ym = r.get("資料年月", "")
            out[r["公司代號"]] = {
                "revYoY": round2(r.get("營業收入-去年同月增減(%)")),
                "revMoM": round2(r.get("營業收入-上月比較增減(%)")),
                "revCumYoY": round2(r.get("累計營業收入-前期比較增減(%)")),
                "revMonth": f"{int(ym[:3]) + 1911}-{ym[3:]}" if len(ym) == 5 else ym,
            }
        return out
    except Exception as e:
        log(f"⚠️ 月營收資料失敗：{e}")
        return {}


# ==========================================
# 2. 證交所：三大法人買賣超（近 N 個交易日）
# ==========================================
def fetch_institutional(codes, n_days=10, max_lookback=25):
    """由今天往回找最近 n_days 個有資料的交易日。
    回傳 (交易日清單(新到舊), {code: [{"date", "foreign", "trust"}...]})，單位：張"""
    hist = {c: [] for c in codes}
    trading_dates = []
    today = datetime.datetime.now(TW_TZ).date()
    for back in range(max_lookback):
        if len(trading_dates) >= n_days:
            break
        d = today - datetime.timedelta(days=back)
        if d.weekday() >= 5:
            continue
        url = f"https://www.twse.com.tw/rwd/zh/fund/T86?date={d:%Y%m%d}&selectType=ALLBUT0999&response=json"
        try:
            payload = get_json(url)
            time.sleep(3)  # 證交所限制請求頻率
            if payload.get("stat") != "OK" or not payload.get("data"):
                continue
            trading_dates.append(d)
            fields = payload["fields"]
            i_code = fields.index("證券代號")
            i_foreign = next(i for i, f in enumerate(fields) if f.startswith("外陸資買賣超股數"))
            i_trust = fields.index("投信買賣超股數")
            for row in payload["data"]:
                code = row[i_code].strip()
                if code in hist:
                    hist[code].append({
                        "date": f"{d:%Y-%m-%d}",
                        "foreign": round((to_num(row[i_foreign]) or 0) / 1000),
                        "trust": round((to_num(row[i_trust]) or 0) / 1000),
                    })
        except Exception as e:
            log(f"⚠️ 法人資料 {d:%Y-%m-%d} 失敗：{e}")
    return trading_dates, hist


def fetch_latest_quotes(d):
    """證交所當日全部上市個股收盤行情（yfinance 當日資料常延遲，用來補最新一根 K 棒）"""
    if d is None:
        return {}
    url = f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL?date={d:%Y%m%d}&response=csv"
    try:
        res = requests.get(url, headers=HEADERS, timeout=30)
        res.raise_for_status()
        df = pd.read_csv(io.StringIO(res.content.decode("utf-8-sig")), dtype=str)
        out = {}
        for _, r in df.iterrows():
            close = to_num(r.get("收盤價"))
            if close is None:
                continue
            out[str(r["證券代號"]).strip()] = {
                "Open": to_num(r.get("開盤價")), "High": to_num(r.get("最高價")),
                "Low": to_num(r.get("最低價")), "Close": close, "Volume": to_num(r.get("成交股數")),
            }
        return out
    except Exception as e:
        log(f"⚠️ 當日行情失敗：{e}")
        return {}


def streak(values):
    """連續買(+)/賣(-)天數，values 由新到舊"""
    if not values or values[0] == 0:
        return 0
    sign = 1 if values[0] > 0 else -1
    n = 0
    for v in values:
        if v * sign > 0:
            n += 1
        else:
            break
    return n * sign


def describe_streak(label, s):
    if s >= 2:
        return f"{label}連{s}買"
    if s <= -2:
        return f"{label}連{-s}賣"
    return None


def fetch_taiex(d):
    if d is None:
        return None
    url = f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={d:%Y%m%d}&type=IND&response=json"
    try:
        res = requests.get(url, headers=HEADERS, timeout=30)
        res.raise_for_status()
        payload = json.loads(res.text, strict=False)
        for row in payload["tables"][0]["data"]:
            if row[0] == "發行量加權股價指數":
                sign = -1 if "-" in row[2] else 1
                return {"close": to_num(row[1]), "changePct": sign * to_num(row[4])}
    except Exception as e:
        log(f"⚠️ 加權指數失敗：{e}")
    return None


# ==========================================
# 3. yfinance：技術面
# ==========================================
def download_history(symbol, period="6mo"):
    df = yf.download(symbol, period=period, interval="1d", progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df.dropna(subset=["Close"])


def rsi(close, n=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    return 100 - 100 / (1 + rs)


def analyze_technical(code, latest=None, latest_date=None):
    try:
        df = download_history(f"{code}.TW")
        if latest and latest_date and (df.empty or df.index[-1].date() < latest_date):
            row = pd.DataFrame([latest], index=pd.DatetimeIndex([pd.Timestamp(latest_date)]))
            df = pd.concat([df[["Open", "High", "Low", "Close", "Volume"]], row])
        if len(df) < 60:
            return None
        close, vol = df["Close"], df["Volume"]
        last, prev = float(close.iloc[-1]), float(close.iloc[-2])
        ma20 = float(close.rolling(20).mean().iloc[-1])
        ma60 = close.rolling(60).mean()
        ma60_now, ma60_prev = float(ma60.iloc[-1]), float(ma60.iloc[-6])
        vol_ma5 = float(vol.rolling(5).mean().iloc[-2])
        high20 = float(close.iloc[-21:-1].max())
        rsi14 = float(rsi(close).iloc[-1])

        bullish = last > ma20 > ma60_now
        vol_breakout = float(vol.iloc[-1]) > vol_ma5 * 1.5
        new_high = last >= high20
        ma60_up = ma60_now > ma60_prev

        score = 50
        score += 12 if last > ma60_now else -8
        score += 10 if last > ma20 else -5
        score += 8 if bullish else 0
        score += 6 if ma60_up else -4
        score += 8 if new_high else 0
        score += 5 if vol_breakout and last > prev else 0
        score += -6 if rsi14 > 80 else (4 if 50 <= rsi14 <= 70 else 0)

        signals = []
        if bullish:
            signals.append("多頭排列")
        if new_high:
            signals.append("創20日高")
        if vol_breakout:
            signals.append("爆量")
        if last < ma60_now:
            signals.append("跌破季線")

        return {
            "close": round(last, 2),
            "changePct": round((last - prev) / prev * 100, 2),
            "ma20": round(ma20, 2),
            "ma60": round(ma60_now, 2),
            "rsi": round(rsi14, 1),
            "volumeLots": int(float(vol.iloc[-1]) // 1000),
            "signals": signals,
            "score": int(clamp(score, 20, 99)),
            "spark": [round(float(v), 2) for v in close.iloc[-60:]],
        }
    except Exception as e:
        log(f"⚠️ {code} 技術面失敗：{e}")
        return None


# ==========================================
# 4. 新聞：Google News RSS + 詞庫情緒分數
# ==========================================
def score_headline(title):
    t = title.lower()
    s = sum(0.35 for w in POS_WORDS if w in t) - sum(0.35 for w in NEG_WORDS if w in t)
    return round(clamp(s, -0.95, 0.95), 2)


def fetch_news(query, region, limit=4):
    q = urllib.parse.quote(f"{query} when:3d")
    if region == "國際":
        url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    else:
        url = f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    try:
        feed = feedparser.parse(requests.get(url, headers=HEADERS, timeout=20).content)
    except Exception as e:
        log(f"⚠️ 新聞 {query} 失敗：{e}")
        return []
    items = []
    for entry in feed.entries[:limit]:
        source = entry.get("source", {}).get("title", "Google News")
        title = entry.title
        if title.endswith(f" - {source}"):
            title = title[: -len(source) - 3]
        s = score_headline(title)
        published = None
        if entry.get("published_parsed"):
            published = datetime.datetime(*entry.published_parsed[:6], tzinfo=datetime.timezone.utc).astimezone(TW_TZ)
        items.append({
            "region": region,
            "source": source,
            "time": f"{published:%m/%d %H:%M}" if published else "",
            "title": title,
            "link": entry.get("link", ""),
            "sentiment": "利多" if s >= 0.3 else ("利空" if s <= -0.3 else "中立"),
            "score": s,
        })
    return items


def avg(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 2) if values else 0.0


# ==========================================
# 5. 評分
# ==========================================
def fundamental_score(rev, val):
    score = 60
    yoy = rev.get("revYoY")
    if yoy is not None:
        score += clamp(yoy * 0.6, -25, 25)
    cum = rev.get("revCumYoY")
    if cum is not None:
        score += clamp(cum * 0.2, -8, 8)
    pe = val.get("pe")
    if pe:
        score += 6 if pe < 15 else (2 if pe < 25 else (-4 if pe < 40 else -10))
    elif val and not pe:
        score -= 6  # 無本益比代表虧損
    return int(clamp(score, 20, 99))


def chips_score(hist):
    if not hist:
        return 60, "無法人資料", None, None
    f_streak = streak([h["foreign"] for h in hist])
    t_streak = streak([h["trust"] for h in hist])
    f5 = sum(h["foreign"] for h in hist[:5])
    t5 = sum(h["trust"] for h in hist[:5])
    score = 60
    score += clamp(f_streak * 3, -15, 15)
    score += clamp(t_streak * 4, -15, 18)
    score += 5 if f5 > 0 else -5
    score += 5 if t5 > 0 else (-3 if t5 < 0 else 0)
    parts = [p for p in (describe_streak("外資", f_streak), describe_streak("投信", t_streak)) if p]
    desc = " / ".join(parts) if parts else "法人動向中性"
    return int(clamp(score, 20, 99)), desc, f5, t5


def main():
    log("啟動台股自動分析引擎...")
    codes = [w["code"] for w in WATCHLIST]

    valuation = fetch_valuation()
    revenue = fetch_monthly_revenue()
    log(f"本益比 {len(valuation)} 檔、月營收 {len(revenue)} 檔")
    trading_dates, inst = fetch_institutional(codes)
    log(f"法人資料 {len(trading_dates)} 個交易日")
    latest_date = trading_dates[0] if trading_dates else None
    quotes = fetch_latest_quotes(latest_date)
    log(f"當日行情 {len(quotes)} 檔（{latest_date}）")

    stocks = []
    for w in WATCHLIST:
        code = w["code"]
        log(f"分析 {code} {w['name']}")
        tech = analyze_technical(code, quotes.get(code), latest_date) or {}
        val = valuation.get(code, {})
        rev = revenue.get(code, {})
        c_score, inst_desc, f5, t5 = chips_score(inst.get(code, []))
        news = fetch_news(w["us_kw"], "國際") + fetch_news(w["tw_kw"], "國內")
        sentiment = avg([n["score"] for n in news])
        news_score = int(clamp(60 + sentiment * 40, 20, 99))
        f_score = fundamental_score(rev, val)

        summary_bits = []
        if rev.get("revYoY") is not None:
            summary_bits.append(f"{rev['revMonth']} 月營收年增 {rev['revYoY']:+.1f}%、月增 {rev['revMoM']:+.1f}%")
        if tech:
            summary_bits.append(f"收盤 {tech['close']}（月線 {tech['ma20']}／季線 {tech['ma60']}，RSI {tech['rsi']}）")
        if f5 is not None:
            summary_bits.append(f"近5日外資 {f5:+,} 張、投信 {t5:+,} 張")
        summary_bits.append(f"國內外新聞 {len(news)} 則，平均情緒 {sentiment:+.2f}")

        stocks.append({
            "code": code,
            "name": w["name"],
            "sector": w["sector"],
            "close": tech.get("close"),
            "changePct": tech.get("changePct"),
            "revYoY": rev.get("revYoY"),
            "revMoM": rev.get("revMoM"),
            "revMonth": rev.get("revMonth"),
            "pe": val.get("pe"),
            "dividendYield": val.get("yield"),
            "instDays": inst_desc,
            "foreign5d": f5,
            "trust5d": t5,
            "signals": tech.get("signals", []),
            "spark": tech.get("spark", []),
            "sentimentScore": sentiment,
            "scores": {
                "fundamental": f_score,
                "chips": c_score,
                "technical": tech.get("score", 50),
                "news": news_score,
            },
            "summary": "；".join(summary_bits) + "。",
            "news": news,
        })

    # 大盤（證交所，與個股同一交易日）與費半（yfinance）
    market = {}
    taiex = fetch_taiex(latest_date)
    if taiex:
        market["taiex"] = taiex
    for key, sym in (("taiex", "^TWII"), ("sox", "^SOX")):
        if key in market:
            continue
        try:
            df = download_history(sym, period="1mo")
            last, prev = float(df["Close"].iloc[-1]), float(df["Close"].iloc[-2])
            market[key] = {"close": round(last, 2), "changePct": round((last - prev) / prev * 100, 2)}
        except Exception as e:
            log(f"⚠️ {sym} 失敗：{e}")

    macro = []
    for t in MACRO_TOPICS:
        items = fetch_news(t["query"], t["region"], limit=5)
        macro.append({"category": t["category"], "sentiment": avg([n["score"] for n in items]), "news": items})

    payload = {
        "updatedAt": datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M"),
        "dataDate": f"{trading_dates[0]:%Y-%m-%d}" if trading_dates else None,
        "market": market,
        "globalSentiment": avg([n["score"] for s in stocks for n in s["news"] if n["region"] == "國際"]
                               + [n["score"] for m in macro for n in m["news"] if n["region"] == "國際"]),
        "localSentiment": avg([n["score"] for s in stocks for n in s["news"] if n["region"] == "國內"]
                              + [n["score"] for m in macro for n in m["news"] if n["region"] == "國內"]),
        "macro": macro,
        "stocks": stocks,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    log(f"✅ 已更新 {OUT_PATH}（{len(stocks)} 檔）")


if __name__ == "__main__":
    main()
