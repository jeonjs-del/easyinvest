"""
동적자산배분 15개 전략 (snowball72/강환국식 규칙).
각 전략: fn(mp, t, ctx) -> {ticker: weight} 또는 None(데이터 부족).
mp: 월말 종가 패널(DataFrame, 컬럼=티커), t: 정수 인덱스, ctx: {'gt_bear','ue_up12','ue_ok'}.
ETF 치환: EFA→IEFA, VWO/EEM→IEMG, DBC→PDBC, IWD→VTV, IWN→VBR, 현금→BIL.
"""
import pandas as pd

TAA_TICKERS = ["SPY", "IEFA", "IEMG", "AGG", "BND", "QQQ", "IWM", "VGK", "EWJ",
               "VNQ", "PDBC", "GLD", "TLT", "HYG", "LQD", "IEF", "TIP", "BIL",
               "SHY", "VTV", "VBR", "SCZ", "REM", "EMB", "BWX"]

# ---- 모멘텀 유틸 ----------------------------------------------------------
def ret(mp, sym, t, k):
    if sym not in mp.columns or t - k < 0:
        return None
    a, b = mp[sym].iloc[t], mp[sym].iloc[t - k]
    if pd.isna(a) or pd.isna(b) or b == 0:
        return None
    return a / b - 1.0


def m13612w(mp, sym, t):
    r = [ret(mp, sym, t, k) for k in (1, 3, 6, 12)]
    return None if None in r else 12 * r[0] + 4 * r[1] + 2 * r[2] + r[3]


def m13612u(mp, sym, t):
    r = [ret(mp, sym, t, k) for k in (1, 3, 6, 12)]
    return None if None in r else sum(r) / 4


def sma_score(mp, sym, t, window):
    """현재가 / 최근 window개 월말 종가 평균 - 1 (window개 = 현재 포함)."""
    if sym not in mp.columns or t - window + 1 < 0:
        return None
    seg = mp[sym].iloc[t - window + 1:t + 1]
    if seg.isna().any() or seg.mean() == 0:
        return None
    return mp[sym].iloc[t] / seg.mean() - 1.0


def top_keys(scores, n):
    items = [(k, v) for k, v in scores.items() if v is not None]
    items.sort(key=lambda x: x[1], reverse=True)
    return [k for k, _ in items[:n]]


def _all(scores):
    return all(v is not None for v in scores.values())


# ---- 방어 슬리브 (BAA/DAA류: SMA/13612 상위 + BIL 대체) ------------------
def _defensive_sma(mp, t, universe, n, window):
    sc = {s: sma_score(mp, s, t, window) for s in universe}
    bil = sma_score(mp, "BIL", t, window)
    if bil is None or len([v for v in sc.values() if v is not None]) < n:
        return None
    w = {}
    for s in top_keys(sc, n):
        tgt = s if sc[s] > bil else "BIL"
        w[tgt] = w.get(tgt, 0.0) + 1.0 / n
    return w


# ==========================================================================
CANARY_BAA = ["SPY", "IEFA", "IEMG", "AGG"]
DEF_BAA = ["TIP", "PDBC", "BIL", "IEF", "TLT", "LQD", "BND"]
OFF_BAA_AGG = ["QQQ", "IEFA", "IEMG", "AGG"]
OFF_BAL = ["SPY", "QQQ", "IWM", "VGK", "EWJ", "IEMG", "VNQ", "PDBC", "GLD", "TLT", "HYG", "LQD"]
MODDM_DEF = ["SHY", "IEF", "TLT", "TIP", "LQD", "HYG", "BWX", "EMB"]
NOVELL = ["SHY", "IEF", "TLT", "TIP", "LQD", "HYG", "BWX", "EMB"]
GTAA_U = ["SPY", "IEFA", "IEF", "PDBC", "VNQ"]
RAA_RISK = ["QQQ", "VBR", "GLD", "IEF", "TLT"]
HAA_OFF = ["SPY", "IWM", "IEFA", "IEMG", "VNQ", "PDBC", "IEF", "TLT"]


def strat_baa_agg(mp, t, ctx):
    can = {s: m13612w(mp, s, t) for s in CANARY_BAA}
    if not _all(can):
        return None
    if all(v > 0 for v in can.values()):
        top = top_keys({s: sma_score(mp, s, t, 13) for s in OFF_BAA_AGG}, 1)
        return {top[0]: 1.0} if top else None
    return _defensive_sma(mp, t, DEF_BAA, 3, 13)


def strat_baa_bal(mp, t, ctx):
    can = {s: m13612w(mp, s, t) for s in CANARY_BAA}
    if not _all(can):
        return None
    if all(v > 0 for v in can.values()):
        top = top_keys({s: sma_score(mp, s, t, 13) for s in OFF_BAL}, 6)
        if len(top) < 6:
            return None
        return {s: 1.0 / 6 for s in top}
    return _defensive_sma(mp, t, DEF_BAA, 3, 13)


def strat_mod_dm(mp, t, ctx):
    spy = ret(mp, "SPY", t, 12)
    if spy is None:
        return None
    if spy > 0:
        ie = ret(mp, "IEFA", t, 12)
        if ie is None:
            return {"SPY": 1.0}
        return {"SPY": 1.0} if spy >= ie else {"IEFA": 1.0}
    sc = {s: ret(mp, s, t, 6) for s in MODDM_DEF}
    if len([v for v in sc.values() if v is not None]) < 3:
        return None
    w = {}
    for s in top_keys(sc, 3):
        tgt = s if sc[s] >= 0 else "BIL"
        w[tgt] = w.get(tgt, 0.0) + 1.0 / 3
    return w


def strat_vaa(mp, t, ctx):
    off = {s: m13612w(mp, s, t) for s in ["SPY", "IEFA", "IEMG", "AGG"]}
    if not _all(off):
        return None
    if all(v > 0 for v in off.values()):
        return {top_keys(off, 1)[0]: 1.0}
    dfn = {s: m13612w(mp, s, t) for s in ["LQD", "IEF", "BIL"]}
    top = top_keys(dfn, 1)
    return {top[0]: 1.0} if top else None


def strat_adm(mp, t, ctx):
    def sc(s):
        r = [ret(mp, s, t, k) for k in (1, 3, 6)]
        return None if None in r else sum(r) / 3
    s_spy, s_scz = sc("SPY"), sc("SCZ")
    if None in (s_spy, s_scz):
        return None
    best, bs = ("SPY", s_spy) if s_spy >= s_scz else ("SCZ", s_scz)
    if bs > 0:
        return {best: 1.0}
    a, b = ret(mp, "TLT", t, 1), ret(mp, "TIP", t, 1)
    if None in (a, b):
        return None
    return {"TLT": 1.0} if a >= b else {"TIP": 1.0}


def strat_haa(mp, t, ctx):
    tip = m13612u(mp, "TIP", t)
    if tip is None:
        return None
    dsc = {s: m13612u(mp, s, t) for s in ["BIL", "IEF"]}
    if not _all(dsc):
        return None
    bdef = top_keys(dsc, 1)[0]
    if tip < 0:
        return {bdef: 1.0}
    sc = {s: m13612u(mp, s, t) for s in HAA_OFF}
    if len([v for v in sc.values() if v is not None]) < 4:
        return None
    w = {}
    for s in top_keys(sc, 4):
        tgt = s if sc[s] >= 0 else bdef
        w[tgt] = w.get(tgt, 0.0) + 0.25
    return w


def strat_gem(mp, t, ctx):
    spy, bil, ie = ret(mp, "SPY", t, 12), ret(mp, "BIL", t, 12), ret(mp, "IEFA", t, 12)
    if None in (spy, bil, ie):
        return None
    if spy > bil:
        return {"SPY": 1.0} if spy >= ie else {"IEFA": 1.0}
    return {"AGG": 1.0}


def strat_daa(mp, t, ctx):
    can = {s: m13612w(mp, s, t) for s in ["IEMG", "BND"]}
    if not _all(can):
        return None
    b = sum(1 for v in can.values() if v < 0)
    off = {s: m13612w(mp, s, t) for s in OFF_BAL}
    dfn = {s: m13612w(mp, s, t) for s in ["BIL", "IEF", "LQD"]}
    dtop = top_keys(dfn, 1)
    otop = top_keys(off, 6)
    if not dtop or (b < 2 and len(otop) < 6):
        return None
    if b == 0:
        return {s: 1.0 / 6 for s in otop}
    if b == 1:
        w = {s: 0.5 / 6 for s in otop}
        w[dtop[0]] = w.get(dtop[0], 0.0) + 0.5
        return w
    return {dtop[0]: 1.0}


def strat_paa(mp, t, ctx):
    sc = {s: sma_score(mp, s, t, 12) for s in OFF_BAL}
    if not _all(sc):
        return None
    n = sum(1 for v in sc.values() if v < 0)
    ief_w = n / 12.0
    top = top_keys(sc, 6)
    if len(top) < 6:
        return None
    w = {"IEF": ief_w}
    each = (1.0 - ief_w) / 6
    for s in top:
        w[s] = w.get(s, 0.0) + each
    return w


def strat_comp_dm(mp, t, ctx):
    pairs = [("SPY", "IEFA"), ("LQD", "HYG"), ("VNQ", "REM"), ("TLT", "GLD")]
    bil = ret(mp, "BIL", t, 12)
    if bil is None:
        return None
    w = {}
    for a, b in pairs:
        ra, rb = ret(mp, a, t, 12), ret(mp, b, t, 12)
        if None in (ra, rb):
            return None
        best, br = (a, ra) if ra >= rb else (b, rb)
        tgt = best if br > bil else "BIL"
        w[tgt] = w.get(tgt, 0.0) + 0.25
    return w


def strat_laa(mp, t, ctx):
    for s in ("VTV", "GLD", "IEF", "QQQ", "SHY"):
        if s not in mp.columns or pd.isna(mp[s].iloc[t]):
            return None
    bear = bool(ctx["gt_bear"].get(mp.index[t], False)) if ctx else False
    slot = "BIL" if bear else "QQQ"
    w = {"VTV": 0.25, "GLD": 0.25, "IEF": 0.25}
    w[slot] = w.get(slot, 0.0) + 0.25
    return w


def strat_raa(mp, t, ctx):
    can = {s: m13612w(mp, s, t) for s in ["IEMG", "BND"]}
    if not _all(can):
        return None
    ue_up = bool(ctx["ue_up12"].get(mp.index[t], False)) if (ctx and ctx.get("ue_ok")) else False
    cond2 = any(v < 0 for v in can.values())
    if ue_up and cond2:
        return {"IEF": 0.5, "TLT": 0.5}
    for s in RAA_RISK:
        if ret(mp, s, t, 1) is None:
            return None
    return {s: 0.2 for s in RAA_RISK}


def strat_nlx_haa(mp, t, ctx):
    tip = m13612u(mp, "TIP", t)
    if tip is None:
        return None
    dsc = {s: m13612u(mp, s, t) for s in ["BIL", "IEF"]}
    if not _all(dsc):
        return None
    bdef = top_keys(dsc, 1)[0]
    if tip < 0:
        return {bdef: 1.0}
    spy = m13612u(mp, "SPY", t)
    w = {"IEF": 0.4}
    if spy is not None and spy < 0:
        w[bdef] = w.get(bdef, 0.0) + 0.6
    else:
        w["SPY"] = w.get("SPY", 0.0) + 0.6
    return w


def strat_gtaa(mp, t, ctx):
    w = {}
    for s in GTAA_U:
        sc = sma_score(mp, s, t, 10)
        if sc is None:
            return None
        if sc > 0:
            w[s] = w.get(s, 0.0) + 0.2
        else:
            w["BIL"] = w.get("BIL", 0.0) + 0.2
    return w


def strat_novell(mp, t, ctx):
    sc = {s: ret(mp, s, t, 6) for s in NOVELL}
    bil = ret(mp, "BIL", t, 6)
    if bil is None or len([v for v in sc.values() if v is not None]) < 3:
        return None
    w = {}
    for s in top_keys(sc, 3):
        tgt = s if sc[s] > bil else "BIL"
        w[tgt] = w.get(tgt, 0.0) + 1.0 / 3
    return w


STRATEGIES = {
    "BAA 공격형": strat_baa_agg,
    "변형 듀얼모멘텀": strat_mod_dm,
    "VAA": strat_vaa,
    "가속 듀얼모멘텀": strat_adm,
    "HAA": strat_haa,
    "오리지널 듀얼모멘텀": strat_gem,
    "BAA 중도형": strat_baa_bal,
    "DAA": strat_daa,
    "PAA": strat_paa,
    "종합 듀얼모멘텀": strat_comp_dm,
    "LAA": strat_laa,
    "RAA": strat_raa,
    "NLX HAA": strat_nlx_haa,
    "GTAA": strat_gtaa,
    "Novell 채권": strat_novell,
}
