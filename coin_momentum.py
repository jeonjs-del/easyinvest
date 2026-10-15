"""코인 탭 '듀얼모멘텀' 하위탭(강환국 알트코인 듀얼모멘텀 업그레이드)의 순수 계산 로직.

규칙: BTC 실시간 현재가 > SMA120(완결 일봉 종가 120개)이면 투자, 아니면 100% 현금. 유니버스는 '현재' 시총 상위
20개(스테이블·래핑/스테이킹 파생·금 토큰·토큰화 펀드 제외), 선택 기간 수익률 상위 3개를 동일
비중 매수, 3일마다 재확인. Streamlit·네트워크 비의존 — 조회·화면은 app.py가 맡는다.

    python -m unittest tests.test_coin_momentum -v
"""
from datetime import date

import pandas as pd

DM_TOP_N = 20
DM_PICKS = 3
DM_SMA_WINDOW = 120

# 유니버스에서 뺄 코인(CoinGecko 심볼, 대문자). 여기만 고치면 된다.
# 토큰화 펀드/RWA는 사용자 목록엔 없었지만 2026-10 실제 상위 60에 FIGR_HELOC·USYC·USDY·BUIDL·
# EURSAFO가 들어와 있었다 — 이자가 붙는 달러/대출 토큰이라 가격이 1.03을 넘어 스테이블 보조
# 규칙에도 안 걸린다.
DM_EXCLUDED = {
    "stablecoin": {"USDT", "USDC", "DAI", "USDE", "FDUSD", "TUSD", "PYUSD", "USD1", "USDS", "USDD",
                   "RLUSD", "BUSD", "USDG", "USDP", "GUSD", "USDF", "USD0", "FRAX", "LUSD", "CRVUSD",
                   "GHO", "EURC", "USDB", "USDX", "SUSDE", "SUSDS"},
    "wrapped": {"WBTC", "WETH", "STETH", "WSTETH", "WEETH", "EETH", "CBBTC", "BTCB", "CBETH", "RETH",
                "METH", "JITOSOL", "MSOL", "BNSOL", "WBETH", "SOLVBTC", "LBTC", "TBTC", "RSETH",
                "EZETH", "WBNB", "STHYPE", "KHYPE"},
    "gold": {"XAUT", "PAXG"},
    "tokenized_fund": {"FIGR_HELOC", "USYC", "USDY", "BUIDL", "EURSAFO", "OUSG", "USTB", "BENJI",
                       "SYRUPUSDC"},
}
DM_EXCLUDED_LABELS = {"stablecoin": "스테이블코인", "wrapped": "래핑·스테이킹 파생",
                      "gold": "금 토큰", "tokenized_fund": "토큰화 펀드·RWA",
                      "stable_rule": "스테이블(보조 규칙)"}
STABLE_PRICE_BAND = (0.97, 1.03)

# 업비트·바이낸스·OKX·바이비트 현물 상장 여부를 2026-10-15에 각 거래소 상장목록 API로 확인한
# 결과. 표시만 하고 순위에서는 빼지 않는다. LEO는 OKX에만 있다(유동성이 얕아 사용자 지정대로 포함).
DM_HARD_TO_TRADE = {"LEO", "WBT", "XMR", "RAIN", "BTW"}

# 표시명 → (CoinGecko markets 필드, 일수). 필드가 None이면 실시간가 / N일 전 일봉 종가로 계산.
DM_PERIODS = {
    "1주": ("price_change_percentage_7d_in_currency", 7),
    "2주": ("price_change_percentage_14d_in_currency", 14),
    "3주": (None, 21),
    "4주": (None, 28),
}
DM_DEFAULT_PERIOD = "2주"


def exclusion_reason(m):
    """markets 항목 하나가 유니버스 제외 대상이면 DM_EXCLUDED 카테고리(또는 "stable_rule"), 아니면 None."""
    sym = (m.get("symbol") or "").upper()
    for cat, syms in DM_EXCLUDED.items():
        if sym in syms:
            return cat
    name = (m.get("name") or "").lower()
    price = m.get("current_price")
    if ("usd" in sym.lower() or "usd" in name) and price is not None \
            and STABLE_PRICE_BAND[0] <= price <= STABLE_PRICE_BAND[1]:
        return "stable_rule"
    return None


def select_universe(markets, top_n=DM_TOP_N):
    """(유니버스 상위 top_n, 제외된 [(항목, 사유)]) 반환. 시총 내림차순으로 다시 정렬한다 —
    CoinGecko의 market_cap_rank에 동률·역전이 있었다(build_coin_ranking 주석)."""
    ordered = sorted((m for m in markets if m.get("market_cap")),
                     key=lambda m: m["market_cap"], reverse=True)
    kept, excluded = [], []
    for m in ordered:
        reason = exclusion_reason(m)
        if reason:
            excluded.append((m, reason))
        elif len(kept) < top_n:
            kept.append(m)
    return kept, excluded


def completed_closes(closes, today_utc):
    """일봉 종가 Series(날짜 인덱스)에서 아직 안 끝난 UTC 당일 이후 봉을 뺀다."""
    s = pd.Series(closes).dropna().sort_index()
    if s.empty:
        return s
    idx_dates = pd.DatetimeIndex(s.index).date
    return s[idx_dates < today_utc]


def sma_filter(closes, window=DM_SMA_WINDOW, price=None):
    """SMA는 완결 일봉 종가 window개로 계산하고, 판정은 price(실시간 현재가)와 비교한다.
    price가 없으면 마지막 완결 종가로 판정. 데이터가 window개 미만이면 None."""
    s = pd.Series(closes).dropna()
    if len(s) < window:
        return None
    close = float(s.iloc[-1])
    sma = float(s.iloc[-window:].mean())
    px = close if price is None else float(price)
    return {"close": close, "sma": sma, "price": px, "live": price is not None,
            "gap": px / sma - 1.0, "above": px > sma, "date": s.index[-1]}


def close_before(closes_by_close_time, cutoff):
    """봉 마감 시각을 인덱스로 둔 종가 Series에서 cutoff 이전에 마감한 마지막 종가.
    거래소마다 일봉 시작 시각이 달라(OKX 1D는 UTC+8 기준) 날짜가 아니라 마감 시각으로 고른다."""
    s = pd.Series(closes_by_close_time).dropna().sort_index()
    s = s[s.index <= cutoff]
    return float(s.iloc[-1]) if len(s) else None


def live_return(price, base):
    if price is None or not base:
        return None
    return price / base - 1.0


def latest_update(markets):
    """markets 응답의 last_updated 중 가장 최근 값(UTC Timestamp). 없으면 None."""
    ts = pd.to_datetime([m.get("last_updated") for m in markets if m.get("last_updated")],
                        utc=True, errors="coerce")
    ts = ts.dropna()
    return ts.max() if len(ts) else None


def lookback_return(closes, days_back):
    """closes[-1] / closes[-1-days_back] - 1. 데이터가 모자라면 None."""
    idx = len(closes) - 1 - days_back
    if idx < 0 or not closes[idx]:
        return None
    return closes[-1] / closes[idx] - 1.0


def rank_momentum(rows, picks=DM_PICKS):
    """rows: [{"symbol", "ret", ...}] — ret(소수 비율) 내림차순 순위표 DataFrame.
    수익률이 없는 코인은 맨 아래(순위 없음). 상위 picks개에 is_pick, 그중 ret<=0이면 neg_momentum."""
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.sort_values("ret", ascending=False, na_position="last", kind="mergesort").reset_index(drop=True)
    has_ret = df["ret"].notna()
    df["rank"] = pd.Series(range(1, len(df) + 1), dtype="Int64").where(has_ret)
    df["is_pick"] = has_ret & (df.index < picks)
    df["neg_momentum"] = df["is_pick"] & (df["ret"] <= 0)
    df["hard_to_trade"] = df["symbol"].str.upper().isin(DM_HARD_TO_TRADE)
    return df


def note_for(row):
    """순위표 '비고' 칸 문자열."""
    notes = []
    if row["is_pick"]:
        notes.append("⚠ 모멘텀 음수(현금 보유 고려)" if row["neg_momentum"] else "🟢 매수 후보")
    if row["hard_to_trade"]:
        notes.append("거래 어려움")
    return " · ".join(notes)


def today_utc_date(now=None):
    ts = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now).tz_convert("UTC")
    return date(ts.year, ts.month, ts.day)
