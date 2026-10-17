"""資料擷取層：證交所、櫃買中心、集保、期交所、yfinance、新聞來源。

所有函式失敗時回傳空值並記錄警告，不會中斷整體流程。
"""
import datetime
import io
import json
import re
import time
import urllib.parse

import feedparser
import pandas as pd
import requests
import yfinance as yf

HEADERS = {"User-Agent": "Mozilla/5.0"}
TW_TZ = datetime.timezone(datetime.timedelta(hours=8))
STOCK_CODE = re.compile(r"^[1-9]\d{3}$")  # 一般股票（排除 ETF、權證、特別股）


def log(msg):
    print(f"[{datetime.datetime.now(TW_TZ):%H:%M:%S}] {msg}", flush=True)


def to_num(v):
    try:
        n = float(str(v).replace(",", "").strip())
        return n if n == n else None  # 排除 NaN
    except (TypeError, ValueError):
        return None


def round2(v):
    n = to_num(v)
    return round(n, 2) if n is not None else None


def get_json(url, strict=True, retries=4, **kw):
    for attempt in range(retries):
        try:
            res = requests.get(url, headers=HEADERS, timeout=40, **kw)
            res.raise_for_status()
            return json.loads(res.text, strict=strict)
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(5 * (attempt + 1))


def safe(label, fn, default):
    try:
        return fn()
    except Exception as e:
        log(f"⚠️ {label} 失敗：{e}")
        return default


def roc_to_date(s):
    s = str(s).strip().replace("/", "")
    return datetime.date(int(s[:-4]) + 1911, int(s[-4:-2]), int(s[-2:]))


# ==========================================
# 交易日 + 三大法人（上市 T86 / 上櫃 dailyTrade）
# ==========================================
def fetch_institutional_history(n_days=10, max_lookback=25):
    """由今天往回找 n_days 個交易日的三大法人買賣超（單位：張）。
    回傳 (交易日清單(新到舊), {code: [{"date","foreign","trust","dealer"}...]})"""
    hist = {}
    dates = []
    today = datetime.datetime.now(TW_TZ).date()
    for back in range(max_lookback):
        if len(dates) >= n_days:
            break
        d = today - datetime.timedelta(days=back)
        if d.weekday() >= 5:
            continue
        twse = safe(f"上市法人 {d}", lambda: get_json(
            f"https://www.twse.com.tw/rwd/zh/fund/T86?date={d:%Y%m%d}&selectType=ALLBUT0999&response=json"), {})
        time.sleep(3)
        if twse.get("stat") != "OK" or not twse.get("data"):
            continue
        dates.append(d)
        f = twse["fields"]
        i_f = next(i for i, x in enumerate(f) if x.startswith("外陸資買賣超股數"))
        i_t = f.index("投信買賣超股數")
        i_d = f.index("自營商買賣超股數")
        for row in twse["data"]:
            hist.setdefault(row[0].strip(), []).append({
                "date": f"{d:%Y-%m-%d}",
                "foreign": round((to_num(row[i_f]) or 0) / 1000),
                "trust": round((to_num(row[i_t]) or 0) / 1000),
                "dealer": round((to_num(row[i_d]) or 0) / 1000),
            })
        tpex = safe(f"上櫃法人 {d}", lambda: get_json(
            f"https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date={d:%Y/%m/%d}&response=json"), {})
        time.sleep(2)
        for table in tpex.get("tables", []):
            for row in table.get("data", []):
                # 欄位：代號,名稱,外資(買,賣,超),外資自營(買,賣,超),外資合計(買,賣,超),投信(買,賣,超),自營商合計(買,賣,超)...
                if len(row) < 24:
                    continue
                hist.setdefault(row[0].strip(), []).append({
                    "date": f"{d:%Y-%m-%d}",
                    "foreign": round((to_num(row[4]) or 0) / 1000),
                    "trust": round((to_num(row[13]) or 0) / 1000),
                    "dealer": round((to_num(row[22]) or 0) / 1000),
                })
    return dates, hist


# ==========================================
# 當日行情（上市 + 上櫃）
# ==========================================
def fetch_quotes(d):
    """回傳 {code: {name, market, open, high, low, close, volume(股), value(元)}}"""
    out = {}

    def twse():
        res = requests.get(f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL?date={d:%Y%m%d}&response=csv",
                           headers=HEADERS, timeout=40)
        res.raise_for_status()
        df = pd.read_csv(io.StringIO(res.content.decode("utf-8-sig")), dtype=str)
        for _, r in df.iterrows():
            code = str(r["證券代號"]).strip()
            close = to_num(r.get("收盤價"))
            if STOCK_CODE.match(code) and close:
                out[code] = {"name": str(r["證券名稱"]).strip(), "market": "上市",
                             "open": to_num(r.get("開盤價")), "high": to_num(r.get("最高價")),
                             "low": to_num(r.get("最低價")), "close": close,
                             "volume": to_num(r.get("成交股數")) or 0, "value": to_num(r.get("成交金額")) or 0}

    def tpex():
        rows = get_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes")
        for r in rows:
            code = r["SecuritiesCompanyCode"].strip()
            close = to_num(r.get("Close"))
            if not (STOCK_CODE.match(code) and close) or roc_to_date(r["Date"]) != d:
                continue
            out[code] = {"name": r["CompanyName"].strip(), "market": "上櫃",
                         "open": to_num(r.get("Open")), "high": to_num(r.get("High")), "low": to_num(r.get("Low")),
                         "close": close, "volume": to_num(r.get("TradingShares")) or 0,
                         "value": to_num(r.get("TransactionAmount")) or 0}

    safe("上市行情", twse, None)
    safe("上櫃行情", tpex, None)
    return out


# ==========================================
# 基本面：本益比、月營收、營益分析、EPS
# ==========================================
def fetch_valuation():
    out = {}

    def twse():
        for r in get_json("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"):
            out[r["Code"]] = {"pe": to_num(r.get("PEratio")), "yield": to_num(r.get("DividendYield")),
                              "pb": to_num(r.get("PBratio"))}

    def tpex():
        for r in get_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"):
            out[r["SecuritiesCompanyCode"]] = {"pe": to_num(r.get("PriceEarningRatio")),
                                               "yield": to_num(r.get("YieldRatio")),
                                               "pb": to_num(r.get("PriceBookRatio"))}

    safe("上市本益比", twse, None)
    safe("上櫃本益比", tpex, None)
    return out


def fetch_revenue():
    out = {}

    def load(url):
        for r in get_json(url):
            ym = r.get("資料年月", "")
            out[r["公司代號"].strip()] = {
                "industry": r.get("產業別", ""),
                "revYoY": round2(r.get("營業收入-去年同月增減(%)")),
                "revMoM": round2(r.get("營業收入-上月比較增減(%)")),
                "revCumYoY": round2(r.get("累計營業收入-前期比較增減(%)")),
                "revMonth": f"{int(ym[:3]) + 1911}-{ym[3:]}" if len(ym) == 5 else ym,
            }

    safe("上市月營收", lambda: load("https://openapi.twse.com.tw/v1/opendata/t187ap05_L"), None)
    safe("上櫃月營收", lambda: load("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"), None)
    return out


def fetch_profitability():
    """毛利率、營業利益率、稅後純益率（當年累計），EPS（當年累計）"""
    out = {}

    def margins_l():
        for r in get_json("https://openapi.twse.com.tw/v1/opendata/t187ap17_L"):
            out.setdefault(r["公司代號"], {}).update({
                "period": f"{int(r['年度']) + 1911}Q{r['季別']}",
                "grossMargin": round2(r.get("毛利率(%)(營業毛利)/(營業收入)")),
                "opMargin": round2(r.get("營業利益率(%)(營業利益)/(營業收入)")),
                "netMargin": round2(r.get("稅後純益率(%)(稅後純益)/(營業收入)")),
            })

    def margins_o():
        for r in get_json("https://www.tpex.org.tw/openapi/v1/mopsfin_187ap17_O"):
            out.setdefault(r["SecuritiesCompanyCode"], {}).update({
                "period": f"{int(r['Year']) + 1911}Q{r['季別']}",
                "grossMargin": round2(r.get("毛利率")),
                "opMargin": round2(r.get("營業利益率")),
                "netMargin": round2(r.get("稅後純益率")),
            })

    def eps_l():
        for r in get_json("https://openapi.twse.com.tw/v1/opendata/t187ap06_L_ci"):
            out.setdefault(r["公司代號"], {})["eps"] = round2(r.get("基本每股盈餘（元）"))

    def eps_o():
        for r in get_json("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_ci"):
            out.setdefault(r["SecuritiesCompanyCode"], {})["eps"] = round2(r.get("基本每股盈餘（元）"))

    for label, fn in (("上市營益", margins_l), ("上櫃營益", margins_o), ("上市EPS", eps_l), ("上櫃EPS", eps_o)):
        safe(label, fn, None)
    return out


# ==========================================
# 籌碼：融資融券、集保股權分散
# ==========================================
def fetch_margin(d):
    """回傳 ({code: {marginBal, marginChg, shortBal, shortChg}}（單位：張）, 大盤融資金額(億))"""
    out = {}
    market_total = {}

    def twse():
        payload = get_json(f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={d:%Y%m%d}&selectType=ALL&response=json")
        for table in payload.get("tables", []):
            fields = table.get("fields", [])
            if fields[:1] == ["項目"]:
                for row in table["data"]:
                    if row[0].startswith("融資金額"):
                        today, prev = to_num(row[5]), to_num(row[4])
                        market_total["marginAmount"] = round(today / 1e5, 1)  # 仟元 -> 億
                        market_total["marginAmountChg"] = round((today - prev) / 1e5, 1)
            elif fields[:1] == ["代號"]:
                for row in table["data"]:
                    code = row[0].strip()
                    if STOCK_CODE.match(code):
                        out[code] = {"marginBal": to_num(row[6]), "marginChg": (to_num(row[6]) or 0) - (to_num(row[5]) or 0),
                                     "shortBal": to_num(row[12]), "shortChg": (to_num(row[12]) or 0) - (to_num(row[11]) or 0)}

    def tpex():
        for r in get_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance"):
            code = r["SecuritiesCompanyCode"].strip()
            if STOCK_CODE.match(code):
                mb, mp = to_num(r.get("MarginPurchaseBalance")), to_num(r.get("MarginPurchaseBalancePreviousDay"))
                sb, sp = to_num(r.get("ShortSaleBalance")), to_num(r.get("ShortSaleBalancePreviousDay"))
                out[code] = {"marginBal": mb, "marginChg": (mb or 0) - (mp or 0),
                             "shortBal": sb, "shortChg": (sb or 0) - (sp or 0)}

    safe("上市融資融券", twse, None)
    safe("上櫃融資融券", tpex, None)
    return out, market_total


def fetch_tdcc():
    """集保股權分散表（每週）。回傳 (資料日期, {code: {big1000, big400, retailPeople, holders}})"""
    def load():
        res = requests.get("https://opendata.tdcc.com.tw/getOD.ashx?id=1-5", headers=HEADERS, timeout=90)
        res.raise_for_status()
        df = pd.read_csv(io.StringIO(res.content.decode("utf-8-sig")), dtype=str)
        df.columns = ["date", "code", "level", "people", "shares", "pct"]
        df["code"] = df["code"].str.strip()
        df = df[df["code"].str.match(STOCK_CODE)]
        df["level"] = df["level"].astype(int)
        df["people"] = pd.to_numeric(df["people"], errors="coerce")
        df["pct"] = pd.to_numeric(df["pct"], errors="coerce")
        out = {}
        for code, g in df.groupby("code"):
            lv = g.set_index("level")
            out[code] = {
                "big1000": round(float(lv.loc[15, "pct"]), 2) if 15 in lv.index else None,
                "big400": round(float(lv.loc[lv.index.isin([12, 13, 14, 15]), "pct"].sum()), 2),
                "retailPeople": int(lv.loc[lv.index.isin(range(1, 9)), "people"].sum()),
            }
        return str(df["date"].iloc[0]), out

    return safe("集保股權分散", load, (None, {}))


# ==========================================
# 大盤與國際指標
# ==========================================
def fetch_taiex(d):
    payload = safe("加權指數", lambda: get_json(
        f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={d:%Y%m%d}&type=IND&response=json", strict=False), {})
    for table in payload.get("tables", []):
        for row in table.get("data", []):
            if row and row[0] == "發行量加權股價指數":
                sign = -1 if "-" in row[2] else 1
                return {"close": to_num(row[1]), "changePct": sign * (to_num(row[4]) or 0)}
    return None


def fetch_otc_index():
    def load():
        rows = get_json("https://www.tpex.org.tw/openapi/v1/tpex_index")
        last = rows[-1]
        close, chg = to_num(last["Close"]), to_num(last["Change"])
        return {"close": close, "changePct": round(chg / (close - chg) * 100, 2), "date": last["Date"]}
    return safe("櫃買指數", load, None)


def fetch_futures_oi():
    """期交所三大法人台指期未平倉淨口數"""
    def load():
        rows = get_json("https://openapi.taifex.com.tw/v1/MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate")
        out = {}
        for r in rows:
            if r.get("ContractCode") == "臺股期貨":
                item = r.get("Item", "")
                key = "foreign" if item.startswith("外資") else {"投信": "trust", "自營商": "dealer"}.get(item)
                if key:
                    out[key] = int(to_num(r.get("OpenInterest(Net)")) or 0)
                    out["date"] = r.get("Date")
        return out
    return safe("期貨未平倉", load, {})


def yf_last(symbol):
    def load():
        df = yf.download(symbol, period="1mo", interval="1d", progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna(subset=["Close"])
        last, prev = float(df["Close"].iloc[-1]), float(df["Close"].iloc[-2])
        return {"close": round(last, 2), "changePct": round((last - prev) / prev * 100, 2)}
    return safe(symbol, load, None)


# ==========================================
# 歷史 K 線（批次）
# ==========================================
def download_histories(codes_markets, period="6mo"):
    """codes_markets: {code: "上市"/"上櫃"}。回傳 {code: DataFrame(Open/High/Low/Close/Volume)}"""
    symbols = {f"{c}.{'TW' if m == '上市' else 'TWO'}": c for c, m in codes_markets.items()}
    out = {}
    tickers = list(symbols)
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        df = safe(f"K線批次 {i}", lambda: yf.download(chunk, period=period, interval="1d", group_by="ticker",
                                                      progress=False, auto_adjust=True, threads=False), None)
        if df is None or df.empty:
            continue
        for sym in chunk:
            try:
                sub = df[sym].dropna(subset=["Close"])
                if len(sub) >= 40:
                    out[symbols[sym]] = sub[["Open", "High", "Low", "Close", "Volume"]]
            except KeyError:
                pass
    return out


# ==========================================
# 新聞
# ==========================================
def _clean_title(title, source):
    if source and title.endswith(f" - {source}"):
        title = title[: -len(source) - 3]
    return re.sub(r"<[^>]+>", "", title).strip()


def google_news(query, region, limit=5, days=3):
    q = urllib.parse.quote(f"{query} when:{days}d")
    if region == "國際":
        url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    else:
        url = f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    try:
        feed = feedparser.parse(requests.get(url, headers=HEADERS, timeout=20).content)
    except Exception as e:
        log(f"⚠️ Google News {query} 失敗：{e}")
        return []
    items = []
    for e in feed.entries[:limit]:
        source = e.get("source", {}).get("title", "Google News")
        ts = None
        if e.get("published_parsed"):
            ts = datetime.datetime(*e.published_parsed[:6], tzinfo=datetime.timezone.utc).astimezone(TW_TZ)
        items.append({"region": region, "source": source, "ts": ts.isoformat() if ts else None,
                      "title": _clean_title(e.title, source), "link": e.get("link", "")})
    return items


def cnyes_news(keyword, limit=5):
    def load():
        url = f"https://ess.api.cnyes.com/ess/api/v1/news/keyword?q={urllib.parse.quote(keyword)}&limit={limit}&page=1"
        items = []
        for it in get_json(url)["data"]["items"]:
            ts = datetime.datetime.fromtimestamp(it["publishAt"], TW_TZ)
            items.append({"region": "國內", "source": "鉅亨網", "ts": ts.isoformat(),
                          "title": _clean_title(it["title"], None),
                          "link": f"https://news.cnyes.com/news/id/{it['newsId']}"})
        return items
    return safe(f"鉅亨網 {keyword}", load, [])


def cnyes_category(category="tw_stock", limit=8):
    def load():
        url = f"https://api.cnyes.com/media/api/v1/newslist/category/{category}?limit={limit}"
        items = []
        for it in get_json(url)["items"]["data"]:
            ts = datetime.datetime.fromtimestamp(it["publishAt"], TW_TZ)
            items.append({"region": "國內", "source": "鉅亨網", "ts": ts.isoformat(), "title": it["title"],
                          "link": f"https://news.cnyes.com/news/id/{it['newsId']}"})
        return items
    return safe(f"鉅亨網分類 {category}", load, [])


def fetch_material_news():
    """上市櫃每日重大訊息 {code: [news...]}"""
    out = {}

    def add(code, date_s, time_s, title):
        try:
            d = roc_to_date(date_s)
            t = str(time_s).zfill(6)
            ts = datetime.datetime(d.year, d.month, d.day, int(t[:2]), int(t[2:4]), tzinfo=TW_TZ).isoformat()
        except Exception:
            ts = None
        out.setdefault(code.strip(), []).append({
            "region": "國內", "source": "公開資訊觀測站重訊", "ts": ts,
            "title": re.sub(r"\s+", "", title)[:120], "link": "https://mops.twse.com.tw/mops/#/web/t05sr01_1"})

    def twse():
        for r in get_json("https://openapi.twse.com.tw/v1/opendata/t187ap04_L"):
            subject = next((v for k, v in r.items() if k.strip() == "主旨"), "")
            add(r["公司代號"], r.get("發言日期"), r.get("發言時間"), subject)

    def tpex():
        for r in get_json("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O"):
            add(r["SecuritiesCompanyCode"], r.get("發言日期"), r.get("發言時間"), r.get("主旨", ""))

    safe("上市重訊", twse, None)
    safe("上櫃重訊", tpex, None)
    return out


def dedupe_news(items):
    seen, out = set(), []
    for n in sorted(items, key=lambda x: x.get("ts") or "", reverse=True):
        key = re.sub(r"\W", "", n["title"].lower())[:40]
        if key and key not in seen:
            seen.add(key)
            out.append(n)
    return out
