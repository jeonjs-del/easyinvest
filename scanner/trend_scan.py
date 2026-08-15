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

주의 — "장기/단기" 구분: 원 사이트가 각 신호 라벨에 붙이는 장기/단기의 정확한
정의는 확인하지 못했다. 여기서는 실무적으로 "종가가 TREND_TERM_MA일 이동평균
위에 있으면 장기, 아래면 단기"로 잠정 정의했다. TREND_TERM_MA 값을 바꾸면
기준이 바뀐다 — 원 사이트 정의가 확인되면 이 함수만 교체하면 된다.
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
LOOKBACK_YEARS = 10
HOLD_PERIODS = [2, 3, 5, 10, 20, 63, 126, 252]
MIN_SAMPLE = 20                 # 대표 보유기간 선정 및 최종 후보 포함의 최소 표본수
MAX_WORKERS = 12

MA_VARIANTS = [("SMA", 10), ("SMA", 20), ("SMA", 30),
               ("EMA", 10), ("EMA", 21), ("EMA", 40), ("EMA", 60)]
MA_UPTREND_LOOKBACK = 20        # 이평이 이 기간 전보다 높으면 '상승추세'로 인정
MA_NEAR_TOLERANCE = 0.03        # 종가가 이평 ±3% 이내면 '근접'(터치는 저가<=이평으로 별도 판정)

BOX_PERIODS = [20, 60, 120]
BOX_MAX_RANGE = 0.30            # (박스상단-박스하단)/박스하단이 이보다 크면 '박스'로 보지 않고 제외

NEWHIGH_PERIODS = [20, 60, 252]

TREND_TERM_MA = 200             # 장기/단기 구분 기준 이평 기간 (위 주석의 잠정 정의 참고)

BUY_ZONE_UPPER_MULT = 1.05      # 매수구간 = [기준선, 기준선 * 이 값]

RS_WEIGHTS = {"3m": 3, "6m": 2, "12m": 1}   # 종합/섹터 RS 가중치 (3:2:1 기본)

STAR_RULES = [                  # (승률 임계값, 표본수 임계값, 별점) — 위에서부터 먼저 만족하는 규칙 적용
    (0.60, 100, 3),
    (0.55, 50, 2),
]
STAR_DEFAULT = 1
RISK_WIN_RATE = 0.50            # 대표 보유기간 승률이 이 미만이면 '위험형' 배지

SAMPLE_LABELS = {"이평눌림목": "터치수", "박스돌파": "돌파수", "신고가돌파": "표본수"}

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


def _fetch_market_cap_usd(ticker):
    import yfinance as yf
    try:
        fi = yf.Ticker(ticker).fast_info
        cap = fi.get("marketCap") if hasattr(fi, "get") else fi["marketCap"]
        return float(cap) if cap else None
    except Exception:
        return None


# ---- 이평 유틸 --------------------------------------------------------------
def _sma(s, n):
    return s.rolling(n).mean()


def _ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def _ma_series(close, kind, period):
    return _sma(close, period) if kind == "SMA" else _ema(close, period)


# ---- 보유기간별 통계 --------------------------------------------------------
def _hold_stats(close_arr, positions, hold_periods, min_sample):
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
        win_rate = len(wins) / len(rets)
        avg_win = float(wins.mean()) if len(wins) else 0.0
        avg_loss = float(losses.mean()) if len(losses) else 0.0
        if avg_loss < 0:
            pf = avg_win / abs(avg_loss)
        else:
            pf = float("inf") if avg_win > 0 else 0.0
        rows.append({"hold_days": h, "win_rate": win_rate, "avg_win": avg_win,
                      "avg_loss": avg_loss, "profit_factor": pf, "n_samples": int(len(rets))})
    return rows


def _pick_representative(rows, min_sample):
    eligible = [r for r in rows if r["n_samples"] >= min_sample]
    if not eligible:
        return None
    return max(eligible, key=lambda r: r["profit_factor"])


def _star_rating(win_rate, n_samples):
    for wr_thr, n_thr, star in STAR_RULES:
        if win_rate >= wr_thr and n_samples >= n_thr:
            return star
    return STAR_DEFAULT


def _build_candidate_rows(meta, signal_type, variant_key, variant_label,
                           close_arr, positions, baseline_arr, trend_term_now):
    n = len(close_arr)
    if len(positions) == 0 or positions[-1] != n - 1:
        return []  # 오늘(마지막 봉) 신호가 유효해야 현재 후보로 채택
    stat_rows = _hold_stats(close_arr, positions, HOLD_PERIODS, MIN_SAMPLE)
    rep = _pick_representative(stat_rows, MIN_SAMPLE)
    if rep is None:
        return []  # 최소 표본수 미달 -> 제외
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
    star = _star_rating(rep["win_rate"], rep["n_samples"])
    risk = rep["win_rate"] < RISK_WIN_RATE

    out = []
    for r in stat_rows:
        row = dict(meta)
        row.update({
            "signal_type": signal_type, "variant_key": variant_key, "variant_label": variant_label,
            "trend_term": trend_term_now,
            "baseline_price": buy_low, "buy_zone_low": buy_low, "buy_zone_high": buy_high,
            "close_price": close_now, "price_status": status,
            "sample_label": SAMPLE_LABELS[signal_type],
            "hold_days": r["hold_days"], "win_rate": r["win_rate"], "avg_win": r["avg_win"],
            "avg_loss": r["avg_loss"], "profit_factor": r["profit_factor"], "n_samples": r["n_samples"],
            "is_representative": r["hold_days"] == rep["hold_days"],
            "star_rating": star, "risk_flag": risk,
        })
        out.append(row)
    return out


# ---- 종목별 신호 스캔 --------------------------------------------------------
def _scan_ticker(meta, df, signal_types):
    df = df.dropna(subset=["Close", "High", "Low"])
    if len(df) < 260:
        return []
    close, high, low = df["Close"], df["High"], df["Low"]
    close_arr = close.to_numpy()

    sma_term = _sma(close, TREND_TERM_MA)
    trend_term_now = ("장기" if (not pd.isna(sma_term.iloc[-1]) and close_arr[-1] >= sma_term.iloc[-1])
                       else "단기")

    results = []

    if "이평눌림목" in signal_types:
        for kind, period in MA_VARIANTS:
            ma = _ma_series(close, kind, period)
            uptrend = (ma > ma.shift(MA_UPTREND_LOOKBACK)).to_numpy()
            near = ((close / ma - 1).abs() <= MA_NEAR_TOLERANCE).to_numpy()
            touch = (low <= ma).to_numpy()
            ma_valid = ma.notna().to_numpy()
            raw = uptrend & (near | touch) & ma_valid
            fresh = raw & ~np.concatenate([[False], raw[:-1]])
            positions = np.where(fresh)[0]
            results.extend(_build_candidate_rows(
                meta, "이평눌림목", f"{kind}_{period}", f"{kind}{period} 근접",
                close_arr, positions, ma.to_numpy(), trend_term_now))

    if "박스돌파" in signal_types:
        for period in BOX_PERIODS:
            box_top = high.rolling(period).max().shift(1)
            box_bottom = low.rolling(period).min().shift(1)
            range_ok = (((box_top - box_bottom) / box_bottom) <= BOX_MAX_RANGE).to_numpy()
            top_valid = box_top.notna().to_numpy()
            raw = (close_arr > box_top.to_numpy()) & range_ok & top_valid
            fresh = raw & ~np.concatenate([[False], raw[:-1]])
            positions = np.where(fresh)[0]
            results.extend(_build_candidate_rows(
                meta, "박스돌파", f"BOX_{period}", f"박스돌파({period}일)",
                close_arr, positions, box_top.to_numpy(), trend_term_now))

    if "신고가돌파" in signal_types:
        for period in NEWHIGH_PERIODS:
            prior_max = close.rolling(period).max().shift(1)
            pm_valid = prior_max.notna().to_numpy()
            raw = (close_arr > prior_max.to_numpy()) & pm_valid
            positions = np.where(raw)[0]  # 연속 신고가는 각각 별개 이벤트로 인정(중복제거 안 함)
            results.extend(_build_candidate_rows(
                meta, "신고가돌파", f"NEWHIGH_{period}", f"{period}일신고가 돌파",
                close_arr, positions, prior_max.to_numpy(), trend_term_now))

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

    us_tickers = [u["ticker"] for u in universe if u["market"] == "US" and u["ticker"] in prices]
    if us_tickers:
        print(f"미국 종목 시가총액(USD) 조회 중... ({len(us_tickers)}개)")
        caps = {}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exe:
            futs = {exe.submit(_fetch_market_cap_usd, tk): tk for tk in us_tickers}
            for fut in as_completed(futs):
                caps[futs[fut]] = fut.result()
        print(f"  시가총액 조회 성공 {sum(1 for v in caps.values() if v)} / {len(us_tickers)}")
    else:
        caps = {}

    usdkrw = None
    if any(u["market"] == "KR" for u in universe):
        try:
            usdkrw = float(fdr.DataReader("USD/KRW", start_date)["Close"].dropna().iloc[-1])
        except Exception as e:
            print(f"  USD/KRW 환율 조회 실패({e}) -> 한국 종목 시총(USD) 미표기")

    close_map = {tk: df["Close"] for tk, df in prices.items()}
    print("RS(상대강도) 계산 중...")
    rs_total, rs_sector = _compute_rs(universe, close_map)

    print("신호 스캔 중...")
    all_rows, done = [], 0
    for u in universe:
        tk = u["ticker"]
        if tk not in prices:
            continue
        meta = {k: v for k, v in u.items() if not k.startswith("_")}
        if meta["market"] == "US":
            meta["market_cap_usd"] = caps.get(tk)
        elif meta["market"] == "KR" and u.get("_market_cap_krw") and usdkrw:
            meta["market_cap_usd"] = u["_market_cap_krw"] / usdkrw
        meta["rs_total"] = rs_total.get(tk)
        meta["rs_sector"] = rs_sector.get(tk)
        all_rows.extend(_scan_ticker(meta, prices[tk], signal_types))
        done += 1
        if done % 100 == 0 or done == len(prices):
            print(f"  진행 {done}/{len(prices)}")

    elapsed = round(time.time() - t0, 1)
    candidate_counts = {}
    if all_rows:
        result = pd.DataFrame(all_rows)
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
