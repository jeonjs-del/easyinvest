"""추세추종 스캐너 배치 스크립트 (계절성과 동일한 2단계 구조).

내 PC에서 직접 실행한다 (Streamlit Cloud에서는 실행하지 않음):
    python scanner/trend_scan.py

신호 3종을 변형별 그리드로 평가한다:
1) 이평눌림목 — 상승추세(이평이 MA_UPTREND_LOOKBACK일 전보다 높음) 종목이
   이동평균선에 근접(종가가 ±MA_NEAR_TOLERANCE 이내) 또는 터치(저가가 이평 이하)했을 때.
2) 박스돌파 — 직전 BOX 기간의 고가(box_top)를 종가가 상향 돌파. 다만 그 기간의
   변동폭이 너무 크면(=박스가 아니라 그냥 상승추세) 신호에서 제외(BOX_MAX_RANGE).
3) 신고가돌파 — 직전 N일 종가 최고치를 오늘 종가가 경신.

각 신호는 여러 변형(이평 종류/기간, 박스 기간, 신고가 기간)의 "그리드"로 평가하고,
변형마다 8개 보유기간의 승률/평균이익/평균손실/손익비/표본수를 계산한다.
보유기간이 아직 안 지난(오늘 포함 최근) 발생은 해당 기간 표본에서 자동 제외된다
(positions + H < n 조건으로 걸러지므로 look-ahead 없음).

오늘(스캔 기준일) 신호가 살아있는 (종목, 변형) 조합만 결과 파일에 남긴다.
Streamlit 앱(app.py)은 이 결과 파일만 읽고 재계산하지 않는다.

종목별 최고 변형만 리스트에 노출: 같은 (종목, 신호유형, 장단기)에서 여러 변형
(예: EMA40/EMA21/SMA20 근접)이 동시에 살아있으면, 별점 -> 초과수익 기반 기대값
(excess_expectancy) -> 표본수 순으로 가장 좋은 변형 하나만 "is_best_variant=True"로
표시한다. 나머지 변형도 행 자체는 결과 파일에 남아있으니(app.py에서 "다른 신호도
발생" 카운트로 사용), 종목당 여러 줄이 중복 노출되던 문제만 리스트 단계에서
해소한다. 종목(티커) 단위로 1행만 남기는 최종 중복 제거는 app.py가 화면 필터를
적용한 뒤 수행한다(신호 유형으로 필터링하면 그 필터 안에서 다시 최고 1개를
골라야 하므로, 스캐너가 미리 고정된 "종목당 1개"를 정해두면 필터와 충돌한다).

승률(win_rate)은 절대수익이 아니라 같은 시장 벤치마크(BENCHMARK_TICKERS: 미국
SPY/한국 KOSPI) 대비 초과수익 기준이다 — "보유기간 수익률 > 같은 기간 벤치마크
수익률"을 승리로 정의한다. 안 그러면 상승장에서 그냥 시장을 따라간 것만으로도
승률이 높게 나오는 신고가돌파가 부당하게 유리해진다. 기존 절대수익 기준 승률은
abs_win_rate로 별도 보관해 카드에 함께 보여준다. 신고가돌파는 상승장에서 신호
자체가 과다발생하기 쉬워 최소 표본수도 MIN_SAMPLE_BY_SIGNAL로 다른 신호보다 더
엄격하게(100) 요구하고, 대표 보유기간도 MIN_REPRESENTATIVE_HOLD_DAYS/
PREFERRED_MIN_HOLD_DAYS로 20일 미만을 배제하고 63일 이상을 우선한다(추세추종은
원래 수개월 단위로 보는 신호라 초단기 보유기간이 대표로 뽑히면 신호 성격이 왜곡된다).

별점(star_rating)은 신호유형별 백분위로 정한다(_assign_star_ratings): 이평눌림목/
박스돌파/신고가돌파 각각 자기 그룹 안에서만 rank_score(excess_expectancy, 초과수익
기반 기대값)의 상위 몇 %인지를 따진다(STAR_TOP_PCT/STAR_MID_PCT). 신호유형 공통의
절대 문턱 하나로 걸렀을 때는 신고가돌파가 상승장에서 승률·초과수익이 구조적으로
높게 나오기 쉬워 3성을 독식했는데, 그룹 내 백분위로 바꾸면 표본이 훨씬 많은
이평눌림목이 자기 그룹의 상위권을 두텁게 차지해 원사이트처럼 이평 비중이 높은
분포에 가까워진다. STAR_GATE_3/STAR_GATE_2는 그 그룹의 '상위'조차 절대적으로
부실할 때 등급을 남발하지 않기 위한 최소 안전장치일 뿐, 주된 기준은 그룹 내
백분위다.

주의 — "장기/단기" 구분은 원 사이트 UI에서 관찰한 것을 역추정한 것으로 확정된
정의가 아니다(예: "EMA10 단기 근접"과 "EMA10 장기 근접"이 같은 이평 기간으로 동시에
존재해, 이평 기간이나 상승추세 필터의 차이가 아니라 "통계를 뽑는 기간 창"이 다른
것으로 보고 재정의했다). 여기서는 STAT_TERMS에 정의한 대로 "단기 = 최근 3년 데이터
로 산출한 통계", "장기 = 최근 10년(조회 가능한 최대 기간) 데이터로 산출한 통계"로
해석한다. 같은 신호·같은 종목이라도 두 창 각각에 대해 별도 행이 나온다.
tests/test_calibration.py로 Oil-Dri Corp(EMA40 장기 근접)·Park Aerospace(박스돌파
장기)의 원사이트 공개 수치와 대조해봤는데, "장기" 창을 10년으로 두면 터치/돌파
표본수와 승률은 원사이트 값에 상당히 근접하지만(예: Oil-Dri EMA40 터치수 81~99
vs 원사이트 121, 승률 49~56% vs 60.2%), 평균손실 크기는 파라미터를 어떻게 바꿔도
원사이트보다 항상 2~3배 크게 나온다(예: -8.5% vs 원사이트 -3.0%). 근접임계값·상승추세
판정기간을 아무리 조정해도 표본수/승률만 조금씩 움직일 뿐 평균손실 배율은 거의
불변이었다 — 이는 신호 판정 파라미터 문제가 아니라, 원사이트가 고정 보유기간이
아니라 손절/트레일링 같은 조기청산 로직으로 손실 쪽을 제한하는 백테스트를 쓰고
있을 가능성을 시사한다(우리 스캐너는 "H일 뒤 종가 청산"만 계산하는 단순 방식).
이 부분은 원사이트 방법론을 확인하지 않는 한 완전히 재현하기 어렵다 — 자세한
과정은 tests/test_calibration.py 상단 주석 참고.
"""
import json
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import FinanceDataReader as fdr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scanner.universe import build_universe

# ---- config (필요시 이 값들만 바꾸면 됨) -----------------------------------
LOOKBACK_YEARS = 10             # 가격 데이터 조회 기간 (STAT_TERMS 최댓값과 맞춰둘 것)
HOLD_PERIODS = [2, 3, 5, 10, 20, 63, 126, 252]
MIN_SAMPLE = 20                 # 대표 보유기간 선정 및 최종 후보 포함의 최소 표본수
MIN_SAMPLE_RATIO = 0.8          # 대표 보유기간 후보 조건: 표본수가 해당 창 전체 신호발생수의 이 비율 이상
REPRESENTATIVE_METRIC = "profit_factor"  # 대표 보유기간 선택 기준: "profit_factor" 또는 "expectancy"
MIN_REPRESENTATIVE_HOLD_DAYS = 20     # 대표 보유기간 후보 최소 조건 — 이 미만(2/3/5/10일)은 아예 후보에서 제외
PREFERRED_MIN_HOLD_DAYS = 63          # 가능하면 이 이상인 후보 중에서 고른다(없으면 20일 이상으로 완화) —
                                       # 초단기(20일 미만)가 대표 보유기간으로 뽑혀 신호 성격을 왜곡하는 문제 방지
MAX_WORKERS = 12

MA_VARIANTS = [("SMA", 10), ("SMA", 20), ("SMA", 30),
               ("EMA", 10), ("EMA", 21), ("EMA", 40), ("EMA", 60)]
MA_UPTREND_LOOKBACK = 20        # 이평이 이 기간 전보다 높으면 '상승추세'로 인정
MA_NEAR_TOLERANCE = 0.02        # 종가가 이평 ±2% 이내면 '근접'(터치는 저가<=이평으로 별도 판정)
                                 # 캘리브레이션(Oil-Dri EMA40)으로 3%에서 2%로 조정 — tests/test_calibration.py 참고

BOX_PERIODS = [20, 60, 120]
BOX_MAX_RANGE = 0.50            # (박스상단-박스하단)/박스하단이 이보다 크면 '박스'로 보지 않고 제외
                                 # 캘리브레이션(Park Aerospace 박스돌파)으로 30%에서 50%로 조정

NEWHIGH_PERIODS = [20, 60, 252]

STAT_TERMS = {"단기": 3, "장기": 10}  # 통계 산출에 쓰는 최근 N년 창 (위 파일 docstring의 추정 정의 참고)
TRADING_DAYS_PER_YEAR = 252

BUY_ZONE_UPPER_MULT = 1.05      # 매수구간 = [기준선, 기준선 * 이 값]

RS_WEIGHTS = {"3m": 3, "6m": 2, "12m": 1}   # 종합/섹터 RS 가중치 (3:2:1 기본)

BENCHMARK_TICKERS = {"US": "SPY", "KR": "^KS11"}  # 초과수익 계산용 시장 벤치마크(각 시장 지수)

# 별점(star_rating) — 신호유형별 백분위 방식. 예전엔 (승률,표본수,손익비,초과수익)
# 절대 문턱 하나를 이평/박스/신고가 공통으로 적용했는데, 신고가돌파는 상승장에서
# 신호 자체가 과다발생하고 승률·초과수익도 구조적으로 높게 나오기 쉬워서 그 절대
# 문턱을 이평/박스보다 훨씬 쉽게 넘었다 — 그 결과 3성이 신고가돌파 쪽으로 쏠리고,
# 원사이트(이평이 압도적 다수)와 정반대의 신호유형 분포가 나왔다.
# 지금은 rank_score(=excess_expectancy, 초과수익 기반 기대값)를 신호유형(signal_type)
# 그룹 안에서만 백분위로 비교한다 — 이평은 이평끼리, 박스는 박스끼리, 신고가는
# 신고가끼리 경쟁하므로 한 신호유형이 표본이 많거나 시장 상황상 유리하다고 해서
# 상위 등급을 독식할 수 없다. STAR_GATE_*는 그 그룹의 '상위'조차 절대적으로 부실할
# 때(예: 표본이 적은 신호유형에서 우연히 1등) 3성/2성을 남발하지 않기 위한 최소
# 안전장치일 뿐, 등급을 가르는 주된 기준은 어디까지나 그룹 내 백분위다.
STAR_TOP_PCT = 0.85              # 신호유형 내 상위 15%(백분위 0.85 이상) -> 3성 후보
STAR_MID_PCT = 0.50              # 신호유형 내 상위 50% -> 2성 후보
STAR_GATE_3 = {"win_rate": 0.50, "profit_factor": 1.2}   # 3성 최소 안전장치
STAR_GATE_2 = {"win_rate": 0.45, "profit_factor": 1.0}   # 2성 최소 안전장치
STAR_DEFAULT = 1
RISK_WIN_RATE = 0.50            # 대표 보유기간 승률(이제 초과수익 기준)이 이 미만이면 '위험형' 배지

SAMPLE_LABELS = {"이평눌림목": "터치수", "박스돌파": "돌파수", "신고가돌파": "표본수"}
MIN_SAMPLE_BY_SIGNAL = {"신고가돌파": 100}  # 신고가는 상승장에서 과다발생하기 쉬워 표본 요건을 더 엄격히

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUTPUT_PARQUET = DATA_DIR / "trend.parquet"
OUTPUT_META = DATA_DIR / "trend_meta.json"


# ---- 데이터 조회 -----------------------------------------------------------
def _fetch_ohlc(ticker, start):
    try:
        df = fdr.DataReader(ticker, start)
        if df is None or df.empty or not {"High", "Low", "Close"}.issubset(df.columns):
            return None
        out = df[["High", "Low", "Close"]].dropna()
        return out if len(out) > 260 else None
    except Exception as e:
        print(f"  [skip:price] {ticker}: {e}")
        return None


# ---- 이평 유틸 --------------------------------------------------------------
def _sma(s, n):
    return s.rolling(n).mean()


def _ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def _ma_series(close, kind, period):
    return _sma(close, period) if kind == "SMA" else _ema(close, period)


# ---- 보유기간별 통계 --------------------------------------------------------
def _hold_stats(close_arr, positions, hold_periods, market_arr=None):
    """market_arr(같은 기간의 벤치마크 종가, 종목 날짜에 정렬됨)를 주면 초과수익 지표도
    함께 계산한다. 벤치마크가 없거나 특정 구간에 값이 없으면 그 구간은 초과수익
    계산에서 제외되고, 유효 데이터가 하나도 없으면 excess_return=None.

    win_rate는 시장(벤치마크) 대비 초과수익 기준이다: "보유기간 수익률 > 같은 기간
    벤치마크 수익률"을 승리로 정의한다 — 그냥 상승장을 따라간 것과 진짜 아웃퍼폼을
    구분하기 위함(안 그러면 신고가돌파처럼 상승장에 자주 발생하는 신호가 부당하게
    승률이 높게 나온다). 기존 절대수익 기준 승률은 abs_win_rate로 별도 보관한다.
    벤치마크 데이터가 없는 구간(market_arr=None 또는 유효 페어 0개)은 win_rate가
    abs_win_rate로 대체된다(캘리브레이션 테스트처럼 벤치마크 없이 호출하는 경우 포함).
    excess_expectancy는 초과수익 기준 기대값 — 승률(초과수익 기준)*평균초과이익 +
    패률*평균초과손실이며, 이는 대수적으로 초과수익들의 평균(excess_return)과 같다.
    벤치마크가 없으면 절대수익 기준 expectancy로 대체한다."""
    n = len(close_arr)
    rows = []
    for h in hold_periods:
        valid = positions[positions + h < n]
        if len(valid) == 0:
            continue
        entry = close_arr[valid]
        exit_ = close_arr[valid + h]
        rets = exit_ / entry - 1.0
        wins = rets[rets > 0]
        losses = rets[rets <= 0]
        abs_win_rate = len(wins) / len(rets)
        avg_win = float(wins.mean()) if len(wins) else 0.0
        avg_loss = float(losses.mean()) if len(losses) else 0.0
        if avg_loss < 0:
            pf = avg_win / abs(avg_loss)
        else:
            pf = float("inf") if avg_win > 0 else 0.0
        expectancy = abs_win_rate * avg_win + (1 - abs_win_rate) * avg_loss

        win_rate = abs_win_rate
        excess_return = None
        excess_expectancy = expectancy
        if market_arr is not None:
            m_entry = market_arr[valid]
            m_exit = market_arr[valid + h]
            ok = ~np.isnan(m_entry) & ~np.isnan(m_exit) & (m_entry > 0)
            if ok.any():
                mkt_rets = m_exit[ok] / m_entry[ok] - 1.0
                excess_vals = rets[ok] - mkt_rets
                win_rate = float((excess_vals > 0).mean())
                excess_return = float(excess_vals.mean())
                excess_expectancy = excess_return

        rows.append({"hold_days": h, "win_rate": win_rate, "abs_win_rate": abs_win_rate,
                      "avg_win": avg_win, "avg_loss": avg_loss, "profit_factor": pf,
                      "expectancy": expectancy, "excess_return": excess_return,
                      "excess_expectancy": excess_expectancy, "n_samples": int(len(rets))})
    return rows


def _pick_representative(rows, min_sample, total_occurrences, min_sample_ratio, metric):
    """대표 보유기간 선정: 최소 표본수(min_sample)와, 창 전체 신호발생수 대비 표본
    비율(min_sample_ratio) 조건을 모두 만족하는 보유기간 중 metric이 최대인 것.
    표본 비율 조건이 없으면 손익비가 보유기간이 길어질수록 커지는 추세추종 신호
    특성상 대표 보유기간이 항상 최장(252일)으로 쏠리는 문제가 있었다.
    MIN_REPRESENTATIVE_HOLD_DAYS 미만(2/3/5/10일)은 애초에 후보에서 제외하고,
    가능하면 PREFERRED_MIN_HOLD_DAYS(63일) 이상 중에서 고른다 — 초단기 보유기간이
    대표로 뽑혀 카드 대표 지표가 신호의 실제 성격(추세추종=수개월 보유)과
    동떨어지는 문제를 막기 위함. 63일 이상 후보가 하나도 없을 때만 20일 이상으로
    완화한다."""
    eligible = [r for r in rows
                if r["hold_days"] >= MIN_REPRESENTATIVE_HOLD_DAYS
                and r["n_samples"] >= min_sample
                and r["n_samples"] >= min_sample_ratio * total_occurrences]
    if not eligible:
        return None
    preferred = [r for r in eligible if r["hold_days"] >= PREFERRED_MIN_HOLD_DAYS]
    pool = preferred if preferred else eligible
    return max(pool, key=lambda r: r[metric])


def _assign_star_ratings(rep_all):
    """대표 보유기간 행(신호유형·종목·변형·장단기 조합마다 1행)에 신호유형별 백분위로
    별점을 매긴다. rank_score로 excess_expectancy(초과수익 기반 기대값)를 쓰고,
    signal_type 그룹 안에서만 순위를 매기므로 이평/박스/신고가가 서로 다른 절대
    수준의 점수를 갖더라도 각자 자기 그룹 상위 X%만 높은 별점을 받는다."""
    pct = rep_all.groupby("signal_type")["excess_expectancy"].rank(pct=True, method="average")
    star = pd.Series(STAR_DEFAULT, index=rep_all.index)
    is_3star = ((pct >= STAR_TOP_PCT)
                & (rep_all["win_rate"] >= STAR_GATE_3["win_rate"])
                & (rep_all["profit_factor"] >= STAR_GATE_3["profit_factor"]))
    star[is_3star] = 3
    is_2star = ((star == STAR_DEFAULT) & (pct >= STAR_MID_PCT)
                & (rep_all["win_rate"] >= STAR_GATE_2["win_rate"])
                & (rep_all["profit_factor"] >= STAR_GATE_2["profit_factor"]))
    star[is_2star] = 2
    return star


def _build_candidate_rows(meta, signal_type, variant_key, variant_label,
                           close_arr, positions, baseline_arr, term_label, market_arr=None):
    n = len(close_arr)
    if len(positions) == 0 or positions[-1] != n - 1:
        return []  # 오늘(마지막 봉) 신호가 유효해야 현재 후보로 채택
    window_start = max(0, n - STAT_TERMS[term_label] * TRADING_DAYS_PER_YEAR)
    term_positions = positions[positions >= window_start]
    total_occurrences = len(term_positions)
    min_sample = MIN_SAMPLE_BY_SIGNAL.get(signal_type, MIN_SAMPLE)
    stat_rows = _hold_stats(close_arr, term_positions, HOLD_PERIODS, market_arr)
    rep = _pick_representative(stat_rows, min_sample, total_occurrences, MIN_SAMPLE_RATIO, REPRESENTATIVE_METRIC)
    if rep is None:
        return []  # 최소 표본수/비율 미달 -> 제외
    baseline = baseline_arr[-1]
    if pd.isna(baseline) or baseline <= 0:
        return []
    close_now = float(close_arr[-1])
    buy_low, buy_high = float(baseline), float(baseline) * BUY_ZONE_UPPER_MULT
    if close_now < buy_low:
        status = "구간아래"
    elif close_now <= buy_high:
        status = "구간내"
    else:
        status = "구간위"
    # 별점(star_rating)은 유니버스 전체 스캔이 끝난 뒤 run()에서 신호유형별 백분위로
    # 한꺼번에 매긴다(_assign_star_ratings) — 종목 하나만 봐서는 신호유형 전체 분포를
    # 알 수 없어 여기서는 계산할 수 없다. risk_flag만 대표 보유기간 자신의 승률
    # (이제 초과수익 기준)로 바로 판정한다.
    risk = rep["win_rate"] < RISK_WIN_RATE

    out = []
    for r in stat_rows:
        row = dict(meta)
        row.update({
            "signal_type": signal_type, "variant_key": variant_key, "variant_label": variant_label,
            "trend_term": term_label,
            "baseline_price": buy_low, "buy_zone_low": buy_low, "buy_zone_high": buy_high,
            "close_price": close_now, "price_status": status,
            "sample_label": SAMPLE_LABELS[signal_type],
            "hold_days": r["hold_days"], "win_rate": r["win_rate"], "abs_win_rate": r["abs_win_rate"],
            "avg_win": r["avg_win"], "avg_loss": r["avg_loss"], "profit_factor": r["profit_factor"],
            "expectancy": r["expectancy"], "excess_return": r["excess_return"],
            "excess_expectancy": r["excess_expectancy"],
            "n_samples": r["n_samples"], "total_occurrences": total_occurrences,
            "is_representative": r["hold_days"] == rep["hold_days"],
            "risk_flag": risk,
        })
        out.append(row)
    return out


# ---- 신호 판정 (테스트에서도 동일 함수를 재사용) ------------------------------
def _fresh(raw):
    """불리언 배열에서 '막 시작된' 지점만 True로 남긴다(연속 구간은 첫날만)."""
    return raw & ~np.concatenate([[False], raw[:-1]])


def detect_ma_near_positions(close, low, kind, period):
    """이평눌림목: 상승추세 + (근접 또는 터치)가 새로 시작된 지점. ma 시리즈도 함께 반환
    (매수구간 기준선으로 쓰임)."""
    ma = _ma_series(close, kind, period)
    uptrend = (ma > ma.shift(MA_UPTREND_LOOKBACK)).to_numpy()
    near = ((close / ma - 1).abs() <= MA_NEAR_TOLERANCE).to_numpy()
    touch = (low <= ma).to_numpy()
    ma_valid = ma.notna().to_numpy()
    raw = uptrend & (near | touch) & ma_valid
    return np.where(_fresh(raw))[0], ma


def detect_box_breakout_positions(close, high, low, period):
    """박스돌파: 직전 period일 박스(고가~저가) 상단을 종가가 상향 돌파, 박스 변동폭이
    BOX_MAX_RANGE 이내인 경우만. box_top 시리즈도 함께 반환(매수구간 기준선)."""
    close_arr = close.to_numpy()
    box_top = high.rolling(period).max().shift(1)
    box_bottom = low.rolling(period).min().shift(1)
    range_ok = (((box_top - box_bottom) / box_bottom) <= BOX_MAX_RANGE).to_numpy()
    top_valid = box_top.notna().to_numpy()
    raw = (close_arr > box_top.to_numpy()) & range_ok & top_valid
    return np.where(_fresh(raw))[0], box_top


def detect_newhigh_positions(close, period):
    """신고가돌파: 직전 period일 종가 최고치를 오늘 종가가 경신. 연속 신고가는
    각각 별개 이벤트로 인정(중복 제거 안 함). prior_max 시리즈도 함께 반환."""
    close_arr = close.to_numpy()
    prior_max = close.rolling(period).max().shift(1)
    pm_valid = prior_max.notna().to_numpy()
    raw = (close_arr > prior_max.to_numpy()) & pm_valid
    return np.where(raw)[0], prior_max


# ---- 종목별 신호 스캔 --------------------------------------------------------
def _scan_ticker(meta, df, signal_types, benchmark=None):
    df = df.dropna(subset=["Close", "High", "Low"])
    if len(df) < 260:
        return []
    close, high, low = df["Close"], df["High"], df["Low"]
    close_arr = close.to_numpy()

    market_arr = None
    if benchmark is not None:
        market_arr = benchmark.reindex(close.index, method="ffill").to_numpy(dtype="float64")

    results = []
    terms = list(STAT_TERMS.keys())

    # 라벨은 원사이트 표기("EMA10 단기 근접", "박스돌파 장기")를 따라 장/단기를 항상 붙인다.
    if "이평눌림목" in signal_types:
        for kind, period in MA_VARIANTS:
            positions, ma = detect_ma_near_positions(close, low, kind, period)
            for term_label in terms:
                results.extend(_build_candidate_rows(
                    meta, "이평눌림목", f"{kind}_{period}", f"{kind}{period} {term_label} 근접",
                    close_arr, positions, ma.to_numpy(), term_label, market_arr))

    if "박스돌파" in signal_types:
        for period in BOX_PERIODS:
            positions, box_top = detect_box_breakout_positions(close, high, low, period)
            for term_label in terms:
                results.extend(_build_candidate_rows(
                    meta, "박스돌파", f"BOX_{period}", f"박스돌파 {term_label}",
                    close_arr, positions, box_top.to_numpy(), term_label, market_arr))

    if "신고가돌파" in signal_types:
        for period in NEWHIGH_PERIODS:
            positions, prior_max = detect_newhigh_positions(close, period)
            for term_label in terms:
                results.extend(_build_candidate_rows(
                    meta, "신고가돌파", f"NEWHIGH_{period}", f"{period}일신고가 {term_label} 돌파",
                    close_arr, positions, prior_max.to_numpy(), term_label, market_arr))

    return results


# ---- RS(상대강도) ------------------------------------------------------------
def _month_return(close, months):
    if len(close) < 2:
        return None
    target = close.index[-1] - pd.DateOffset(months=months)
    past = close[close.index <= target]
    if past.empty:
        return None
    return float(close.iloc[-1] / past.iloc[-1] - 1.0)


def _rs_raw_score(close):
    r3, r6, r12 = _month_return(close, 3), _month_return(close, 6), _month_return(close, 12)
    if None in (r3, r6, r12):
        return None
    w3, w6, w12 = RS_WEIGHTS["3m"], RS_WEIGHTS["6m"], RS_WEIGHTS["12m"]
    return (w3 * r3 + w6 * r6 + w12 * r12) / (w3 + w6 + w12)


def _percentile_map(raw_scores):
    s = pd.Series(raw_scores, dtype="float64").dropna()
    if s.empty:
        return {}
    pct = s.rank(pct=True, method="average")
    return {k: int(min(99, v * 100)) for k, v in pct.items()}


def _compute_rs(universe, close_map):
    """종합 RS: market 내 백분위. 섹터 RS: (market,sector) 평균수익률을 market 내 섹터끼리 백분위."""
    raw = {u["ticker"]: _rs_raw_score(close_map[u["ticker"]])
           for u in universe if u["ticker"] in close_map}

    by_market = {}
    for u in universe:
        tk = u["ticker"]
        if raw.get(tk) is not None:
            by_market.setdefault(u["market"], {})[tk] = raw[tk]
    rs_total = {}
    for scores in by_market.values():
        rs_total.update(_percentile_map(scores))

    sector_groups = {}
    for u in universe:
        tk = u["ticker"]
        if raw.get(tk) is None or not u.get("sector"):
            continue
        sector_groups.setdefault((u["market"], u["sector"]), []).append(raw[tk])
    sector_avg_by_market = {}
    for (mkt, sec), vals in sector_groups.items():
        sector_avg_by_market.setdefault(mkt, {})[sec] = sum(vals) / len(vals)
    sector_pct_by_market = {mkt: _percentile_map(scores) for mkt, scores in sector_avg_by_market.items()}
    rs_sector = {}
    for u in universe:
        tk = u["ticker"]
        sec_pct = sector_pct_by_market.get(u["market"], {})
        if u.get("sector") in sec_pct:
            rs_sector[tk] = sec_pct[u["sector"]]

    return rs_total, rs_sector


# ---- 메인 -------------------------------------------------------------------
def run(signal_types=None, markets=None):
    """signal_types/markets를 좁혀서 소규모 테스트 실행이 가능하도록 파라미터화.
    기본 실행(python trend_scan.py)은 전체 신호 3종 x 전체 유니버스(한국+미국)."""
    t0 = time.time()
    signal_types = signal_types or ["이평눌림목", "박스돌파", "신고가돌파"]
    universe = build_universe()
    if markets:
        universe = [u for u in universe if u["market"] in markets]
    print(f"유니버스 {len(universe)}종목 · 신호: {', '.join(signal_types)}")

    start_date = (pd.Timestamp.today() - pd.DateOffset(years=LOOKBACK_YEARS)).strftime("%Y-%m-%d")

    print("가격 데이터(OHLC) 수집 중...")
    prices, failed, done = {}, [], 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exe:
        futs = {exe.submit(_fetch_ohlc, u["ticker"], start_date): u["ticker"] for u in universe}
        for fut in as_completed(futs):
            tk = futs[fut]
            df = fut.result()
            done += 1
            if df is not None:
                prices[tk] = df
            else:
                failed.append(tk)
            if done % 100 == 0 or done == len(futs):
                print(f"  진행 {done}/{len(futs)}")
    print(f"  가격 조회 성공 {len(prices)} / 실패 {len(failed)}")
    if failed:
        head = ", ".join(failed[:30])
        print(f"  실패 종목: {head}{' ...' if len(failed) > 30 else ''}")

    # 미국 종목 시가총액(USD)은 scanner/universe.py가 유니버스 구성 단계에서
    # 네이버 marketValue API로 이미 채워왔으므로 여기서 따로 조회하지 않는다.
    usdkrw = None
    if any(u["market"] == "KR" for u in universe):
        try:
            usdkrw = float(fdr.DataReader("USD/KRW", start_date)["Close"].dropna().iloc[-1])
        except Exception as e:
            print(f"  USD/KRW 환율 조회 실패({e}) -> 한국 종목 시총(USD) 미표기")

    close_map = {tk: df["Close"] for tk, df in prices.items()}
    print("RS(상대강도) 계산 중...")
    rs_total, rs_sector = _compute_rs(universe, close_map)

    print("시장 벤치마크(초과수익 계산용) 조회 중...")
    benchmarks = {}
    for mkt, btk in BENCHMARK_TICKERS.items():
        if not any(u["market"] == mkt for u in universe):
            continue
        try:
            bdf = fdr.DataReader(btk, start_date)
            benchmarks[mkt] = bdf["Close"].dropna()
        except Exception as e:
            print(f"  벤치마크({btk}) 조회 실패({e}) -> {mkt} 초과수익 미반영")

    print("신호 스캔 중...")
    all_rows, done = [], 0
    for u in universe:
        tk = u["ticker"]
        if tk not in prices:
            continue
        meta = {k: v for k, v in u.items() if not k.startswith("_")}
        if meta["market"] == "KR" and u.get("_market_cap_krw") and usdkrw:
            meta["market_cap_usd"] = u["_market_cap_krw"] / usdkrw
        meta["rs_total"] = rs_total.get(tk)
        meta["rs_sector"] = rs_sector.get(tk)
        all_rows.extend(_scan_ticker(meta, prices[tk], signal_types, benchmarks.get(u["market"])))
        done += 1
        if done % 100 == 0 or done == len(prices):
            print(f"  진행 {done}/{len(prices)}")

    elapsed = round(time.time() - t0, 1)
    candidate_counts = {}
    if all_rows:
        result = pd.DataFrame(all_rows)

        # 별점: 유니버스 전체(대표 보유기간 행 전부)가 모인 지금 신호유형별 백분위로
        # 한번에 매기고, 같은 후보(종목,신호유형,변형,장단기)의 모든 보유기간 행에
        # 동일하게 적용한다.
        rep_all = result[result["is_representative"]].copy()
        rep_all["star_rating"] = _assign_star_ratings(rep_all)
        star_map = rep_all[["ticker", "signal_type", "variant_key", "trend_term", "star_rating"]]
        result = result.merge(star_map, on=["ticker", "signal_type", "variant_key", "trend_term"], how="left")
        result["star_rating"] = result["star_rating"].fillna(STAR_DEFAULT).astype(int)

        # 종목별 최고 변형 선택 (별점 -> 초과수익 기반 기대값 -> 표본수 순): (종목,신호유형,장단기)당
        # 대표 보유기간 행 하나만 비교 대상으로 삼아 변형 중 최고를 고르고,
        # 그 변형에 속한 모든 보유기간 행에 is_best_variant=True를 표시한다.
        rep = result[result["is_representative"]].copy()
        rep_ranked = rep.sort_values(["star_rating", "excess_expectancy", "n_samples"],
                                      ascending=[False, False, False])
        best_variant = (rep_ranked.drop_duplicates(subset=["ticker", "signal_type", "trend_term"], keep="first")
                         [["ticker", "signal_type", "trend_term", "variant_key"]]
                         .assign(is_best_variant=True))
        result = result.merge(best_variant, on=["ticker", "signal_type", "trend_term", "variant_key"], how="left")
        result["is_best_variant"] = result["is_best_variant"].fillna(False).astype(bool)

        rep = result[result["is_representative"]]
        candidate_counts = rep.groupby("signal_type")["ticker"].nunique().to_dict()
        print("신호별 현재 후보 종목수:")
        for k, v in candidate_counts.items():
            print(f"  {k}: {v}종목")

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        result.to_parquet(OUTPUT_PARQUET, index=False)
        meta_out = {
            "as_of": pd.Timestamp.today().strftime("%Y-%m-%d"),
            "lookback_years": LOOKBACK_YEARS,
            "hold_periods": HOLD_PERIODS,
            "min_sample": MIN_SAMPLE,
            "min_sample_ratio": MIN_SAMPLE_RATIO,
            "representative_metric": REPRESENTATIVE_METRIC,
            "min_representative_hold_days": MIN_REPRESENTATIVE_HOLD_DAYS,
            "preferred_min_hold_days": PREFERRED_MIN_HOLD_DAYS,
            "stat_terms": STAT_TERMS,
            "ma_near_tolerance": MA_NEAR_TOLERANCE,
            "box_max_range": BOX_MAX_RANGE,
            "star_top_pct": STAR_TOP_PCT,
            "star_mid_pct": STAR_MID_PCT,
            "star_gate_3": STAR_GATE_3,
            "star_gate_2": STAR_GATE_2,
            "min_sample_by_signal": MIN_SAMPLE_BY_SIGNAL,
            "benchmark_tickers": {k: v for k, v in BENCHMARK_TICKERS.items() if k in benchmarks},
            "signal_types": signal_types,
            "universe_size": len(universe),
            "scanned_tickers": len(prices),
            "failed_tickers": failed,
            "result_rows": len(result),
            "candidate_counts": candidate_counts,
            "elapsed_sec": elapsed,
        }
        with open(OUTPUT_META, "w", encoding="utf-8") as f:
            json.dump(meta_out, f, ensure_ascii=False, indent=2)
        print(f"저장: {OUTPUT_PARQUET} ({len(result):,}행)")
    else:
        print("현재 유효한 신호 후보가 없습니다 — 결과 파일을 저장하지 않았습니다.")

    print(f"소요시간 {elapsed}초 · 스캔종목 {len(prices)}개 · 실패 {len(failed)}개")
    return {"elapsed_sec": elapsed, "scanned": len(prices), "failed": len(failed),
            "candidate_counts": candidate_counts}


if __name__ == "__main__":
    run()
