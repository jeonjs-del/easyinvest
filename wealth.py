"""은퇴 탭(총자산 · 경제적 자유)의 순수 계산 로직.

Streamlit·네트워크에 의존하지 않는다 — 화면과 외부 조회는 app.py가 맡고, 여기 함수들은
tests/test_wealth.py로 오프라인 검증한다. 모든 금액의 내부 단위는 "원"이다.
"""
import calendar
import copy
import re
import statistics
import xml.etree.ElementTree as ET
from datetime import date, datetime

MAN = 10_000
EOK = 100_000_000

# 금융자산 항목: (키, 표시명, 보유 종목 종류). 예금은 잔액만 입력한다.
SECTIONS = [
    ("deposit", "예금", None),
    ("retirement", "퇴직연금", "etf"),
    ("personal", "개인연금", "etf"),
    ("stock", "주식", "stock"),
    ("crypto", "암호화폐", "crypto"),
]

# 암호화폐 allowlist: 표시 심볼 → (한글명, Yahoo Finance 코드).
# HYPE는 Yahoo에 동명 자산이 여럿이라 "HYPE-USD"가 아니라 하이퍼리퀴드 고유 코드
# HYPE32196-USD를 써야 한다(일별 가격 응답 확인됨).
CRYPTO_CATALOG = {
    "BTC": ("비트코인", "BTC-USD"),
    "ETH": ("이더리움", "ETH-USD"),
    "SOL": ("솔라나", "SOL-USD"),
    "XRP": ("리플", "XRP-USD"),
    "BNB": ("비앤비", "BNB-USD"),
    "DOGE": ("도지코인", "DOGE-USD"),
    "ADA": ("에이다", "ADA-USD"),
    "LINK": ("체인링크", "LINK-USD"),
    "HYPE": ("하이퍼리퀴드", "HYPE32196-USD"),
}

_US_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9]{0,5}([.\-][A-Z]{1,2})?$")
_KR_CODE_RE = re.compile(r"^[0-9A-Z]{6}$")

DEFAULT_FIRE = {
    "start_override": None,     # None이면 현재 금융 순자산을 시작 자산으로 사용
    "annual_saving": 0,
    "inflation": 2.0,
    "years": 10,
    "annual_return": 5.0,
    "withdraw_rate": 4.0,
    "target_monthly": 0,
}

_DEFAULT_PROPERTY = {
    "address": "", "complex": "", "trade_complex": "", "dong": "", "lawd_cd": "",
    "pyeong": "", "area_m2": 0.0, "acquired_on": None, "acquired_price": 0,
    "value": 0, "value_asof": None, "value_source": "manual",
    "mortgage": 0, "private_loan": 0,
}


# ---------------------------------------------------------------------------
#  금액 표시
# ---------------------------------------------------------------------------
def man_to_won(man):
    return int(round(float(man) * MAN))


def won_to_man(won):
    return int(round(float(won or 0) / MAN))


def apply_man_input(stored_won, input_man):
    """만원 입력값을 원으로 반영. 입력이 저장값을 만원으로 본 것과 같으면 저장값을 그대로
    돌려준다 — 새로고침·재저장 때마다 만원↔원 변환이 반복 적용돼 값이 달라지는 걸 막는다."""
    stored_won = int(stored_won or 0)
    if won_to_man(stored_won) == int(input_man):
        return stored_won
    return man_to_won(input_man)


def _trim(x, digits=2):
    s = f"{x:,.{digits}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def format_krw(won):
    """절댓값 1억원 이상은 억원, 미만은 만원 단위. 1,495,000,000 → '14.95억원'."""
    if won is None:
        return "—"
    won = float(won)
    sign = "-" if won < 0 else ""
    a = abs(won)
    if a >= EOK:
        return f"{sign}{_trim(round(a / EOK, 2))}억원"
    man = int(a / MAN + 0.5)
    if man >= MAN:          # 9,999.5만원 이상은 반올림하면 1억원
        return f"{sign}1억원"
    if man == 0:
        return "0원" if a < 0.5 else f"{sign}{int(a + 0.5):,}원"
    return f"{sign}{man:,}만원"


_DIGITS = "영일이삼사오육칠팔구"
_SMALL_UNITS = ["", "십", "백", "천"]
_BIG_UNITS = ["", "만", "억", "조", "경"]


def _korean_group(n):
    out = ""
    for i in range(3, -1, -1):
        d = (n // 10 ** i) % 10
        if d == 0:
            continue
        # 십·백·천 앞의 '일'은 생략(십사억, 사백만)
        out += ("" if d == 1 and i > 0 else _DIGITS[d]) + _SMALL_UNITS[i]
    return out


def korean_amount(won):
    """원 금액을 한글로. 1,495,000,000 → '십사억 구천오백만원'."""
    won = int(round(float(won or 0)))
    if won == 0:
        return "영원"
    sign = "마이너스 " if won < 0 else ""
    n, parts, idx = abs(won), [], 0
    while n > 0:
        n, grp = divmod(n, 10_000)
        if grp:
            parts.append(_korean_group(grp) + _BIG_UNITS[idx])
        idx += 1
    return sign + " ".join(reversed(parts)) + "원"


# ---------------------------------------------------------------------------
#  날짜
# ---------------------------------------------------------------------------
def to_date(v):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def month_end(year, month):
    return date(year, month, calendar.monthrange(year, month)[1])


def years_between(d1, d2):
    d1, d2 = to_date(d1), to_date(d2)
    if not d1 or not d2:
        return None
    return (d2 - d1).days / 365.25


# ---------------------------------------------------------------------------
#  문서(저장 데이터) 정규화
# ---------------------------------------------------------------------------
def default_doc():
    fin = {"debt": 0}
    for key, _, kind in SECTIONS:
        fin[key] = {"cash": 0} if kind is None else {"cash": 0, "holdings": []}
    return {"version": 1, "property": dict(_DEFAULT_PROPERTY), "financial": fin,
            "records": [], "fire": dict(DEFAULT_FIRE), "last_quotes": {}}


def normalize_doc(raw):
    """저장된 문서에 없는 키만 기본값으로 채운다. 사용자가 넣은 0이나 비운 목록은 그대로 둔다."""
    doc = default_doc()
    if not isinstance(raw, dict):
        return doc
    raw = copy.deepcopy(raw)
    if isinstance(raw.get("property"), dict):
        doc["property"].update(raw["property"])
    fin_raw = raw.get("financial") if isinstance(raw.get("financial"), dict) else {}
    if "debt" in fin_raw:
        doc["financial"]["debt"] = fin_raw["debt"]
    for key, _, kind in SECTIONS:
        sec = fin_raw.get(key)
        if not isinstance(sec, dict):
            continue
        if "cash" in sec:
            doc["financial"][key]["cash"] = sec["cash"]
        if kind is not None and isinstance(sec.get("holdings"), list):
            doc["financial"][key]["holdings"] = [
                h for h in sec["holdings"]
                if isinstance(h, dict) and h.get("symbol") and h.get("market") in ("KR", "US", "CRYPTO")
            ]
    if isinstance(raw.get("records"), list):
        doc["records"] = sorted(
            (r for r in raw["records"] if isinstance(r, dict) and to_date(r.get("date"))),
            key=lambda r: r["date"])
    if isinstance(raw.get("fire"), dict):
        doc["fire"].update(raw["fire"])
    if isinstance(raw.get("last_quotes"), dict):
        doc["last_quotes"] = raw["last_quotes"]
    return doc


def valid_symbol(market, symbol):
    if market == "KR":
        return bool(_KR_CODE_RE.match(symbol or ""))
    if market == "US":
        return bool(_US_TICKER_RE.match(symbol or ""))
    if market == "CRYPTO":
        return symbol in CRYPTO_CATALOG
    return False


def quote_key(market, symbol):
    return f"{market}:{symbol}"


def upsert_holding(holdings, market, symbol, name, qty):
    """같은 항목 안에서 같은 종목을 다시 추가하면 수량을 더한다."""
    out = [dict(h) for h in holdings]
    for h in out:
        if h["market"] == market and h["symbol"] == symbol:
            h["qty"] = float(h.get("qty") or 0) + float(qty)
            return out
    out.append({"market": market, "symbol": symbol, "name": name, "qty": float(qty)})
    return out


# ---------------------------------------------------------------------------
#  시세: 확정 종가 · 환율
# ---------------------------------------------------------------------------
def pick_confirmed_close(rows, cutoff):
    """rows: [(date, close), ...]. cutoff '미만' 날짜 중 가장 최근의 유효 종가.
    cutoff 당일(진행 중인 봉)과 결측은 제외한다. 없으면 None."""
    best = None
    for d, c in rows:
        d = to_date(d)
        if d is None or c is None or c != c or c <= 0 or d >= cutoff:
            continue
        if best is None or d > best[0]:
            best = (d, float(c))
    return best


def pick_fx(rows, price_date, cutoff):
    """종가 기준일 '이전(당일 포함)'이면서 확정된(cutoff 미만) 가장 최근 환율."""
    best = None
    for d, c in rows:
        d = to_date(d)
        if d is None or c is None or c != c or c <= 0 or d > price_date or d >= cutoff:
            continue
        if best is None or d > best[0]:
            best = (d, float(c))
    return best


def value_holding(qty, quote):
    """quote: {'close','date','currency','fx','status'} 또는 None. 원화 평가액 또는 None."""
    if not quote or quote.get("close") is None:
        return None
    if quote.get("currency") == "USD":
        if not quote.get("fx"):
            return None
        return float(qty) * quote["close"] * quote["fx"]
    return float(qty) * quote["close"]


def section_total(section, quotes):
    """(소계 원, 상태). 상태: 'ok' | 'stale' | 'incomplete'. 미완료 종목은 소계에서 빠진다."""
    total, status = float(section.get("cash") or 0), "ok"
    for h in section.get("holdings") or []:
        q = quotes.get(quote_key(h["market"], h["symbol"]))
        v = value_holding(h.get("qty") or 0, q)
        if v is None:
            status = "incomplete"
            continue
        total += v
        if q.get("status") == "stale" and status == "ok":
            status = "stale"
    return total, status


def summarize(doc, quotes):
    prop, fin = doc["property"], doc["financial"]
    sections, worst = {}, "ok"
    for key, _, _ in SECTIONS:
        sections[key] = section_total(fin[key], quotes)
        st = sections[key][1]
        if st == "incomplete" or (st == "stale" and worst == "ok"):
            worst = st
    financial = sum(v for v, _ in sections.values())
    property_value = float(prop.get("value") or 0)
    property_debt = float(prop.get("mortgage") or 0) + float(prop.get("private_loan") or 0)
    financial_debt = float(fin.get("debt") or 0)
    total = property_value + financial
    debt = property_debt + financial_debt
    return {"property": property_value, "financial": financial,
            "property_debt": property_debt, "financial_debt": financial_debt,
            "total": total, "debt": debt, "net": total - debt,
            "financial_net": financial - financial_debt,
            "sections": sections, "status": worst}


# ---------------------------------------------------------------------------
#  부동산
# ---------------------------------------------------------------------------
def property_change(acquired_price, value, acquired_on, asof):
    """취득 대비 (증감액, 증감률%, CAGR%). 계산할 수 없는 값은 None."""
    acquired_price, value = float(acquired_price or 0), float(value or 0)
    if acquired_price <= 0:
        return None, None, None
    diff = value - acquired_price
    pct = diff / acquired_price * 100
    yrs = years_between(acquired_on, asof)
    cagr = None
    if yrs and yrs > 0 and value > 0:
        cagr = ((value / acquired_price) ** (1 / yrs) - 1) * 100
    return diff, pct, cagr


def _norm_name(s):
    return re.sub(r"\s+", "", str(s or ""))


def parse_molit_xml(text):
    """국토교통부 아파트 매매 실거래가 응답 → (거래 리스트, 오류메시지).
    신규(영문 태그)·구(한글 태그) 응답을 모두 받는다. 구조·숫자·날짜가 어긋난 항목은 버린다."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return [], "XML 파싱 실패"
    code = (root.findtext(".//resultCode") or "").strip()
    if code not in ("00", "000"):
        msg = (root.findtext(".//resultMsg") or root.findtext(".//returnAuthMsg")
               or root.findtext(".//errMsg") or "응답 코드 없음")
        return [], f"{code or '-'} {msg.strip()}"

    def pick(item, *tags):
        for t in tags:
            v = item.findtext(t)
            if v is not None and v.strip() != "":
                return v.strip()
        return ""

    trades = []
    for item in root.iter("item"):
        try:
            d = date(int(pick(item, "dealYear", "년")), int(pick(item, "dealMonth", "월")),
                     int(pick(item, "dealDay", "일")))
            price = int(pick(item, "dealAmount", "거래금액").replace(",", "")) * MAN
            area = float(pick(item, "excluUseAr", "전용면적"))
        except ValueError:
            continue
        if price <= 0 or area <= 0:
            continue
        cancelled = bool(pick(item, "cdealType", "해제여부") or pick(item, "cdealDay", "해제사유발생일"))
        trades.append({
            "date": d, "price": price, "area": area,
            "complex": pick(item, "aptNm", "아파트"), "dong": pick(item, "umdNm", "법정동"),
            "floor": pick(item, "floor", "층"), "cancelled": cancelled,
        })
    return trades, None


def match_trades(trades, complex_name, dong, area_m2, today, area_tol=1.0):
    """동일 단지·법정동·전용면적 ±tol 거래 중 가장 최근 계약일 것. 취소·미래 거래 제외.
    같은 날 여러 건이면 가격 중앙값. 반환: {'date','price','count','trades'} 또는 None."""
    cn, dn = _norm_name(complex_name), _norm_name(dong)
    ok = [t for t in trades
          if not t["cancelled"] and t["date"] <= today
          and _norm_name(t["complex"]) == cn and _norm_name(t["dong"]) == dn
          and abs(t["area"] - float(area_m2)) <= area_tol]
    if not ok:
        return None
    latest = max(t["date"] for t in ok)
    same = [t for t in ok if t["date"] == latest]
    return {"date": latest, "price": int(statistics.median(t["price"] for t in same)),
            "count": len(same), "trades": same}


def should_apply_trade(trade_date, acquired_on, value_asof):
    """취득일·현재 평가 기준일보다 이전 거래로는 평가액을 되돌리지 않는다."""
    for d in (to_date(acquired_on), to_date(value_asof)):
        if d and trade_date < d:
            return False
    return True


def recent_months(today, n=12):
    """오늘이 속한 달부터 거꾸로 n개월의 'YYYYMM'."""
    y, m, out = today.year, today.month, []
    for _ in range(n):
        out.append(f"{y}{m:02d}")
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out


# ---------------------------------------------------------------------------
#  자산 기록
# ---------------------------------------------------------------------------
RECORD_FIELDS = ("property", "financial", "property_debt", "financial_debt")


def make_record(d, property_value, financial, property_debt, financial_debt):
    return {"date": to_date(d).isoformat(), "property": int(round(property_value)),
            "financial": int(round(financial)), "property_debt": int(round(property_debt)),
            "financial_debt": int(round(financial_debt))}


def upsert_record(records, rec):
    """같은 날짜가 있으면 그 날짜만 교체. 날짜순 정렬해 새 리스트 반환."""
    out = [r for r in records if r["date"] != rec["date"]]
    out.append(rec)
    return sorted(out, key=lambda r: r["date"])


def delete_record(records, d):
    key = to_date(d).isoformat()
    return [r for r in records if r["date"] != key]


def can_record_on(d, today):
    return to_date(d) <= today


def record_totals(rec):
    total = rec["property"] + rec["financial"]
    return {"total": total, "net": total - rec["property_debt"] - rec["financial_debt"],
            "property": rec["property"], "financial": rec["financial"]}


def period_last(records, period):
    """월별('M')·연별('Y')로 묶어 각 기간의 마지막 저장 기록만 남긴다. 빈 기간은 채우지 않는다."""
    n = 7 if period == "M" else 4
    last = {}
    for r in sorted(records, key=lambda r: r["date"]):
        last[r["date"][:n]] = r
    return [(k, last[k]) for k in sorted(last)]


# ---------------------------------------------------------------------------
#  경제적 자유
# ---------------------------------------------------------------------------
def project_assets(start, annual_saving, inflation_pct, years, return_pct):
    """매년 기존 자산에 수익률을 적용한 뒤 연말 저축액을 더한다. 저축액은 물가상승률만큼 증가."""
    asset, saving = float(start), float(annual_saving)
    r, g = return_pct / 100, inflation_pct / 100
    for _ in range(int(years)):
        asset = asset * (1 + r) + saving
        saving *= 1 + g
    return asset


def monthly_spend(asset, withdraw_pct):
    return asset * withdraw_pct / 100 / 12


def real_value(amount, inflation_pct, years):
    return amount / (1 + inflation_pct / 100) ** int(years)


def years_to_target(start, annual_saving, inflation_pct, return_pct, withdraw_pct,
                    target_monthly, max_years=100):
    """현재 구매력 기준 월 사용액이 목표에 처음 도달하는 은퇴 시점(0~max_years년). 없으면 None."""
    if target_monthly is None or target_monthly <= 0:
        return None
    for n in range(max_years + 1):
        asset = project_assets(start, annual_saving, inflation_pct, n, return_pct)
        if real_value(monthly_spend(asset, withdraw_pct), inflation_pct, n) >= target_monthly:
            return n
    return None
