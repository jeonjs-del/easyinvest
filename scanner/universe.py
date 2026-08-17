"""매수 탭 계절성/추세추종 스캐너의 대상 종목(유니버스) 구성.

KOSPI200: KRX 공식 지수구성종목 API가 이 환경에서 불안정하게 응답해,
코스피 시장 시가총액 상위 KR_UNIVERSE_SIZE종목으로 근사한다(실제 지수 구성과
소폭 다를 수 있음 — 정확한 종목 리스트가 필요하면 이 값을 늘리거나
공식 구성종목 파일로 교체).

미국: NASDAQ+NYSE+AMEX 전종목(ADR 포함)을 네이버 marketValue API로 직접 조회한다.
이전에는 S&P500(500종목)만 썼는데, 원사이트(easyinvesting.app) 추세추종 상위권에
Seneca Foods·Fortress Biotech·BrainsWay·Grupo Cibest ADR 같은 중소형주/ADR이 나와
S&P500 유니버스로는 겹치지 않았다. 이 API는 시가총액(USD)도 함께 주므로
yfinance로 종목당 따로 조회하지 않고 한 번에 걸러낸다 — 스캔 규모가 커져도
빠르다(전종목 조회 ~10초).
시총 하한 US_MIN_MARKET_CAP_USD로 1차 필터링한다. $1B로 필터하면 원사이트 상위권
예시 중 Fortress Biotech(~$98M)·BrainsWay(~$655M)·Park Aerospace(~$830M)가 통째로
빠져버려 목적(중소형주 포함)에 어긋난다. 그래서 훨씬 낮은 $50M을 기본값으로 둔다
— 극소형/거래정지에 가까운 종목만 걸러내고, 나머지는 이후 스캔 단계의 데이터
품질 체크(가격 이력 길이 등)에서 자연스럽게 걸러지도록 한다.

섹터는 시장별로 서로 다른 원 체계를 그대로 쓴다(서로 매핑/번역하지 않음):
- 한국: FDR의 'KRX-DESC'(DART 기반) Industry 컬럼 = 한국표준산업분류(KSIC) 업종명.
  전종목의 '소속부'를 뜻하는 'Sector' 컬럼과는 다른 것이니 혼동하지 말 것.
  우선주는 DART에 별도 코드로 없는 경우가 많아, 종목명에서 우선주 접미사
  (우/2우B 등)를 뗀 보통주 이름으로 재조회한다.
- 미국: S&P500 리스트의 GICS Sector 컬럼 그대로. S&P500에 없는(대부분 중소형)
  종목은 GICS 분류가 없으므로 섹터 None("미분류")으로 남긴다 — 네이버 API가 주는
  업종명은 한국어 KSIC류 분류라 GICS와 체계가 달라 섞어 쓰지 않는다.
섹터를 찾지 못하면 None(앱에서 "미분류"로 표시)으로 두고, 다른 시장 값으로
채우지 않는다.

각 항목당 확장하려면 아래 상수만 조정하면 된다.
"""
import json
import re

import pandas as pd
import requests
import FinanceDataReader as fdr

KR_UNIVERSE_SIZE = 200          # KOSPI200 근사용 종목 수
US_MIN_MARKET_CAP_USD = 50e6    # 미국 유니버스 1차 시총 필터(위 설명 참고)

_NAVER_HEADERS = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_NAVER_EXCHANGES = ["NASDAQ", "NYSE", "AMEX"]

_PREF_SUFFIX_RE = re.compile(r"\d?우B?$")  # 한국 우선주 종목명 접미사: 우, 우B, 2우B 등


def _normalize_us_symbol(sym):
    """Yahoo/FDR 조회용으로 '.'을 '-'로 치환 (예: BRK.B -> BRK-B)."""
    return sym.replace(".", "-")


def _kr_industry_lookup():
    """FDR 'KRX-DESC'(DART 기반)에서 Code/Name -> 업종(Industry, KSIC) 매핑을 만든다."""
    desc = fdr.StockListing("KRX-DESC")
    valid = desc.dropna(subset=["Industry"])
    by_code = valid.set_index("Code")["Industry"].to_dict()
    by_name = valid.set_index("Name")["Industry"].to_dict()
    return by_code, by_name


def _kr_sector_for(code, name, by_code, by_name):
    sector = by_code.get(code)
    if sector:
        return sector
    base_name = _PREF_SUFFIX_RE.sub("", name)  # 우선주 -> 보통주 이름으로 재조회
    return by_name.get(base_name)


def get_kr_universe():
    df = fdr.StockListing("KRX")
    kospi = df[df["Market"] == "KOSPI"].copy()
    kospi = kospi.sort_values("Marcap", ascending=False).head(KR_UNIVERSE_SIZE)
    by_code, by_name = _kr_industry_lookup()
    out = []
    for _, r in kospi.iterrows():
        sector = _kr_sector_for(r["Code"], r["Name"], by_code, by_name)
        out.append({
            "ticker": r["Code"],
            "name": r["Name"],
            "market": "KR",
            "country": "한국",
            "sector": sector,
            "sector_source": "KRX-DESC(KSIC)" if sector else None,
            "asset_class": "주식",
            "market_cap_usd": None,       # run() 단계에서 USD/KRW로 환산해 채움
            "_market_cap_krw": float(r["Marcap"]) if pd.notna(r["Marcap"]) else None,
        })
    return out


def _sp500_gics_lookup():
    """S&P500 구성종목의 GICS Sector만 별도로 가져온다 (ticker -> sector)."""
    df = fdr.StockListing("S&P500")
    out = {}
    for _, r in df.iterrows():
        sector = r.get("Sector") or None
        if sector:
            out[_normalize_us_symbol(r["Symbol"])] = sector
    return out


def _fetch_naver_exchange_listing(exchange):
    """네이버 marketValue API에서 거래소 전종목 + 시가총액(USD, marketValueRaw)을 가져온다.
    FDR의 StockListing('NASDAQ'/'NYSE'/'AMEX')도 내부적으로 이 API를 쓰지만 종목명/
    업종코드만 남기고 시가총액을 버리므로, 직접 호출해 시가총액까지 확보한다."""
    rows = []
    page = 1
    while True:
        url = (f"http://api.stock.naver.com/stock/exchange/{exchange}/"
               f"marketValue?page={page}&pageSize=100")
        r = requests.get(url, headers=_NAVER_HEADERS, timeout=15)
        jo = json.loads(r.content.decode("utf-8"))
        stocks = jo.get("stocks", [])
        if not stocks:
            break
        rows.extend(stocks)
        page += 1
    return rows


def get_us_universe():
    gics = _sp500_gics_lookup()
    seen, out = set(), []
    for exchange in _NAVER_EXCHANGES:
        for s in _fetch_naver_exchange_listing(exchange):
            sym = s.get("symbolCode")
            if not sym or sym in seen:
                continue
            cap_raw = s.get("marketValueRaw")
            cap = float(cap_raw) if cap_raw else None
            if cap is None or cap < US_MIN_MARKET_CAP_USD:
                continue
            seen.add(sym)
            ticker = _normalize_us_symbol(sym)
            sector = gics.get(ticker)
            out.append({
                "ticker": ticker,
                "name": s.get("stockNameEng") or sym,
                "market": "US",
                "country": "미국",
                "sector": sector,
                "sector_source": "S&P500(GICS)" if sector else None,
                "asset_class": "주식",
                "market_cap_usd": cap,
                "_market_cap_krw": None,
            })
    return out


def build_universe():
    return get_kr_universe() + get_us_universe()
