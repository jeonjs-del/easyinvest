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

from strategies import STRATEGIES, TAA_TICKERS

st.set_page_config(page_title="나의 투자 대시보드", layout="wide")

WATCHLIST_FILE = "watchlist.json"
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
DEFAULT_INDICES = {
    "코스피": "KS11", "코스닥": "KQ11", "S&P500": "US500", "나스닥종합": "IXIC",
    "다우": "DJI", "러셀2000": "RUT", "닛케이225": "N225", "상해종합": "SSEC",
    "항셍": "HSI", "대만가권": "TWII", "독일DAX": "DAX", "프랑스CAC40": "CAC40",
}


def normalize(sym):
    s = sym.strip().upper()
    if s.endswith(".KS") or s.endswith(".KQ"):
        s = s[:-3]
    if s.isdigit():
        s = s.zfill(6)
    return s


def load_watchlist():
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                return [normalize(str(c)) for c in data]
        except Exception:
            pass
    return DEFAULT_WATCHLIST.copy()


def save_watchlist(codes):
    try:
        with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(codes, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # Cloud filesystem은 재시작 시 초기화 — 저장 실패는 무시


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


def _color_scale(val, vmin, vmax):
    """빨강→흰색→초록 배경색 CSS 반환."""
    try:
        v = float(val)
    except (TypeError, ValueError):
        return ""
    if pd.isna(v):
        return ""
    span = float(vmax) - float(vmin)
    t = 0.5 if span == 0 else max(0.0, min(1.0, (v - float(vmin)) / span))
    if t < 0.5:
        ratio = t * 2
        r = int(215 + (255 - 215) * ratio)
        g = int(25  + (255 - 25)  * ratio)
        b = int(28  + (255 - 28)  * ratio)
    else:
        ratio = (t - 0.5) * 2
        r = int(255 + (26  - 255) * ratio)
        g = int(255 + (152 - 255) * ratio)
        b = int(255 + (80  - 255) * ratio)
    return f"background-color: rgb({r},{g},{b})"


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
    return pd.DataFrame(cols).dropna(how="all") if cols else pd.DataFrame()


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
    return {"equity": equity, "mret": mr, "cagr": cagr, "mdd": mdd,
            "sharpe": sharpe, "current": fn(mp, n - 1, ctx) or {}}


@st.cache_data(ttl=60 * 60 * 4)
def run_all_strategies():
    mp = load_monthly_panel()
    if mp.empty:
        return {}, pd.DataFrame(), True
    ctx = build_ctx(list(mp.index))
    res = {name: backtest(fn, mp, ctx) for name, fn in STRATEGIES.items()}
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
_names_raw, _stock_options = get_krx_listings()
# 지수 항목을 names dict에 병합 (캐시 결과를 직접 변경하지 않기 위해 복사)
names = dict(_names_raw)
for _idx_name, _idx_sym in DEFAULT_INDICES.items():
    names.setdefault(_idx_sym, _idx_name)
# 검색 옵션: 지수를 앞에 배치 후 종목·ETF 목록 이어붙임
_index_options = sorted([f"{n} ({s})" for n, s in DEFAULT_INDICES.items()])
search_options = _index_options + _stock_options

if "watchlist" not in st.session_state:
    st.session_state.watchlist = load_watchlist()
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

watchlist = st.session_state.watchlist

tab1, tab2, tab3, tab4 = st.tabs(["📊 차트", "⭐ 관심목록", "⚖️ 동적자산배분", "💰 프리미엄"])

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
            save_watchlist(st.session_state.watchlist)
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
                save_watchlist(st.session_state.watchlist)
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
            "1일": period_return(df, "1일"),
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
        col_vals = rank_df[ret_period].dropna()
        vmin_w = float(col_vals.min()) if not col_vals.empty else -0.1
        vmax_w = float(col_vals.max()) if not col_vals.empty else  0.1
        fn_w = lambda v, lo=vmin_w, hi=vmax_w: _color_scale(v, lo, hi)
        styled_rank = _apply_bg(
            rank_df.style.format(
                {"현재가": "{:,.2f}", "1일": "{:+.2%}", ret_period: "{:+.2%}"},
                na_rep="—",
            ),
            fn_w, subset=[ret_period],
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

        def pos_str(w):
            return " / ".join(
                f"{k} {v*100:.0f}%" for k, v in sorted(w.items(), key=lambda x: -x[1])
            )

        table = [
            {"전략명": nm, "현재 포지션": pos_str(r["current"]),
             "CAGR": r["cagr"], "MDD": r["mdd"], "Sharpe": r["sharpe"]}
            for nm, r in results.items() if r
        ]
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

        r = results[pick]
        st.markdown(f"#### 전략 상세 — {pick}")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("CAGR", f"{r['cagr']:+.1%}")
        m2.metric("MDD",  f"{r['mdd']:.1%}")
        m3.metric("Sharpe", f"{r['sharpe']:.2f}")
        m4.metric("현재 포지션", pos_str(r["current"]))

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
        fn_h = lambda v: _color_scale(v, -10, 10)
        styled_heat = _apply_bg(heat_full.style.format("{:+.1f}", na_rep="—"), fn_h)
        st.markdown(
            f'<div style="overflow-x:auto;font-size:0.85rem">'
            f'{styled_heat.to_html()}'
            f'</div>',
            unsafe_allow_html=True,
        )

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

st.divider()
st.caption("※ 규칙 기반 계산기이며 투자 자문이 아닙니다. "
           "배당 미반영 종가 기반이라 실제 성과와 차이가 날 수 있습니다. "
           "투자 판단과 책임은 본인에게 있습니다.")
