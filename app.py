"""
나만의 투자 대시보드 — 차트 · 관심목록 · 동적자산배분 (탭 분리)
데이터: FinanceDataReader (국내/미국 주식·ETF·지수 + FRED). 서버 수집 → CORS 제약 없음.
실행:  streamlit run app.py   (같은 폴더에 strategies.py 필요)
"""
import json
import math
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

# 매수 탭과 무관한 별도 탭: 이동평균 대비 위치
MA_TAB_INDICES_FILE = "ma_tab_indices.json"
MA_TAB_INDICES_GIST_FILENAME = "ma_tab_indices.json"
MA_TAB_PERIODS_FILE = "ma_tab_periods.json"
MA_TAB_PERIODS_GIST_FILENAME = "ma_tab_periods.json"
# 심볼은 scanner/seasonality_scan.py 재스캔 때와 별개로 이 자리에서 직접 fdr.DataReader로
# 확인한 것 — NDX(나스닥100)·US500·KS11·KQ11은 FDR이 그대로 지원하고, BTCUSD는
# "BTCUSD"/"BTC-USD" 심볼이 404라 "BTC/USD"(FDR 코인 조회 문법)로 대체했다.
DEFAULT_MA_INDICES = [
    {"symbol": "KS11", "label": "코스피"},
    {"symbol": "KQ11", "label": "코스닥"},
    {"symbol": "US500", "label": "S&P500"},
    {"symbol": "NDX", "label": "나스닥100"},
    {"symbol": "BTC/USD", "label": "BTCUSD"},
]
DEFAULT_MA_PERIODS = [5, 10, 20, 50, 120, 200]
MA_PERIOD_MIN, MA_PERIOD_MAX = 1, 500
MA_GAP_COLOR_CAP = 0.20   # 이격도 색상 진하기의 절대값 상한 (±20%, 이상은 최대 진하기로 클립)

# ---- 코인 탭 -----------------------------------------------------------
COIN_UNIVERSE_POOL = 200   # CoinGecko 시가총액 상위 몇 개까지를 "전체" 유니버스 풀로 볼지
# RS(상대강도) 종합 점수 = 1~4주 수익률의 유니버스 내 백분위(0~100) 가중평균.
# 가중치는 최근 주에 더 크게 뒀다(원사이트 스타일 모멘텀 — 최근 흐름을 더 반영).
COIN_RS_WEEK_WEIGHTS = {1: 4, 2: 3, 3: 2, 4: 1}
COIN_CHART_PERIOD_DAYS = {"1년": 365, "3년": 1095, "전체": 100000}
COIN_CHART_INTERVAL_MAP = {"일봉": "1d", "주봉": "1w", "월봉": "1M"}
COIN_RET_COLOR_CAP = 0.50  # 코인은 변동성이 커서 관심목록(±30%)보다 상한을 넉넉히 잡음

# Hyperliquid의 빌더 배포 퍼프 DEX 중 전통자산(주식·지수·원자재·환율 등)을 취급하는
# "xyz" dex 유니버스를 그대로 쓴다. 카테고리 분류는 dict 하드코딩(수동/휴리스틱) —
# Hyperliquid가 자산을 추가/변경하면 이 dict만 고치면 된다. 목록에 없는 심볼은
# CATEGORY_FALLBACK으로 분류된다.
HL_XYZ_DEX = "xyz"
# 이 dict에 없는 심볼(Hyperliquid가 새로 추가한 자산)은 일단 이 기본값으로 분류된다.
# xyz dex 유니버스는 대부분 미국 개별 종목이라 "미국주식"을 기본값으로 뒀다 —
# 아래 dict에 명시적으로 적은 것들(해외기업·지수·원자재·환율·ETF·비상장)만 예외.
HL_CATEGORY_FALLBACK = "미국주식"
HL_CATEGORIES = ["전체", "미국주식", "글로벌주식", "지수", "원자재", "환율", "비상장", "섹터"]
HL_SYMBOL_CATEGORY = {
    # 지수
    "XYZ100": "지수", "SP500": "지수", "KR200": "지수", "JP225": "지수", "NIFTY": "지수",
    "IBOV": "지수", "VIX": "지수", "VOL": "지수",
    # 환율
    "EUR": "환율", "GBP": "환율", "JPY": "환율", "KRW": "환율", "DXY": "환율",
    # 원자재
    "GOLD": "원자재", "SILVER": "원자재", "COPPER": "원자재", "NATGAS": "원자재",
    "URANIUM": "원자재", "ALUMINIUM": "원자재", "PLATINUM": "원자재", "PALLADIUM": "원자재",
    "CL": "원자재", "BRENTOIL": "원자재", "CORN": "원자재", "WHEAT": "원자재", "TTF": "원자재",
    # 섹터 ETF
    "SMH": "섹터", "SOXL": "섹터", "XLE": "섹터", "XBI": "섹터", "MAGS": "섹터",
    "URNM": "섹터", "DRAM": "섹터",
    # 글로벌주식(미국 외 기업·ADR·해외 ETF)
    "TSM": "글로벌주식", "BABA": "글로벌주식", "SMSN": "글로벌주식", "HYUNDAI": "글로벌주식",
    "SOFTBANK": "글로벌주식", "KIOXIA": "글로벌주식", "EWY": "글로벌주식", "EWJ": "글로벌주식",
    "EWT": "글로벌주식", "EWZ": "글로벌주식", "ASML": "글로벌주식", "NOK": "글로벌주식",
    "ARM": "글로벌주식", "IBIDEN": "글로벌주식", "SKHX": "글로벌주식", "SKHY": "글로벌주식",
    "CXMT": "글로벌주식",
    # 비상장/프리IPO 성격
    "SPCX": "비상장", "H100": "비상장", "GIGADEV": "비상장", "PURRDAT": "비상장",
    "USAR": "비상장", "SHAZ": "비상장", "KSTR": "비상장", "KORU": "비상장",
    "MINIMAX": "비상장", "UNITREE": "비상장", "LYTE": "비상장", "NCLD": "비상장",
    "ZHIPU": "비상장", "STRC": "비상장", "BOT": "비상장", "BIRD": "비상장", "CBRS": "비상장",
    # 이 dict에 없는 나머지(TSLA/NVDA/AAPL 등 미국 개별 종목 대부분)는
    # HL_CATEGORY_FALLBACK("미국주식")으로 분류된다.
}
# 주요 종목만 한글명을 달아준다(전부 번역하지 않음 — 목록에 없으면 심볼만 표시).
# 나중에 필요하면 이 dict에 항목을 추가하면 된다.
HL_SYMBOL_KOREAN_NAME = {
    "XYZ100": "XYZ 100지수", "SP500": "S&P500", "KR200": "코스피200", "JP225": "니케이225",
    "NIFTY": "인도 니프티", "IBOV": "브라질 보베스파", "VIX": "변동성지수", "DXY": "달러인덱스",
    "EUR": "유로", "GBP": "파운드", "JPY": "엔", "KRW": "원",
    "GOLD": "금", "SILVER": "은", "COPPER": "구리", "NATGAS": "천연가스", "URANIUM": "우라늄",
    "ALUMINIUM": "알루미늄", "PLATINUM": "백금", "PALLADIUM": "팔라듐", "CL": "WTI 원유",
    "BRENTOIL": "브렌트유", "CORN": "옥수수", "WHEAT": "밀",
    "AAPL": "애플", "TSLA": "테슬라", "NVDA": "엔비디아", "GOOGL": "구글", "AMZN": "아마존",
    "MSFT": "마이크로소프트", "META": "메타", "AMD": "AMD", "NFLX": "넷플릭스",
    "COIN": "코인베이스", "PLTR": "팔란티어", "HOOD": "로빈후드", "MSTR": "마이크로스트래티지",
    "COST": "코스트코", "DELL": "델", "IBM": "IBM", "AVGO": "브로드컴", "QCOM": "퀄컴",
    "INTC": "인텔", "GME": "게임스탑", "ZM": "줌", "EBAY": "이베이", "CRWD": "크라우드스트라이크",
    "RDDT": "레딧", "MRNA": "모더나", "ASML": "ASML", "BABA": "알리바바", "TSM": "TSMC",
    "SMSN": "삼성전자", "HYUNDAI": "현대차", "SOFTBANK": "소프트뱅크",
    "SMH": "반도체 ETF", "SOXL": "반도체 3배 ETF", "XLE": "에너지 섹터 ETF",
    "XBI": "바이오 ETF", "MAGS": "매그니피센트7 ETF", "SPCX": "스페이스X",
}
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
TREND_MAX_DISPLAY = 100         # 표시 종목 수 상한(원사이트와 동일)


def normalize(sym):
    s = sym.strip().upper()
    if s.endswith(".KS") or s.endswith(".KQ"):
        s = s[:-3]
    if s.isdigit():
        s = s.zfill(6)
    return s


def _gist_configured():
    try:
        g = st.secrets.get("gist")
        return bool(g and g.get("token") and g.get("id"))
    except Exception:
        return False


def _gist_headers(token):
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}


@st.cache_data(ttl=300)
def _gist_fetch_content(token, gist_id, filename):
    """Gist 안의 특정 파일 raw 내용을 반환. 파일이 없거나 비어있으면 빈 문자열.
    같은 Gist 하나에 watchlist.json 외에 이평 탭용 파일들도 함께 저장한다."""
    r = requests.get(f"https://api.github.com/gists/{gist_id}",
                      headers=_gist_headers(token), timeout=10)
    r.raise_for_status()
    files = r.json().get("files", {})
    f = files.get(filename)
    return f.get("content", "") if f else ""


def _load_json_list_local(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return list(default)


def _save_json_list_local(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # Cloud filesystem은 재시작 시 초기화 — 저장 실패는 무시


def load_json_list(gist_filename, local_path, default, item_fn=None):
    """(리스트, storage_mode) 반환. storage_mode: 'gist' 또는 'local'.
    item_fn이 주어지면 항목마다 적용해 정규화한다(예: 관심목록 종목코드 normalize)."""
    item_fn = item_fn or (lambda x: x)
    if _gist_configured():
        token, gist_id = st.secrets["gist"]["token"], st.secrets["gist"]["id"]
        try:
            content = _gist_fetch_content(token, gist_id, gist_filename)
        except Exception:
            # API 호출 실패 → 로컬 파일 방식으로 폴백
            return _load_json_list_local(local_path, default), "local"
        if content and content.strip():
            try:
                data = json.loads(content)
                if isinstance(data, list):
                    # 저장된 값이 빈 배열이어도 그대로 유지 (사용자가 전부 삭제한 상태)
                    return [item_fn(c) for c in data], "gist"
            except Exception:
                pass
        # Gist는 연결됐지만 파일이 비어있음/저장된 적 없음 → 디폴트 사용
        return list(default), "gist"
    return _load_json_list_local(local_path, default), "local"


def save_json_list(data, mode, gist_filename, local_path):
    if mode == "gist":
        try:
            token, gist_id = st.secrets["gist"]["token"], st.secrets["gist"]["id"]
            r = requests.patch(
                f"https://api.github.com/gists/{gist_id}",
                headers=_gist_headers(token),
                json={"files": {gist_filename: {
                    "content": json.dumps(data, ensure_ascii=False, indent=2)
                }}},
                timeout=10,
            )
            r.raise_for_status()
            _gist_fetch_content.clear()  # 캐시 무효화 → 다음 로드 시 최신값 반영
        except Exception:
            pass  # 저장 실패는 무시 (다음 rerun에서 재시도 가능)
        return
    _save_json_list_local(local_path, data)


def load_watchlist():
    return load_json_list(GIST_FILENAME, WATCHLIST_FILE, DEFAULT_WATCHLIST,
                           item_fn=lambda c: normalize(str(c)))


def save_watchlist(codes, mode):
    save_json_list(codes, mode, GIST_FILENAME, WATCHLIST_FILE)


def load_ma_indices():
    return load_json_list(MA_TAB_INDICES_GIST_FILENAME, MA_TAB_INDICES_FILE, DEFAULT_MA_INDICES)


def save_ma_indices(items, mode):
    save_json_list(items, mode, MA_TAB_INDICES_GIST_FILENAME, MA_TAB_INDICES_FILE)


def load_ma_periods():
    return load_json_list(MA_TAB_PERIODS_GIST_FILENAME, MA_TAB_PERIODS_FILE, DEFAULT_MA_PERIODS,
                           item_fn=lambda p: int(p))


def save_ma_periods(periods, mode):
    save_json_list(periods, mode, MA_TAB_PERIODS_GIST_FILENAME, MA_TAB_PERIODS_FILE)


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
#  이동평균 탭
# ============================================================================
def _validate_ma_symbol(symbol):
    """추가 버튼을 눌렀을 때 즉석에서 FDR 조회가 되는 심볼인지 확인(최근 30일만
    가볍게 조회) — 잘못된 심볼을 목록에 넣어 이후 스캔마다 계속 실패하는 것을 방지."""
    try:
        start = (pd.Timestamp.today() - pd.Timedelta(days=30)).strftime("%Y-%m-%d")
        df = fdr.DataReader(symbol, start)
        return df is not None and not df.empty and "Close" in df.columns and df["Close"].notna().any()
    except Exception:
        return False


@st.cache_data(ttl=60 * 30)
def _fetch_ma_row(symbol, periods):
    """symbol의 현재가·1일등락률과, periods 각각의 SMA 대비 이격도(현재가/SMA-1)를 계산.
    조회 자체가 실패하면 None. 개별 이평 기간은 데이터가 모자라도 그 칸만 None으로
    비우고 나머지는 계산한다(종목 전체를 제외하지 않음)."""
    if not periods:
        return None
    max_p = max(periods)
    start = (pd.Timestamp.today() - pd.Timedelta(days=max(400, max_p * 3))).strftime("%Y-%m-%d")
    try:
        df = fdr.DataReader(symbol, start)
    except Exception:
        return None
    if df is None or df.empty or "Close" not in df.columns:
        return None
    close = df["Close"].dropna()
    if len(close) < 2:
        return None
    price = float(close.iloc[-1])
    day_ret = float(price / close.iloc[-2] - 1.0)
    gaps = {}
    for p in periods:
        if len(close) < p:
            gaps[p] = None
            continue
        sma = float(close.iloc[-p:].mean())
        gaps[p] = (price / sma - 1.0) if sma else None
    return {"price": price, "day_ret": day_ret, "last_date": close.index[-1], "gaps": gaps}


# ============================================================================
#  코인 탭 — 랭킹(CoinGecko + Binance) / 글로벌 24-7(Hyperliquid xyz dex)
# ============================================================================
@st.cache_data(ttl=300)
def _fetch_coingecko_markets(pool_size):
    """CoinGecko 공개 markets 엔드포인트(무인증) — 시세·시총·거래량·24h/7d/30d
    변동률·7일 스파크라인을 한 번에 준다."""
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": pool_size,
                    "page": 1, "sparkline": "true", "price_change_percentage": "24h,7d,30d"},
            timeout=20,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


@st.cache_data(ttl=60 * 60 * 24)
def _fetch_binance_usdt_symbols():
    """Binance 공개 exchangeInfo(무인증) — USDT 마켓이 있는 심볼 집합. 1·2·3·4주
    수익률(RS 계산용)과 캔들차트는 CoinGecko가 아니라 여기서 받는다(과거 일봉이
    필요한데 CoinGecko는 무료 플랜에서 일 단위 히스토리 접근이 제한적)."""
    try:
        r = requests.get("https://api.binance.com/api/v3/exchangeInfo", timeout=20)
        r.raise_for_status()
        return {s["symbol"] for s in r.json()["symbols"]
                if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"}
    except Exception:
        return set()


@st.cache_data(ttl=60 * 60)
def _fetch_binance_klines(symbol, interval, limit=500):
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": min(limit, 1000)},
            timeout=15,
        )
        r.raise_for_status()
        return [{"time": pd.to_datetime(row[0], unit="ms"), "open": float(row[1]),
                  "high": float(row[2]), "low": float(row[3]), "close": float(row[4]),
                  "volume": float(row[5])}
                 for row in r.json()]
    except Exception:
        return []


def _lookback_return(closes, days_back):
    n = len(closes)
    idx = n - 1 - days_back
    if idx < 0:
        return None
    base = closes[idx]
    return (closes[-1] / base - 1.0) if base else None


@st.cache_data(ttl=300)
def build_coin_ranking(pool_size=COIN_UNIVERSE_POOL):
    """(결과 DataFrame, 상태) 반환. 상태: "ok"/"coingecko_fail"/"binance_fail"/
    "no_match"/"no_data" — 실패 원인을 화면에서 구분해 보여주기 위함.

    RS(상대강도) 계산: 1·2·3·4주 수익률 각각을 이 유니버스(풀) 내에서 백분위
    (0~100, pandas rank(pct=True))로 바꾼 뒤, COIN_RS_WEEK_WEIGHTS 가중평균을
    "종합RS"로 쓴다. 가중치는 최근 주(1주 전)에 더 크게 둬서 최근 모멘텀을 더
    반영한다 — 절대수익률이 아니라 "같은 시점 다른 코인들 대비 얼마나 잘했는가"를
    보는 것이 RS의 취지라, 반드시 유니버스 내부 비교(percentile)로 계산한다."""
    markets = _fetch_coingecko_markets(pool_size)
    if not markets:
        return pd.DataFrame(), "coingecko_fail"
    usdt_syms = _fetch_binance_usdt_symbols()
    if not usdt_syms:
        return pd.DataFrame(), "binance_fail"

    candidates = [(m, m["symbol"].upper() + "USDT") for m in markets]
    candidates = [(m, s) for m, s in candidates if s in usdt_syms]
    if not candidates:
        return pd.DataFrame(), "no_match"

    def _fetch_row(item):
        m, bsym = item
        # 일봉 400개(약 400일치)면 1~4주 수익률뿐 아니라 3·6·12개월 수익률까지
        # 같은 호출 하나로 다 계산할 수 있다(컬럼 토글에 맞춰 다시 조회하지 않음).
        candles = _fetch_binance_klines(bsym, "1d", 400)
        if len(candles) < 29:
            return None
        closes = [c["close"] for c in candles]
        rets_week = [_lookback_return(closes, d) for d in (7, 14, 21, 28)]
        if any(v is None for v in rets_week):
            return None
        ret_1w, ret_2w, ret_3w, ret_4w = rets_week
        # CoinGecko는 %(예: -1.2)로 주는데 앱 전체 관례(RET_COLOR_CAP 등)는 소수
        # 비율(예: -0.012)이라 여기서 100으로 나눠 맞춘다.
        def _pct(key):
            v = m.get(key)
            return v / 100.0 if v is not None else None
        return {
            "symbol": m["symbol"].upper(), "name": m["name"], "binance_symbol": bsym,
            "price": m["current_price"],
            "chg_24h": _pct("price_change_percentage_24h_in_currency"),
            "chg_7d": _pct("price_change_percentage_7d_in_currency"),
            "chg_30d": _pct("price_change_percentage_30d_in_currency"),
            "market_cap": m.get("market_cap"), "market_cap_rank": m.get("market_cap_rank"),
            "volume": m.get("total_volume"),
            "sparkline": (m.get("sparkline_in_7d") or {}).get("price") or [],
            "ret_1w": ret_1w, "ret_2w": ret_2w, "ret_3w": ret_3w, "ret_4w": ret_4w,
            "ret_3m": _lookback_return(closes, 90),
            "ret_6m": _lookback_return(closes, 182),
            "ret_12m": _lookback_return(closes, 365),
        }

    with ThreadPoolExecutor(max_workers=12) as exe:
        rows = list(exe.map(_fetch_row, candidates))
    rows = [r for r in rows if r is not None]
    if not rows:
        return pd.DataFrame(), "no_data"

    df = pd.DataFrame(rows)
    for w in (1, 2, 3, 4):
        df[f"rs_{w}w"] = df[f"ret_{w}w"].rank(pct=True) * 100
    wsum = sum(COIN_RS_WEEK_WEIGHTS.values())
    df["rs_total"] = sum(df[f"rs_{w}w"] * wt for w, wt in COIN_RS_WEEK_WEIGHTS.items()) / wsum
    df = df.sort_values("rs_total", ascending=False).reset_index(drop=True)
    df.insert(0, "#", df.index + 1)
    return df, "ok"


def _lwc_time(ts, intraday):
    """intraday=True면 UTCTimestamp(정수 초, 시:분까지 표시), False면 기존 차트
    탭과 같은 'YYYY-MM-DD' 문자열."""
    t = pd.Timestamp(ts)
    return int(t.timestamp()) if intraday else t.strftime("%Y-%m-%d")


def _render_price_chart(candles, key, height=420, intraday=False, closed_mask=None):
    """candles: [{"time","open","high","low","close"}, ...] 정렬된 리스트.
    closed_mask는 candles와 길이가 같은 bool 리스트로, True인 구간(정규장 휴장)을
    회색으로 표시한다. Lightweight Charts엔 배경 음영 프리미티브가 없어서, 캔들의
    가격축과는 무관한 별도 오버레이 스케일(visible=False)에 값 0/1짜리 꽉 찬 Area
    시리즈를 캔들보다 먼저(=아래에) 깔아 흉내낸다 — 오버레이 스케일이 독립적이라
    캔들 쪽 자동 스케일에는 영향을 주지 않는다."""
    series_list = []
    if closed_mask is not None and any(closed_mask):
        bg_data = [{"time": _lwc_time(c["time"], intraday), "value": 1 if closed else 0}
                    for c, closed in zip(candles, closed_mask)]
        series_list.append({
            "type": "Area", "data": bg_data,
            "options": {
                "topColor": "rgba(120,120,120,0.35)", "bottomColor": "rgba(120,120,120,0.35)",
                "lineColor": "rgba(0,0,0,0)", "lineWidth": 1, "priceLineVisible": False,
                "lastValueVisible": False, "crosshairMarkerVisible": False,
                "priceScaleId": "closed_bg",
            },
            "priceScale": {"scaleMargins": {"top": 0, "bottom": 0}, "visible": False},
        })
    candle_data = [{"time": _lwc_time(c["time"], intraday), "open": round(c["open"], 6),
                     "high": round(c["high"], 6), "low": round(c["low"], 6), "close": round(c["close"], 6)}
                    for c in candles]
    series_list.append({
        "type": "Candlestick", "data": candle_data,
        "options": {"upColor": "#26a69a", "downColor": "#ef5350", "borderVisible": False,
                    "wickUpColor": "#26a69a", "wickDownColor": "#ef5350"},
    })
    chart_opts = {
        "height": height,
        "layout": {"background": {"type": "solid", "color": "#FFFFFF"}, "textColor": "#333333"},
        "rightPriceScale": {"scaleMargins": {"top": 0.05, "bottom": 0.05},
                             "borderColor": "rgba(197,203,206,0.5)"},
        "timeScale": {"borderColor": "rgba(197,203,206,0.5)", "rightOffset": 5,
                       "timeVisible": intraday, "secondsVisible": False},
        "grid": {"vertLines": {"color": "rgba(197,203,206,0.2)"},
                  "horzLines": {"color": "rgba(197,203,206,0.3)"}},
        "crosshair": {"mode": 1},
    }
    renderLightweightCharts([{"chart": chart_opts, "series": series_list}], key=key)


@st.cache_data(ttl=60 * 60 * 24)
def _fetch_hl_xyz_universe():
    """Hyperliquid 공개 info API(무인증) — 빌더 배포 퍼프 DEX 중 전통자산을 다루는
    "xyz" dex의 유니버스(심볼 목록, "xyz:AAPL" 형태)."""
    try:
        r = requests.post("https://api.hyperliquid.xyz/info",
                           json={"type": "meta", "dex": HL_XYZ_DEX}, timeout=15)
        r.raise_for_status()
        return [u["name"] for u in r.json().get("universe", [])]
    except Exception:
        return []


@st.cache_data(ttl=300)
def _fetch_hl_candles(coin, interval, lookback_ms):
    try:
        now_ms = int(pd.Timestamp.utcnow().timestamp() * 1000)
        req = {"type": "candleSnapshot",
               "req": {"coin": coin, "interval": interval,
                        "startTime": now_ms - lookback_ms, "endTime": now_ms}}
        r = requests.post("https://api.hyperliquid.xyz/info", json=req, timeout=15)
        r.raise_for_status()
        return [{"time": pd.to_datetime(row["t"], unit="ms"), "open": float(row["o"]),
                  "high": float(row["h"]), "low": float(row["l"]), "close": float(row["c"]),
                  "volume": float(row["v"])} for row in r.json()]
    except Exception:
        return []


@st.cache_data(ttl=300)
def build_global247():
    """(결과 DataFrame, 상태) 반환. 1시간봉 31일치 하나로 현재가·1h·24h·7d·30d
    변동률을 전부 계산한다(심볼당 API 호출 1번 — 굳이 여러 인터벌을 따로 안 불러
    Hyperliquid에 보내는 요청 수를 줄인다)."""
    coins = _fetch_hl_xyz_universe()
    if not coins:
        return pd.DataFrame(), "hl_fail"

    def _fetch_row(coin):
        candles = _fetch_hl_candles(coin, "1h", 31 * 24 * 3600 * 1000)
        if len(candles) < 25:
            return None
        closes = [c["close"] for c in candles]
        n = len(closes)

        def _chg(hours_back):
            idx = n - 1 - hours_back
            if idx < 0:
                return None
            base = closes[idx]
            return (closes[-1] / base - 1.0) if base else None

        sym = coin.split(":", 1)[-1]
        return {
            "symbol": sym, "hl_symbol": coin, "price": closes[-1],
            "chg_1h": _chg(1), "chg_24h": _chg(24), "chg_7d": _chg(24 * 7), "chg_30d": _chg(24 * 30),
            "category": HL_SYMBOL_CATEGORY.get(sym, HL_CATEGORY_FALLBACK),
        }

    with ThreadPoolExecutor(max_workers=12) as exe:
        rows = list(exe.map(_fetch_row, coins))
    rows = [r for r in rows if r is not None]
    if not rows:
        return pd.DataFrame(), "no_data"
    df = pd.DataFrame(rows).sort_values("chg_24h", ascending=False, na_position="last").reset_index(drop=True)
    return df, "ok"


def _us_market_closed_mask(times):
    """미국 정규장(09:30~16:00 America/New_York, 평일) 기준 휴장 여부의 근사치.
    공휴일 캘린더는 반영하지 않는다(주말+시간대만 체크) — 이 앱의 다른 달력 근사
    (계절성 탭의 진입일 계산 등)와 같은 수준의 단순화라 문서화만 해둔다."""
    out = []
    for t in times:
        ts = pd.Timestamp(t)
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        et = ts.tz_convert("America/New_York")
        is_weekday = et.weekday() < 5
        in_hours = (9, 30) <= (et.hour, et.minute) < (16, 0)
        out.append(not (is_weekday and in_hours))
    return out


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


def _round_half_up(x):
    """0.5 지점을 항상 위로 반올림. 파이썬 기본 `:.0f` 포맷은 0.5를 짝수 쪽으로
    반올림(banker's rounding)해서, 예: 323372.5원이 323,372원으로 내려가 검증 예시
    (323,373원)와 어긋나는 경우가 있다 — 화면 표시용 금액은 전부 이 함수로 반올림한다."""
    return math.floor(x + 0.5) if x >= 0 else -math.floor(-x + 0.5)


def _krw_abbrev(x):
    """1,412만원 스타일 축약 표기 (원 단위 미만은 버림)."""
    x = _round_half_up(x)
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
if "ma_indices" not in st.session_state:
    st.session_state.ma_indices, st.session_state.ma_indices_mode = load_ma_indices()
if "ma_periods" not in st.session_state:
    st.session_state.ma_periods, st.session_state.ma_periods_mode = load_ma_periods()
if "coin_rank_symbol" not in st.session_state:
    st.session_state.coin_rank_symbol = None       # 랭킹 탭 상단 차트 대상(binance_symbol)
if "coin_g247_symbol" not in st.session_state:
    st.session_state.coin_g247_symbol = None       # 글로벌24-7 탭 하단 차트 대상(hl_symbol)

st.title("📈 나의 투자 대시보드")
st.caption("차트 · 관심목록 · 동적자산배분 — 탭 전환. 데이터: FinanceDataReader")
if st.session_state.storage_mode == "local":
    st.caption("💾 로컬 저장 모드 — Gist 미설정 또는 연결 실패로, 이 기기에만 저장됩니다.")

watchlist = st.session_state.watchlist

tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs(
    ["📊 차트", "⭐ 관심목록", "⚖️ 동적자산배분", "💰 프리미엄", "🛒 매수", "📏 이동평균", "🪙 코인"])

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

            # 1차: (종목,신호유형,장단기)에서 최고 변형만 남긴다(예: EMA40/EMA21/SMA20
            # 근접이 동시에 살아있으면 그중 하나만). 이 단계에서도 종목 하나가 신호유형별로
            # 여러 번 나올 수 있다(예: 이평눌림목+박스돌파 동시 신호) — 그래서 필터를 다
            # 적용한 뒤 아래에서 티커 기준으로 한 번 더 최상위 1행만 남긴다. 필터 적용 후에
            # 다시 고르는 이유: "신호=이평눌림목"으로만 볼 때도 그 조건 안에서 최고 1개를
            # 골라야 하는데, 스캐너 단계에서 신호유형을 넘나드는 고정 1위를 미리 정해두면
            # 필터와 결과가 어긋난다.
            rep = tdf[tdf["is_representative"] & tdf["is_best_variant"]].copy()

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

            # 2차: 티커 기준 중복 제거(별점 -> 초과수익 -> 표본수 순 최상위 1행만).
            # 2차 기준을 손익비 대신 초과수익(벤치마크 대비 실제 아웃퍼폼 크기)으로 쓴다.
            rep = rep.sort_values(["star_rating", "excess_return", "n_samples"],
                                   ascending=[False, False, False])
            rep = rep.drop_duplicates(subset="ticker", keep="first")

            if sort_label == "별 우선":
                rep = rep.sort_values(["star_rating", "rs_total", "excess_return"],
                                       ascending=[False, False, False])
            elif sort_label == "RS순":
                rep = rep.sort_values("rs_total", ascending=False)
            else:
                rep = rep.sort_values("win_rate", ascending=False)
            rep = rep.reset_index(drop=True)

            total_unique = len(rep)
            rep = rep.head(TREND_MAX_DISPLAY)
            if total_unique > TREND_MAX_DISPLAY:
                st.caption(f"{len(rep)}종목 표시 (조건 충족 {total_unique}종목 중 상위 {TREND_MAX_DISPLAY}) · {sort_label}")
            else:
                st.caption(f"{total_unique}종목 · {sort_label}")

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

                        m1, m2, m3, m4, m5, m6, m7 = st.columns(7)
                        m1.metric("승률(초과수익 기준)", f"{r['win_rate']:.0%}")
                        m2.metric("평균이익", f"{r['avg_win']:+.1%}")
                        m3.metric("평균손실", f"{r['avg_loss']:+.1%}")
                        m4.metric("손익비", f"{r['profit_factor']:.2f}")
                        m5.metric(r["sample_label"], f"{int(r['n_samples'])}")
                        excess = r.get("excess_return")
                        m6.metric("초과수익(벤치마크 대비)", f"{excess:+.1%}" if pd.notna(excess) else "—")
                        abs_wr = r.get("abs_win_rate")
                        m7.metric("승률(절대수익 기준)", f"{abs_wr:.0%}" if pd.notna(abs_wr) else "—")

                        # 이 종목이 지금 다른 신호(유형/변형/장단기)로도 살아있는지 — 티커
                        # 중복 제거로 위 카드엔 최상위 1개만 보이므로 나머지는 여기서 안내.
                        others = tdf[
                            (tdf["ticker"] == r["ticker"]) & tdf["is_representative"] & tdf["is_best_variant"]
                            & ~((tdf["signal_type"] == r["signal_type"])
                                & (tdf["variant_key"] == r["variant_key"])
                                & (tdf["trend_term"] == r["trend_term"]))
                        ].sort_values(["star_rating", "excess_return"], ascending=[False, False])
                        if not others.empty:
                            if st.checkbox(f"이 종목의 다른 신호 보기 ({len(others)}개)", key=f"others_{i}_{r['ticker']}"):
                                for _, o in others.iterrows():
                                    o_stars = "★" * int(o["star_rating"]) + "☆" * (3 - int(o["star_rating"]))
                                    st.caption(
                                        f"{o_stars} [{o['signal_type']}] {o['variant_label']} · "
                                        f"{int(o['hold_days'])}일 보유 · 승률 {o['win_rate']:.0%} · "
                                        f"손익비 {o['profit_factor']:.2f}"
                                    )

                        st.markdown("##### 보유기간별 성적표 (★ = 대표 보유기간)")
                        sub = (tdf[(tdf["ticker"] == r["ticker"]) & (tdf["variant_key"] == r["variant_key"])
                                   & (tdf["trend_term"] == r["trend_term"])]
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

            # 투자자산: 원화/달러 종목을 오가도 서로 다른 통화의 숫자가 섞이지 않도록
            # 통화별로 세션에 값을 따로 유지한다(관심목록처럼 Gist에 영구 저장하진
            # 않고, 세션이 유지되는 동안만 — 다시 열 때마다 남아있길 원하면 알려달라).
            asset_key = "plan_asset_krw" if p_is_krw else "plan_asset_usd"
            if asset_key not in st.session_state:
                st.session_state[asset_key] = 100_000_000.0 if p_is_krw else 100_000.0

            ac1, ac2 = st.columns([3, 2])
            with ac1:
                asset = st.number_input(
                    f"투자자산 ({'원' if p_is_krw else 'USD'})", min_value=0.0,
                    step=(1_000_000.0 if p_is_krw else 1_000.0),
                    format="%.0f" if p_is_krw else "%.2f", key=asset_key,
                )
            with ac2:
                st.markdown("<div style='height:1.8em'></div>", unsafe_allow_html=True)
                if asset > 0:
                    st.caption(f"= {_krw_abbrev(asset)}" if p_is_krw else f"= ${asset:,.2f}")
            asset_valid = asset > 0
            if not asset_valid:
                st.error("투자자산은 0보다 큰 값을 입력해주세요.")

            mode = st.radio("방식", ["디폴트", "직접 입력"], horizontal=True, key="plan_mode")
            # 디폴트: 1%/7%/3배 고정, 투자자산만 입력. 직접 입력: 네 값 모두 조정 가능.
            if mode == "직접 입력":
                q1, q2, q3 = st.columns(3)
                loss_pct = q1.number_input(
                    "최대손실(%)", min_value=0.1, value=1.0, step=0.1, key="plan_loss") / 100
                stop_pct = q2.number_input(
                    "손절폭(%)", min_value=0.1, value=7.0, step=0.5, key="plan_stop") / 100
                tp_mult = q3.number_input(
                    "익절배수", min_value=0.1, value=3.0, step=0.5, key="plan_mult")
            else:
                loss_pct, stop_pct, tp_mult = 0.01, 0.07, 3.0

            # 불타기(분할 추가매수): 원 사이트의 정확한 규칙이 확인되지 않아, 우선
            # 총 매수금액을 1차 60% / 2차 40%로 나누는 임시 규칙으로 구현했다.
            # 규칙이 확인되면 아래 pyramiding 분기만 교체하면 된다.
            pyramiding = st.radio("불타기", ["안 함", "함"], horizontal=True, key="plan_pyramid")

            if asset_valid and st.button("계획 보기", type="primary"):
                st.session_state.plan_computed = True

            if asset_valid and st.session_state.get("plan_computed"):
                plan = _position_sizing_plan(buy_price, asset, loss_pct, stop_pct, tp_mult)
                if plan is None or plan["shares"] <= 0:
                    st.warning("손절폭·매수가 조합상 매수 가능 주식 수가 0입니다. 파라미터를 확인해주세요.")
                else:
                    if p_is_krw:
                        header_price = f"{buy_price:,.0f}원"
                        amt_str = f"{_round_half_up(plan['buy_amount']):,}원 ({_krw_abbrev(plan['buy_amount'])})"
                        loss_str = f"{_round_half_up(plan['worst_loss']):,}원"
                        stop_str = f"{math.floor(plan['stop_price']):,}원"  # 손절가는 원 단위 버림
                        tp1_str = f"{_round_half_up(plan['tp1_price']):,}원"
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
            # 매수창은 "오늘"을 기준으로 잡는다(스캔 기준일 as_of가 아니라 앱을 보는
            # 시점) — entry_date가 [오늘-BUY_WINDOW_BEFORE, 오늘+BUY_WINDOW_AFTER]
            # 안에 들어오는 종목만 남긴다. 예전엔 반대로 entry_date를 기준으로 창을
            # 잡아(entry-2~entry+5 안에 오늘이 들어오는지) 사실상 [오늘-5~오늘+2]를
            # 보는 것과 같아져, 앞뒤가 뒤집힌 채로 필터링되고 있었다.
            window_start = today_ts - pd.Timedelta(days=BUY_WINDOW_BEFORE)
            window_end = today_ts + pd.Timedelta(days=BUY_WINDOW_AFTER)
            work = work[(work["entry_date"] >= window_start) & (work["entry_date"] <= window_end)].copy()
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

            # 티커 기준 중복 제거(승률 -> 평균수익률 -> 표본연수 순 최상위 1행만) —
            # 같은 종목이 진입일만 다른 여러 (월,월중n번째거래일,보유기간) 조합으로
            # 동시에 매수창에 들어오면(예: Ares Management, Gartner) 한 번만 보여준다.
            work = work.sort_values(["win_rate", "avg_return", "n_years"], ascending=[False, False, False])
            work = work.drop_duplicates(subset="ticker", keep="first")

            sort_label = st.selectbox(
                "정렬", ["승률 높은순", "평균수익률 높은순", "진입 임박순"], key="season_sort")
            if sort_label == "승률 높은순":
                work = work.sort_values(["win_rate", "avg_return"], ascending=[False, False])
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

# =====================  이동평균  ===========================================
with tab6:
    st.subheader("📏 이동평균 대비 위치")
    st.caption("등록한 지수·종목의 현재가가 각 이동평균선 위/아래 어디에 있는지 한눈에 봅니다.")

    with st.expander("⚙️ 지수·이동평균 목록 관리"):
        mc1, mc2 = st.columns(2)
        with mc1:
            st.markdown("**지수·종목 목록**")
            cur_items = st.session_state.ma_indices
            if cur_items:
                idx_labels = [f"{it['label']} ({it['symbol']})" for it in cur_items]
                idx_remove = st.multiselect("삭제할 항목", idx_labels, key="ma_idx_remove_sel")
                if st.button("선택 삭제", key="ma_idx_remove_btn") and idx_remove:
                    remove_syms = {s.rsplit("(", 1)[-1].rstrip(")") for s in idx_remove}
                    st.session_state.ma_indices = [
                        it for it in cur_items if it["symbol"] not in remove_syms]
                    save_ma_indices(st.session_state.ma_indices, st.session_state.ma_indices_mode)
                    st.rerun()
            else:
                st.caption("등록된 지수·종목이 없습니다.")
            with st.form("ma_idx_add_form", clear_on_submit=True):
                new_sym = st.text_input("심볼 (예: KS11, AAPL, 005930)")
                new_label = st.text_input("표시 이름 (비우면 자동)")
                idx_add_sub = st.form_submit_button("추가")
            if idx_add_sub and new_sym.strip():
                sym = normalize(new_sym.strip())
                if sym in {it["symbol"] for it in st.session_state.ma_indices}:
                    st.warning(f"이미 등록된 심볼입니다: {sym}")
                elif not _validate_ma_symbol(sym):
                    st.error(f"'{sym}' 데이터를 조회할 수 없습니다 — 심볼을 확인해주세요.")
                else:
                    label = new_label.strip() or names.get(sym) or sym
                    st.session_state.ma_indices = st.session_state.ma_indices + [
                        {"symbol": sym, "label": label}]
                    save_ma_indices(st.session_state.ma_indices, st.session_state.ma_indices_mode)
                    st.success(f"'{label}({sym})' 추가됨")
                    st.rerun()
        with mc2:
            st.markdown("**이동평균 기간(일)**")
            cur_periods_mgmt = st.session_state.ma_periods
            if cur_periods_mgmt:
                period_remove = st.multiselect(
                    "삭제할 기간", sorted(cur_periods_mgmt), key="ma_period_remove_sel")
                if st.button("선택 삭제", key="ma_period_remove_btn") and period_remove:
                    st.session_state.ma_periods = [
                        p for p in cur_periods_mgmt if p not in period_remove]
                    save_ma_periods(st.session_state.ma_periods, st.session_state.ma_periods_mode)
                    st.rerun()
            else:
                st.caption("등록된 이동평균이 없습니다.")
            with st.form("ma_period_add_form", clear_on_submit=True):
                new_period = st.number_input(
                    "기간(일)", min_value=MA_PERIOD_MIN, max_value=MA_PERIOD_MAX, value=20, step=1)
                period_add_sub = st.form_submit_button("추가")
            if period_add_sub:
                np_ = int(new_period)
                if np_ in st.session_state.ma_periods:
                    st.warning(f"이미 등록된 기간입니다: {np_}일")
                else:
                    st.session_state.ma_periods = sorted(st.session_state.ma_periods + [np_])
                    save_ma_periods(st.session_state.ma_periods, st.session_state.ma_periods_mode)
                    st.rerun()

        if st.button("🔄 기본값으로 초기화", key="ma_reset_btn"):
            st.session_state.ma_indices = DEFAULT_MA_INDICES.copy()
            st.session_state.ma_periods = DEFAULT_MA_PERIODS.copy()
            save_ma_indices(st.session_state.ma_indices, st.session_state.ma_indices_mode)
            save_ma_periods(st.session_state.ma_periods, st.session_state.ma_periods_mode)
            st.rerun()

    ma_indices = st.session_state.ma_indices
    ma_periods = sorted(st.session_state.ma_periods)

    if not ma_indices:
        st.info("등록된 지수·종목이 없습니다. 위 '지수·이동평균 목록 관리'에서 추가해주세요.")
    elif not ma_periods:
        st.info("등록된 이동평균 기간이 없습니다. 위 '지수·이동평균 목록 관리'에서 추가해주세요.")
    else:
        with st.spinner("시세 조회 중..."):
            with ThreadPoolExecutor(max_workers=8) as exe:
                fetch_results = list(exe.map(
                    lambda it: (it, _fetch_ma_row(it["symbol"], tuple(ma_periods))), ma_indices))

        period_cols = [f"{p}일선" for p in ma_periods]
        rows, failed = [], []
        for it, data in fetch_results:
            if data is None:
                failed.append(it["label"])
                continue
            gaps, above, valid = {}, 0, 0
            for p in ma_periods:
                g = data["gaps"].get(p)
                gaps[f"{p}일선"] = g
                if g is not None:
                    valid += 1
                    if g > 0:
                        above += 1
            if valid == 0:
                failed.append(it["label"])
                continue
            rows.append({
                "지수": it["label"], "현재가": data["price"], "1일등락률": data["day_ret"],
                **gaps, "요약": f"{valid}개 중 {above}개 위",
                "_above": above, "_valid": valid, "_last_date": data["last_date"],
            })

        if failed:
            st.warning("조회 실패로 표에서 제외됨: " + ", ".join(failed))

        if not rows:
            st.info("표시할 데이터가 없습니다.")
        else:
            df_ma = pd.DataFrame(rows)
            as_of = df_ma["_last_date"].max()
            st.caption(f"데이터 기준일: {as_of:%Y-%m-%d}")

            sort_label = st.selectbox("정렬", ["강한 순", "약한 순", "이름순"], key="ma_sort")
            if sort_label == "강한 순":
                df_ma = df_ma.sort_values(["_above", "_valid"], ascending=[False, False])
            elif sort_label == "약한 순":
                df_ma = df_ma.sort_values(["_above", "_valid"], ascending=[True, False])
            else:
                df_ma = df_ma.sort_values("지수")
            df_ma = df_ma.reset_index(drop=True)

            display_cols = ["지수", "현재가", "1일등락률"] + period_cols + ["요약"]
            disp = df_ma[display_cols]

            def _fmt_gap(v):
                if pd.isna(v):
                    return "—"
                return f"{'▲' if v >= 0 else '▼'} {v:+.1%}"

            styled = disp.style.format(
                {"현재가": "{:,.2f}", "1일등락률": "{:+.2%}", **{c: _fmt_gap for c in period_cols}},
                na_rep="—",
            )
            # 이평 칸들은 전부 같은 단위(이격도 %)라 컬럼별이 아니라 표 전체에서
            # 공통 진하기 상한을 써야 "5일선 -15%"와 "200일선 -15%"가 똑같이 진하게
            # 보인다(관심목록 수익률 표는 색칠 대상 컬럼이 하나뿐이라 컬럼별 상한이면
            # 충분했지만, 여기는 컬럼이 여러 개라 다르게 처리).
            all_gaps = pd.concat([df_ma[c] for c in period_cols]) if period_cols else pd.Series(dtype=float)
            gap_cap = _abs_cap(all_gaps, MA_GAP_COLOR_CAP)
            for c in period_cols:
                styled = _apply_bg(styled, lambda v, cap=gap_cap: _color_scale_zero(v, cap), subset=[c])
            ret_cap = _abs_cap(df_ma["1일등락률"], RET_COLOR_CAP)
            styled = _apply_bg(styled, lambda v, cap=ret_cap: _color_scale_zero(v, cap), subset=["1일등락률"])

            # use_container_width라 열이 많아지면 표 자체가 가로 스크롤됨(모바일 포함).
            st.dataframe(styled, use_container_width=True, hide_index=True,
                         height=min(60 + 35 * len(disp), 600))

# =====================  코인  ================================================
with tab7:
    sub_rank, sub_g247, sub_onchain = st.tabs(["🏆 랭킹", "🌐 글로벌 24-7", "⛓️ 온체인"])

    # ---- 랭킹 ---------------------------------------------------------------
    with sub_rank:
        st.caption("데이터: CoinGecko(시세·시총·스파크라인) + Binance(과거 일봉 — RS·주간/월간 "
                   "수익률 계산용). 시세는 5분, 과거 일봉은 1시간 캐시.")

        fc1, fc2, fc3, fc4, fc5, fc6 = st.columns([1.1, 1, 1.3, 1, 1, 1.2])
        tier_label = fc1.selectbox("시총 구간", ["top10", "top20", "top50", "top100", "전체"],
                                    index=4, key="coin_tier")
        rank_n = fc2.number_input("순위 ≤ (0=미적용)", min_value=0, value=0, step=10, key="coin_rank_n")
        mcap_min_m = fc3.number_input("시총 ≥ 백만$ (0=미적용)", min_value=0.0, value=0.0, step=100.0,
                                       key="coin_mcap_min")
        show_week_rs = fc4.checkbox("+주간RS", key="coin_col_weekrs")
        show_week_ret = fc5.checkbox("+주간수익", key="coin_col_weekret")
        show_month_ret = fc6.checkbox("+3·6·12개월", key="coin_col_monthret")

        with st.spinner("코인 시세·RS 조회 중..."):
            rank_df, rank_status = build_coin_ranking(COIN_UNIVERSE_POOL)

        _rank_errors = {
            "coingecko_fail": "CoinGecko 시세 조회에 실패했습니다. 잠시 후 다시 시도해주세요.",
            "binance_fail": "Binance 심볼 목록 조회에 실패했습니다. 잠시 후 다시 시도해주세요.",
            "no_match": "CoinGecko 상위 코인 중 Binance USDT 마켓이 있는 코인이 없습니다.",
            "no_data": "표시할 코인 데이터가 없습니다(전 종목 히스토리 조회 실패).",
        }
        if rank_status in _rank_errors:
            st.warning(f"⚠️ {_rank_errors[rank_status]}")
        elif rank_df.empty:
            st.info("표시할 코인 데이터가 없습니다.")
        else:
            work = rank_df.copy()
            _tier_map = {"top10": 10, "top20": 20, "top50": 50, "top100": 100, "전체": None}
            _tier_n = _tier_map[tier_label]
            if _tier_n is not None:
                work = work[work["market_cap_rank"] <= _tier_n]
            if rank_n > 0:
                work = work[work["market_cap_rank"] <= rank_n]
            if mcap_min_m > 0:
                work = work[work["market_cap"] >= mcap_min_m * 1e6]
            work = work.reset_index(drop=True)

            if work.empty:
                st.info("조건에 맞는 코인이 없습니다.")
            else:
                st.caption(f"{len(work)}개 코인 · 기본 정렬: 종합RS (표 헤더 클릭으로 재정렬 가능)")

                col_data = {
                    "#": work["#"], "이름": work["symbol"] + " · " + work["name"],
                    "가격": work["price"], "24h": work["chg_24h"], "7일": work["chg_7d"],
                    "30일": work["chg_30d"], "종합RS": work["rs_total"],
                    "시총": work["market_cap"], "거래량": work["volume"],
                    "7일 스파크라인": work["sparkline"],
                }
                if show_week_rs:
                    for w in (1, 2, 3, 4):
                        col_data[f"{w}주RS"] = work[f"rs_{w}w"]
                if show_week_ret:
                    for w in (1, 2, 3, 4):
                        col_data[f"{w}주수익"] = work[f"ret_{w}w"]
                if show_month_ret:
                    col_data["3개월"] = work["ret_3m"]
                    col_data["6개월"] = work["ret_6m"]
                    col_data["12개월"] = work["ret_12m"]

                disp = pd.DataFrame(col_data)
                pct_cols = [c for c in ["24h", "7일", "30일"]
                            + ([f"{w}주수익" for w in (1, 2, 3, 4)] if show_week_ret else [])
                            + (["3개월", "6개월", "12개월"] if show_month_ret else [])]

                styled = disp.style
                for c in pct_cols:
                    _cap = _abs_cap(disp[c], COIN_RET_COLOR_CAP)
                    styled = _apply_bg(styled, lambda v, cap=_cap: _color_scale_zero(v, cap), subset=[c])

                col_config = {
                    "#": st.column_config.NumberColumn(width="small"),
                    "가격": st.column_config.NumberColumn(format="$%.6g"),
                    "24h": st.column_config.NumberColumn(format="percent"),
                    "7일": st.column_config.NumberColumn(format="percent"),
                    "30일": st.column_config.NumberColumn(format="percent"),
                    "종합RS": st.column_config.NumberColumn(format="%.0f"),
                    "시총": st.column_config.NumberColumn(format="compact"),
                    "거래량": st.column_config.NumberColumn(format="compact"),
                    "7일 스파크라인": st.column_config.LineChartColumn(width="medium"),
                }
                for w in (1, 2, 3, 4):
                    col_config[f"{w}주RS"] = st.column_config.NumberColumn(format="%.0f")
                    col_config[f"{w}주수익"] = st.column_config.NumberColumn(format="percent")
                for c in ("3개월", "6개월", "12개월"):
                    col_config[c] = st.column_config.NumberColumn(format="percent")

                rank_ev = st.dataframe(
                    styled, use_container_width=True, hide_index=True, column_config=col_config,
                    height=min(60 + 35 * len(disp), 600),
                    on_select="rerun", selection_mode="single-row", key="coin_rank_table",
                )
                _sel_rows = rank_ev.selection.rows if rank_ev and rank_ev.selection else []
                if _sel_rows:
                    _sel_sym = work.iloc[_sel_rows[0]]["binance_symbol"]
                    if _sel_sym != st.session_state.coin_rank_symbol:
                        st.session_state.coin_rank_symbol = _sel_sym
                        st.rerun()

                chart_sym = st.session_state.coin_rank_symbol or work.iloc[0]["binance_symbol"]
                chart_row = work[work["binance_symbol"] == chart_sym]
                chart_label = (chart_row.iloc[0]["symbol"] + " · " + chart_row.iloc[0]["name"]
                               if not chart_row.empty else chart_sym)
                st.markdown(f"#### {chart_label}")
                cc1, cc2 = st.columns(2)
                chart_period = cc1.radio("기간", list(COIN_CHART_PERIOD_DAYS), index=0,
                                          horizontal=True, key="coin_rank_period")
                chart_iv_label = cc2.radio("봉", list(COIN_CHART_INTERVAL_MAP), index=0,
                                            horizontal=True, key="coin_rank_iv")
                _iv = COIN_CHART_INTERVAL_MAP[chart_iv_label]
                _bars_needed = {"1d": 366, "1w": 53, "1M": 13}[_iv] \
                    if chart_period == "1년" else \
                    ({"1d": 1096, "1w": 157, "1M": 37} if chart_period == "3년"
                     else {"1d": 1000, "1w": 1000, "1M": 1000})[_iv]
                candles = _fetch_binance_klines(chart_sym, _iv, _bars_needed)
                if not candles:
                    st.warning("차트 데이터를 불러오지 못했습니다.")
                else:
                    _render_price_chart(candles, key=f"lwc_coin_rank_{chart_sym}_{_iv}", intraday=False)

    # ---- 글로벌 24-7 ----------------------------------------------------------
    with sub_g247:
        st.info("ℹ️ 이 가격은 코인거래소(Hyperliquid)에서 형성되는 시세이며, 정규 거래소의 "
                "공식 가격이 아닌 참고용입니다. 유동성이 낮아 실제 거래소 가격과 괴리가 있을 수 있습니다.")
        st.caption("데이터: Hyperliquid 공개 API(xyz 퍼프 DEX — 주식·지수·원자재·환율 등 전통자산을 "
                   "24시간 거래). 5분 캐시.")

        cat_filter = st.selectbox("카테고리", HL_CATEGORIES, index=0, key="g247_cat")

        with st.spinner("Hyperliquid 시세 조회 중..."):
            g247_df, g247_status = build_global247()

        _g247_errors = {
            "hl_fail": "Hyperliquid 유니버스 조회에 실패했습니다. 잠시 후 다시 시도해주세요.",
            "no_data": "표시할 데이터가 없습니다(전 종목 캔들 조회 실패).",
        }
        if g247_status in _g247_errors:
            st.warning(f"⚠️ {_g247_errors[g247_status]}")
        elif g247_df.empty:
            st.info("표시할 데이터가 없습니다.")
        else:
            work247 = g247_df.copy()
            if cat_filter != "전체":
                work247 = work247[work247["category"] == cat_filter]
            work247 = work247.reset_index(drop=True)

            if work247.empty:
                st.info("이 카테고리에 해당하는 자산이 없습니다.")
            else:
                st.caption(f"{len(work247)}개 자산 · 24h 변동률 기준 정렬(표 헤더 클릭으로 재정렬 가능)")

                kr_name = work247["symbol"].map(lambda s: HL_SYMBOL_KOREAN_NAME.get(s))
                disp247 = pd.DataFrame({
                    "이름": [f"{s} · {k}" if k else s for s, k in zip(work247["symbol"], kr_name)],
                    "현재가": work247["price"], "1h": work247["chg_1h"], "24h": work247["chg_24h"],
                    "7일": work247["chg_7d"], "30일": work247["chg_30d"], "카테고리": work247["category"],
                })
                styled247 = disp247.style
                for c in ["1h", "24h", "7일", "30일"]:
                    _cap = _abs_cap(disp247[c], COIN_RET_COLOR_CAP)
                    styled247 = _apply_bg(styled247, lambda v, cap=_cap: _color_scale_zero(v, cap), subset=[c])

                g247_ev = st.dataframe(
                    styled247, use_container_width=True, hide_index=True,
                    column_config={
                        "현재가": st.column_config.NumberColumn(format="$%.6g"),
                        "1h": st.column_config.NumberColumn(format="percent"),
                        "24h": st.column_config.NumberColumn(format="percent"),
                        "7일": st.column_config.NumberColumn(format="percent"),
                        "30일": st.column_config.NumberColumn(format="percent"),
                    },
                    height=min(60 + 35 * len(disp247), 600),
                    on_select="rerun", selection_mode="single-row", key="coin_g247_table",
                )
                _sel247 = g247_ev.selection.rows if g247_ev and g247_ev.selection else []
                if _sel247:
                    _sel_hl = work247.iloc[_sel247[0]]["hl_symbol"]
                    if _sel_hl != st.session_state.coin_g247_symbol:
                        st.session_state.coin_g247_symbol = _sel_hl
                        st.rerun()

                g247_sym = st.session_state.coin_g247_symbol or work247.iloc[0]["hl_symbol"]
                g247_row = work247[work247["hl_symbol"] == g247_sym]
                g247_sym_short = g247_row.iloc[0]["symbol"] if not g247_row.empty else g247_sym
                g247_kr = HL_SYMBOL_KOREAN_NAME.get(g247_sym_short)
                st.markdown(f"#### {g247_sym_short}" + (f" · {g247_kr}" if g247_kr else ""))

                iv_label = st.radio("인터벌", ["1m", "5m", "15m", "1h", "4h", "1d"], index=3,
                                     horizontal=True, key="coin_g247_iv")
                _lookback_ms = {
                    "1m": 6 * 3600 * 1000, "5m": 24 * 3600 * 1000, "15m": 3 * 24 * 3600 * 1000,
                    "1h": 14 * 24 * 3600 * 1000, "4h": 45 * 24 * 3600 * 1000,
                    "1d": 365 * 24 * 3600 * 1000,
                }[iv_label]
                g247_candles = _fetch_hl_candles(g247_sym, iv_label, _lookback_ms)
                if not g247_candles:
                    st.warning("차트 데이터를 불러오지 못했습니다.")
                else:
                    _closed_mask = _us_market_closed_mask([c["time"] for c in g247_candles])
                    _render_price_chart(g247_candles, key=f"lwc_g247_{g247_sym}_{iv_label}",
                                         intraday=True, closed_mask=_closed_mask)
                    st.caption("회색 배경 = 미국 정규장(09:30~16:00 ET, 평일) 휴장 구간(공휴일 미반영 근사치)")

    # ---- 온체인 ---------------------------------------------------------------
    with sub_onchain:
        st.info("⛓️ 온체인 지표 탭은 준비 중입니다. 아래는 각 지표를 무료 API로 구할 수 있는지 "
                "조사한 결과입니다.")
        onchain_survey = pd.DataFrame([
            {"지표": "MVRV", "가능여부": "가능", "소스": "bitcoin-data.com (무료, 무인증)"},
            {"지표": "NUPL", "가능여부": "가능", "소스": "bitcoin-data.com"},
            {"지표": "SOPR", "가능여부": "가능", "소스": "bitcoin-data.com"},
            {"지표": "장기보유자 SOPR(LTH-SOPR)", "가능여부": "가능", "소스": "bitcoin-data.com"},
            {"지표": "푸엘 멀티플(Puell Multiple)", "가능여부": "가능", "소스": "bitcoin-data.com"},
            {"지표": "실현가격(Realized Price)", "가능여부": "가능", "소스": "bitcoin-data.com"},
            {"지표": "MVRV Z-Score", "가능여부": "가능", "소스": "bitcoin-data.com"},
            {"지표": "Pi Cycle Top", "가능여부": "자체계산", "소스": "가격(일봉)만으로 계산 가능 — 111일 SMA vs 350일 SMA×2"},
            {"지표": "Mayer Multiple", "가능여부": "자체계산", "소스": "가격 ÷ 200일 SMA, 가격만으로 계산 가능"},
            {"지표": "200주 이평(200W MA)", "가능여부": "자체계산", "소스": "가격(주봉)만으로 계산 가능"},
            {"지표": "해시레이트", "가능여부": "가능", "소스": "Blockchain.com Charts API(무료, 무인증)"},
            {"지표": "해시리본(Hash Ribbons)", "가능여부": "자체계산", "소스": "해시레이트의 30일/60일 SMA — Blockchain.com 원자료로 직접 계산"},
            {"지표": "활성주소(Active Addresses)", "가능여부": "가능", "소스": "Blockchain.com Charts API(n-unique-addresses)"},
            {"지표": "공포탐욕지수", "가능여부": "가능", "소스": "alternative.me Fear & Greed Index API(무료, 무인증)"},
            {"지표": "펀딩비(Funding Rate)", "가능여부": "가능", "소스": "Binance/Hyperliquid 공개 API(fundingRate) — 이미 이 앱이 쓰는 소스 재사용 가능"},
            {"지표": "미결제약정(Open Interest)", "가능여부": "가능", "소스": "Binance futures 공개 API(openInterest) — 무료, 무인증"},
            {"지표": "ETF 흐름(BTC ETF Flow)", "가능여부": "부분", "소스": "Farside Investors — 공식 API는 없고 웹페이지 스크레이핑 필요(구조 변경에 취약)"},
            {"지표": "김치프리미엄", "가능여부": "가능", "소스": "이미 프리미엄 탭에서 계산 중인 로직 재사용"},
            {"지표": "글로벌 M2", "가능여부": "가능", "소스": "FRED(M2SL 등, 무료 API키 필요·무료 발급) — 국가별 M2 합산은 직접 구성 필요"},
            {"지표": "CoinGlass 계열 지표 전반(청산맵 등)", "가능여부": "불가(무료로는)", "소스": "CoinGlass 공식 API는 유료 플랜부터 제공, 무료 티어는 웹 화면만"},
        ])
        st.dataframe(onchain_survey, use_container_width=True, hide_index=True,
                     height=min(60 + 35 * len(onchain_survey), 760))
        st.caption("가능=무료·무인증 API로 직접 조회 / 자체계산=가격 등 원자료만으로 이 앱에서 "
                   "계산 가능 / 부분=공식 API 없이 우회 방법(스크레이핑 등)만 존재 / 불가=무료로는 "
                   "확인한 방법이 없음. 구현은 아직 하지 않았고, 다음 라운드에서 우선순위를 정해 "
                   "진행하면 됩니다.")

st.divider()
st.caption("※ 규칙 기반 계산기이며 투자 자문이 아닙니다. "
           "배당 미반영 종가 기반이라 실제 성과와 차이가 날 수 있습니다. "
           "투자 판단과 책임은 본인에게 있습니다.")
