"""계절성 스캐너 배치 스크립트.

내 PC에서 직접 실행한다 (Streamlit Cloud에서는 실행하지 않음):
    python scanner/seasonality_scan.py

한국(KOSPI200 근사)·미국(S&P500) 유니버스 전 종목의 최근 LOOKBACK_YEARS년
일봉을 내려받아, (종목, 진입월, 월중 n번째 거래일, 보유기간) 조합별로
연도별 수익률의 승률·평균수익률·표본연수를 계산한다.

"월중 n번째 거래일"을 진입 시점 단위로 쓰는 이유: 캘린더 날짜(예: 매년 8/15)는
요일이 매년 달라져 실제 거래일이 흔들리므로, 재현성 있는 계절성 신호로는
월중 거래일 인덱스(예: '8월의 11번째 거래일')가 더 안정적이다. 화면에 보여줄
달력 날짜는 app.py가 조회 시점에 영업일 캘린더로 근사해서 계산한다.

결과는 승률 >= WIN_RATE_THRESHOLD, 표본연수 >= MIN_SAMPLE_YEARS인 조합만
data/seasonality.parquet(+ data/seasonality_meta.json)에 저장한다.
Streamlit 앱(app.py)은 이 두 파일을 읽기만 하고 재계산하지 않는다.
"""
import json
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import FinanceDataReader as fdr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scanner.universe import build_universe

warnings.filterwarnings("ignore", category=DeprecationWarning)

# ---- config (필요시 이 값들만 바꾸면 됨) -----------------------------------
LOOKBACK_YEARS = 10
HOLD_PERIODS = [10, 15, 20, 25, 30]     # 보유기간 후보 (거래일 기준)
WIN_RATE_THRESHOLD = 0.90               # 결과에 남길 최소 승률
MIN_SAMPLE_YEARS = 7                    # 표본 연수가 이보다 적으면 신뢰 불가로 제외
MAX_WORKERS = 12

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUTPUT_PARQUET = DATA_DIR / "seasonality.parquet"
OUTPUT_META = DATA_DIR / "seasonality_meta.json"


def _fetch_price(ticker, start):
    try:
        df = fdr.DataReader(ticker, start)
        if df is None or df.empty:
            return None
        col = "Adj Close" if "Adj Close" in df.columns else "Close"
        s = df[col].dropna()
        return s if len(s) > 250 else None
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


def _scan_one(meta, price, hold_periods):
    """한 종목의 (월, 월중n번째거래일, 보유기간)별 연도별 승률/평균수익률 계산."""
    df = pd.DataFrame({"close": price})
    df["year"] = df.index.year
    df["month"] = df.index.month
    df["tdom"] = df.groupby(["year", "month"]).cumcount() + 1

    rows = []
    for hold in hold_periods:
        fwd = df["close"].shift(-hold) / df["close"] - 1.0
        tmp = pd.DataFrame({
            "month": df["month"], "tdom": df["tdom"], "fwd_ret": fwd,
        }).dropna(subset=["fwd_ret"])
        if tmp.empty:
            continue
        grp = tmp.groupby(["month", "tdom"])["fwd_ret"]
        stats = grp.agg(n_years="count", win_rate=lambda s: float((s > 0).mean()),
                         avg_return="mean").reset_index()
        stats["hold_days"] = hold
        rows.append(stats)
    if not rows:
        return pd.DataFrame()
    out = pd.concat(rows, ignore_index=True)
    for k, v in meta.items():
        out[k] = v
    return out


def run():
    t0 = time.time()
    universe = build_universe()
    n_kr = sum(1 for u in universe if u["country"] == "한국")
    n_us = sum(1 for u in universe if u["country"] == "미국")
    print(f"유니버스 {len(universe)}종목 (한국 {n_kr} / 미국 {n_us})")

    start_date = (pd.Timestamp.today() - pd.DateOffset(years=LOOKBACK_YEARS)).strftime("%Y-%m-%d")

    print("가격 데이터 수집 중...")
    prices, failed = {}, []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exe:
        futs = {exe.submit(_fetch_price, u["ticker"], start_date): u["ticker"] for u in universe}
        for fut in as_completed(futs):
            tk = futs[fut]
            s = fut.result()
            (prices if s is not None else {})
            if s is not None:
                prices[tk] = s
            else:
                failed.append(tk)
    print(f"  가격 조회 성공 {len(prices)} / 실패 {len(failed)}")
    if failed:
        head = ", ".join(failed[:30])
        print(f"  실패 종목: {head}{' ...' if len(failed) > 30 else ''}")

    us_tickers = [u["ticker"] for u in universe if u["country"] == "미국" and u["ticker"] in prices]
    print(f"미국 종목 시가총액(USD) 조회 중... ({len(us_tickers)}개)")
    caps = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exe:
        futs = {exe.submit(_fetch_market_cap_usd, tk): tk for tk in us_tickers}
        for fut in as_completed(futs):
            caps[futs[fut]] = fut.result()
    print(f"  시가총액 조회 성공 {sum(1 for v in caps.values() if v)} / {len(us_tickers)}")

    usdkrw = None
    try:
        usdkrw = float(fdr.DataReader("USD/KRW", start_date)["Close"].dropna().iloc[-1])
    except Exception as e:
        print(f"  USD/KRW 환율 조회 실패({e}) -> 한국 종목 시총(USD) 미표기")

    print("계절성 통계 계산 중...")
    all_rows = []
    for u in universe:
        tk = u["ticker"]
        if tk not in prices:
            continue
        meta = {k: v for k, v in u.items() if not k.startswith("_")}
        if meta["country"] == "미국":
            meta["market_cap_usd"] = caps.get(tk)
        elif meta["country"] == "한국" and u.get("_market_cap_krw") and usdkrw:
            meta["market_cap_usd"] = u["_market_cap_krw"] / usdkrw
        stats = _scan_one(meta, prices[tk], HOLD_PERIODS)
        if not stats.empty:
            all_rows.append(stats)

    if not all_rows:
        print("계산된 결과가 없습니다 — 종료합니다.")
        return

    result = pd.concat(all_rows, ignore_index=True)
    before = len(result)
    result = result[(result["win_rate"] >= WIN_RATE_THRESHOLD) & (result["n_years"] >= MIN_SAMPLE_YEARS)]
    result = result.reset_index(drop=True)
    print(f"승률>={WIN_RATE_THRESHOLD:.0%} & 표본>={MIN_SAMPLE_YEARS}년 필터: {before:,}건 -> {len(result):,}건")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    result.to_parquet(OUTPUT_PARQUET, index=False)

    elapsed = round(time.time() - t0, 1)
    meta_out = {
        "as_of": pd.Timestamp.today().strftime("%Y-%m-%d"),
        "lookback_years": LOOKBACK_YEARS,
        "hold_periods": HOLD_PERIODS,
        "win_rate_threshold": WIN_RATE_THRESHOLD,
        "min_sample_years": MIN_SAMPLE_YEARS,
        "universe_size": len(universe),
        "scanned_tickers": len(prices),
        "failed_tickers": failed,
        "result_rows": len(result),
        "elapsed_sec": elapsed,
    }
    with open(OUTPUT_META, "w", encoding="utf-8") as f:
        json.dump(meta_out, f, ensure_ascii=False, indent=2)

    print(f"완료: {len(result):,}건 저장 -> {OUTPUT_PARQUET}")
    print(f"소요시간 {elapsed}초 · 스캔종목 {len(prices)}개 · 실패 {len(failed)}개")


if __name__ == "__main__":
    run()
