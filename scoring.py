"""量化評分：基本面、籌碼面、技術面（皆為 20~99 分）。"""


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ==========================================
# 基本面
# ==========================================
def fundamental(rev, prof, val):
    score = 55
    notes = []
    yoy = rev.get("revYoY")
    if yoy is not None:
        score += clamp(yoy * 0.5, -20, 20)
        if yoy >= 30:
            notes.append("營收年增>30%")
    cum = rev.get("revCumYoY")
    if cum is not None:
        score += clamp(cum * 0.2, -8, 8)
    mom = rev.get("revMoM")
    if mom is not None:
        score += clamp(mom * 0.1, -3, 3)
    gm = prof.get("grossMargin")
    if gm is not None:
        score += 5 if gm >= 40 else (2 if gm >= 20 else (-3 if gm < 10 else 0))
    om = prof.get("opMargin")
    if om is not None:
        score += 4 if om >= 15 else (-8 if om < 0 else 0)
    eps = prof.get("eps")
    if eps is not None and eps < 0:
        score -= 8
        notes.append("虧損")
    pe = val.get("pe")
    if pe:
        score += 6 if pe < 12 else (3 if pe < 20 else (0 if pe < 30 else (-4 if pe < 50 else -8)))
    return int(clamp(score, 20, 99)), notes


# ==========================================
# 籌碼面
# ==========================================
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


def chips(inst_hist, avg_vol_lots, margin, tdcc_now, tdcc_prev):
    score = 55
    detail = {}
    tags = []
    if inst_hist:
        fs = streak([h["foreign"] for h in inst_hist])
        ts = streak([h["trust"] for h in inst_hist])
        f5 = sum(h["foreign"] for h in inst_hist[:5])
        t5 = sum(h["trust"] for h in inst_hist[:5])
        d5 = sum(h["dealer"] for h in inst_hist[:5])
        detail.update(foreignStreak=fs, trustStreak=ts, foreign5d=f5, trust5d=t5, dealer5d=d5)
        score += clamp(fs * 2.5, -12, 12)
        score += clamp(ts * 3.5, -12, 16)
        if avg_vol_lots:
            ratio = (f5 + t5) / (avg_vol_lots * 5)  # 法人 5 日買超占成交量比例
            score += clamp(ratio * 60, -10, 10)
        tags += [t for t in (describe_streak("外資", fs), describe_streak("投信", ts)) if t]
    if margin:
        detail.update(marginBal=margin.get("marginBal"), marginChg=margin.get("marginChg"),
                      shortBal=margin.get("shortBal"))
        mb, mc = margin.get("marginBal") or 0, margin.get("marginChg") or 0
        if mb > 0 and mc / mb > 0.03:
            score -= 4  # 融資快速增加，籌碼趨於凌亂
            tags.append("融資大增")
        elif mb > 0 and mc / mb < -0.03:
            score += 2
        if mb > 0 and (margin.get("shortBal") or 0) / mb > 0.3:
            tags.append("券資比高")
    if tdcc_now:
        detail.update(big1000=tdcc_now.get("big1000"), big400=tdcc_now.get("big400"),
                      retailPeople=tdcc_now.get("retailPeople"))
        if tdcc_prev and tdcc_now.get("big1000") is not None and tdcc_prev[0] is not None:
            chg = round(tdcc_now["big1000"] - tdcc_prev[0], 2)
            detail["big1000Chg"] = chg
            score += clamp(chg * 6, -8, 8)
            if chg >= 0.3:
                tags.append("千張大戶增")
            elif chg <= -0.3:
                tags.append("千張大戶減")
        if tdcc_prev and tdcc_prev[2]:
            retail_chg = tdcc_now["retailPeople"] - tdcc_prev[2]
            detail["retailPeopleChg"] = retail_chg
            score += 2 if retail_chg < 0 else -1
    desc = " / ".join(tags[:3]) if tags else "法人動向中性"
    return int(clamp(score, 20, 99)), desc, detail


# ==========================================
# 技術面
# ==========================================
def rsi(close, n=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    return 100 - 100 / (1 + rs)


def technical(df):
    close, vol = df["Close"], df["Volume"]
    last, prev = float(close.iloc[-1]), float(close.iloc[-2])
    ma5 = float(close.rolling(5).mean().iloc[-1])
    ma20 = float(close.rolling(20).mean().iloc[-1])
    ma60s = close.rolling(min(60, len(close))).mean()
    ma60, ma60_prev = float(ma60s.iloc[-1]), float(ma60s.iloc[-6])
    vol_ma5 = float(vol.iloc[-6:-1].mean())
    high20 = float(close.iloc[-21:-1].max())
    rsi14 = float(rsi(close).iloc[-1])
    ret20 = (last / float(close.iloc[-21]) - 1) * 100 if len(close) > 21 else 0
    bias60 = (last / ma60 - 1) * 100

    bullish = last > ma20 > ma60
    vol_breakout = float(vol.iloc[-1]) > vol_ma5 * 1.5
    new_high = last >= high20
    ma60_up = ma60 > ma60_prev

    score = 50
    score += 12 if last > ma60 else -8
    score += 8 if last > ma20 else -5
    score += 3 if last > ma5 else 0
    score += 8 if bullish else 0
    score += 6 if ma60_up else -4
    score += 8 if new_high else 0
    score += 5 if vol_breakout and last > prev else (-4 if vol_breakout and last < prev else 0)
    score += -6 if rsi14 > 80 else (4 if 50 <= rsi14 <= 70 else (-3 if rsi14 < 30 else 0))
    score += -5 if bias60 > 25 else 0  # 季線乖離過大

    signals = []
    if bullish:
        signals.append("多頭排列")
    if new_high:
        signals.append("創20日高")
    if vol_breakout:
        signals.append("爆量")
    if bias60 > 25:
        signals.append("季線乖離大")
    if last < ma60:
        signals.append("跌破季線")
    if rsi14 < 30:
        signals.append("RSI超賣")

    return {
        "close": round(last, 2),
        "changePct": round((last - prev) / prev * 100, 2),
        "ma20": round(ma20, 2),
        "ma60": round(ma60, 2),
        "rsi": round(rsi14, 1),
        "ret20": round(ret20, 1),
        "bias60": round(bias60, 1),
        "volumeLots": int(float(vol.iloc[-1]) // 1000),
        "avgVolLots": int(float(vol.iloc[-20:].mean()) // 1000),
        "signals": signals,
        "score": int(clamp(score, 20, 99)),
        "spark": [round(float(v), 2) for v in close.iloc[-60:]],
    }
