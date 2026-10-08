"""
나만의 투자 대시보드 — 차트 · 관심목록 · 동적자산배분 (탭 분리)
데이터: FinanceDataReader (국내/미국 주식·ETF·지수 + FRED). 서버 수집 → CORS 제약 없음.
실행:  streamlit run app.py   (같은 폴더에 strategies.py 필요)
"""
import contextlib
import hmac
import json
import math
import os
import re
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import unquote

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from streamlit.errors import StreamlitAPIException
import requests
import FinanceDataReader as fdr
import yfinance as yf
# 로컬 패치 래퍼: 줌 상태 복원 + dblclick 지원
from lwc_local import renderLightweightCharts

import wealth as W
from strategies import (STRATEGIES, TAA_TICKERS, augment_panel,
                         VOLDM_TICKERS, VOLDM_ASSET_META, voldm_source_type)

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
# 은퇴 탭 자산 데이터. 개인 자산이라 로컬 폴백 파일은 .gitignore에 등록돼 있다(커밋 금지).
WEALTH_FILE = "wealth.json"
WEALTH_GIST_FILENAME = "wealth.json"
MOLIT_APT_TRADE_URL = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade"
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
# Yahoo가 "NDX"를 404로 돌려주기 시작해(2026-10 확인) "^NDX"로 조회해야 한다. Gist/로컬에
# 이미 "NDX"로 저장된 목록이 있어 저장값은 그대로 두고 조회 직전에만 치환한다.
MA_SYMBOL_ALIASES = {"NDX": "^NDX"}
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
        df = fdr.DataReader(MA_SYMBOL_ALIASES.get(symbol, symbol), start)
    except Exception as e:
        print(f"[ma] 조회 실패: {symbol} {type(e).__name__}: {e}")
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
#  코인 탭 — 랭킹(CoinGecko + 일봉 히스토리 다중소스) / 글로벌 24-7(Hyperliquid)
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


@st.cache_data(ttl=60 * 60)
def _fetch_binance_klines(symbol, interval, limit=500):
    """상단 차트(기간/봉 전환) 전용 — Binance 직접 호출 + data-api.binance.vision
    폴백. 랭킹 표의 RS·주간/월간 수익률 계산은 이 함수를 쓰지 않고 아래
    _fetch_coin_daily_history의 다중소스 체인을 쓴다(이유는 그 함수 docstring)."""
    for host in ("https://api.binance.com", "https://data-api.binance.vision"):
        try:
            r = requests.get(
                f"{host}/api/v3/klines",
                params={"symbol": symbol, "interval": interval, "limit": min(limit, 1000)},
                timeout=15,
            )
        except requests.exceptions.RequestException as e:
            print(f"[coin] 차트용 klines 실패({host}, {symbol}): {type(e).__name__}: {e}")
            continue
        if r.status_code != 200:
            print(f"[coin] 차트용 klines 실패({host}, {symbol}): HTTP {r.status_code} {r.text[:150]}")
            continue
        rows = r.json()
        if not rows:
            continue
        return [{"time": pd.to_datetime(row[0], unit="ms"), "open": float(row[1]),
                  "high": float(row[2]), "low": float(row[3]), "close": float(row[4]),
                  "volume": float(row[5])} for row in rows]
    return []


def _lookback_return(closes, days_back):
    n = len(closes)
    idx = n - 1 - days_back
    if idx < 0:
        return None
    base = closes[idx]
    return (closes[-1] / base - 1.0) if base else None


def _log_coin_fetch_fail(source, detail):
    print(f"[coin] {source} 실패: {detail}")


def _fetch_daily_binance_style(host, base_symbol, source_name, limit=400):
    """Binance(api.binance.com)/Binance 공개 데이터 미러(data-api.binance.vision)
    공용 파서 — 응답 스키마가 동일하다."""
    sym = base_symbol + "USDT"
    try:
        r = requests.get(f"{host}/api/v3/klines",
                          params={"symbol": sym, "interval": "1d", "limit": min(limit, 1000)},
                          timeout=15)
    except requests.exceptions.Timeout:
        _log_coin_fetch_fail(source_name, f"{sym} 타임아웃")
        return None
    except requests.exceptions.RequestException as e:
        _log_coin_fetch_fail(source_name, f"{sym} {type(e).__name__}: {e}")
        return None
    if r.status_code != 200:
        # 미국 IP를 막는 지역제한이면 보통 451, 과호출이면 429로 온다 — 로그에서 구분되게 남긴다.
        _log_coin_fetch_fail(source_name, f"{sym} HTTP {r.status_code}: {r.text[:150]}")
        return None
    rows = r.json()
    if not rows:
        _log_coin_fetch_fail(source_name, f"{sym} 빈 응답")
        return None
    return [{"time": pd.to_datetime(row[0], unit="ms"), "close": float(row[4])} for row in rows]


def _fetch_daily_coinbase(base_symbol):
    """Coinbase Exchange candles — 이미 프리미엄 탭에서 쓰는 Coinbase API와 같은 회사,
    검증된 소스. granularity=86400(일봉), 응답은 [time,low,high,open,close,volume]
    최신순이라 시간순으로 다시 정렬한다."""
    sym = f"{base_symbol}-USD"
    try:
        r = requests.get(f"https://api.exchange.coinbase.com/products/{sym}/candles",
                          params={"granularity": 86400}, timeout=15,
                          headers={"User-Agent": "easyinvest/1.0"})
    except requests.exceptions.Timeout:
        _log_coin_fetch_fail("Coinbase", f"{sym} 타임아웃")
        return None
    except requests.exceptions.RequestException as e:
        _log_coin_fetch_fail("Coinbase", f"{sym} {type(e).__name__}: {e}")
        return None
    if r.status_code != 200:
        _log_coin_fetch_fail("Coinbase", f"{sym} HTTP {r.status_code}: {r.text[:150]}")
        return None
    rows = r.json()
    if not rows:
        _log_coin_fetch_fail("Coinbase", f"{sym} 빈 응답")
        return None
    rows = sorted(rows, key=lambda row: row[0])
    return [{"time": pd.to_datetime(row[0], unit="s"), "close": float(row[4])} for row in rows]


def _fetch_daily_bybit(base_symbol):
    sym = base_symbol + "USDT"
    try:
        r = requests.get("https://api.bybit.com/v5/market/kline",
                          params={"category": "spot", "symbol": sym, "interval": "D", "limit": 400},
                          timeout=15)
    except requests.exceptions.Timeout:
        _log_coin_fetch_fail("Bybit", f"{sym} 타임아웃")
        return None
    except requests.exceptions.RequestException as e:
        _log_coin_fetch_fail("Bybit", f"{sym} {type(e).__name__}: {e}")
        return None
    if r.status_code != 200:
        _log_coin_fetch_fail("Bybit", f"{sym} HTTP {r.status_code}: {r.text[:150]}")
        return None
    data = r.json()
    rows = (data.get("result") or {}).get("list") or []
    if not rows:
        _log_coin_fetch_fail("Bybit", f"{sym} 빈 응답({data.get('retMsg')})")
        return None
    rows = sorted(rows, key=lambda row: int(row[0]))
    return [{"time": pd.to_datetime(int(row[0]), unit="ms"), "close": float(row[4])} for row in rows]


def _fetch_daily_okx(base_symbol):
    sym = f"{base_symbol}-USDT"
    try:
        r = requests.get("https://www.okx.com/api/v5/market/candles",
                          params={"instId": sym, "bar": "1D", "limit": 300}, timeout=15)
    except requests.exceptions.Timeout:
        _log_coin_fetch_fail("OKX", f"{sym} 타임아웃")
        return None
    except requests.exceptions.RequestException as e:
        _log_coin_fetch_fail("OKX", f"{sym} {type(e).__name__}: {e}")
        return None
    if r.status_code != 200:
        _log_coin_fetch_fail("OKX", f"{sym} HTTP {r.status_code}: {r.text[:150]}")
        return None
    data = r.json()
    rows = data.get("data") or []
    if not rows:
        _log_coin_fetch_fail("OKX", f"{sym} 빈 응답({data.get('msg')})")
        return None
    rows = sorted(rows, key=lambda row: int(row[0]))
    return [{"time": pd.to_datetime(int(row[0]), unit="ms"), "close": float(row[4])} for row in rows]


def _fetch_daily_coingecko_market_chart(coin_id, days=365):
    """CoinGecko market_chart — 최후 폴백. 무료 티어 레이트리밋에 걸리기 쉬워서
    코인당 최대 1회만 호출되게(다른 소스가 전부 실패한 경우에만) 체인 맨 끝에 둔다.
    days=400을 넘겨봤더니 "요청 범위가 허용 범위를 초과"(401)로 거부됐다 — 무료
    티어는 최근 365일까지만 일 단위 히스토리를 준다(그 이상은 유료). 365일이면
    RS(1~4주)·3·6개월 수익률은 그대로 되고, 12개월 수익률만 한 칸 모자라 계산이
    안 될 수 있다(그 경우 12개월 칸만 비게 됨)."""
    if not coin_id:
        return None
    try:
        r = requests.get(f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart",
                          params={"vs_currency": "usd", "days": days, "interval": "daily"}, timeout=15)
    except requests.exceptions.Timeout:
        _log_coin_fetch_fail("CoinGecko", f"{coin_id} 타임아웃")
        return None
    except requests.exceptions.RequestException as e:
        _log_coin_fetch_fail("CoinGecko", f"{coin_id} {type(e).__name__}: {e}")
        return None
    if r.status_code != 200:
        _log_coin_fetch_fail("CoinGecko", f"{coin_id} HTTP {r.status_code}: {r.text[:150]}")
        return None
    prices = r.json().get("prices") or []
    if not prices:
        _log_coin_fetch_fail("CoinGecko", f"{coin_id} 빈 응답")
        return None
    return [{"time": pd.to_datetime(ts, unit="ms"), "close": float(p)} for ts, p in prices]


@st.cache_data(ttl=60 * 60)
def _fetch_coin_daily_history(base_symbol, coin_id):
    """일봉 종가 히스토리를 다중 소스 폴백 체인으로 받는다: (closes_rows, 소스이름)
    반환, 전부 실패하면 (None, None).

    Binance(api.binance.com)는 미국 IP를 지역차단해 HTTP 451을 반환하는 사례가
    있다 — Streamlit Community Cloud는 AWS 미국 리전에서 돈다. 로컬(한국)에서는
    되는데 배포본에서만 "Binance 조회 실패"가 나는 게 바로 이 증상이었다. 그래서
    Binance 하나에 의존하지 않고 아래 순서로 첫 성공을 쓴다:
    1) Binance 직접 — 로컬 등 차단 안 된 환경에서는 여기서 바로 성공.
    2) data-api.binance.vision — Binance가 만든 별도 공개 데이터 미러. 지역차단
       정책이 api.binance.com과 다를 수 있어 시도해볼 가치가 있음(직접 검증은
       못 했고, 실패하면 바로 다음으로 넘어가므로 안전).
    3) Coinbase Exchange candles — 프리미엄 탭에서 이미 쓰는 Coinbase사 API라
       접근성 검증됨.
    4) Bybit 공개 스팟 kline.
    5) OKX 공개 candles.
    6) CoinGecko market_chart — 시세용으로 이미 쓰는 소스라 요청 자체는 늘 되지만
       무료 티어 레이트리밋이 낮아 진짜 마지막에만 쓴다.
    각 시도의 실패 사유(HTTP 상태코드/응답 본문/예외 종류)는 print()로 로그에
    남긴다 — Streamlit Cloud의 "Manage app" 로그에서 그대로 보인다."""
    attempts = [
        ("Binance", lambda: _fetch_daily_binance_style("https://api.binance.com", base_symbol, "Binance")),
        ("data-api.binance.vision",
         lambda: _fetch_daily_binance_style("https://data-api.binance.vision", base_symbol,
                                             "data-api.binance.vision")),
        ("Coinbase", lambda: _fetch_daily_coinbase(base_symbol)),
        ("Bybit", lambda: _fetch_daily_bybit(base_symbol)),
        ("OKX", lambda: _fetch_daily_okx(base_symbol)),
        ("CoinGecko", lambda: _fetch_daily_coingecko_market_chart(coin_id)),
    ]
    for name, fn in attempts:
        rows = fn()
        if rows and len(rows) >= 29:
            return rows, name
    return None, None


def _coingecko_pct(m, key):
    # CoinGecko는 %(예: -1.2)로 주는데 앱 전체 관례(RET_COLOR_CAP 등)는 소수
    # 비율(예: -0.012)이라 여기서 100으로 나눠 맞춘다.
    v = m.get(key)
    return v / 100.0 if v is not None else None


@st.cache_data(ttl=300)
def build_coin_ranking(pool_size=COIN_UNIVERSE_POOL):
    """(결과 DataFrame, 상태, 출처별 코인수) 반환. 상태는 "ok" 또는 "coingecko_fail"
    (CoinGecko 자체가 죽어 표시할 시세가 하나도 없는 경우)뿐이다 — 일봉 히스토리
    (RS·주간/월간 수익률) 조회가 코인별로 실패해도 그 코인의 현재가·24h·7일·30일·
    시총·거래량·스파크라인은 CoinGecko 데이터만으로 정상 표시하고, RS·주간/월간
    관련 열만 비워둔다(표 전체가 사라지지 않게).

    RS(상대강도) 계산: 1·2·3·4주 수익률 각각을 이 유니버스(풀) 내에서 백분위
    (0~100, pandas rank(pct=True))로 바꾼 뒤, COIN_RS_WEEK_WEIGHTS 가중평균을
    "종합RS"로 쓴다. 가중치는 최근 주(1주 전)에 더 크게 둬서 최근 모멘텀을 더
    반영한다 — 절대수익률이 아니라 "같은 시점 다른 코인들 대비 얼마나 잘했는가"를
    보는 것이 RS의 취지라, 반드시 유니버스 내부 비교(percentile)로 계산한다.

    기본 정렬은 종합RS가 아니라 시총 순위(market_cap_rank) 오름차순 — 원사이트처럼
    BTC·ETH·XRP... 순으로 뜨게 한다. "#"열도 시총 순위값 그대로 담아서, 화면에서
    표 헤더를 클릭해 다른 기준(종합RS 등)으로 재정렬해도 "#"이 1,2,3으로 다시
    매겨지지 않고 그 코인의 시총 순위를 계속 보여준다."""
    markets = _fetch_coingecko_markets(pool_size)
    if not markets:
        return pd.DataFrame(), "coingecko_fail", {}

    def _fetch_row(m):
        base = m["symbol"].upper()
        history, source = _fetch_coin_daily_history(base, m.get("id"))
        row = {
            "symbol": base, "name": m["name"], "coin_id": m.get("id"),
            "price": m["current_price"],
            "chg_24h": _coingecko_pct(m, "price_change_percentage_24h_in_currency"),
            "chg_7d": _coingecko_pct(m, "price_change_percentage_7d_in_currency"),
            "chg_30d": _coingecko_pct(m, "price_change_percentage_30d_in_currency"),
            "market_cap": m.get("market_cap"), "market_cap_rank": m.get("market_cap_rank"),
            "volume": m.get("total_volume"),
            "sparkline": (m.get("sparkline_in_7d") or {}).get("price") or [],
            "ret_1w": None, "ret_2w": None, "ret_3w": None, "ret_4w": None,
            "ret_3m": None, "ret_6m": None, "ret_12m": None,
            "history_source": source,
        }
        if history:
            closes = [c["close"] for c in history]
            rets_week = [_lookback_return(closes, d) for d in (7, 14, 21, 28)]
            if all(v is not None for v in rets_week):
                row["ret_1w"], row["ret_2w"], row["ret_3w"], row["ret_4w"] = rets_week
            row["ret_3m"] = _lookback_return(closes, 90)
            row["ret_6m"] = _lookback_return(closes, 182)
            row["ret_12m"] = _lookback_return(closes, 365)
        return row

    with ThreadPoolExecutor(max_workers=10) as exe:
        rows = list(exe.map(_fetch_row, markets))

    df = pd.DataFrame(rows)
    # CoinGecko가 주는 market_cap_rank 필드를 그대로 믿지 않고 실제 market_cap 값으로
    # 우리가 다시 매긴다 — 확인해보니 그 필드에 동률(예: 두 코인이 똑같이 9위)과
    # 스냅샷이 어긋난 역전(더 큰 시총인데 순위 숫자는 더 나쁘게 나오는 경우)이
    # 실제로 있었다. min 방식(동률은 같은 순위, 다음 순위는 그만큼 건너뜀)+시총
    # 없는 코인은 최하위로 보낸다.
    df["market_cap_rank"] = (df["market_cap"].rank(method="min", ascending=False, na_option="bottom")
                              .astype(int))
    for w in (1, 2, 3, 4):
        df[f"rs_{w}w"] = df[f"ret_{w}w"].rank(pct=True) * 100
    wsum = sum(COIN_RS_WEEK_WEIGHTS.values())
    week_cols = [f"ret_{w}w" for w in (1, 2, 3, 4)]
    has_all_weeks = df[week_cols].notna().all(axis=1)
    df["rs_total"] = np.nan
    df.loc[has_all_weeks, "rs_total"] = sum(
        df.loc[has_all_weeks, f"rs_{w}w"] * wt for w, wt in COIN_RS_WEEK_WEIGHTS.items()) / wsum
    # 기본 정렬·"#"열은 시총 순위(원사이트와 동일한 BTC·ETH·XRP... 순). RS 기준
    # 정렬은 표 헤더를 클릭해 쓰면 되고("#"은 그 행의 시총 순위 그대로 유지돼
    # 다시 매겨지지 않는다), 정렬을 초기화하면 이 기본 순서(시총순)로 돌아온다.
    df = df.sort_values("market_cap_rank", ascending=True, na_position="last").reset_index(drop=True)
    df.insert(0, "#", df["market_cap_rank"])

    source_counts = df["history_source"].fillna("실패").value_counts().to_dict()
    return df, "ok", source_counts


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


def _month_diff(a, b):
    return (b.year - a.year) * 12 + (b.month - a.month)


def _mdd_detail(mr):
    """월간 수익률 시리즈 → MDD 구간(전고점·저점·회복월). 월말 기준이라 월중 낙폭은 안 잡힌다.
    첫 달부터 하락하는 경우도 잡히도록 시작 직전 월말을 1.0으로 두고 계산한다."""
    if mr is None or len(mr) == 0:
        return None
    start = mr.index[0] - pd.offsets.MonthEnd(1)
    eq = pd.concat([pd.Series([1.0], index=[start]), (1 + mr).cumprod()])
    dd = eq / eq.cummax() - 1
    trough = dd.idxmin()
    if dd.loc[trough] >= 0:
        return {"mdd": 0.0, "peak": None, "trough": None, "recovery": None}
    peak = eq.loc[:trough].idxmax()
    after = eq.loc[trough:]
    rec = after[after >= eq.loc[peak]]
    return {"mdd": float(dd.loc[trough]), "peak": peak, "trough": trough,
            "recovery": rec.index[0] if len(rec) else None, "last": eq.index[-1]}


def _ytd_stats(mr):
    """올해 들어 현재까지의 누적 수익률과 그 구간의 MDD (연환산하지 않음)."""
    year = pd.Timestamp.now(tz="Asia/Seoul").year
    y = mr[mr.index.year == year] if mr is not None else None
    if y is None or len(y) == 0:
        return None, None
    return float((1 + y).prod() - 1), _mdd_detail(y)["mdd"]


def backtest(fn, mp, ctx):
    n = len(mp)
    rets, dates, held = [], [], []
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
        # t월 말 신호로 정한 비중을 t+1월 동안 보유 → mret과 같은 날짜(t+1)에 묶어 저장
        held.append({"signal": mp.index[t], "weights": dict(w)})
    if not rets:
        return None
    mr = pd.Series(rets, index=dates)
    equity = (1 + mr).cumprod()
    yrs = len(mr) / 12
    cagr = equity.iloc[-1] ** (1 / yrs) - 1 if yrs > 0 else np.nan
    mdd = (equity / equity.cummax() - 1).min()
    sharpe = (mr.mean() * 12) / (mr.std() * np.sqrt(12)) if mr.std() > 0 else np.nan
    # 패널의 마지막 행이 아직 끝나지 않은 이번 달이면 그 행은 월중 가격이라 신호가 매일
    # 바뀐다 — 현재 포지션은 마지막으로 "마감된" 월말 신호로 고정해 다음 달 1일까지 유지한다.
    # (Cloud는 UTC라 한국 날짜 기준으로 판정)
    now_kst = pd.Timestamp.now(tz="Asia/Seoul")
    last = mp.index[-1]
    last_sig = n - 2 if (last.year, last.month) == (now_kst.year, now_kst.month) else n - 1
    recent_positions = []
    for i in range(3):
        idx = last_sig - i
        if idx < 0:
            break
        recent_positions.append((mp.index[idx], fn(mp, idx, ctx) or {}))
    return {"equity": equity, "mret": mr, "cagr": cagr, "mdd": mdd,
            "sharpe": sharpe,
            "current": (fn(mp, last_sig, ctx) or {}) if last_sig >= 0 else {},
            "recent_positions": recent_positions,
            "held": pd.Series(held, index=dates, dtype=object)}


# 결과 딕셔너리 구조(backtest()의 반환 키)가 바뀌면 이 값을 올려서
# st.cache_data에 남아있는 구버전 캐시를 무효화한다.
TAA_RESULTS_VERSION = 5


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
        except Exception as e:
            # 전략 하나가 실패해도 나머지는 계속 계산 — 사유는 화면에 띄울 수 있게 남긴다.
            print(f"[taa] '{name}' 백테스트 실패:\n{traceback.format_exc()}")
            res[name] = {"error": f"{type(e).__name__}: {e}"}
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
#  은퇴 탭 (총자산 · 경제적 자유) — 계산은 wealth.py, 여기는 저장·외부조회·화면
# ============================================================================
def load_wealth():
    """(doc, mode, error) 반환. 관심목록과 달리 조회 실패 시 기본값으로 넘어가지 않고 error를
    돌려준다 — 빈 화면을 열어 둔 채 저장해서 실제 자산 데이터를 덮어쓰는 사고를 막기 위해서다."""
    local = None
    if os.path.exists(WEALTH_FILE):
        try:
            with open(WEALTH_FILE, encoding="utf-8") as f:
                local = json.load(f)
        except Exception as e:
            return None, "local", f"로컬 저장 파일({WEALTH_FILE})을 읽지 못했습니다: {type(e).__name__}"
    if _gist_configured():
        token, gist_id = st.secrets["gist"]["token"], st.secrets["gist"]["id"]
        try:
            content = _gist_fetch_content(token, gist_id, WEALTH_GIST_FILENAME)
        except Exception as e:
            print(f"[wealth] Gist 조회 실패: {type(e).__name__}")
            return None, "gist", "저장소(Gist) 조회에 실패했습니다."
        if content and content.strip():
            try:
                return W.normalize_doc(json.loads(content)), "gist", None
            except Exception:
                return None, "gist", "저장된 데이터 형식이 올바르지 않습니다."
        # Gist에 아직 저장된 적 없음 → 로컬 파일이 있으면 그걸 초기값으로 쓴다
        return W.normalize_doc(local), "gist", None
    return W.normalize_doc(local), "local", None


def save_wealth(doc, mode):
    """저장 성공 여부 반환. 자산 데이터는 저장 실패를 조용히 넘기지 않는다."""
    body = json.dumps(doc, ensure_ascii=False, indent=2)
    if mode == "gist":
        try:
            token, gist_id = st.secrets["gist"]["token"], st.secrets["gist"]["id"]
            r = requests.patch(f"https://api.github.com/gists/{gist_id}",
                               headers=_gist_headers(token),
                               json={"files": {WEALTH_GIST_FILENAME: {"content": body}}}, timeout=15)
            r.raise_for_status()
            _gist_fetch_content.clear()
            return True
        except Exception as e:
            print(f"[wealth] Gist 저장 실패: {type(e).__name__}")
            return False
    try:
        with open(WEALTH_FILE, "w", encoding="utf-8") as f:
            f.write(body)
        return True
    except Exception as e:
        print(f"[wealth] 로컬 저장 실패: {type(e).__name__}: {e}")
        return False


def _wealth_secret(section, key):
    try:
        v = st.secrets.get(section)
        return (v.get(key) if hasattr(v, "get") else v) or ""
    except Exception:
        return ""


@st.cache_data(ttl=60 * 60 * 24)
def get_kr_etf_options():
    try:
        df = fdr.StockListing("ETF/KR")
        return sorted(f"{n} ({str(c).zfill(6)})" for c, n in zip(df["Symbol"], df["Name"]))
    except Exception as e:
        print(f"[wealth] ETF 목록 조회 실패: {type(e).__name__}: {e}")
        return []


@st.cache_data(ttl=60 * 30, show_spinner=False)
def _wealth_price_rows(market, symbol):
    """최근 일봉의 (날짜, 원본 종가) 목록. 보유수량 평가용이라 수정주가(Adj Close)가 아니라
    Close를 쓴다. allowlist(형식 검증)를 통과한 심볼만 조회한다. 실패 시 None."""
    if not W.valid_symbol(market, symbol):
        return None
    try:
        if market == "CRYPTO":
            df = yf.download(W.CRYPTO_CATALOG[symbol][1], period="1mo",
                             auto_adjust=False, progress=False)
            close = df["Close"] if not df.empty else pd.Series(dtype=float)
            if isinstance(close, pd.DataFrame):
                close = close.iloc[:, 0]
        else:
            start = (datetime.today() - timedelta(days=30)).strftime("%Y-%m-%d")
            close = fdr.DataReader(symbol, start)["Close"]
    except Exception as e:
        print(f"[wealth] 가격 조회 실패: {market}:{symbol} {type(e).__name__}: {e}")
        return None
    rows = [(d.date(), float(c)) for d, c in close.items()]
    return rows or None


@st.cache_data(ttl=60 * 30, show_spinner=False)
def _wealth_fx_rows():
    try:
        start = (datetime.today() - timedelta(days=30)).strftime("%Y-%m-%d")
        close = fdr.DataReader("USD/KRW", start)["Close"]
        return [(d.date(), float(c)) for d, c in close.items()]
    except Exception as e:
        print(f"[wealth] 환율 조회 실패: {type(e).__name__}: {e}")
        return []


def _wealth_quotes(doc):
    """보유 종목 전체의 확정 종가 시세. (quotes, 저장된 정상 시세가 갱신됐는지)."""
    now_kst = pd.Timestamp.now(tz="Asia/Seoul")
    today = now_kst.date()
    # 진행 중인 봉 제외: 국내는 한국 당일, 미국은 한국 시각 오전 7시 전이면 전날 봉도 아직
    # 장중(미 동부 16시 마감 = 한국 05~06시), 코인은 UTC 일봉이라 UTC 당일.
    cutoffs = {"KR": today, "US": (now_kst - pd.Timedelta(hours=7)).date(),
               "CRYPTO": pd.Timestamp.now(tz="UTC").date()}
    keys = {(h["market"], h["symbol"])
            for sec, _, kind in W.SECTIONS if kind
            for h in doc["financial"][sec]["holdings"]}
    if not keys:
        return {}, False
    with ThreadPoolExecutor(max_workers=8) as ex:
        rows_by_key = dict(zip(keys, ex.map(lambda k: _wealth_price_rows(*k), keys)))
    fx_rows = _wealth_fx_rows() if any(m != "KR" for m, _ in keys) else []
    quotes, changed = {}, False
    for (market, symbol), rows in rows_by_key.items():
        qk = W.quote_key(market, symbol)
        currency = "KRW" if market == "KR" else "USD"
        picked = W.pick_confirmed_close(rows or [], cutoffs[market])
        fx = W.pick_fx(fx_rows, picked[0], today) if (picked and currency == "USD") else None
        if picked and (currency == "KRW" or fx):
            q = {"close": picked[1], "date": picked[0].isoformat(), "currency": currency,
                 "fx": fx[1] if fx else None, "fx_date": fx[0].isoformat() if fx else None}
            if doc["last_quotes"].get(qk) != q:
                doc["last_quotes"][qk] = dict(q)
                changed = True
            quotes[qk] = dict(q, status="ok")
        elif qk in doc["last_quotes"]:
            quotes[qk] = dict(doc["last_quotes"][qk], status="stale")
        else:
            quotes[qk] = {"close": picked[1] if picked else None,
                          "date": picked[0].isoformat() if picked else None,
                          "currency": currency, "fx": None, "fx_date": None, "status": "missing"}
    return quotes, changed


@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def _molit_month(lawd_cd, ym, _day):
    """국토교통부 아파트 매매 실거래가 한 달치(일별 캐시). 실패는 예외로 올려 캐시에 남기지 않는다.
    인증키는 인자로 받지 않고(캐시 키·로그 노출 방지) 여기서만 읽으며, 요청 예외 메시지에는
    키가 든 URL이 들어 있으므로 그대로 출력하지 않는다."""
    key = _wealth_secret("MOLIT_SERVICE_KEY", "")
    trades, page = [], 1
    while True:
        try:
            r = requests.get(MOLIT_APT_TRADE_URL, timeout=20, params={
                "serviceKey": unquote(key), "LAWD_CD": lawd_cd, "DEAL_YMD": ym,
                "pageNo": page, "numOfRows": 1000})
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"{ym} 요청 실패({type(e).__name__})") from None
        if r.status_code != 200:
            raise RuntimeError(f"{ym} HTTP {r.status_code}")
        got, err = W.parse_molit_xml(r.text)
        if err:
            raise RuntimeError(f"{ym} {err}")
        trades += got
        if len(got) < 1000 or page >= 5:
            return trades
        page += 1


def _wealth_property_trades(prop, today):
    """최근 12개월 실거래 매칭 결과. {'configured','match','errors'}"""
    if not _wealth_secret("MOLIT_SERVICE_KEY", ""):
        return {"configured": False, "match": None, "errors": []}
    lawd = str(prop.get("lawd_cd") or "").strip()
    if not (re.fullmatch(r"\d{5}", lawd) and prop.get("trade_complex") and prop.get("dong")
            and float(prop.get("area_m2") or 0) > 0):
        return {"configured": True, "match": None,
                "errors": ["지역코드(5자리)·실거래가 조회 단지명·법정동·전용면적을 입력해야 조회합니다."]}

    def _one(ym):
        try:
            return _molit_month(lawd, ym, today.isoformat()), None
        except Exception as e:
            print(f"[wealth] 실거래가 조회 실패: {e}")
            return [], str(e)

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(_one, W.recent_months(today, 12)))
    trades = [t for got, _ in results for t in got]
    errors = [e for _, e in results if e]
    match = W.match_trades(trades, prop["trade_complex"], prop["dong"], prop["area_m2"], today)
    return {"configured": True, "match": match, "errors": errors}


def _wealth_rerun():
    """은퇴 탭만 다시 그린다. 탭 안의 입력으로 시작된 실행이 아니면(페이지 첫 로딩 등)
    fragment 범위 재실행이 허용되지 않아 전체 재실행으로 넘어간다."""
    try:
        st.rerun(scope="fragment")
    except StreamlitAPIException:
        st.rerun()


# st.tabs는 모든 탭 코드를 매번 실행하므로, fragment로 떼어 놓지 않으면 이 탭의 입력 하나가
# 앞의 7개 탭을 전부 다시 돌린다(저장 후 재실행까지 두 번).
@st.fragment
def _render_wealth_tab():
    fmt = W.format_krw
    st.caption("총자산 · 경제적 자유 — 다른 탭의 관심목록과는 별도로 관리하는 자산 목록입니다. "
               "금액은 만원 단위로 입력하고 원 단위로 저장합니다. 바꾼 값은 바로 저장됩니다.")

    pw = _wealth_secret("wealth", "password")
    if pw and not st.session_state.get("wealth_unlocked"):
        entered = st.text_input("은퇴 탭 비밀번호", type="password", key="wealth_pw")
        if entered:
            if hmac.compare_digest(entered.encode(), str(pw).encode()):
                st.session_state.wealth_unlocked = True
                _wealth_rerun()
            st.error("비밀번호가 맞지 않습니다.")
        return

    if "wealth_doc" not in st.session_state:
        loaded, load_mode, load_err = load_wealth()
        if load_err:
            st.error(f"{load_err} 기존 데이터를 덮어쓰지 않도록 이 탭을 열지 않았습니다.")
            if st.button("다시 시도", key="wealth_retry"):
                _gist_fetch_content.clear()
                _wealth_rerun()
            return
        st.session_state.wealth_doc, st.session_state.wealth_mode = loaded, load_mode
        st.session_state.wealth_rev = 0
    doc, mode = st.session_state.wealth_doc, st.session_state.wealth_mode
    rev = st.session_state.wealth_rev
    prop, fin, fire = doc["property"], doc["financial"], doc["fire"]
    today = pd.Timestamp.now(tz="Asia/Seoul").date()

    def K(name):
        # 가져오기·실거래가 반영처럼 화면 밖에서 값이 바뀌면 rev를 올려 입력칸을 새 값으로 다시 만든다
        return f"w{rev}_{name}"

    def commit(reset_inputs=False):
        ok = save_wealth(doc, mode)
        st.session_state.wealth_save_err = not ok
        st.session_state.wealth_saved_at = pd.Timestamp.now(tz="Asia/Seoul").strftime("%H:%M:%S")
        if reset_inputs:
            st.session_state.wealth_rev = rev + 1
        _wealth_rerun()

    def won_input(label, name, won, help=None):
        man = st.number_input(f"{label} (만원)", min_value=0, value=W.won_to_man(won), step=100,
                              key=K(name), help=help)
        st.caption(W.korean_amount(W.man_to_won(man)))
        return W.apply_man_input(won, man)

    if not pw:
        st.caption("🔓 비밀번호 미설정 — 이 앱 주소를 아는 사람은 누구나 이 탭을 보고 수정할 수 있습니다. "
                   "secrets에 `[wealth] password = \"...\"`를 넣으면 잠깁니다.")
    if st.session_state.get("wealth_save_err"):
        st.error("저장에 실패했습니다. 화면의 값이 저장소에 반영되지 않았을 수 있습니다.")
    elif st.session_state.get("wealth_saved_at"):
        st.caption(f"💾 마지막 저장 {st.session_state.wealth_saved_at}"
                   + (" · Gist" if mode == "gist" else " · 이 기기(로컬 파일)"))

    # ---- 시세·실거래가 ----------------------------------------------------
    quotes, quotes_changed = _wealth_quotes(doc)
    trade_info = _wealth_property_trades(prop, today)
    tm = trade_info["match"]
    if (tm and not trade_info["errors"]
            and W.should_apply_trade(tm["date"], prop.get("acquired_on"), prop.get("value_asof"))
            and (tm["price"] != int(prop.get("value") or 0)
                 or tm["date"].isoformat() != prop.get("value_asof"))):
        prop.update(value=tm["price"], value_asof=tm["date"].isoformat(), value_source="molit")
        commit(reset_inputs=True)
    if quotes_changed:
        save_wealth(doc, mode)   # 장애 때 쓸 정상 시세 보관(종목당 하루 한 번꼴)

    s = W.summarize(doc, quotes)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("총자산", fmt(s["total"]))
    c2.metric("총부채", fmt(s["debt"]))
    c3.metric("순자산", fmt(s["net"]))
    c4.metric("금융 순자산", fmt(s["financial_net"]))
    if s["status"] == "incomplete":
        st.warning("가격 또는 환율을 받지 못한 종목이 있어 평가가 미완료입니다. 해당 종목은 합계에서 "
                   "빠져 있으며, 현재 금액 기록 저장은 막혀 있습니다.")
    elif s["status"] == "stale":
        st.warning("일부 종목은 시세 조회에 실패해 이전에 저장된 가격으로 평가했습니다(stale). "
                   "현재 금액 기록 저장은 막혀 있습니다.")

    dirty = False

    # ---- 부동산 -----------------------------------------------------------
    with st.expander(f"🏠 부동산 · 평가액 {fmt(s['property'])} · 부채 {fmt(s['property_debt'])}",
                     key="wealth_exp_property"):
        new = dict(prop)
        a1, a2 = st.columns(2)
        new["address"] = a1.text_input("주소", prop.get("address") or "", key=K("p_address"))
        new["complex"] = a2.text_input("단지", prop.get("complex") or "", key=K("p_complex"))
        b1, b2, b3, b4 = st.columns(4)
        new["pyeong"] = b1.text_input("평형", str(prop.get("pyeong") or ""), key=K("p_pyeong"))
        new["area_m2"] = b2.number_input("전용면적 (㎡)", min_value=0.0, step=1.0, format="%.2f",
                                          value=float(prop.get("area_m2") or 0), key=K("p_area"))
        new["acquired_on"] = b3.date_input("취득일", W.to_date(prop.get("acquired_on")),
                                            max_value=today, key=K("p_acq_on"))
        new["value_asof"] = b4.date_input("평가 기준일", W.to_date(prop.get("value_asof")),
                                           max_value=today, key=K("p_asof"))
        m1, m2 = st.columns(2)
        with m1:
            new["acquired_price"] = won_input("취득금액", "p_acq_price", prop.get("acquired_price"))
            new["mortgage"] = won_input("담보대출", "p_mortgage", prop.get("mortgage"))
        with m2:
            new["value"] = won_input("현재 평가액", "p_value", prop.get("value"))
            new["private_loan"] = won_input("사적대출", "p_private", prop.get("private_loan"))
        for dk in ("acquired_on", "value_asof"):
            new[dk] = new[dk].isoformat() if new[dk] else None

        diff, pct, cagr = W.property_change(new["acquired_price"], new["value"],
                                             new["acquired_on"], new["value_asof"])
        g1, g2, g3 = st.columns(3)
        g1.metric("취득 대비 증감액", "계산 불가" if diff is None else fmt(diff),
                  None if pct is None else f"{pct:+.2f}%")
        g2.metric("증감률", "계산 불가" if pct is None else f"{pct:+.2f}%")
        g3.metric("CAGR", "계산 불가" if cagr is None else f"{cagr:+.2f}%")

        st.markdown("###### 국토교통부 실거래가")
        t1, t2, t3 = st.columns(3)
        new["trade_complex"] = t1.text_input("실거래가 조회 단지명", prop.get("trade_complex") or "",
                                              key=K("p_trade_complex"))
        new["dong"] = t2.text_input("법정동", prop.get("dong") or "", key=K("p_dong"))
        new["lawd_cd"] = t3.text_input("지역코드(5자리)", str(prop.get("lawd_cd") or ""),
                                        key=K("p_lawd")).strip()
        if not trade_info["configured"]:
            st.info("실거래가 자동 조회가 꺼져 있습니다 — secrets에 `MOLIT_SERVICE_KEY`(공공데이터포털 "
                    "아파트 매매 실거래가 인증키)를 넣으면 최근 12개월 거래로 평가액을 갱신합니다.")
        else:
            if trade_info["errors"]:
                st.warning("실거래가 조회가 일부 실패해 기존 평가액을 유지했습니다: "
                           + " / ".join(trade_info["errors"][:3]))
            if tm:
                floors = ", ".join(f"{t['floor']}층" for t in tm["trades"] if t["floor"])
                areas = ", ".join(sorted({f"{t['area']:g}㎡" for t in tm["trades"]}))
                st.caption(
                    f"출처: 국토교통부 아파트 매매 실거래가 · 계약일 {tm['date']:%Y-%m-%d} · 전용 {areas}"
                    f" · {floors or '층 정보 없음'} · 매매가 {fmt(tm['price'])}"
                    + (f" (같은 날 {tm['count']}건의 중앙값)" if tm["count"] > 1 else "")
                    + " — 같은 단지·면적의 최근 거래이며, 개별 동·호수의 감정가와는 다를 수 있습니다.")
            elif not trade_info["errors"]:
                st.caption("최근 12개월 안에 조건(단지·법정동·전용면적 ±1㎡)에 맞는 거래가 없어 "
                           "기존 평가액을 유지합니다.")
        if prop.get("value_source") == "molit":
            st.caption("현재 평가액은 실거래가로 자동 반영된 값입니다. 직접 고치면 수동 입력값으로 바뀝니다.")

        if new != prop:
            asof_moved = False
            if new["value"] != prop.get("value"):
                new["value_source"] = "manual"
                if new["value_asof"] == prop.get("value_asof"):
                    new["value_asof"] = today.isoformat()   # 평가액만 고쳤으면 기준일은 오늘
                    asof_moved = True
            doc["property"] = new
            commit(reset_inputs=asof_moved)

    # ---- 금융자산 ---------------------------------------------------------
    with st.expander(f"💵 금융자산 · 평가액 {fmt(s['financial'])} · 부채 {fmt(s['financial_debt'])}",
                     key="wealth_exp_financial"):
        st.caption("현금 잔액과 종목 평가액은 겹치지 않게 입력하세요(종목으로 넣은 금액을 현금에 또 넣지 않기). "
                   "종목 가격은 한국 시간 기준 전일까지 마감된 일봉의 실제 종가이며 실시간 시세가 아닙니다.")
        for sec, title, kind in W.SECTIONS:
            sub, sub_status = s["sections"][sec]
            badge = {"ok": "", "stale": " · ⚠️ stale", "incomplete": " · ⚠️ 평가 미완료"}[sub_status]
            with st.expander(f"{title} · 소계 {fmt(sub)}{badge}", key=f"wealth_exp_{sec}"):
                section = fin[sec]
                cash = won_input("잔액" if kind is None else "현금·미투자 잔액", f"{sec}_cash",
                                 section.get("cash"))
                if cash != int(section.get("cash") or 0):
                    section["cash"] = cash
                    dirty = True
                if kind is None:
                    continue

                for h in list(section["holdings"]):
                    hid = f"{sec}_{h['market']}_{h['symbol']}"
                    q = quotes.get(W.quote_key(h["market"], h["symbol"])) or {}
                    val = W.value_holding(h.get("qty") or 0, q)
                    h1, h2, h3, h4 = st.columns([4, 3, 4, 1])
                    h1.markdown(f"**{h.get('name') or h['symbol']}**  \n`{h['symbol']}`")
                    qty = h2.number_input("수량", min_value=0.0, value=float(h.get("qty") or 0),
                                          format="%.8g", key=K(f"{hid}_qty"))
                    if q.get("close") is None:
                        price_txt = "종가 없음"
                    elif q["currency"] == "USD":
                        price_txt = (f"${q['close']:,.4g} ({q['date']})"
                                     + (f" · 환율 {q['fx']:,.2f} ({q['fx_date']})" if q.get("fx") else " · 환율 없음"))
                    else:
                        price_txt = f"{q['close']:,.0f}원 ({q['date']})"
                    state = {"ok": "", "stale": " · ⚠️ stale(저장된 가격)", "missing": " · ⚠️ 평가 미완료"}[
                        q.get("status", "missing")]
                    h3.markdown(f"{price_txt}{state}  \n평가액 **{fmt(val) if val is not None else '—'}**")
                    if h4.button("삭제", key=K(f"{hid}_del")):
                        section["holdings"] = [x for x in section["holdings"] if x is not h]
                        dirty = True
                    elif abs(qty - float(h.get("qty") or 0)) > 1e-12:
                        h["qty"] = qty
                        dirty = True

                st.markdown("###### 종목 추가")
                add_market, add_symbol, add_name = None, None, None
                if kind == "crypto":
                    pick = st.selectbox(
                        "코인 검색", list(W.CRYPTO_CATALOG), index=None, key=K(f"{sec}_pick"),
                        placeholder="코인 선택 (한글명·심볼 검색)",
                        format_func=lambda c: f"{W.CRYPTO_CATALOG[c][0]} ({c})")
                    if pick:
                        add_market, add_symbol = "CRYPTO", pick
                        add_name = f"{W.CRYPTO_CATALOG[pick][0]} ({pick})"
                else:
                    us = False
                    if kind == "stock":
                        us = st.radio("시장", ["국내", "미국"], horizontal=True,
                                      key=K(f"{sec}_mkt")) == "미국"
                    if us:
                        tkr = st.text_input("미국 티커", key=K(f"{sec}_us"),
                                            placeholder="예: SPY, AAPL").strip().upper()
                        if tkr:
                            add_market, add_symbol, add_name = "US", tkr, tkr
                    else:
                        options = get_kr_etf_options() if kind == "etf" else _stock_options
                        pick = st.selectbox(
                            "ETF 검색" if kind == "etf" else "종목·ETF 검색", options, index=None,
                            key=K(f"{sec}_pick"), placeholder="이름·코드 검색")
                        if pick:
                            add_market = "KR"
                            add_symbol = pick.rsplit("(", 1)[-1].rstrip(")")
                            add_name = pick.rsplit(" (", 1)[0]
                add_qty = st.number_input("수량", min_value=0.0, value=0.0, format="%.8g",
                                          key=K(f"{sec}_addqty"))
                if st.button("추가", key=K(f"{sec}_add")):
                    if not add_symbol or not W.valid_symbol(add_market, add_symbol):
                        st.error("종목을 선택(입력)하세요. 등록할 수 없는 심볼입니다.")
                    elif add_qty <= 0:
                        st.error("수량을 0보다 크게 입력하세요.")
                    elif add_market == "US" and not _wealth_price_rows("US", add_symbol):
                        st.error(f"'{add_symbol}' 가격을 조회할 수 없어 추가하지 않았습니다.")
                    else:
                        section["holdings"] = W.upsert_holding(
                            section["holdings"], add_market, add_symbol, add_name, add_qty)
                        commit(reset_inputs=True)

        debt = won_input("금융 부채(신용대출·마이너스통장 등)", "fin_debt", fin.get("debt"))
        if debt != int(fin.get("debt") or 0):
            fin["debt"] = debt
            dirty = True

    # ---- 자산 변화 --------------------------------------------------------
    records = doc["records"]
    with st.expander(f"📈 자산 변화 · 기록 {len(records)}건"
                     + (f" · 최근 {records[-1]['date']}" if records else ""),
                     key="wealth_exp_records"):
        st.caption("기록은 저장하는 순간의 금액 숫자를 그대로 보관합니다. 이후 종목·수량·시세·환율이 바뀌어도 "
                   "과거 기록은 바뀌지 않고, 입력하지 않은 기간을 추정해 채우지 않습니다.")
        blocked = s["status"] != "ok"

        def record_now(d):
            doc["records"] = W.upsert_record(records, W.make_record(
                d, s["property"], s["financial"], s["property_debt"], s["financial_debt"]))
            commit()

        r1, r2 = st.columns(2)
        with r1:
            if st.button("오늘 자산 기록 저장", key=K("rec_today"), disabled=blocked):
                record_now(today)
        with r2:
            months, (my, mm) = [], (today.year, today.month)
            for _ in range(36):
                months.append(f"{my}-{mm:02d}")
                my, mm = (my - 1, 12) if mm == 1 else (my, mm - 1)
            sel_month = st.selectbox("월말 기록할 월", months, index=1, key=K("rec_month"))
            me = W.month_end(int(sel_month[:4]), int(sel_month[5:]))
            future = not W.can_record_on(me, today)
            if st.button("현재 금액으로 월말 기록 저장", key=K("rec_month_btn"),
                         disabled=blocked or future):
                record_now(me)
            if future:
                st.caption(f"{me:%Y-%m-%d}은 아직 오지 않은 날짜라 저장할 수 없습니다(월말부터 가능).")
        if blocked:
            st.caption("⚠️ 평가 미완료 또는 stale 자산이 있어 현재 금액 기록 저장이 막혀 있습니다.")

        if records:
            period = st.radio("단위", ["월별", "연별"], horizontal=True, key=K("rec_period"))
            pts = W.period_last(records, "M" if period == "월별" else "Y")
            totals = [W.record_totals(r) for _, r in pts]
            big = max(abs(v) for t in totals for v in t.values()) >= W.EOK
            unit, unit_label = (W.EOK, "억원") if big else (W.MAN, "만원")
            rfig = go.Figure()
            for name, field, color in (("총자산", "total", "#1971c2"), ("순자산", "net", "#2f9e44"),
                                        ("부동산", "property", "#e8590c"), ("금융자산", "financial", "#9c36b5")):
                rfig.add_trace(go.Scatter(
                    x=[k for k, _ in pts], y=[t[field] / unit for t in totals], name=name,
                    mode="lines+markers", line=dict(color=color),
                    customdata=[fmt(t[field]) for t in totals],
                    hovertemplate="%{x} · " + name + " %{customdata}<extra></extra>"))
            rfig.update_layout(height=340, margin=dict(l=0, r=0, t=10, b=0),
                               legend=dict(orientation="h", y=1.12, x=0),
                               yaxis=dict(ticksuffix=unit_label, tickformat=",.4~g"))
            rfig.update_xaxes(type="category", fixedrange=True)
            rfig.update_yaxes(fixedrange=True)
            st.plotly_chart(rfig, use_container_width=True, key=K("rec_chart"),
                            config={"scrollZoom": False, "displayModeBar": False})
            st.dataframe(pd.DataFrame([
                {"기록일": r["date"], "총자산": fmt(W.record_totals(r)["total"]),
                 "순자산": fmt(W.record_totals(r)["net"]), "부동산": fmt(r["property"]),
                 "금융자산": fmt(r["financial"]), "부동산 부채": fmt(r["property_debt"]),
                 "금융 부채": fmt(r["financial_debt"])} for r in reversed(records)]),
                use_container_width=True, hide_index=True)
        else:
            st.info("저장된 기록이 없습니다.")

        st.markdown("###### 과거 기록 추가·수정")
        target = st.selectbox("대상", ["새 기록"] + [r["date"] for r in reversed(records)],
                              key=K("rec_target"))
        base = next((r for r in records if r["date"] == target), None)
        tag = target if base else "new"
        rec_date = st.date_input("기록일", W.to_date(base["date"]) if base else today,
                                 max_value=today, key=K(f"rec_{tag}_date"), disabled=bool(base))
        e1, e2 = st.columns(2)
        with e1:
            e_prop = won_input("부동산 평가액", f"rec_{tag}_prop", base["property"] if base else 0)
            e_pdebt = won_input("부동산 부채", f"rec_{tag}_pdebt", base["property_debt"] if base else 0)
        with e2:
            e_fin = won_input("금융자산 총액", f"rec_{tag}_fin", base["financial"] if base else 0)
            e_fdebt = won_input("금융 부채", f"rec_{tag}_fdebt", base["financial_debt"] if base else 0)
        s1, s2 = st.columns(2)
        if s1.button("기록 저장", key=K(f"rec_{tag}_save")):
            doc["records"] = W.upsert_record(records, W.make_record(rec_date, e_prop, e_fin, e_pdebt, e_fdebt))
            commit(reset_inputs=True)
        if base and s2.button("이 기록 삭제", key=K(f"rec_{tag}_del")):
            doc["records"] = W.delete_record(records, base["date"])
            commit(reset_inputs=True)

    # ---- 경제적 자유 계산기 -----------------------------------------------
    start = s["financial_net"] if fire.get("start_override") is None else fire["start_override"]
    f_years = int(fire.get("years") or 0)
    f_asset = W.project_assets(start, fire.get("annual_saving") or 0, fire.get("inflation") or 0,
                               f_years, fire.get("annual_return") or 0)
    f_monthly = W.monthly_spend(f_asset, fire.get("withdraw_rate") or 0)
    with st.expander(f"🏖 경제적 자유 계산기 · 은퇴 후 월 사용액 {fmt(f_monthly)}",
                     key="wealth_exp_fire"):
        nf = dict(fire)
        use_override = st.checkbox("시작 금액 직접 입력", value=fire.get("start_override") is not None,
                                   key=K("f_use_override"))
        if use_override:
            nf["start_override"] = won_input(
                "시작 금융 순자산", "f_start",
                fire["start_override"] if fire.get("start_override") is not None else max(s["financial_net"], 0))
        else:
            nf["start_override"] = None
            st.caption(f"시작 금융 순자산: 현재 금융 순자산 {fmt(s['financial_net'])} (부동산 제외)")
        i1, i2 = st.columns(2)
        with i1:
            nf["annual_saving"] = won_input("연간 저축액", "f_saving", fire.get("annual_saving"))
            nf["years"] = st.number_input("은퇴까지 남은 연수", 0, 100, int(fire.get("years") or 0),
                                          key=K("f_years"))
            nf["annual_return"] = st.number_input("연 투자수익률 (%)", -50.0, 100.0,
                                                  float(fire.get("annual_return") or 0), 0.5, key=K("f_ret"))
        with i2:
            nf["target_monthly"] = won_input("목표 월 생활비(현재 구매력)", "f_target",
                                             fire.get("target_monthly"))
            nf["inflation"] = st.number_input("물가상승률 = 저축 증가율 (%)", -10.0, 50.0,
                                              float(fire.get("inflation") or 0), 0.5, key=K("f_inf"))
            nf["withdraw_rate"] = st.number_input("연 인출률 (%)", 0.0, 100.0,
                                                  float(fire.get("withdraw_rate") or 0), 0.5, key=K("f_wd"))

        o1, o2, o3 = st.columns(3)
        o1.metric(f"{f_years}년 후 은퇴 금융자산", fmt(f_asset))
        o2.metric("은퇴 첫해 월 사용액", fmt(f_monthly))
        o3.metric("현재 구매력 기준 월 사용액",
                  fmt(W.real_value(f_monthly, fire.get("inflation") or 0, f_years)))
        target = fire.get("target_monthly") or 0
        if target > 0:
            n = W.years_to_target(start, fire.get("annual_saving") or 0, fire.get("inflation") or 0,
                                  fire.get("annual_return") or 0, fire.get("withdraw_rate") or 0, target)
            if n == 0:
                st.success(f"목표 월 생활비 {fmt(target)}: 지금 가능합니다.")
            elif n is not None:
                st.info(f"목표 월 생활비 {fmt(target)}: {n}년 후 가능합니다.")
            else:
                st.warning(f"목표 월 생활비 {fmt(target)}: 100년 내 도달할 수 없습니다.")
        else:
            st.caption("목표 월 생활비를 입력하면 도달 시점을 계산합니다.")
        st.caption("연금 수령 가능 시점·중도인출 제한·세금·수수료는 반영하지 않은 별도 사항입니다. "
                   "인출률(기본 4%)은 입력한 가정일 뿐이며 자산이 영구히 유지된다는 보장이 아닙니다.")
        if nf != fire:
            doc["fire"] = nf
            dirty = True

    # ---- 내보내기·가져오기 ------------------------------------------------
    with st.expander("🗂 데이터 내보내기 · 가져오기", key="wealth_exp_io"):
        st.download_button("현재 데이터 내보내기(JSON)", json.dumps(doc, ensure_ascii=False, indent=2),
                           file_name=f"wealth_{today:%Y%m%d}.json", mime="application/json",
                           key=K("io_export"))
        up = st.file_uploader("가져올 JSON 파일(이 탭에서 내보낸 형식)", type="json", key=K("io_upload"))
        if up is not None:
            try:
                incoming = json.loads(up.getvalue().decode("utf-8"))
                if not isinstance(incoming, dict) or "financial" not in incoming:
                    raise ValueError("형식 불일치")
                imported = W.normalize_doc(incoming)
            except Exception as e:
                st.error(f"가져올 수 없는 파일입니다: {type(e).__name__}")
            else:
                isum = W.summarize(imported, {})
                st.caption(f"가져올 내용 — 부동산 {fmt(isum['property'])} · 기록 {len(imported['records'])}건 · "
                           "보유 종목 "
                           f"{sum(len(imported['financial'][k]['holdings']) for k, _, kd in W.SECTIONS if kd)}개")
                if st.button("현재 데이터를 이 파일로 교체", key=K("io_apply")):
                    st.session_state.wealth_doc = imported
                    doc = imported
                    commit(reset_inputs=True)

    if dirty:
        commit()


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
    st.session_state.coin_rank_symbol = None       # 랭킹 탭 상단 차트 대상(코인 base 심볼, 예: "BTC")
if "coin_g247_symbol" not in st.session_state:
    st.session_state.coin_g247_symbol = None       # 글로벌24-7 탭 하단 차트 대상(hl_symbol)

st.title("📈 나의 투자 대시보드")
st.caption("차트 · 관심목록 · 동적자산배분 — 탭 전환. 데이터: FinanceDataReader")
if st.session_state.storage_mode == "local":
    st.caption("💾 로컬 저장 모드 — Gist 미설정 또는 연결 실패로, 이 기기에만 저장됩니다.")

watchlist = st.session_state.watchlist

tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8 = st.tabs(
    ["📊 차트", "⭐ 관심목록", "⚖️ 동적자산배분", "💰 프리미엄", "🛒 매수", "📏 이동평균", "🪙 코인", "🏖 은퇴"])

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
    st.caption("모멘텀 기반 월별 리밸런싱 · 백테스트는 사용 ETF가 모두 상장된 이후 구간만 계산됩니다. "
               "현재 포지션은 전월 말 종가로 확정되며 다음 달 1일까지 바뀌지 않습니다.")

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
            "KOSPI_BF": "KOSPI200(278530, 2017-11~ / KS200 소급)",
            "363580": "KODEX200IT TR(363580)",
            "114260": "국고채3년(114260)",
            "148070": "국고채10년(148070)",
            "439870": "국고채30년(439870)",
            "US_BOND_SHORT_KRW": "미국단기국채환노출(SHY합성)",
            "US_BOND_MID_KRW": "미국10년국채환노출(IEF합성)",
            "US_BOND_LONG_KRW": "미국장기국채환노출(TLT합성)",
            "CASH": "현금",
        }

        _STRAT_DATA_NOTES = {
            "변동성 변형 듀얼모멘텀":
                "안전자산 6종 중 국고채3/10/30년(114260/148070/439870)은 Close 가격만 사용 — "
                "연 1회(12월) 분배금을 지급하는데 총수익 반영이 안 돼 있어 미국채(환노출 합성,"
                " 총수익)와의 1개월 모멘텀 비교가 구조적으로 유리/불리할 수 있음. "
                "KOSPI200은 2017-11-21 이전을 KS200 가격지수로 소급(배당 미반영). "
                "KOSPI200 IT 오버레이는 2020-09-25 이후만 적용(그 전엔 KOSPI200 100%). "
                "국고채30년은 2022-08-23 이전엔 안전자산 후보에서 제외.",
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
            if not r or "error" in r:
                failed_strats.append(f"{nm}({r['error']})" if r else f"{nm}(계산 가능한 구간 없음)")
                continue
            table.append({
                "전략명": nm, "현재 포지션": pos_str(r.get("current") or {}),
                "직전월 포지션": _prev_pos_str(r),
                "CAGR": r.get("cagr"), "MDD": r.get("mdd"), "Sharpe": r.get("sharpe"),
                "올해 수익률": _ytd_stats(r.get("mret"))[0],
                "올해 MDD": _ytd_stats(r.get("mret"))[1],
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

            strat_names = list(tdf["전략명"])
            # 클릭한 셀은 위젯 상태로만 넘어온다 — 선택 행 하이라이트를 표에 입히려면
            # 표를 그리기 전에 미리 읽어야 한다.
            _cells = ((st.session_state.get("strat_table") or {}).get("selection") or {}).get("cells") or []
            if _cells and 0 <= _cells[0][0] < len(strat_names):
                st.session_state.taa_pick = strat_names[_cells[0][0]]
            pick = st.session_state.get("taa_pick")
            if pick not in strat_names:
                pick = strat_names[0]  # 선택이 없으면 CAGR 1위

            def _hl_pick(row):
                css = "background-color: rgba(255,212,59,0.35); font-weight: 700"
                return [css if row["전략명"] == pick else ""] * len(row)

            st.caption("전략명(행의 아무 칸)을 클릭하면 아래에 상세가 표시됩니다.")
            try:
                st.dataframe(
                    tdf.style.format({"CAGR": "{:+.1%}", "MDD": "{:.1%}", "Sharpe": "{:.2f}",
                                      "올해 수익률": "{:+.1%}", "올해 MDD": "{:.1%}"},
                                     na_rep="—").apply(_hl_pick, axis=1),
                    use_container_width=True, hide_index=True,
                    height=min(60 + 35 * len(tdf), 600),
                    on_select="rerun", selection_mode="single-cell", key="strat_table",
                )
            except Exception as _se:
                # 셀 선택 미지원 Streamlit 버전 → 전략명 버튼 목록으로 대체
                print(f"[taa] 셀 선택 미지원, 버튼 목록으로 대체: {type(_se).__name__}: {_se}")
                for _nm in strat_names:
                    if st.button(("▶ " if _nm == pick else "") + _nm, key=f"strat_btn_{_nm}"):
                        st.session_state.taa_pick = _nm
                        st.rerun()

            r = results[pick]
            st.markdown(f"#### 전략 상세 — {pick}")

            @contextlib.contextmanager
            def _detail_section(section):
                """상세 화면을 구역별로 격리 — 한 구역이 실패해도 나머지는 그대로 표시한다."""
                try:
                    yield
                except Exception as _de:
                    tb = traceback.format_exc()
                    print(f"[taa] '{pick}' 상세 '{section}' 실패:\n{tb}")
                    st.error(f"'{pick}' 전략 — '{section}' 표시 중 오류: {type(_de).__name__}: {_de}")
                    with st.expander("오류 상세(트레이스백)"):
                        st.code(tb)

            with _detail_section("요약 지표"):
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("CAGR", f"{r['cagr']:+.1%}")
                m2.metric("MDD",  f"{r['mdd']:.1%}")
                m3.metric("Sharpe", f"{r['sharpe']:.2f}")
                m4.metric("현재 포지션", pos_str(r.get("current") or {}))

                ytd_ret, ytd_mdd = _ytd_stats(r["mret"])
                md = _mdd_detail(r["mret"])
                y1, y2, y3, y4 = st.columns(4)
                y1.metric("올해 수익률", "—" if ytd_ret is None else f"{ytd_ret:+.1%}")
                y2.metric("올해 MDD", "—" if ytd_mdd is None else f"{ytd_mdd:.1%}")
                if md and md["peak"] is not None:
                    y3.metric("MDD 발생 기간",
                              f"{md['peak']:%Y-%m} ~ {md['trough']:%Y-%m}",
                              f"{_month_diff(md['peak'], md['trough'])}개월 하락", delta_color="off")
                    if md["recovery"] is not None:
                        y4.metric("전고점 회복", f"{md['recovery']:%Y-%m}",
                                  f"저점 후 {_month_diff(md['trough'], md['recovery'])}개월", delta_color="off")
                    else:
                        y4.metric("전고점 회복", "미회복",
                                  f"저점 후 {_month_diff(md['trough'], md['last'])}개월 경과", delta_color="off")
                else:
                    y3.metric("MDD 발생 기간", "—")
                    y4.metric("전고점 회복", "—")
                st.caption("MDD 구간은 월말 기준 전고점 → 저점이며, 회복 기간은 저점 이후 전고점을 "
                           "다시 넘어선 첫 월말까지의 개월 수입니다. 올해 수익률은 연환산하지 않은 누적값입니다.")

                bt_start =r["equity"].index[0].strftime("%Y-%m")
                bt_end = r["equity"].index[-1].strftime("%Y-%m")
                note = _STRAT_DATA_NOTES.get(pick)
                st.caption(f"백테스트 구간: {bt_start} ~ {bt_end}"
                           + (f" · ⚠️ {note}" if note else ""))

            if pick == "변동성 변형 듀얼모멘텀":
                with _detail_section("데이터 출처 상세"):
                    with st.expander("데이터 출처 상세 (총수익 여부 · backfill/proxy 구간)"):
                        meta_rows = []
                        for key, ticker in VOLDM_TICKERS.items():
                            meta = VOLDM_ASSET_META.get(key, {})
                            meta_rows.append({
                                "자산": key, "사용 컬럼": ticker,
                                "총수익 반영": "O" if meta.get("total_return") else "X(가격 기준)",
                                "비고": meta.get("note", ""),
                            })
                        st.dataframe(pd.DataFrame(meta_rows), use_container_width=True, hide_index=True)

                        fn = STRATEGIES[pick]
                        # run_all_strategies는 ctx를 반환하지 않는다 — 캐시된 build_ctx로 다시 얻는다.
                        ctx = build_ctx(list(mp.index))
                        n_months = n_proxy_months = 0
                        proxy_by_key = {}
                        for t in range(len(mp) - 1):
                            w = fn(mp, t, ctx)
                            if not w:
                                continue
                            n_months += 1
                            flagged = False
                            for key, ticker in VOLDM_TICKERS.items():
                                if ticker not in w:
                                    continue
                                st_type = voldm_source_type(mp, key, t)
                                if st_type in ("benchmark_index", "proxy"):
                                    flagged = True
                                    proxy_by_key[key] = proxy_by_key.get(key, 0) + 1
                            if flagged:
                                n_proxy_months += 1
                        pct = (n_proxy_months / n_months * 100) if n_months else 0.0
                        st.caption(
                            f"전체 {n_months}개월 중 benchmark_index/proxy 자산이 선택된 달: "
                            f"{n_proxy_months}개월({pct:.0f}%)"
                        )
                        if proxy_by_key:
                            breakdown = " · ".join(f"{k}: {v}개월" for k, v in proxy_by_key.items())
                            st.caption(f"자산별 내역 — {breakdown}")

            with _detail_section("최근 3개월 포지션"):
                # 신호는 월말에 확정되고 그 비중을 다음 달에 보유한다 — 히트맵 툴팁(보유월 기준)과
                # 대조할 수 있게 두 달을 나란히 적는다.
                recent_rows = [
                    {"신호 기준월": d.strftime("%Y-%m"),
                     "보유월": (d + pd.offsets.MonthEnd(1)).strftime("%Y-%m"),
                     "포지션": pos_str(w)}
                    for d, w in (r.get("recent_positions") or [])
                ]
                st.markdown("##### 최근 3개월 포지션")
                if recent_rows:
                    st.dataframe(pd.DataFrame(recent_rows), use_container_width=True, hide_index=True)
                else:
                    st.caption("표시할 포지션 이력이 없습니다.")

            with _detail_section("누적 수익률 차트"):
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

            with _detail_section("연월별 수익률 히트맵"):
                mr = r["mret"]
                heat = mr.groupby([mr.index.year, mr.index.month]).first().unstack() * 100
                heat.columns = [f"{m}월" for m in heat.columns]
                annual = (
                    mr.groupby(mr.index.year)
                      .apply(lambda x: (1 + x).prod() - 1) * 100
                ).rename("연간")
                heat_full = heat.join(annual)
                # held는 mret과 같은 날짜(보유월)로 색인돼 있어 셀과 1:1로 대응한다.
                held = r.get("held")
                tips = pd.DataFrame("", index=heat_full.index, columns=heat_full.columns)
                if held is not None:
                    for d, h in held.items():
                        tips.loc[d.year, f"{d.month}월"] = f"{d:%Y-%m}: {pos_str(h['weights'])}"
                st.markdown("##### 연월별 수익률 (%)")
                st.caption("월 칸에 커서를 올리면 그 달에 보유한 포지션(전월 말 신호)이 표시됩니다.")
                fn_h = lambda v: _color_scale_zero(v, 10)
                styled_heat = _apply_bg(heat_full.style.format("{:+.1f}", na_rep="—"), fn_h)
                styled_heat = styled_heat.set_tooltips(tips, as_title_attribute=True)
                st.markdown(
                    f'<div style="overflow-x:auto;font-size:0.85rem">'
                    f'{styled_heat.to_html()}'
                    f'</div>',
                    unsafe_allow_html=True,
                )
                if held is not None and len(held):
                    # 터치 기기에서는 호버가 안 되므로 같은 내용을 표로도 제공
                    with st.expander("월별 보유 포지션 표 (모바일용 · 최신순)"):
                        held_rows = [
                            {"보유월": d.strftime("%Y-%m"),
                             "신호 기준월": h["signal"].strftime("%Y-%m"),
                             "포지션": pos_str(h["weights"]),
                             "수익률(%)": mr.loc[d] * 100}
                            for d, h in held.iloc[::-1].items()
                        ]
                        st.dataframe(
                            _apply_bg(pd.DataFrame(held_rows).style.format({"수익률(%)": "{:+.1f}"}),
                                      fn_h, subset=["수익률(%)"]),
                            use_container_width=True, hide_index=True, height=420,
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
        st.caption("데이터: CoinGecko(시세·시총·스파크라인). 과거 일봉(RS·주간/월간 수익률용)은 "
                   "Binance→data-api.binance.vision→Coinbase→Bybit→OKX→CoinGecko 순으로 첫 성공한 "
                   "소스를 씀(지역 차단·장애 대비 다중소스 폴백 — 아래 표 하단 출처 참고). "
                   "시세는 5분, 과거 일봉은 1시간 캐시.")

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
            rank_df, rank_status, source_counts = build_coin_ranking(COIN_UNIVERSE_POOL)

        if rank_status == "coingecko_fail":
            st.warning("⚠️ CoinGecko 시세 조회에 실패했습니다. 잠시 후 다시 시도해주세요.")
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
                st.caption(f"{len(work)}개 코인 · 기본 정렬: 시총순 (표 헤더 클릭으로 재정렬 가능, "
                           "'#'열은 항상 시총 순위 고정)")

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
                _src_label = " · ".join(f"{k} {v}개" for k, v in source_counts.items())
                st.caption(f"과거 일봉 출처: {_src_label}")

                _sel_rows = rank_ev.selection.rows if rank_ev and rank_ev.selection else []
                if _sel_rows:
                    _sel_sym = work.iloc[_sel_rows[0]]["symbol"]
                    if _sel_sym != st.session_state.coin_rank_symbol:
                        st.session_state.coin_rank_symbol = _sel_sym
                        st.rerun()

                chart_base = st.session_state.coin_rank_symbol or work.iloc[0]["symbol"]
                chart_row = work[work["symbol"] == chart_base]
                chart_sym = chart_base + "USDT"
                chart_label = (chart_row.iloc[0]["symbol"] + " · " + chart_row.iloc[0]["name"]
                               if not chart_row.empty else chart_base)
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

# =====================  은퇴 (총자산 · 경제적 자유)  ========================
with tab8:
    _render_wealth_tab()
