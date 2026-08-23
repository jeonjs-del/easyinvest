"""계절성 스캐너 배치 스크립트.

내 PC에서 직접 실행한다 (Streamlit Cloud에서는 실행하지 않음):
    python scanner/seasonality_scan.py

한국(KOSPI200 근사)·미국(NASDAQ+NYSE+AMEX 전 종목, scanner/universe.py의
US_MIN_MARKET_CAP_USD 시총 필터만 적용 — 추세추종 스캐너와 동일한
build_universe()를 그대로 재사용한다) 유니버스 전 종목의 최근 LOOKBACK_YEARS년
일봉을 내려받아, (종목, 진입월, 월중 n번째 거래일, 보유기간) 조합별로
연도별 수익률의 승률·평균수익률·표본연수를 계산한다.

LOOKBACK_YEARS=15가 기본값이다(원사이트가 15년 기준으로 보임 — 승률 93% =
15년 중 14번 성공 같은 값과 일치). MIN_SAMPLE_YEARS=13으로, 상장한 지 얼마
안 돼 15년 중 13년 미만의 표본만 가진 종목은 결과에서 아예 제외한다 —
그렇지 않으면 "9/10년인데 승률 100%"처럼 표본이 얇은 종목이 승률만으로
부풀려져 상위에 올라오는 문제가 있었다. 승률 자체는 항상 성공 횟수 ÷ 실제
표본수(n_years)로 계산하고(고정 분모를 쓰지 않음), 화면에는 "n_years/
lookback_years년"으로 실제 표본 기간을 그대로 보여준다(app.py "표본" 열).

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
LOOKBACK_YEARS = 15                     # 원사이트 기준 추정치(15년 중 n번 성공 방식과 일치)
HOLD_PERIODS = [10, 15, 20, 25, 30]     # 보유기간 후보 (거래일 기준)
WIN_RATE_THRESHOLD = 0.90               # 결과에 남길 최소 승률
MIN_SAMPLE_YEARS = 13                   # LOOKBACK_YEARS년 중 이보다 표본이 적으면(상장 얼마 안 된 종목 등) 제외
MAX_WORKERS = 12

# 미국 유니버스 시총 하한: 추세추종(trend_scan.py)의 $50M보다 훨씬 낮게 잡는다.
# 원사이트 계절성 상위권에 Manhattan Bridge Capital($46.6M) 같은 초소형주가 나와
# $50M로는 걸러진다. $5M은 실질적으로 데이터 품질 문제(시총 0/결측에 가까운
# 거래정지·상장폐지 직전 종목)만 걸러내는 수준이고, 실제 시총 구간 필터링은
# app.py UI(MARKET_CAP_BUCKETS)에서 사용자가 고르게 한다.
US_MIN_MARKET_CAP_USD = 5e6

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
    universe = build_universe(us_min_market_cap_usd=US_MIN_MARKET_CAP_USD)
    n_kr = sum(1 for u in universe if u["market"] == "KR")
    n_us = sum(1 for u in universe if u["market"] == "US")
    print(f"유니버스 {len(universe)}종목 (한국 {n_kr} / 미국 {n_us})")
    kr_unclassified = sum(1 for u in universe if u["market"] == "KR" and not u.get("sector"))
    us_unclassified = sum(1 for u in universe if u["market"] == "US" and not u.get("sector"))
    print(f"섹터 미분류: 한국 {kr_unclassified}/{n_kr} · 미국 {us_unclassified}/{n_us}")

    start_date = (pd.Timestamp.today() - pd.DateOffset(years=LOOKBACK_YEARS)).strftime("%Y-%m-%d")

    print("가격 데이터 수집 중...")
    prices, failed = {}, []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exe:
        futs = {exe.submit(_fetch_price, u["ticker"], start_date): u["ticker"] for u in universe}
        for fut in as_completed(futs):
            tk = futs[fut]
            s = fut.result()
            if s is not None:
                prices[tk] = s
            else:
                failed.append(tk)
    print(f"  가격 조회 성공 {len(prices)} / 실패 {len(failed)}")
    if failed:
        head = ", ".join(failed[:30])
        print(f"  실패 종목: {head}{' ...' if len(failed) > 30 else ''}")

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
        if meta["market"] == "KR" and u.get("_market_cap_krw") and usdkrw:
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
