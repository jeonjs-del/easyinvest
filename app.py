"""
나만의 투자 대시보드 — 차트 · 관심목록 · 동적자산배분 (탭 분리)
데이터: FinanceDataReader (국내/미국 주식·ETF·지수 + FRED). 서버 수집 → CORS 제약 없음.
실행:  streamlit run app.py   (같은 폴더에 strategies.py 필요)
"""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import requests
import FinanceDataReader as fdr
import yfinance as yf
# 로컬 패치 래퍼: 줌 상태 복원 + dblclick 지원
from lwc_local import renderLightweightCharts

from strategies import STRATEGIES, TAA_TICKERS, augment_panel

st.set_page_config(page_title="나의 투자 대시보드", layout="wide")

WATCHLIST_FILE = "watchlist.json"
GIST_FILENAME = "watchlist.json"
DEFAULT_WATCHLIST = ["069500", "133690", "114260", "005930"]
SMA_PERIODS = [5, 10, 20, 50, 60, 120, 200]
EMA_PERIODS = [9, 21, 65]
PERIOD_DAYS = {"1W": 7, "1M": 30, "3M": 90, "1Y": 365, "3Y": 1095, "5Y": 1825, "All": 100000}
PERIOD_BSPACING = {"1W": 120, "1M": 36, "3M": 13, "1Y": 4, "3Y": 1.6, "5Y": 0.9, "All": 0.3}
MA_COLORS = {
    5: "#1f77b4", 10: "#ff7f0e", 20: "#2ca02c", 50: "#d62728",
    60: "#8c564b", 120: "#bcbd22", 200: "#e377c2",
    9: "#9467bd", 21: "#17becf", 65: "#aec7e8",
}
RETURN_PERIODS = ["1일", "5일", "1개월", "3개월", "6개월", "1년"]
RET_COLOR_CAP = 0.30  # 수익률 색상 진하기의 절대값 상한 (±30%, 이상은 최대 진하기로 클립)
DEFAULT_INDICES = {
    "코스피": "KS11", "코스닥": "KQ11", "S&P500": "US500", "나스닥종합": "IXIC",
    "다우": "DJI", "러셀2000": "RUT", "닛케이225": "N225", "상해종합": "SSEC",
    "항셍": "HSI", "대만가권": "TWII", "독일DAX": "DAX", "프랑스CAC40": "CAC40",
}

# 매수 탭 · 계절성: scanner/seasonality_scan.py가 만든 결과 파일만 읽는다(앱은 재계산 안 함)
SEASONALITY_PARQUET = "data/seasonality.parquet"
SEASONALITY_META = "data/seasonality_meta.json"
SEASONALITY_STALE_DAYS = 30   # 기준일이 이보다 오래되면 경고 표시
BUY_WINDOW_BEFORE = 2         # 진입일 -2일부터
BUY_WINDOW_AFTER = 5          # 진입일 +5일까지 매수창 유지
MARKET_CAP_BUCKETS = {
    "전체": 0, "≥$1B": 1e9, "≥$10B": 1e10, "≥$100B": 1e11,
}

# 매수 탭 · 추세추종: scanner/trend_scan.py가 만든 결과 파일만 읽는다(앱은 재계산 안 함)
TREND_PARQUET = "data/trend.parquet"
TREND_META = "data/trend_meta.json"
TREND_STALE_DAYS = 30
TREND_CAP_BUCKETS = {"전체": 0, "≥$1B": 1e9, "≥$10B": 1e10}


def normalize(sym):
    s = sym.strip().upper()
    if s.endswith(".KS") or s.endswith(".KQ"):
        s = s[:-3]
    if s.isdigit():
        s = s.zfill(6)
    return s


def _load_watchlist_local():
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                return [normalize(str(c)) for c in data]
        except Exception:
            pass
    return DEFAULT_WATCHLIST.copy()


def _save_watchlist_local(codes):
    try:
        with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(codes, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # Cloud filesystem은 재시작 시 초기화 — 저장 실패는 무시


def _gist_configured():
    try:
        g = st.secrets.get("gist")
        return bool(g and g.get("token") and g.get("id"))
    except Exception:
        return False


def _gist_headers(token):
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}


@st.cache_data(ttl=300)
def _gist_fetch_content(token, gist_id):
    """Gist의 watchlist.json raw 내용을 반환. 파일이 없거나 비어있으면 빈 문자열."""
    r = requests.get(f"https://api.github.com/gists/{gist_id}",
                      headers=_gist_headers(token), timeout=10)
    r.raise_for_status()
    files = r.json().get("files", {})
    f = files.get(GIST_FILENAME)
    return f.get("content", "") if f else ""


def load_watchlist():
    """(관심목록, storage_mode) 반환. storage_mode: 'gist' 또는 'local'."""
    if _gist_configured():
        token, gist_id = st.secrets["gist"]["token"], st.secrets["gist"]["id"]
        try:
            content = _gist_fetch_content(token, gist_id)
        except Exception:
            # API 호출 실패 → 로컬 파일 방식으로 폴백
            return _load_watchlist_local(), "local"
        if content and content.strip():
            try:
                data = json.loads(content)
                if isinstance(data, list):
                    # 저장된 값이 빈 배열이어도 그대로 유지 (사용자가 전부 삭제한 상태)
                    return [normalize(str(c)) for c in data], "gist"
            except Exception:
                pass
        # Gist는 연결됐지만 파일이 비어있음/저장된 적 없음 → 디폴트 사용
        return DEFAULT_WATCHLIST.copy(), "gist"
    return _load_watchlist_local(), "local"


def save_watchlist(codes, mode):
    if mode == "gist":
        try:
            token, gist_id = st.secrets["gist"]["token"], st.secrets["gist"]["id"]
            r = requests.patch(
                f"https://api.github.com/gists/{gist_id}",
                headers=_gist_headers(token),
                json={"files": {GIST_FILENAME: {
                    "content": json.dumps(codes, ensure_ascii=False, indent=2)
                }}},
                timeout=10,
            )
            r.raise_for_status()
            _gist_fetch_content.clear()  # 캐시 무효화 → 다음 로드 시 최신값 반영
        except Exception:
            pass  # 저장 실패는 무시 (다음 rerun에서 재시도 가능)
        return
    _save_watchlist_local(codes)


@st.cache_data(ttl=60 * 60 * 24)
def get_krx_listings():
    """KRX 주식 + 한국 ETF 통합 목록. (code→name dict, 정렬된 display 옵션 리스트)."""
    code_name = {}
    try:
        df = fdr.StockListing("KRX")
        cc = "Code" if "Code" in df.columns else df.columns[0]
        cn = "Name" if "Name" in df.columns else df.columns[1]
        for code, name in zip(df[cc].astype(str).str.zfill(6), df[cn]):
            code_name[code] = str(name)
    except Exception:
        pass
    try:
        df_etf = fdr.StockListing("ETF/KR")
        sc = "Symbol" if "Symbol" in df_etf.columns else df_etf.columns[0]
        nc = "Name"   if "Name"   in df_etf.columns else df_etf.columns[2]
        for code, name in zip(df_etf[sc].astype(str).str.zfill(6), df_etf[nc]):
            code_name[code] = str(name)
    except Exception:
        pass
    options = sorted([f"{name} ({code})" for code, name in code_name.items()])
    return code_name, options


@st.cache_data(ttl=60 * 60)
def get_price(code, start):
    try:
        df = fdr.DataReader(code, start)
        return df if df is not None and not df.empty else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def name_of(code, names):
    return names.get(code, code)


def month_return(df, months):
    if df.empty:
        return None
    close = df["Close"].dropna()
    if len(close) < 2:
        return None
    target = close.index[-1] - pd.DateOffset(months=months)
    past = close[close.index <= target]
    return None if past.empty else float(close.iloc[-1] / past.iloc[-1] - 1.0)


def period_return(df, key):
    close = df["Close"].dropna() if not df.empty else pd.Series(dtype=float)
    if len(close) < 2:
        return None
    if key in ("1일", "5일"):
        n = 1 if key == "1일" else 5
        return None if len(close) <= n else float(close.iloc[-1] / close.iloc[-1 - n] - 1)
    return month_return(df, {"1개월": 1, "3개월": 3, "6개월": 6, "1년": 12}[key])


def _color_scale_zero(val, cap):
    """0 기준 대칭 배경색 CSS 반환: 양수=초록, 음수=빨강, 0=무채색(무색).
    |val|/cap 비율로 진하기를 정하고, cap을 넘는 값은 최대 진하기로 클립."""
    try:
        v = float(val)
    except (TypeError, ValueError):
        return ""
    if pd.isna(v) or cap <= 0 or v == 0:
        return ""
    intensity = min(abs(v) / cap, 1.0)
    if v > 0:
        r = int(255 + (26  - 255) * intensity)
        g = int(255 + (152 - 255) * intensity)
        b = int(255 + (80  - 255) * intensity)
    else:
        r = int(255 + (215 - 255) * intensity)
        g = int(255 + (25  - 255) * intensity)
        b = int(255 + (28  - 255) * intensity)
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    text = " color: #ffffff;" if luminance < 140 else ""
    return f"background-color: rgb({r},{g},{b});{text}"


def _abs_cap(values, hard_cap):
    """values의 절대값 최대치를 정규화 상한으로 쓰되, hard_cap을 넘지 않게 클립.
    한 종목의 극단값 때문에 나머지 색이 전부 옅어지는 것을 방지."""
    m = pd.Series(values, dtype="float64").abs().max()
    if pd.isna(m) or m <= 0:
        return hard_cap
    return min(float(m), hard_cap)


def _apply_bg(styler, fn, subset=None):
    try:
        return styler.map(fn, subset=subset)
    except AttributeError:
        return styler.applymap(fn, subset=subset)


# ============================================================================
#  동적자산배분 엔진
# ============================================================================
@st.cache_data(ttl=60 * 60 * 4)
def load_monthly_panel():
    def _fetch(tk):
        df = get_price(tk, "1990-01-01")
        if df.empty:
            return tk, None
        col = "Adj Close" if "Adj Close" in df.columns else "Close"
        return tk, df[col].resample("ME").last()

    with ThreadPoolExecutor(max_workers=10) as exe:
        results = list(exe.map(_fetch, TAA_TICKERS))

    cols = {tk: s for tk, s in results if s is not None}
    if not cols:
        return pd.DataFrame()
    df = pd.DataFrame(cols).dropna(how="all")
    return augment_panel(df)


@st.cache_data(ttl=60 * 60 * 4)
def build_ctx(index_list):
    idx = pd.DatetimeIndex(index_list)
    ue_ok = True
    try:
        ue = fdr.DataReader("FRED:UNRATE").iloc[:, 0].dropna().resample("ME").last()
        ue_bear = (ue > ue.rolling(12).mean()).shift(1).fillna(False)
        ue_up12 = (ue > ue.shift(12)).shift(1).fillna(False)
    except Exception:
        ue_ok = False
        ue_bear = pd.Series(dtype=bool)
        ue_up12 = pd.Series(dtype=bool)
    spy = get_price("SPY", "1990-01-01")
    spy_bear = pd.Series(dtype=bool)
    if not spy.empty:
        c = spy["Close"]
        spy_bear = (c < c.rolling(200).mean()).fillna(False)
    gt, up = {}, {}
    for d in idx:
        sb = bool(spy_bear.asof(d)) if len(spy_bear) else False
        ub = bool(ue_bear.asof(d)) if (ue_ok and len(ue_bear)) else None
        gt[d] = (sb and ub) if ub is not None else sb
        up[d] = bool(ue_up12.asof(d)) if (ue_ok and len(ue_up12)) else False
    return {"gt_bear": pd.Series(gt, index=idx), "ue_up12": pd.Series(up, index=idx), "ue_ok": ue_ok}


def _ret(s, t, k):
    if t - k < 0:
        return None
    a, b = s.iloc[t], s.iloc[t - k]
    return None if (pd.isna(a) or pd.isna(b) or b == 0) else a / b - 1.0


def backtest(fn, mp, ctx):
    n = len(mp)
    rets, dates = [], []
    for t in range(n - 1):
        w = fn(mp, t, ctx)
        if not w:
            continue
        nxt = 0.0
        for sym, wt in w.items():
            r = _ret(mp[sym], t + 1, 1) if sym in mp.columns else None
            if r is not None:
                nxt += wt * r
        rets.append(nxt)
        dates.append(mp.index[t + 1])
    if not rets:
        return None
    mr = pd.Series(rets, index=dates)
    equity = (1 + mr).cumprod()
    yrs = len(mr) / 12
    cagr = equity.iloc[-1] ** (1 / yrs) - 1 if yrs > 0 else np.nan
    mdd = (equity / equity.cummax() - 1).min()
    sharpe = (mr.mean() * 12) / (mr.std() * np.sqrt(12)) if mr.std() > 0 else np.nan
    recent_positions = []
    for i in range(3):
        idx = n - 1 - i
        if idx < 0:
            break
        recent_positions.append((mp.index[idx], fn(mp, idx, ctx) or {}))
    return {"equity": equity, "mret": mr, "cagr": cagr, "mdd": mdd,
            "sharpe": sharpe, "current": fn(mp, n - 1, ctx) or {},
            "recent_positions": recent_positions}


# 결과 딕셔너리 구조(backtest()의 반환 키)가 바뀌면 이 값을 올려서
# st.cache_data에 남아있는 구버전 캐시를 무효화한다.
TAA_RESULTS_VERSION = 3


@st.cache_data(ttl=60 * 60 * 4)
def run_all_strategies(_version=TAA_RESULTS_VERSION):
    mp = load_monthly_panel()
    if mp.empty:
        return {}, pd.DataFrame(), True
    ctx = build_ctx(list(mp.index))
    res = {}
    for name, fn in STRATEGIES.items():
        try:
            res[name] = backtest(fn, mp, ctx)
        except Exception:
            res[name] = None  # 전략 하나가 실패해도 나머지는 계속 계산
    return res, mp, ctx["ue_ok"]


# ============================================================================
#  프리미엄 데이터 함수
# ============================================================================

@st.cache_data(ttl=120)
def _upbit_btc_now():
    r = requests.get(
        "https://api.upbit.com/v1/ticker",
        params={"markets": "KRW-BTC"}, timeout=5,
    )
    r.raise_for_status()
    return float(r.json()[0]["trade_price"])


@st.cache_data(ttl=120)
def _coinbase_btc_now():
    r = requests.get(
        "https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=5,
    )
    r.raise_for_status()
    return float(r.json()["data"]["amount"])


@st.cache_data(ttl=300)
def _upbit_btc_history():
    import time as _time
    all_candles, to_str, needed = [], None, 1095
    while needed > 0:
        params = {"market": "KRW-BTC", "count": min(needed, 200)}
        if to_str:
            params["to"] = to_str
        resp = requests.get(
            "https://api.upbit.com/v1/candles/days",
            params=params, timeout=10,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        all_candles.extend(batch)
        to_str = batch[-1]["candle_date_time_utc"]
        needed -= len(batch)
        if len(batch) < 200:
            break
        _time.sleep(0.12)
    if not all_candles:
        return pd.DataFrame()
    raw = pd.DataFrame(all_candles)
    raw["date"] = pd.to_datetime(raw["candle_date_time_kst"].str[:10])
    result = (raw.set_index("date")[["trade_price"]]
                 .rename(columns={"trade_price": "KRW"})
                 .sort_index())
    return result[~result.index.duplicated(keep="last")]


def _yf_close(ticker, period="3y"):
    df = yf.download(ticker, period=period, auto_adjust=True, progress=False)
    if df.empty:
        return pd.DataFrame()
    s = df["Close"]
    if isinstance(s, pd.DataFrame):
        s = s.iloc[:, 0]
    if s.index.tz is not None:
        s.index = s.index.tz_convert(None)
    s.name = "Close"  # normalize: yfinance 1.x uses ticker name as column
    return s.to_frame()


@st.cache_data(ttl=3600)
def _btcusd_history():
    df = _yf_close("BTC-USD", "3y")
    return df.rename(columns={"Close": "USD"}) if not df.empty else df


@st.cache_data(ttl=3600)
def _usdkrw_history():
    start = (datetime.today() - timedelta(days=1095)).strftime("%Y-%m-%d")
    try:
        df = fdr.DataReader("USD/KRW", start)
        if not df.empty:
            return df[["Close"]].rename(columns={"Close": "USDKRW"})
    except Exception:
        pass
    df = _yf_close("USDKRW=X", "3y")
    return df.rename(columns={"Close": "USDKRW"}) if not df.empty else df


@st.cache_data(ttl=3600)
def _gld_history():
    df = _yf_close("GLD", "3y")
    return df.rename(columns={"Close": "GLD"}) if not df.empty else df


@st.cache_data(ttl=3600)
def _ace_gold_history():
    start = (datetime.today() - timedelta(days=1095)).strftime("%Y-%m-%d")
    df = fdr.DataReader("411060", start)
    if df.empty:
        return pd.DataFrame()
    return df[["Close"]].rename(columns={"Close": "ACE"})


# ============================================================================
#  매수 탭 · 계절성 (scanner/seasonality_scan.py 결과 파일 읽기 전용)
# ============================================================================
@st.cache_data(ttl=60 * 30)
def load_seasonality():
    if not (os.path.exists(SEASONALITY_PARQUET) and os.path.exists(SEASONALITY_META)):
        return pd.DataFrame(), {}
    try:
        df = pd.read_parquet(SEASONALITY_PARQUET)
        with open(SEASONALITY_META, encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:
        return pd.DataFrame(), {}
    return df, meta


def _nearest_occurrence(month, tdom, today_ts):
    """(월, 월중n번째영업일)이 today_ts에 가장 가까이 실현되는 실제 날짜를 근사
    (공휴일은 무시하고 주말만 제외 — 매수창이 ±수일 폭이라 오차는 허용 범위)."""
    candidates = []
    for y in (today_ts.year - 1, today_ts.year, today_ts.year + 1):
        bdays = pd.bdate_range(f"{y}-{int(month):02d}-01", periods=40)
        bdays = bdays[bdays.month == int(month)]
        if len(bdays) >= tdom:
            candidates.append(bdays[int(tdom) - 1])
    if not candidates:
        return pd.NaT
    return min(candidates, key=lambda d: abs((d - today_ts).days))


def _market_cap_bucket_mask(series, bucket, buckets=MARKET_CAP_BUCKETS):
    floor = buckets[bucket]
    if floor <= 0:
        return pd.Series(True, index=series.index)
    return series >= floor


# ============================================================================
#  매수 탭 · 추세추종 (scanner/trend_scan.py 결과 파일 읽기 전용)
# ============================================================================
@st.cache_data(ttl=60 * 30)
def load_trend():
    if not (os.path.exists(TREND_PARQUET) and os.path.exists(TREND_META)):
        return pd.DataFrame(), {}
    try:
        df = pd.read_parquet(TREND_PARQUET)
        with open(TREND_META, encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:
        return pd.DataFrame(), {}
    return df, meta


# ============================================================================
#  매수 탭 · 매매계획 (순수 계산기, 스캐너 불필요)
# ============================================================================
def _position_sizing_plan(price, asset, max_loss_pct, stop_pct, tp_mult):
    """매수가 P, 총자산 A, 최대손실률 L, 손절폭 S, 익절배수 M으로 포지션 사이징 계산.
    손절가=P*(1-S), 주수=floor(A*L / (P-손절가)), 익절1차=P*(1+S*M)."""
    stop_price = price * (1 - stop_pct)
    risk_per_share = price - stop_price
    if risk_per_share <= 0:
        return None
    max_loss_amount = asset * max_loss_pct
    shares = int(max_loss_amount // risk_per_share)
    buy_amount = price * shares
    worst_loss = risk_per_share * shares
    worst_loss_pct = (worst_loss / asset) if asset else 0.0
    tp1_price = price * (1 + stop_pct * tp_mult)
    return {
        "stop_price": stop_price, "shares": shares, "buy_amount": buy_amount,
        "worst_loss": worst_loss, "worst_loss_pct": worst_loss_pct, "tp1_price": tp1_price,
    }


def _krw_abbrev(x):
    """1,412만원 스타일 축약 표기 (원 단위 미만은 버림)."""
    x = int(round(x))
    eok, rem = divmod(x, 100_000_000)
    man = rem // 10_000
    parts = []
    if eok:
        parts.append(f"{eok}억")
    if man:
        parts.append(f"{man:,}만")
    if not parts:
        parts.append("0")
    return "".join(parts) + "원"


# ============================================================================
_names_raw, _stock_options = get_krx_listings()
# 지수 항목을 names dict에 병합 (캐시 결과를 직접 변경하지 않기 위해 복사)
names = dict(_names_raw)
for _idx_name, _idx_sym in DEFAULT_INDICES.items():
    names.setdefault(_idx_sym, _idx_name)
# 검색 옵션: 지수를 앞에 배치 후 종목·ETF 목록 이어붙임
_index_options = sorted([f"{n} ({s})" for n, s in DEFAULT_INDICES.items()])
search_options = _index_options + _stock_options

if "watchlist" not in st.session_state:
    st.session_state.watchlist, st.session_state.storage_mode = load_watchlist()
if "chart_ticker" not in st.session_state:
    st.session_state.chart_ticker = (
        st.session_state.watchlist[0] if st.session_state.watchlist else "005930"
    )
if "taa_results" not in st.session_state:
    st.session_state.taa_results = None
# 차트 클릭 측정용
if "click_pts" not in st.session_state:
    st.session_state.click_pts = []       # [(date_str, close_price), ...]

st.title("📈 나의 투자 대시보드")
st.caption("차트 · 관심목록 · 동적자산배분 — 탭 전환. 데이터: FinanceDataReader")
if st.session_state.storage_mode == "local":
    st.caption("💾 로컬 저장 모드 — Gist 미설정 또는 연결 실패로, 이 기기에만 저장됩니다.")

watchlist = st.session_state.watchlist

tab1, tab2, tab3, tab4, tab5 = st.tabs(
    ["📊 차트", "⭐ 관심목록", "⚖️ 동적자산배분", "💰 프리미엄", "🛒 매수"])

# =====================  차트  ==============================================
with tab1:
    # 검색 UI: 자동완성 드롭다운(선택 즉시 조회) + 직접 입력 폼(Enter 조회)
    sc1, sc2 = st.columns([3, 2])
    with sc1:
        sel_option = st.selectbox(
            "검색",
            options=search_options,
            index=None,
            placeholder="종목·지수 선택 (한글명·코드 검색)",
            label_visibility="collapsed",
            key="krx_select",
        )
        if sel_option is not None:
            _sel_code = normalize(sel_option.rsplit("(", 1)[-1].rstrip(")"))
            if _sel_code != st.session_state.chart_ticker:
                st.session_state.chart_ticker = _sel_code
                st.session_state.click_pts = []
                st.rerun()
    with sc2:
        with st.form("_fs", clear_on_submit=True, border=False):
            _fc, _bc = st.columns([4, 1])
            _ft = _fc.text_input(
                "입력",
                placeholder="종목명·티커·지수 검색",
                label_visibility="collapsed",
            )
            _sub = _bc.form_submit_button("조회", use_container_width=True)
        if _sub and _ft.strip():
            st.session_state.chart_ticker = normalize(_ft.strip())
            st.session_state.click_pts = []
            st.session_state.pop("krx_select", None)
            st.rerun()

    if watchlist:
        chips = st.columns(min(len(watchlist), 8))
        for i, c in enumerate(watchlist[:8]):
            if chips[i].button(name_of(c, names), key=f"chip_{c}", use_container_width=True):
                st.session_state.chart_ticker = c
                st.session_state.click_pts = []
                st.session_state.pop("krx_select", None)
                st.rerun()

    ticker = normalize(st.session_state.chart_ticker)
    full = get_price(ticker, "2010-01-01")

    o1, o2, o3, o4 = st.columns([2, 1, 2, 1])
    with o1:
        period = st.radio("기간", list(PERIOD_DAYS), index=3,
                          horizontal=True, label_visibility="collapsed")
    with o2:
        logscale = st.toggle("로그", value=False)
    with o3:
        sma_sel = st.multiselect("SMA", SMA_PERIODS, default=[20, 50, 200])
    with o4:
        ema_sel = st.multiselect("EMA", EMA_PERIODS, default=[])

    if full.empty:
        st.warning(f"'{ticker}' 데이터를 불러오지 못했습니다.")
    else:
        last = float(full["Close"].iloc[-1])
        prev = float(full["Close"].iloc[-2]) if len(full) > 1 else last
        chg = last - prev
        _hcol, _scol = st.columns([10, 1])
        _hcol.markdown(
            f"### {name_of(ticker, names)}  ·  {last:,.2f}  "
            f"<span style='color:{'#e03131' if chg>=0 else '#1971c2'}'>"
            f"{chg:+,.2f} ({chg/prev:+.2%})</span>",
            unsafe_allow_html=True,
        )
        _in_wl = ticker in st.session_state.watchlist
        if _scol.button(
            "⭐" if _in_wl else "☆",
            key="star_btn",
            help="관심목록에서 제거" if _in_wl else "관심목록에 추가",
            use_container_width=True,
        ):
            if _in_wl:
                st.session_state.watchlist.remove(ticker)
            else:
                st.session_state.watchlist.append(ticker)
            save_watchlist(st.session_state.watchlist, st.session_state.storage_mode)
            st.rerun()

        df = full[~full.index.duplicated(keep="last")].sort_index()
        ohlc_ok = df[["Open", "High", "Low", "Close"]].notna().all(axis=1)
        df_ohlc = df[ohlc_ok]

        # ticker/period가 바뀌면 클릭 상태 초기화
        _tkey = f"{ticker}_{period}"
        if st.session_state.get("_chart_tkey") != _tkey:
            st.session_state.click_pts = []
            st.session_state["_click_ts_seen"] = None
            st.session_state["_chart_tkey"] = _tkey

        candle_data = [
            {"time": t.strftime("%Y-%m-%d"),
             "open":  round(float(o), 6), "high": round(float(h), 6),
             "low":   round(float(l), 6), "close": round(float(c), 6)}
            for t, o, h, l, c in zip(
                df_ohlc.index,
                df_ohlc["Open"], df_ohlc["High"], df_ohlc["Low"], df_ohlc["Close"],
            )
        ]
        has_vol = "Volume" in df.columns and df["Volume"].notna().any()

        # 클릭 마커 (세션 상태 기반 → 이전 rerun에서 저장된 값)
        _pts_now = st.session_state.click_pts
        _markers = []
        for _i, (_d, _) in enumerate(_pts_now):
            _markers.append({
                "time": _d,
                "position": "aboveBar",
                "color": "#1971c2" if _i == 0 else "#e03131",
                "shape": "arrowDown",
                "text": "①" if _i == 0 else "②",
                "size": 2,
            })

        series_list = [{
            "type": "Candlestick",
            "data": candle_data,
            "options": {
                "upColor": "#26a69a", "downColor": "#ef5350",
                "borderVisible": False,
                "wickUpColor": "#26a69a", "wickDownColor": "#ef5350",
            },
            **({"markers": _markers} if _markers else {}),
        }]

        for p in sma_sel:
            vals = df["Close"].rolling(p).mean().dropna()
            series_list.append({
                "type": "Line",
                "data": [{"time": t.strftime("%Y-%m-%d"), "value": round(float(v), 6)}
                         for t, v in zip(vals.index, vals.values)],
                "options": {
                    "color": MA_COLORS.get(p, "#999999"),
                    "lineWidth": 1, "title": f"SMA{p}",
                    "priceLineVisible": False, "lastValueVisible": False,
                    "crosshairMarkerVisible": False,
                },
            })

        for p in ema_sel:
            vals = df["Close"].ewm(span=p, adjust=False).mean().dropna()
            series_list.append({
                "type": "Line",
                "data": [{"time": t.strftime("%Y-%m-%d"), "value": round(float(v), 6)}
                         for t, v in zip(vals.index, vals.values)],
                "options": {
                    "color": MA_COLORS.get(p, "#e377c2"),
                    "lineWidth": 1, "lineStyle": 1, "title": f"EMA{p}",
                    "priceLineVisible": False, "lastValueVisible": False,
                    "crosshairMarkerVisible": False,
                },
            })

        if has_vol:
            vol_ok = df["Volume"].notna() & ohlc_ok
            df_vol = df[vol_ok]
            vol_data = [
                {"time": t.strftime("%Y-%m-%d"),
                 "value": float(v),
                 "color": "#26a69a" if c >= o else "#ef5350"}
                for t, v, c, o in zip(
                    df_vol.index,
                    df_vol["Volume"], df_vol["Close"], df_vol["Open"],
                )
            ]
            series_list.append({
                "type": "Histogram",
                "data": vol_data,
                "options": {"priceFormat": {"type": "volume"}, "priceScaleId": ""},
                "priceScale": {"scaleMargins": {"top": 0.75, "bottom": 0}},
            })

        chart_opts = {
            "height": 480,
            "layout": {
                "background": {"type": "solid", "color": "#FFFFFF"},
                "textColor": "#333333",
            },
            "rightPriceScale": {
                "scaleMargins": {"top": 0.05, "bottom": 0.25 if has_vol else 0.05},
                "mode": 1 if logscale else 0,
                "borderColor": "rgba(197,203,206,0.5)",
            },
            "overlayPriceScales": {"scaleMargins": {"top": 0.75, "bottom": 0}},
            "timeScale": {
                "borderColor": "rgba(197,203,206,0.5)",
                "barSpacing": PERIOD_BSPACING.get(period, 4),
                "rightOffset": 5,
            },
            "grid": {
                "vertLines": {"color": "rgba(197,203,206,0.2)"},
                "horzLines": {"color": "rgba(197,203,206,0.3)"},
            },
            "crosshair": {"mode": 1},
        }

        # renderLightweightCharts returns {time, prices} on click, else None
        _click = renderLightweightCharts(
            [{"chart": chart_opts, "series": series_list}],
            key=f"lwc_{ticker}",
        )

        # 클릭 이벤트 처리 (중복 방지 가드: _click_ts_seen)
        if _click and isinstance(_click, dict):
            if _click.get("dblclick"):
                # 더블클릭 → 측정 초기화 (줌 상태는 JS측이 보존)
                if st.session_state.get("_click_ts_seen") != "dblclick":
                    st.session_state["_click_ts_seen"] = "dblclick"
                    st.session_state.click_pts = []
                    st.rerun()
            else:
                _ct = _click.get("time")
                if _ct and _ct != st.session_state.get("_click_ts_seen"):
                    st.session_state["_click_ts_seen"] = _ct
                    try:
                        _ts_pd = pd.Timestamp(_ct)
                        _avail = df.index[df.index <= _ts_pd]
                        if not _avail.empty:
                            _actual_d = _avail[-1].strftime("%Y-%m-%d")
                            _price    = float(df.loc[_avail[-1], "Close"])
                            _cur_pts  = st.session_state.click_pts
                            if len(_cur_pts) >= 2:
                                st.session_state.click_pts = [(_actual_d, _price)]
                            else:
                                st.session_state.click_pts = _cur_pts + [(_actual_d, _price)]
                            st.rerun()
                    except Exception:
                        pass

        # 클릭 측정 결과 표시
        _pts = st.session_state.click_pts
        if len(_pts) == 1:
            _d0, _p0 = _pts[0]
            st.caption(
                f"📍 시작점: **{_d0}**  종가 `{_p0:,.2f}` — "
                "두 번째 지점을 클릭하세요.  (더블클릭으로 초기화)"
            )
        elif len(_pts) == 2:
            _d0, _p0 = _pts[0]
            _d1, _p1 = _pts[1]
            _diff = _p1 - _p0
            _pct  = _diff / _p0 if _p0 != 0 else 0.0
            _up   = _diff >= 0
            _col  = "#2ca02c" if _up else "#e03131"
            _arr  = "▲" if _up else "▼"
            st.markdown(
                f"📍 **{_d0}** `{_p0:,.2f}` → **{_d1}** `{_p1:,.2f}`  "
                f"<span style='color:{_col};font-size:1.05em;font-weight:bold'>"
                f"{_arr} {abs(_diff):,.2f} &nbsp;({_pct:+.2%})</span>",
                unsafe_allow_html=True,
            )
            st.caption("더블클릭으로 초기화됩니다.")

        csv_cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
        csv = df[csv_cols].to_csv().encode("utf-8-sig")
        st.download_button("⬇️ CSV 다운로드 (날짜·OHLCV)", csv,
                           file_name=f"{ticker}_full.csv", mime="text/csv")

# =====================  관심목록 (순위)  ===================================
with tab2:
    wc1, wc2 = st.columns([1, 2])
    with wc1:
        mode = st.radio("모드", ["내 관심목록", "디폴트(글로벌 지수)"],
                        horizontal=True, label_visibility="collapsed")
    with wc2:
        ret_period = st.radio("기간", RETURN_PERIODS, index=4,
                              horizontal=True, label_visibility="collapsed")

    # 내 관심목록 모드: 종목별 ✕ 제거 버튼
    if mode == "내 관심목록" and st.session_state.watchlist:
        _wl = st.session_state.watchlist
        _rm_cols = st.columns(min(len(_wl), 8))
        for _i, _wc in enumerate(_wl[:8]):
            if _rm_cols[_i].button(
                f"{name_of(_wc, names)} ✕",
                key=f"wl_rm_{_wc}",
                use_container_width=True,
            ):
                st.session_state.watchlist.remove(_wc)
                save_watchlist(st.session_state.watchlist, st.session_state.storage_mode)
                st.rerun()

    universe = ([(name_of(c, names), c) for c in st.session_state.watchlist]
                if mode == "내 관심목록" else list(DEFAULT_INDICES.items()))
    start_hist = (datetime.today() - timedelta(days=420)).strftime("%Y-%m-%d")

    def _fetch_row(args):
        disp, sym = args
        df = get_price(sym, start_hist)
        if df.empty:
            return None
        return {
            "종목": f"{disp} ({sym})",
            "현재가": float(df["Close"].iloc[-1]),
            ret_period: period_return(df, ret_period),
        }

    with st.spinner("시세 조회 중..."):
        with ThreadPoolExecutor(max_workers=12) as exe:
            row_results = list(exe.map(_fetch_row, universe))

    rows = [r for r in row_results if r is not None]

    if rows:
        rank_df = (pd.DataFrame(rows)
                   .sort_values(ret_period, ascending=False, na_position="last")
                   .reset_index(drop=True))
        rank_df.insert(0, "순위", rank_df.index + 1)
        styled_rank = rank_df.style.format(
            {"현재가": "{:,.2f}", ret_period: "{:+.2%}"},
            na_rep="—",
        )
        color_cols = [ret_period] if ret_period in rank_df.columns else []
        for _col in color_cols:
            _cap = _abs_cap(rank_df[_col], RET_COLOR_CAP)
            styled_rank = _apply_bg(
                styled_rank,
                lambda v, cap=_cap: _color_scale_zero(v, cap),
                subset=[_col],
            )
        st.dataframe(styled_rank, use_container_width=True, hide_index=True,
                     height=min(60 + 35 * len(rank_df), 520))
    else:
        st.info("표시할 데이터가 없습니다.")

# =====================  동적자산배분  ======================================
with tab3:
    st.caption("모멘텀 기반 월별 리밸런싱 · 백테스트는 사용 ETF가 모두 상장된 이후 구간만 계산됩니다.")

    # [1] 버튼 없이 자동 계산. 미계산 상태면 spinner 표시 후 실행 (캐시 있으면 즉시)
    if st.session_state.taa_results is None:
        with st.spinner("미국 ETF 25종 병렬 로딩 및 전략 계산 중 (최초 1~2분)..."):
            st.session_state.taa_results = run_all_strategies()

    results, mp, ue_ok = st.session_state.taa_results

    if not results:
        st.warning("전략 데이터를 불러오지 못했습니다. (미국 ETF 데이터 조회 실패)")
    else:
        if not ue_ok:
            st.caption("⚠️ FRED 실업률 미조회 → LAA/RAA는 시장신호만으로 계산되었습니다.")

        _KR_ETF_LABEL = {
            "069500": "KODEX200(069500)",
            "278530": "KODEX200TR(278530)",
            "363580": "KODEX200IT TR(363580)",
            "114260": "국고채3년(114260)",
            "148070": "국고채10년(148070)",
            "439870": "국고채30년(439870)",
            "US_BOND_SHORT_KRW": "미국단기국채환노출(SHY합성)",
            "US_BOND_MID_KRW": "미국10년국채환노출(IEF합성)",
            "US_BOND_LONG_KRW": "미국장기국채환노출(TLT합성)",
            "CASH": "현금",
        }

        def pos_str(w):
            return " / ".join(
                f"{_KR_ETF_LABEL.get(k, k)} {v*100:.0f}%"
                for k, v in sorted(w.items(), key=lambda x: -x[1])
            )

        def _prev_pos_str(r):
            rp = r.get("recent_positions") or []
            return pos_str(rp[1][1]) if len(rp) > 1 else "—"

        table, failed_strats = [], []
        for nm, r in results.items():
            if not r:
                failed_strats.append(nm)
                continue
            table.append({
                "전략명": nm, "현재 포지션": pos_str(r.get("current") or {}),
                "직전월 포지션": _prev_pos_str(r),
                "CAGR": r.get("cagr"), "MDD": r.get("mdd"), "Sharpe": r.get("sharpe"),
            })

        if failed_strats:
            st.caption(f"⚠️ 계산 실패로 제외된 전략: {', '.join(failed_strats)}")

        if not table:
            st.warning("표시할 전략이 없습니다.")
        else:
            tdf = (pd.DataFrame(table)
                   .sort_values("CAGR", ascending=False)
                   .reset_index(drop=True))
            tdf.insert(0, "순위", tdf.index + 1)

            sel = st.dataframe(
                tdf.style.format({"CAGR": "{:+.1%}", "MDD": "{:.1%}", "Sharpe": "{:.2f}"},
                                 na_rep="—"),
                use_container_width=True, hide_index=True,
                height=min(60 + 35 * len(tdf), 600),
                on_select="rerun", selection_mode="single-row", key="strat_table",
            )
            sel_rows = sel.selection.rows if sel and hasattr(sel, "selection") else []
            pick_idx = sel_rows[0] if sel_rows else 0
            pick = tdf.iloc[pick_idx]["전략명"]

            r = results.get(pick)
            if not r:
                st.warning(f"'{pick}' 전략 데이터를 불러오지 못했습니다.")
            else:
                try:
                    st.markdown(f"#### 전략 상세 — {pick}")
                    m1, m2, m3, m4 = st.columns(4)
                    m1.metric("CAGR", f"{r['cagr']:+.1%}")
                    m2.metric("MDD",  f"{r['mdd']:.1%}")
                    m3.metric("Sharpe", f"{r['sharpe']:.2f}")
                    m4.metric("현재 포지션", pos_str(r.get("current") or {}))

                    recent_positions = r.get("recent_positions") or []
                    recent_rows = [
                        {"기준월": d.strftime("%Y-%m"), "포지션": pos_str(w)}
                        for d, w in recent_positions
                    ]
                    st.markdown("##### 최근 3개월 포지션")
                    if recent_rows:
                        st.dataframe(pd.DataFrame(recent_rows), use_container_width=True, hide_index=True)
                    else:
                        st.caption("표시할 포지션 이력이 없습니다.")

                    spy_eq = (1 + mp["SPY"].pct_change().reindex(r["equity"].index).fillna(0)).cumprod()
                    cfig = go.Figure()
                    cfig.add_trace(go.Scatter(x=r["equity"].index, y=r["equity"],
                                              name="내 전략", line=dict(color="#e03131")))
                    cfig.add_trace(go.Scatter(x=spy_eq.index, y=spy_eq,
                                              name="SPY", line=dict(color="#1971c2", dash="dot")))
                    cfig.update_layout(
                        height=340, margin=dict(l=0, r=0, t=10, b=0), yaxis_type="log",
                        legend=dict(orientation="h", y=1.02, x=0),
                        title="누적 수익률 (로그 스케일)",
                    )
                    cfig.update_xaxes(fixedrange=True)
                    cfig.update_yaxes(fixedrange=True)
                    st.plotly_chart(cfig, use_container_width=True,
                                    config={"scrollZoom": False, "displayModeBar": False})

                    mr = r["mret"]
                    heat = mr.groupby([mr.index.year, mr.index.month]).first().unstack() * 100
                    heat.columns = [f"{m}월" for m in heat.columns]
                    annual = (
                        mr.groupby(mr.index.year)
                          .apply(lambda x: (1 + x).prod() - 1) * 100
                    ).rename("연간")
                    heat_full = heat.join(annual)
                    st.markdown("##### 연월별 수익률 (%)")
                    fn_h = lambda v: _color_scale_zero(v, 10)
                    styled_heat = _apply_bg(heat_full.style.format("{:+.1f}", na_rep="—"), fn_h)
                    st.markdown(
                        f'<div style="overflow-x:auto;font-size:0.85rem">'
                        f'{styled_heat.to_html()}'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
                except Exception as _de:
                    st.error(f"'{pick}' 전략 상세 표시 중 오류가 발생했습니다: {_de}")

# =====================  프리미엄  ==========================================
with tab4:
    # ── 섹션 1: 김치프리미엄 ────────────────────────────────────────────
    st.subheader("🌶️ 김치프리미엄")
    st.caption("업비트 BTC/KRW vs 코인베이스 BTC/USD × 원달러 환율")
    kp_period = st.radio("기간", ["1년", "2년", "전체"], index=0,
                         horizontal=True, key="kp_period")
    try:
        upbit_now   = _upbit_btc_now()
        cb_now      = _coinbase_btc_now()
        usdkrw_now_df = _usdkrw_history()
        usdkrw_now  = (float(usdkrw_now_df["USDKRW"].dropna().iloc[-1])
                       if not usdkrw_now_df.empty else None)
        if usdkrw_now:
            cb_krw_now = cb_now * usdkrw_now
            kimchi_now = (upbit_now / cb_krw_now - 1) * 100
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("김치프리미엄",         f"{kimchi_now:+.2f}%")
            k2.metric("업비트 BTC",           f"₩{upbit_now:,.0f}")
            k3.metric("코인베이스 BTC (KRW)", f"₩{cb_krw_now:,.0f}")
            k4.metric("USD/KRW",              f"{usdkrw_now:,.1f}")

        upbit_hist  = _upbit_btc_history()
        btcusd_hist = _btcusd_history()
        usdkrw_hist = _usdkrw_history()
        if not upbit_hist.empty and not btcusd_hist.empty and not usdkrw_hist.empty:
            km = (upbit_hist
                  .join(btcusd_hist, how="inner")
                  .join(usdkrw_hist, how="inner")
                  .dropna())
            km["premium"] = (km["KRW"] / (km["USD"] * km["USDKRW"]) - 1) * 100
            pmin, pmax, pmean = km["premium"].min(), km["premium"].max(), km["premium"].mean()
            st.caption(
                f"과거 범위: 최저 {pmin:+.1f}% · 최고 {pmax:+.1f}% · 평균 {pmean:+.1f}%"
            )
            if kp_period == "1년":
                km = km[km.index >= km.index[-1] - pd.DateOffset(years=1)]
            elif kp_period == "2년":
                km = km[km.index >= km.index[-1] - pd.DateOffset(years=2)]
            kfig = go.Figure()
            kfig.add_trace(go.Scatter(
                x=km.index, y=km["premium"], mode="lines",
                line=dict(color="#e03131", width=1.5),
                fill="tozeroy", fillcolor="rgba(224,49,49,0.1)",
            ))
            kfig.add_hline(y=0, line_dash="dot", line_color="#888", line_width=1)
            kfig.update_layout(
                height=280, margin=dict(l=0, r=0, t=10, b=0),
                yaxis_ticksuffix="%", showlegend=False,
            )
            st.plotly_chart(kfig, use_container_width=True,
                            config={"displayModeBar": False})
            st.caption("출처: Upbit API · Coinbase API · FinanceDataReader (USD/KRW)")
        else:
            st.info("히스토리 데이터 조회 실패")
    except Exception as _ke:
        st.error(f"김치프리미엄 오류: {_ke}")

    st.divider()

    # ── 섹션 2: 금치프리미엄 ────────────────────────────────────────────
    st.subheader("🥇 금치프리미엄")
    st.caption("ACE KRX금현물 ETF(411060) vs GLD(SPDR Gold, 1주=2.876g) × 원달러 환율")
    gp_period = st.radio("기간", ["1년", "2년", "전체"], index=0,
                         horizontal=True, key="gp_period")
    try:
        ace_hist      = _ace_gold_history()
        gld_hist      = _gld_history()
        usdkrw_hist2  = _usdkrw_history()
        if not ace_hist.empty and not gld_hist.empty and not usdkrw_hist2.empty:
            GLD_G = 2.876  # 1 GLD share = 2.876 g
            gm = (ace_hist
                  .join(gld_hist, how="inner")
                  .join(usdkrw_hist2, how="inner")
                  .dropna())
            gm["intl_g"] = gm["GLD"] / GLD_G * gm["USDKRW"]
            calib = float((gm["ACE"] / gm["intl_g"]).median())
            gm["premium"] = (gm["ACE"] / calib / gm["intl_g"] - 1) * 100
            latest = gm.iloc[-1]
            gpmin, gpmax, gpmean = gm["premium"].min(), gm["premium"].max(), gm["premium"].mean()
            g1, g2, g3, g4 = st.columns(4)
            g1.metric("금치프리미엄",       f"{float(latest['premium']):+.2f}%")
            g2.metric("ACE 국내금 (원/g)",  f"₩{float(latest['ACE']) / calib:,.0f}")
            g3.metric("GLD 국제금 (원/g)",  f"₩{float(latest['intl_g']):,.0f}")
            g4.metric("과거범위",           f"{gpmin:+.1f}% ~ {gpmax:+.1f}%")
            st.caption(
                f"과거 범위: 최저 {gpmin:+.1f}% · 최고 {gpmax:+.1f}% · 평균 {gpmean:+.1f}%"
            )
            if gp_period == "1년":
                gm = gm[gm.index >= gm.index[-1] - pd.DateOffset(years=1)]
            elif gp_period == "2년":
                gm = gm[gm.index >= gm.index[-1] - pd.DateOffset(years=2)]
            gfig = go.Figure()
            gfig.add_trace(go.Scatter(
                x=gm.index, y=gm["premium"], mode="lines",
                line=dict(color="#f4a261", width=1.5),
                fill="tozeroy", fillcolor="rgba(244,162,97,0.15)",
            ))
            gfig.add_hline(y=0, line_dash="dot", line_color="#888", line_width=1)
            gfig.update_layout(
                height=280, margin=dict(l=0, r=0, t=10, b=0),
                yaxis_ticksuffix="%", showlegend=False,
            )
            st.plotly_chart(gfig, use_container_width=True,
                            config={"displayModeBar": False})
            st.caption(
                "출처: FinanceDataReader (ACE KRX금현물 411060 · USD/KRW) · Yahoo Finance (GLD)"
            )
        else:
            st.info("금치프리미엄 히스토리 데이터 조회 실패")
    except Exception as _ge:
        st.error(f"금치프리미엄 오류: {_ge}")

# =====================  매수  ==============================================
with tab5:
    sub_season, sub_trend, sub_plan = st.tabs(["🌱 계절성", "📈 추세추종", "🗓 매매계획"])

    with sub_trend:
        tdf, tmeta = load_trend()
        if tdf.empty:
            st.warning(
                "추세추종 데이터가 없습니다. 로컬에서 `python scanner/trend_scan.py`를 "
                "먼저 실행해 `data/trend.parquet`를 생성해주세요."
            )
        else:
            as_of = pd.Timestamp(tmeta.get("as_of"))
            today_ts = pd.Timestamp(datetime.today().date())
            days_old = (today_ts - as_of).days
            stale = days_old > TREND_STALE_DAYS
            status = f"신호 기준일 {as_of:%Y-%m-%d} ({days_old}일 경과)"
            if stale:
                st.warning(f"⚠️ {status} — 갱신이 오래되어 재실행을 권장합니다.")
            else:
                st.caption(status)

            rep = tdf[tdf["is_representative"]].copy()

            f1, f2, f3, f4, f5 = st.columns(5)
            countries = ["전체"] + sorted(rep["country"].dropna().unique().tolist())
            f_country = f1.selectbox("국가", countries, key="trend_country")
            classes = ["전체"] + sorted(rep["asset_class"].dropna().unique().tolist())
            f_class = f2.selectbox("자산", classes, key="trend_class")
            signals = ["전체"] + sorted(rep["signal_type"].dropna().unique().tolist())
            f_signal = f3.selectbox("신호", signals, key="trend_signal")
            f_cap = f4.selectbox("시총", list(TREND_CAP_BUCKETS.keys()), key="trend_cap")
            sort_label = f5.selectbox("정렬", ["별 우선", "RS순", "승률순"], key="trend_sort")

            if f_country != "전체":
                rep = rep[rep["country"] == f_country]
            if f_class != "전체":
                rep = rep[rep["asset_class"] == f_class]
            if f_signal != "전체":
                rep = rep[rep["signal_type"] == f_signal]
            if f_cap != "전체":
                rep = rep[_market_cap_bucket_mask(rep["market_cap_usd"], f_cap, TREND_CAP_BUCKETS)]

            if sort_label == "별 우선":
                rep = rep.sort_values(["star_rating", "profit_factor"], ascending=[False, False])
            elif sort_label == "RS순":
                rep = rep.sort_values("rs_total", ascending=False)
            else:
                rep = rep.sort_values("win_rate", ascending=False)
            rep = rep.reset_index(drop=True)

            st.caption(f"{len(rep)}종목 · {sort_label}")

            if rep.empty:
                st.info("조건에 맞는 후보가 없습니다.")
            else:
                for i, r in rep.iterrows():
                    stars = "★" * int(r["star_rating"]) + "☆" * (3 - int(r["star_rating"]))
                    risk_badge = " 🔺위험형" if r["risk_flag"] else ""
                    label = (
                        f"{i + 1}. {stars} {r['name']}({r['ticker']}) · [{r['signal_type']}] "
                        f"{r['variant_label']} · {int(r['hold_days'])}일 보유{risk_badge}"
                    )
                    with st.expander(label):
                        b1, b2, b3 = st.columns(3)
                        sector_label = r["sector"] or "미분류"
                        b1.metric("종합 RS", int(r["rs_total"]) if pd.notna(r["rs_total"]) else "—")
                        b2.metric(f"{sector_label} RS",
                                  int(r["rs_sector"]) if pd.notna(r["rs_sector"]) else "—")
                        b3.metric("추세 구분", r["trend_term"])

                        c1, c2, c3 = st.columns(3)
                        c1.metric("매수구간", f"{r['buy_zone_low']:,.2f}~{r['buy_zone_high']:,.2f}")
                        c2.metric("현재가", f"{r['close_price']:,.2f}")
                        c3.metric("가격 위치", r["price_status"])

                        m1, m2, m3, m4, m5 = st.columns(5)
                        m1.metric("승률", f"{r['win_rate']:.0%}")
                        m2.metric("평균이익", f"{r['avg_win']:+.1%}")
                        m3.metric("평균손실", f"{r['avg_loss']:+.1%}")
                        m4.metric("손익비", f"{r['profit_factor']:.2f}")
                        m5.metric(r["sample_label"], f"{int(r['n_samples'])}")

                        st.markdown("##### 보유기간별 성적표 (★ = 대표 보유기간)")
                        sub = (tdf[(tdf["ticker"] == r["ticker"]) & (tdf["variant_key"] == r["variant_key"])]
                               .sort_values("hold_days"))
                        tbl = pd.DataFrame({
                            "보유일": sub["hold_days"].astype(int),
                            "승률": sub["win_rate"],
                            "평균이익": sub["avg_win"],
                            "평균손실": sub["avg_loss"],
                            "손익비": sub["profit_factor"],
                            r["sample_label"]: sub["n_samples"].astype(int),
                            "대표": sub["is_representative"].map(lambda v: "★" if v else ""),
                        })
                        st.dataframe(
                            tbl.style.format({"승률": "{:.0%}", "평균이익": "{:+.1%}",
                                              "평균손실": "{:+.1%}", "손익비": "{:.2f}"}),
                            use_container_width=True, hide_index=True,
                        )

    with sub_plan:
        if "plan_ticker" not in st.session_state:
            st.session_state.plan_ticker = st.session_state.chart_ticker

        pc1, pc2 = st.columns([3, 2])
        with pc1:
            _psel = st.selectbox(
                "종목 검색",
                options=search_options, index=None,
                placeholder="종목명·코드 검색 (한국 상장 종목/ETF)",
                label_visibility="collapsed", key="plan_krx_select",
            )
            if _psel is not None:
                _psel_code = normalize(_psel.rsplit("(", 1)[-1].rstrip(")"))
                if _psel_code != st.session_state.plan_ticker:
                    st.session_state.plan_ticker = _psel_code
                    st.session_state.pop("plan_computed", None)
                    st.rerun()
        with pc2:
            with st.form("_plan_fs", clear_on_submit=True, border=False):
                _pfc, _pbc = st.columns([4, 1])
                _pft = _pfc.text_input(
                    "직접 입력", placeholder="티커 직접 입력 (예: AAPL)",
                    label_visibility="collapsed",
                )
                _psub = _pbc.form_submit_button("조회", use_container_width=True)
            if _psub and _pft.strip():
                st.session_state.plan_ticker = normalize(_pft.strip())
                st.session_state.pop("plan_krx_select", None)
                st.session_state.pop("plan_computed", None)
                st.rerun()

        p_ticker = normalize(st.session_state.plan_ticker)
        p_is_krw = p_ticker.isdigit()
        p_start = (datetime.today() - timedelta(days=400)).strftime("%Y-%m-%d")
        p_df = get_price(p_ticker, p_start)

        if p_df.empty:
            st.warning(f"'{p_ticker}' 가격 데이터를 불러오지 못했습니다.")
        else:
            p_close = p_df["Close"].dropna()
            latest_price = float(p_close.iloc[-1])
            latest_date = p_close.index[-1]
            ma20 = float(p_close.iloc[-20:].mean()) if len(p_close) >= 20 else None
            cur_label = f"{name_of(p_ticker, names)} ({p_ticker})"

            st.markdown(f"**{cur_label}**")
            if p_is_krw:
                st.caption(f"현재가 {latest_price:,.0f}원 ({latest_date:%Y-%m-%d} 종가)")
            else:
                st.caption(f"현재가 ${latest_price:,.2f} ({latest_date:%Y-%m-%d} 종가)")

            buy_price = st.number_input(
                "매수가", min_value=0.0,
                value=round(latest_price, 0) if p_is_krw else round(latest_price, 2),
                step=(100.0 if p_is_krw else 0.5), key=f"plan_price_{p_ticker}",
            )

            mode = st.radio("방식", ["디폴트", "직접 입력"], horizontal=True, key="plan_mode")
            default_asset = 100_000_000.0 if p_is_krw else 100_000.0
            # 미국(달러) 종목은 원화 기본값(1억원)을 그대로 쓸 수 없어, 자산 기준통화도
            # 종목 통화에 맞춰 별도 기본값($100,000)을 둔다 — "직접 입력"에서 조정 가능.
            if mode == "직접 입력":
                q1, q2, q3, q4 = st.columns(4)
                asset = q1.number_input(
                    "투자자산", min_value=0.0, value=default_asset,
                    step=(1_000_000.0 if p_is_krw else 1_000.0), key="plan_asset")
                loss_pct = q2.number_input(
                    "최대손실(%)", min_value=0.1, value=1.0, step=0.1, key="plan_loss") / 100
                stop_pct = q3.number_input(
                    "손절폭(%)", min_value=0.1, value=7.0, step=0.5, key="plan_stop") / 100
                tp_mult = q4.number_input(
                    "익절배수", min_value=0.1, value=3.0, step=0.5, key="plan_mult")
            else:
                asset, loss_pct, stop_pct, tp_mult = default_asset, 0.01, 0.07, 3.0

            # 불타기(분할 추가매수): 원 사이트의 정확한 규칙이 확인되지 않아, 우선
            # 총 매수금액을 1차 60% / 2차 40%로 나누는 임시 규칙으로 구현했다.
            # 규칙이 확인되면 아래 pyramiding 분기만 교체하면 된다.
            pyramiding = st.radio("불타기", ["안 함", "함"], horizontal=True, key="plan_pyramid")

            if st.button("계획 보기", type="primary"):
                st.session_state.plan_computed = True

            if st.session_state.get("plan_computed"):
                plan = _position_sizing_plan(buy_price, asset, loss_pct, stop_pct, tp_mult)
                if plan is None or plan["shares"] <= 0:
                    st.warning("손절폭·매수가 조합상 매수 가능 주식 수가 0입니다. 파라미터를 확인해주세요.")
                else:
                    if p_is_krw:
                        header_price = f"{buy_price:,.0f}원"
                        amt_str = f"{plan['buy_amount']:,.0f}원 ({_krw_abbrev(plan['buy_amount'])})"
                        loss_str = f"{plan['worst_loss']:,.0f}원"
                        stop_str = f"{plan['stop_price']:,.0f}원"
                        tp1_str = f"{plan['tp1_price']:,.0f}원"
                        ma_str = f"{ma20:,.0f}원" if ma20 is not None else "—"
                    else:
                        header_price = f"${buy_price:,.2f}"
                        amt_str = f"${plan['buy_amount']:,.2f}"
                        loss_str = f"${plan['worst_loss']:,.2f}"
                        stop_str = f"${plan['stop_price']:,.2f}"
                        tp1_str = f"${plan['tp1_price']:,.2f}"
                        ma_str = f"${ma20:,.2f}" if ma20 is not None else "—"

                    st.markdown(f"##### {name_of(p_ticker, names)} · 매수가 {header_price} 기준 계획입니다")

                    st.markdown(f"**① 매수 {amt_str} · {plan['shares']}주**")
                    st.caption(f"최악의 경우 손실 {loss_str} (자산의 {plan['worst_loss_pct']:.1%})")

                    if pyramiding == "함":
                        shares_1 = int(plan["shares"] * 0.6)
                        shares_2 = plan["shares"] - shares_1
                        amt_1, amt_2 = buy_price * shares_1, buy_price * shares_2
                        if p_is_krw:
                            st.caption(
                                f"불타기(임시 규칙): 1차 {amt_1:,.0f}원({shares_1}주, 60%) · "
                                f"2차 {amt_2:,.0f}원({shares_2}주, 40%)")
                        else:
                            st.caption(
                                f"불타기(임시 규칙): 1차 ${amt_1:,.2f}({shares_1}주, 60%) · "
                                f"2차 ${amt_2:,.2f}({shares_2}주, 40%)")

                    st.markdown(f"**② 손절 (-{stop_pct:.0%}) {stop_str}**")
                    st.caption("여기 오면 무조건 전량 매도")

                    st.markdown(f"**③ 익절 1차 (+{stop_pct * tp_mult:.0%}) {tp1_str}**")
                    st.caption("여기 오면 절반 매도")

                    st.markdown("**④ 익절 2차: 20일 이평 이탈 시 나머지 전량 매도**")
                    st.caption(f"오늘 기준 20일 이평 {ma_str} · ⚠️ 이 값은 매일 변합니다")

                    st.caption("주식 수는 소수점을 버립니다. 세금·수수료는 반영하지 않은 숫자입니다.")

    with sub_season:
        sdf, smeta = load_seasonality()
        if sdf.empty:
            st.warning(
                "계절성 데이터가 없습니다. 로컬에서 `python scanner/seasonality_scan.py`를 "
                "먼저 실행해 `data/seasonality.parquet`를 생성해주세요."
            )
        else:
            as_of = pd.Timestamp(smeta.get("as_of"))
            today_ts = pd.Timestamp(datetime.today().date())
            days_old = (today_ts - as_of).days
            stale = days_old > SEASONALITY_STALE_DAYS
            win_thr = smeta.get("win_rate_threshold", 0.9)
            date_label = f"{as_of:%Y-%m-%d} ({days_old}일 경과)"
            status = (
                f"계절성 데이터 기준일 {date_label} · 월간 갱신 · "
                f"오늘 기준 매수창(-{BUY_WINDOW_BEFORE}~+{BUY_WINDOW_AFTER}일) · 승률≥{win_thr:.0%}"
            )
            if stale:
                st.warning(f"⚠️ {status} — 갱신이 오래되어 재실행을 권장합니다.")
            else:
                st.caption(status)

            work = sdf.copy()
            # (월, 월중n번째영업일) 조합 수는 최대 12*23=276개로 한정되므로
            # 조합별로 한 번만 계산해 매핑한다(행 단위 apply보다 훨씬 빠름).
            pairs = work[["month", "tdom"]].drop_duplicates()
            occ_map = {
                (m, d): _nearest_occurrence(m, d, today_ts)
                for m, d in pairs.itertuples(index=False)
            }
            work["entry_date"] = [occ_map[(m, d)] for m, d in zip(work["month"], work["tdom"])]
            work = work.dropna(subset=["entry_date"])
            window_start = work["entry_date"] - pd.Timedelta(days=BUY_WINDOW_BEFORE)
            window_end = work["entry_date"] + pd.Timedelta(days=BUY_WINDOW_AFTER)
            work = work[(today_ts >= window_start) & (today_ts <= window_end)].copy()
            work["exit_date"] = work.apply(
                lambda r: r["entry_date"] + pd.tseries.offsets.BDay(int(r["hold_days"])), axis=1)

            # 필터 옵션은 실제 존재하는 값만 구성 (데이터 없는 필터는 만들지 않음)
            f1, f2, f3, f4 = st.columns(4)
            countries = ["전체"] + sorted(work["country"].dropna().unique().tolist())
            f_country = f1.selectbox("국가", countries, key="season_country")
            classes = ["전체"] + sorted(work["asset_class"].dropna().unique().tolist())
            f_class = f2.selectbox("자산군", classes, key="season_class")
            f_cap = f3.selectbox("시총", list(MARKET_CAP_BUCKETS.keys()), key="season_cap")

            if f_country != "전체":
                work = work[work["country"] == f_country]
            if f_class != "전체":
                work = work[work["asset_class"] == f_class]
            if f_cap != "전체":
                work = work[_market_cap_bucket_mask(work["market_cap_usd"], f_cap)]

            # 섹터는 국가별로 서로 다른 원 체계를 그대로 쓴다(한국 KSIC 업종 / 미국 GICS).
            # "전체" 국가에서는 두 체계가 섞이므로 "[국가] 섹터명"으로 구분해 보여준다.
            sec_df = work.dropna(subset=["sector"])
            if f_country == "전체":
                sector_options = ["전체"] + sorted(
                    f"[{c}] {s}" for c, s in
                    sec_df[["country", "sector"]].drop_duplicates().itertuples(index=False))
            else:
                sector_options = ["전체"] + sorted(sec_df["sector"].dropna().unique().tolist())
            f_sector = f4.selectbox("섹터", sector_options, key="season_sector",
                                     disabled=(len(sector_options) == 1))
            if f_sector != "전체":
                if f_country == "전체":
                    sel_country, sel_sector = f_sector[1:].split("] ", 1)
                    work = work[(work["country"] == sel_country) & (work["sector"] == sel_sector)]
                else:
                    work = work[work["sector"] == f_sector]

            sort_label = st.selectbox(
                "정렬", ["승률 높은순", "평균수익률 높은순", "진입 임박순"], key="season_sort")
            if sort_label == "승률 높은순":
                work = work.sort_values("win_rate", ascending=False)
            elif sort_label == "평균수익률 높은순":
                work = work.sort_values("avg_return", ascending=False)
            else:
                work["_days_away"] = (work["entry_date"] - today_ts).abs()
                work = work.sort_values("_days_away")

            work = work.reset_index(drop=True)
            st.caption(f"{len(work)}종목 · {sort_label}")

            if work.empty:
                st.info("현재 매수창에 들어온 종목이 없습니다.")
            else:
                rows = []
                for i, r in work.iterrows():
                    rows.append({
                        "순위": i + 1,
                        "종목": f"{r['name']} ({r['ticker']})",
                        "국가": r["country"],
                        "섹터": r["sector"] or "미분류",
                        "매수 시기": (
                            f"{r['entry_date']:%m/%d}~{r['exit_date']:%m/%d} "
                            f"({int(r['hold_days'])}일 보유)"
                        ),
                        "승률": r["win_rate"],
                        "평균수익률": r["avg_return"],
                        "표본": f"{int(r['n_years'])}/{smeta.get('lookback_years', 10)}년",
                    })
                disp = pd.DataFrame(rows)
                st.dataframe(
                    disp.style.format({"승률": "{:.0%}", "평균수익률": "{:+.1%}"}),
                    use_container_width=True, hide_index=True,
                    height=min(60 + 35 * len(disp), 700),
                )

st.divider()
st.caption("※ 규칙 기반 계산기이며 투자 자문이 아닙니다. "
           "배당 미반영 종가 기반이라 실제 성과와 차이가 날 수 있습니다. "
           "투자 판단과 책임은 본인에게 있습니다.")
