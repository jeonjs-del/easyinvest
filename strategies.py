"""
동적자산배분 16개 전략 (snowball72/강환국식 규칙 + 변동성 변형 듀얼모멘텀).
각 전략: fn(mp, t, ctx) -> {ticker: weight} 또는 None(데이터 부족).
mp: 월말 종가 패널(DataFrame, 컬럼=티커), t: 정수 인덱스, ctx: {'gt_bear','ue_up12','ue_ok'}.
ETF 치환: EFA→IEFA, VWO/EEM→IEMG, DBC→PDBC, IWD→VTV, IWN→VBR, 현금→BIL.
"CASH" 티커는 mp에 없는 심볼이라 backtest()가 수익률 0으로 처리한다(변동성 변형 듀얼모멘텀에서 사용).

[변동성 변형 듀얼모멘텀 데이터 한계 — 2026-09-20 조사, AGENTS.md 데이터소스 섹션과 동기화]
- 국고채 3/10/30년 ETF(114260/148070/439870)는 전부 연 1회(12월) 분배금을 지급하는 실측
  확인됨(각 최근 1년 분배율 1.31%/3.62%/1.96%, funetf.co.kr 공시 기준). 그런데 이 세 종목은
  mp에 Close 가격만 들어있어 총수익이 아니다 — SPY/미국채(Adj Close)나 KOSPI200·IT
  TR(무분배 재투자형)와 섞으면 안전자산 6종 1개월 모멘텀 비교가 구조적으로 왜곡된다
  (스펙 9항 위배). 국고채 TR형 ETF가 국내에 없어 티커 교체로는 해결 불가 — 최소한
  VOLDM_ASSET_META로 어떤 자산이 가격 기준인지 명시해 화면/문서에 노출한다.
- KOSPI200(278530, 2017-11-21 상장)은 augment_panel()이 KS200(FDR 무료 캐시, 1990~ 제공,
  코스피200 가격지수·배당 미반영) 수익률로 상장 이전 구간을 소급 연결한 합성 컬럼
  "KOSPI_BF"를 사용한다(가격 레벨 단순접합이 아니라 상장일 기준 수익률 체이닝).
- KOSPI200 IT(363580, 2020-09-25 상장)와 국고채30년(439870, 2022-08-23 상장)은 상장 이전
  구간을 메울 무료·비로그인 데이터원이 없어(KRX data.krx.co.kr의 지수/ETF 조회 API는
  로그인 필요 — pykrx도 동일 이유로 실패, FDR의 무료 GitHub 캐시엔 KS11/KQ11/KS200만
  있고 업종지수·TR지수는 없음) backfill을 적용하지 않는다. 상장 전에는 각각 IT 오버레이
  생략(KOSPI200 100%로 대체) / 안전자산 후보 제외로 기존 동작을 유지한다.
"""
import pandas as pd

TAA_TICKERS = ["SPY", "IEFA", "IEMG", "AGG", "BND", "QQQ", "IWM", "VGK", "EWJ",
               "VNQ", "PDBC", "GLD", "TLT", "HYG", "LQD", "IEF", "TIP", "BIL",
               "SHY", "VTV", "VBR", "SCZ", "REM", "EMB", "BWX", "069500",
               "278530", "363580", "114260", "148070", "439870", "USD/KRW",
               "KS200"]

# ---- 변동성 변형 듀얼모멘텀 종목 매핑 (교체 가능하도록 dict로 관리) ---------
# KOSPI는 278530을 KS200(코스피200 지수)으로 상장 전 구간을 소급 연결한 합성 컬럼
# "KOSPI_BF"를 가리킨다(augment_panel 참고). KOSPI_IT는 신호·실행 동일 TR ETF 그대로 사용
# (backfill 없음). 미국채 3종은 mp에 직접 없고 augment_panel()이 USD 자산 x USD/KRW로
# 합성한 파생 컬럼(*_KRW)을 가리킨다.
VOLDM_TICKERS = {
    "KOSPI": "KOSPI_BF",        # 278530(KODEX 200TR) + KS200 소급합성
    "KOSPI_IT": "363580",       # KODEX 200IT TR
    "SPY": "SPY",
    "KR_BOND_SHORT": "114260",  # KODEX 국고채3년
    "KR_BOND_MID": "148070",    # KIWOOM 국고채10년
    "KR_BOND_LONG": "439870",   # KODEX 국고채30년액티브
    "US_BOND_SHORT": "US_BOND_SHORT_KRW",  # SHY(1-3Y) x USD/KRW 합성
    "US_BOND_MID": "US_BOND_MID_KRW",      # IEF(7-10Y) x USD/KRW 합성
    "US_BOND_LONG": "US_BOND_LONG_KRW",    # TLT(20Y+) x USD/KRW 합성
}
# 합성 파생컬럼명 -> 원본 USD ETF 티커
VOLDM_FX_SOURCE = {
    "US_BOND_SHORT_KRW": "SHY",
    "US_BOND_MID_KRW": "IEF",
    "US_BOND_LONG_KRW": "TLT",
}
VOLDM_FX_TICKER = "USD/KRW"
# KOSPI 상장 전 backfill에 쓸 (실거래 ETF 컬럼, 벤치마크 지수 컬럼) 쌍.
VOLDM_BACKFILL = {"KOSPI_BF": ("278530", "KS200")}
# 안전자산 동률 시 우선순위(높은 순). config처럼 여기서만 바꾸면 됨.
VOLDM_SAFE_PRIORITY = ["KR_BOND_SHORT", "KR_BOND_MID", "KR_BOND_LONG",
                        "US_BOND_SHORT", "US_BOND_MID", "US_BOND_LONG"]
VOLDM_VOL_THRESHOLD = 0.35
VOLDM_IT_WEIGHT = 0.25

# 자산별 데이터 성격 메타(화면/보고용). source_type: actual_etf(실거래 ETF, 상장 이후) /
# benchmark_index(상장 전 벤치마크 지수로 소급) / proxy(해외자산 환노출 합성).
# total_return: 배당/이자 분배가 가격에 재투자되어 있는지(True) 여부. 국고채 3종은 연 1회
# 분배금을 지급하는데 Close 가격만 쓰므로 False(가격지수 기준, 총수익 아님).
VOLDM_ASSET_META = {
    "KOSPI": {"total_return": False,
              "note": "278530은 TR(무분배 재투자)이라 상장 후 구간은 총수익이지만, "
                      "2017-11-21 이전은 KS200 가격지수로 소급해 배당 미반영 구간이 섞여있음"},
    "KOSPI_IT": {"total_return": True,
                 "note": "363580 TR(무분배 재투자), 2020-09-25 이전 데이터 없음(backfill 안 함)"},
    "SPY": {"total_return": True, "note": "Adj Close(배당 재투자) 사용"},
    "KR_BOND_SHORT": {"total_return": False,
                       "note": "114260, 연 1회(12월) 분배금 지급(최근 1년 분배율 1.31%) — Close만 사용, 총수익 아님"},
    "KR_BOND_MID": {"total_return": False,
                     "note": "148070, 연 1회(12월) 분배금 지급(최근 1년 분배율 3.62%) — Close만 사용, 총수익 아님"},
    "KR_BOND_LONG": {"total_return": False,
                      "note": "439870, 연 1회(12월) 분배금 지급(최근 1년 분배율 1.96%) — Close만 사용, 총수익 아님. "
                              "2022-08-23 이전 데이터 없음(backfill 안 함)"},
    "US_BOND_SHORT": {"total_return": True, "note": "SHY Adj Close × USD/KRW 합성(proxy)"},
    "US_BOND_MID": {"total_return": True, "note": "IEF Adj Close × USD/KRW 합성(proxy)"},
    "US_BOND_LONG": {"total_return": True, "note": "TLT Adj Close × USD/KRW 합성(proxy)"},
}


def _backfill_with_index(mp, etf_col, index_col):
    """etf_col의 상장 이전 구간을 index_col의 수익률로 소급 연결한다(수익률 체이닝 —
    가격 레벨을 그대로 이어붙이지 않고, 상장일 값과 지수값의 비율(scale)을 과거 전체에
    동일 적용해 상장일에서 수치가 정확히 맞물리게 한다).
    반환: (합성 가격 Series, 출처 태그 Series — 'actual_etf'/'benchmark_index')."""
    if etf_col not in mp.columns or index_col not in mp.columns:
        return None, None
    etf, idx = mp[etf_col], mp[index_col]
    etf_start = etf.first_valid_index()
    if etf_start is None:
        return None, None
    idx_at_start = idx.loc[etf_start] if etf_start in idx.index else None
    src = pd.Series(pd.NA, index=mp.index, dtype="object")
    src.loc[etf.notna()] = "actual_etf"
    if idx_at_start is None or pd.isna(idx_at_start) or idx_at_start == 0:
        return etf.copy(), src
    scale = etf.loc[etf_start] / idx_at_start
    composite = etf.copy()
    pre_mask = (mp.index < etf_start) & idx.notna()
    composite.loc[pre_mask] = idx.loc[pre_mask] * scale
    src.loc[pre_mask] = "benchmark_index"
    return composite, src


def voldm_source_type(mp, key, t):
    """VOLDM_TICKERS[key] 컬럼이 시점 t에서 actual_etf/benchmark_index/proxy 중 무엇인지."""
    col = VOLDM_TICKERS.get(key)
    if col is None:
        return None
    src_col = f"{col}_SRC"
    if src_col in mp.columns:
        v = mp[src_col].iloc[t]
        return v if pd.notna(v) else None
    if col not in mp.columns or pd.isna(mp[col].iloc[t]):
        return None
    return "proxy" if col in VOLDM_FX_SOURCE else "actual_etf"


def augment_panel(mp):
    """SHY/IEF/TLT(USD 총수익) x USD/KRW로 미국채 원화환노출 합성 시리즈를 패널에 추가.
    KRW_return = (1+USD_bond_return) * (1+USDKRW_return) - 1 을 월말 지수(기준값 1.0)로 누적."""
    mp = mp.copy()
    if VOLDM_FX_TICKER not in mp.columns:
        return mp
    fx = mp[VOLDM_FX_TICKER]
    fx_start = fx.first_valid_index()
    for krw_col, usd_col in VOLDM_FX_SOURCE.items():
        if usd_col not in mp.columns or fx_start is None:
            continue
        usd = mp[usd_col]
        usd_start = usd.first_valid_index()
        if usd_start is None:
            continue
        start = max(usd_start, fx_start)
        usd_ret = usd.pct_change()
        fx_ret = fx.pct_change()
        krw_ret = (1 + usd_ret) * (1 + fx_ret) - 1
        idx = pd.Series(index=mp.index, dtype="float64")
        idx.loc[start] = 1.0
        after = mp.index[mp.index > start]
        idx.loc[after] = (1 + krw_ret.loc[after]).cumprod()
        mp[krw_col] = idx
        mp[f"{krw_col}_SRC"] = pd.Series(
            ["proxy" if pd.notna(v) else pd.NA for v in idx], index=mp.index, dtype="object")

    for composite_col, (etf_col, index_col) in VOLDM_BACKFILL.items():
        composite, src = _backfill_with_index(mp, etf_col, index_col)
        if composite is not None:
            mp[composite_col] = composite
            mp[f"{composite_col}_SRC"] = src
    return mp

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


def strat_kr_mod_dm(mp, t, ctx):
    spy6 = ret(mp, "SPY", t, 6)
    if spy6 is None:
        return None
    if spy6 > 0:
        k200 = ret(mp, "069500", t, 6)
        if k200 is None:
            return {"SPY": 1.0}
        return {"SPY": 1.0} if spy6 >= k200 else {"069500": 1.0}
    sc = {s: ret(mp, s, t, 6) for s in MODDM_DEF}
    if len([v for v in sc.values() if v is not None]) < 3:
        return None
    w = {}
    for s in top_keys(sc, 3):
        tgt = s if sc[s] >= 0 else "BIL"
        w[tgt] = w.get(tgt, 0.0) + 1.0 / 3
    return w


def _kospi_vol_12m(mp, sym, t):
    """최근 12개 월간수익률의 표본표준편차(ddof=1) x sqrt(12)."""
    if sym not in mp.columns or t - 12 < 0:
        return None
    prices = mp[sym].iloc[t - 12:t + 1]
    if prices.isna().any():
        return None
    rets = prices.pct_change().dropna()
    if len(rets) < 12:
        return None
    return rets.std(ddof=1) * (12 ** 0.5)


def _voldm_safe_asset(mp, t):
    K = VOLDM_TICKERS
    best_key, best_val = None, None
    for key in VOLDM_SAFE_PRIORITY:
        v = ret(mp, K[key], t, 1)
        if v is None:
            continue
        if best_val is None or v > best_val:
            best_key, best_val = key, v
    if best_key is None or best_val <= 0:
        return {"CASH": 1.0}
    return {K[best_key]: 1.0}


def strat_vol_dm(mp, t, ctx):
    """변동성 변형 듀얼모멘텀: KOSPI/SPY 3개월 모멘텀으로 위험자산 선택 후,
    KOSPI 선택 시 12개월 변동성(35% 임계)로 IT 슬리브 비중 조정, 둘 다 약세면 6개 안전자산 1개월 모멘텀 1위 선택."""
    K = VOLDM_TICKERS
    kospi_3m = ret(mp, K["KOSPI"], t, 3)
    spy_3m = ret(mp, K["SPY"], t, 3)
    if kospi_3m is None or spy_3m is None:
        return None
    if kospi_3m > spy_3m and kospi_3m > 0:
        vol = _kospi_vol_12m(mp, K["KOSPI"], t)
        if vol is None:
            return None
        if vol < VOLDM_VOL_THRESHOLD:
            it_col = K["KOSPI_IT"]
            it_ok = it_col in mp.columns and pd.notna(mp[it_col].iloc[t])
            if it_ok:
                return {K["KOSPI"]: 1.0 - VOLDM_IT_WEIGHT, it_col: VOLDM_IT_WEIGHT}
            # KOSPI200 IT ETF 상장 전(2020-09-25 이전)에는 오버레이 불가 → KOSPI200 100%로 대체
            return {K["KOSPI"]: 1.0}
        return {K["KOSPI"]: 1.0}
    if spy_3m >= kospi_3m and spy_3m > 0:
        return {K["SPY"]: 1.0}
    return _voldm_safe_asset(mp, t)


STRATEGIES = {
    "BAA 공격형": strat_baa_agg,
    "변형 듀얼모멘텀": strat_mod_dm,
    "한국형 변형 듀얼모멘텀": strat_kr_mod_dm,
    "변동성 변형 듀얼모멘텀": strat_vol_dm,
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
