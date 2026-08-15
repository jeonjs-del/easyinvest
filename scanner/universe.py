"""매수 탭 계절성 스캐너의 대상 종목(유니버스) 구성.

KOSPI200: KRX 공식 지수구성종목 API가 이 환경에서 불안정하게 응답해,
코스피 시장 시가총액 상위 KR_UNIVERSE_SIZE종목으로 근사한다(실제 지수 구성과
소폭 다를 수 있음 — 정확한 종목 리스트가 필요하면 이 값을 늘리거나
공식 구성종목 파일로 교체).
S&P500: FinanceDataReader가 제공하는 위키피디아 기반 구성종목을 그대로 사용.

섹터는 시장별로 서로 다른 원 체계를 그대로 쓴다(서로 매핑/번역하지 않음):
- 한국: FDR의 'KRX-DESC'(DART 기반) Industry 컬럼 = 한국표준산업분류(KSIC) 업종명.
  전종목의 '소속부'를 뜻하는 'Sector' 컬럼과는 다른 것이니 혼동하지 말 것.
  우선주는 DART에 별도 코드로 없는 경우가 많아, 종목명에서 우선주 접미사
  (우/2우B 등)를 뗀 보통주 이름으로 재조회한다.
- 미국: S&P500 리스트의 GICS Sector 컬럼 그대로.
섹터를 찾지 못하면 None(앱에서 "미분류"로 표시)으로 두고, 다른 시장 값으로
채우지 않는다.

각 항목당 확장하려면 아래 상수만 조정하면 된다.
"""
import re

import pandas as pd
import FinanceDataReader as fdr

KR_UNIVERSE_SIZE = 200   # KOSPI200 근사용 종목 수
US_UNIVERSE_SIZE = None  # S&P500 전체 사용(제한 없음). 정수로 주면 그 개수만.

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


def get_us_universe():
    df = fdr.StockListing("S&P500")
    if US_UNIVERSE_SIZE:
        df = df.head(US_UNIVERSE_SIZE)
    out = []
    for _, r in df.iterrows():
        sector = r.get("Sector") or None
        out.append({
            "ticker": _normalize_us_symbol(r["Symbol"]),
            "name": r["Name"],
            "market": "US",
            "country": "미국",
            "sector": sector,
            "sector_source": "S&P500(GICS)" if sector else None,
            "asset_class": "주식",
            "market_cap_usd": None,       # run() 단계에서 yfinance로 채움
            "_market_cap_krw": None,
        })
    return out


def build_universe():
    return get_kr_universe() + get_us_universe()
