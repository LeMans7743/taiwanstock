"""新聞情緒與 AI 解讀。

有設定環境變數 GEMINI_API_KEY 時使用 Gemini API（REST，免安裝 SDK）。
免費額度以「模型」為單位計算，一個模型用完會自動換同金鑰下的其他 flash 模型接力；
全部用完或沒有金鑰時，退回關鍵字詞庫評分，網站仍可正常更新。
"""
import json
import os
import re
import time

import requests

from sources import log

API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").strip()
MAX_MODELS = int(os.environ.get("GEMINI_MAX_MODELS", "4"))  # 接力最多用幾個模型
MIN_INTERVAL = float(os.environ.get("GEMINI_MIN_INTERVAL", "13"))  # 免費額度每分鐘次數有限
BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
SKIP_WORDS = ("image", "tts", "audio", "live", "embed", "vision", "robotics", "computer-use")

_last_call = 0.0
_models = None          # 接力順序
_model_idx = 0
_disabled_reason = None if API_KEY else "未設定 GEMINI_API_KEY"
stats = {"calls": 0, "ok": 0, "failed": 0, "byModel": {}}


def _version_key(name):
    nums = [int(x) for x in re.findall(r"\d+", name)]
    return (0 if "preview" in name or "exp" in name else 1, "lite" not in name, nums)


def _discover_models():
    """向 Google 查詢此金鑰可用的 flash 模型；每個模型各有獨立的免費額度，可接力使用。"""
    chain = [MODEL]
    try:
        res = requests.get(f"{BASE_URL}/models?pageSize=200", headers={"x-goog-api-key": API_KEY}, timeout=30)
        res.raise_for_status()
        names = [m["name"].split("/", 1)[1] for m in res.json().get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])]
        flash = [n for n in names if "flash" in n and not any(w in n for w in SKIP_WORDS) and n != MODEL]
        chain += sorted(flash, key=_version_key, reverse=True)
    except Exception as e:
        log(f"⚠️ 查詢 Gemini 模型清單失敗：{e}")
    chain = chain[:MAX_MODELS]
    log(f"Gemini 模型接力順序：{' → '.join(chain)}")
    return chain


def current_model():
    return _models[_model_idx] if _models and _model_idx < len(_models) else None


def _next_model(reason):
    """目前模型無法使用，換下一個；全部用完則停用 AI。"""
    global _model_idx, _disabled_reason
    log(f"⚠️ {current_model()}：{reason}")
    _model_idx += 1
    if current_model():
        log(f"↪ 改用 {current_model()}")
    else:
        _disabled_reason = f"所有 Gemini 模型皆無法使用（最後原因：{reason}）"
        log(f"⚠️ {_disabled_reason}，其餘改用詞庫")


def enabled():
    global _models
    if _disabled_reason is None and _models is None:
        _models = _discover_models()
    return _disabled_reason is None and current_model() is not None


def status():
    used = [m for m, n in stats["byModel"].items() if n]
    return {"engine": "Gemini (" + " / ".join(used) + ")" if used else "關鍵字詞庫",
            "aiCalls": stats["ok"], "aiFailed": stats["failed"], "byModel": stats["byModel"],
            "note": _disabled_reason}


def _call(prompt, schema, retries=3):
    """呼叫 Gemini，回傳解析後的 JSON；額度用完自動換下一個模型；全部失敗回傳 None。"""
    global _last_call, _disabled_reason
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json", "responseSchema": schema},
    }
    while enabled():
        model = current_model()
        res = None
        for attempt in range(retries):
            wait = MIN_INTERVAL - (time.time() - _last_call)
            if wait > 0:
                time.sleep(wait)
            _last_call = time.time()
            stats["calls"] += 1
            try:
                res = requests.post(f"{BASE_URL}/models/{model}:generateContent", json=body, timeout=180,
                                    headers={"x-goog-api-key": API_KEY, "Content-Type": "application/json"})
            except requests.RequestException as e:
                log(f"⚠️ Gemini 連線失敗：{e}")
                time.sleep(5)
                continue
            if res.status_code == 200:
                try:
                    text = res.json()["candidates"][0]["content"]["parts"][0]["text"]
                    result = json.loads(text)
                except (KeyError, IndexError, ValueError) as e:
                    log(f"⚠️ {model} 回應格式錯誤：{e}")
                    break
                stats["ok"] += 1
                stats["byModel"][model] = stats["byModel"].get(model, 0) + 1
                return result
            try:
                msg = res.json()["error"]["message"][:200]
            except (ValueError, KeyError, TypeError):
                msg = res.text[:200]
            if res.status_code in (401, 403) and "API key" in msg:
                _disabled_reason = f"Gemini 金鑰錯誤：{msg}"
                log(f"⚠️ {_disabled_reason}")
                return None
            if res.status_code in (400, 403, 404):
                _next_model(f"錯誤 {res.status_code} {msg[:100]}")
                break
            if res.status_code == 429 and "PerDay" in res.text:  # quotaId 在 details 裡，需檢查完整內容
                _next_model("今日免費額度已用完")
                break
            log(f"⚠️ {model} {res.status_code}，{20 * (attempt + 1)} 秒後重試：{msg[:120]}")
            time.sleep(20 * (attempt + 1))
        else:
            if res is not None and res.status_code == 429:
                _next_model("持續限流（額度可能已用完）")
                continue
            stats["failed"] += 1
            return None
        if res is not None and res.status_code == 200:  # 回應格式錯誤，不換模型
            stats["failed"] += 1
            return None
    stats["failed"] += 1
    return None


# ==========================================
# 關鍵字詞庫（備援）
# ==========================================
POS_WORDS = {
    "創新高": 2, "創高": 2, "新高": 1.5, "大增": 1.5, "倍增": 2, "翻倍": 2, "上修": 2, "調升": 1.5, "優於預期": 2,
    "強勁": 1.5, "暢旺": 1.5, "滿載": 1.5, "供不應求": 2, "缺貨": 1, "接單": 1, "拿下": 1, "訂單": 0.5, "擴產": 1,
    "買超": 1, "加碼": 1, "利多": 1.5, "突破": 1, "漲停": 1.5, "大漲": 1.5, "成長": 1, "看好": 1, "回溫": 1,
    "轉盈": 1.5, "獲利": 0.5, "受惠": 1, "樂觀": 1, "目標價": 0.5, "上調": 1.5,
    "surge": 2, "soar": 2, "beat": 2, "record": 1.5, "upgrade": 2, "raise": 1, "strong": 1, "rally": 1.5,
    "growth": 1, "jump": 1.5, "gain": 1, "bullish": 2, "outperform": 1.5, "boost": 1, "demand": 0.5,
}
NEG_WORDS = {
    "下修": 2, "調降": 1.5, "不如預期": 2, "低於預期": 2, "砍單": 2, "衰退": 1.5, "下滑": 1.5, "減少": 1, "疲弱": 1.5,
    "賣超": 1, "減碼": 1, "利空": 1.5, "跌破": 1.5, "跌停": 1.5, "重挫": 2, "大跌": 1.5, "崩": 1.5, "虧損": 1.5,
    "延後": 1, "遞延": 1, "匯損": 1, "關稅": 1, "制裁": 1.5, "調查": 1, "危機": 1.5, "風險": 0.5, "示警": 1,
    "停工": 1.5, "裁員": 1.5, "違約": 2, "交保": 2, "偵訊": 2, "罰": 1,
    "drop": 1.5, "plunge": 2, "fall": 1, "slump": 2, "cut": 1.5, "miss": 2, "downgrade": 2, "weak": 1.5,
    "decline": 1.5, "loss": 1.5, "bearish": 2, "tariff": 1, "probe": 1.5, "lawsuit": 1.5, "warn": 1.5, "sell-off": 2,
}
NEGATIONS = ["不", "未", "沒", "無", "非", "not ", "no "]


def keyword_score(title):
    t = title.lower()
    total = 0.0
    for words, sign in ((POS_WORDS, 1), (NEG_WORDS, -1)):
        for w, weight in words.items():
            idx = t.find(w)
            if idx < 0:
                continue
            negated = any(t[max(0, idx - len(n) - 1):idx].endswith(n) for n in NEGATIONS) and w not in ("不如預期",)
            total += weight * (-sign if negated else sign)
    return round(max(-0.95, min(0.95, total * 0.25)), 2)


def _label(score):
    return "利多" if score >= 0.25 else ("利空" if score <= -0.25 else "中立")


def _avg(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 2) if values else 0.0


# ==========================================
# 個股分析
# ==========================================
STOCK_ITEM = {
    "type": "OBJECT",
    "properties": {
        "code": {"type": "STRING"},
        "news": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "id": {"type": "INTEGER"},
            "score": {"type": "NUMBER", "description": "對該股股價影響 -1.0(強烈利空)~+1.0(強烈利多)"},
            "relevance": {"type": "NUMBER", "description": "與該公司的關聯度 0~1"},
        }, "required": ["id", "score", "relevance"]}},
        "sentiment": {"type": "NUMBER", "description": "綜合新聞情緒 -1.0~+1.0"},
        "outlook": {"type": "STRING", "enum": ["偏多", "中性", "偏空"]},
        "summary": {"type": "STRING", "description": "60~100 字繁體中文趨勢研判"},
        "bullPoints": {"type": "ARRAY", "items": {"type": "STRING"}},
        "risks": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["code", "news", "sentiment", "outlook", "summary", "bullPoints", "risks"],
}
BATCH_SCHEMA = {"type": "OBJECT", "properties": {"stocks": {"type": "ARRAY", "items": STOCK_ITEM}},
                "required": ["stocks"]}


def _keyword_result(news):
    for n in news:
        n["score"] = keyword_score(n["title"])
        n["sentiment"] = _label(n["score"])
    return {"sentiment": _avg([n["score"] for n in news]), "engine": "keyword"}


def analyze_stocks(batch):
    """batch: [(stock, news)...]，stock 含 code/name/sector/facts。
    一次呼叫分析多檔以節省免費額度。回傳 {code: (news, result)}"""
    out = {s["code"]: (news, _keyword_result(news)) for s, news in batch}
    if not enabled():
        return out

    blocks = []
    for s, news in batch:
        headlines = "\n".join(f"  [{i}] ({n['region']}/{n['source']}) {n['title']}" for i, n in enumerate(news)) or "  （無新聞）"
        blocks.append(f"### {s['code']} {s['name']}（{s.get('sector', '')}）\n"
                      f"量化數據：{json.dumps(s['facts'], ensure_ascii=False)}\n新聞：\n{headlines}")
    prompt = f"""你是專業的台股研究員。以下有 {len(batch)} 檔股票，各附量化數據與近 3 日國內外新聞標題，請逐檔評估短中期股價趨勢，stocks 陣列每檔一筆（code 填股票代號）。

{chr(10).join(blocks)}

每檔要求：
1. news：逐則給出對「該股」股價的影響分數（-1~+1）與關聯度（0~1），id 對應該股新聞編號；與該公司無關或僅順帶提及的新聞，關聯度給低分、影響分數趨近 0。
2. sentiment：綜合新聞情緒（以關聯度加權）。
3. outlook：結合量化數據與新聞，研判偏多／中性／偏空。
4. summary：60~100 字繁體中文，說明主要驅動因素，不要重複列數字。
5. bullPoints、risks：各 1~3 點，每點 25 字以內，具體指出題材或風險。
不要給買賣建議或目標價。"""
    result = _call(prompt, BATCH_SCHEMA)
    if not result:
        return out
    for item in result.get("stocks", []):
        code = str(item.get("code", "")).strip()
        if code not in out:
            continue
        news = out[code][0]
        for n_item in item.get("news", []):
            i = n_item.get("id")
            if isinstance(i, int) and 0 <= i < len(news):
                sc = round(max(-1.0, min(1.0, float(n_item.get("score", 0)))), 2)
                news[i].update(score=sc, relevance=round(float(n_item.get("relevance", 0)), 2), sentiment=_label(sc))
        out[code] = (news, {
            "sentiment": round(max(-1.0, min(1.0, float(item.get("sentiment", 0)))), 2),
            "outlook": item.get("outlook"),
            "summary": item.get("summary", "").strip(),
            "bullPoints": [p for p in item.get("bullPoints", []) if p][:3],
            "risks": [p for p in item.get("risks", []) if p][:3],
            "engine": "gemini",
        })
    return out


# ==========================================
# 大盤分析
# ==========================================
MARKET_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "globalSentiment": {"type": "NUMBER", "description": "國際新聞對台股影響 -1~+1"},
        "localSentiment": {"type": "NUMBER", "description": "國內新聞對台股影響 -1~+1"},
        "categories": {"type": "ARRAY", "items": {"type": "NUMBER"}, "description": "依序對應每個新聞類別的情緒 -1~+1"},
        "outlook": {"type": "STRING", "enum": ["偏多", "中性", "偏空"]},
        "commentary": {"type": "STRING", "description": "120~200 字繁體中文大盤趨勢研判"},
        "hotThemes": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "目前最受關注的 3~5 個產業題材"},
        "risks": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "2~4 個需留意的風險"},
    },
    "required": ["globalSentiment", "localSentiment", "categories", "outlook", "commentary", "hotThemes", "risks"],
}


def analyze_market(market, macro, top_stocks):
    for cat in macro:
        for n in cat["news"]:
            n["score"] = keyword_score(n["title"])
            n["sentiment"] = _label(n["score"])
        cat["sentiment"] = _avg([n["score"] for n in cat["news"]])
    intl = [n["score"] for c in macro for n in c["news"] if n["region"] == "國際"]
    local = [n["score"] for c in macro for n in c["news"] if n["region"] == "國內"]
    fallback = {"globalSentiment": _avg(intl), "localSentiment": _avg(local), "engine": "keyword"}
    if not enabled():
        return fallback

    sections = []
    for i, cat in enumerate(macro):
        lines = "\n".join(f"- ({n['source']}) {n['title']}" for n in cat["news"][:10])
        sections.append(f"類別{i}：{cat['category']}\n{lines}")
    prompt = f"""你是專業的台股策略分析師。請根據以下市場數據、國內外新聞與今日量化評分前段班個股，研判台股大盤短中期趨勢。

【市場數據】
{json.dumps(market, ensure_ascii=False)}

【新聞（依類別）】
{chr(10).join(sections)}

【量化評分前段班個股】
{json.dumps(top_stocks, ensure_ascii=False)}

要求：
1. categories 請依類別0、1、2…的順序各給一個情緒分數。
2. commentary 用 120~200 字繁體中文，說明資金面（外資現貨/期貨、融資）、國際連動與產業動向。
3. 資金流向的描述必須以市場數據中的 sectorFlows（各產業外資、投信近 5 日買賣超金額，單位億元）為依據：
   - 只有買超金額為正的產業才能說「資金流入」；若流入金額遠小於流出金額，應描述為「資金撤出」而非「轉向」。
   - 不要僅因個股量化排名高或新聞提及，就推論資金流入該產業。
4. hotThemes：每個題材後面用括號註明依據，例如「航運（外資買超）」、「CPO（新聞熱議）」、「驅動IC（營收與技術面強）」。
5. 不要給買賣建議。"""
    result = _call(prompt, MARKET_SCHEMA)
    if not result:
        return fallback
    for cat, s in zip(macro, result.get("categories", [])):
        cat["sentiment"] = round(max(-1.0, min(1.0, float(s))), 2)
    return {
        "globalSentiment": round(float(result.get("globalSentiment", 0)), 2),
        "localSentiment": round(float(result.get("localSentiment", 0)), 2),
        "outlook": result.get("outlook"),
        "commentary": result.get("commentary", "").strip(),
        "hotThemes": result.get("hotThemes", [])[:5],
        "risks": result.get("risks", [])[:4],
        "engine": "gemini",
    }
