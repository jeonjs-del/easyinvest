"""은퇴 탭 계산 로직(wealth.py) 오프라인 검증. 네트워크 불필요.

    python -m unittest tests.test_wealth -v
"""
import json
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wealth as W  # noqa: E402


class AmountTest(unittest.TestCase):
    def test_format_boundaries(self):
        self.assertEqual(W.format_krw(1_234_000_000), "12.34억원")
        self.assertEqual(W.format_krw(100_000_000), "1억원")
        self.assertEqual(W.format_krw(40_000_000), "4,000만원")
        self.assertEqual(W.format_krw(99_990_000), "9,999만원")
        self.assertEqual(W.format_krw(99_999_999), "1억원")
        self.assertEqual(W.format_krw(0), "0원")
        self.assertEqual(W.format_krw(None), "—")

    def test_format_negative(self):
        self.assertEqual(W.format_krw(-1_234_000_000), "-12.34억원")
        self.assertEqual(W.format_krw(-40_000_000), "-4,000만원")
        self.assertEqual(W.format_krw(-100_000_000), "-1억원")

    def test_korean(self):
        self.assertEqual(W.korean_amount(W.man_to_won(123400)), "십이억 삼천사백만원")
        self.assertEqual(W.korean_amount(W.man_to_won(400)), "사백만원")
        self.assertEqual(W.korean_amount(100_000_000), "일억원")
        self.assertEqual(W.korean_amount(0), "영원")

    def test_man_won_roundtrip_is_stable(self):
        # 만원 배수가 아닌 저장값도 같은 입력으로 다시 저장하면 그대로여야 한다
        stored = 1_234_567_890
        for _ in range(5):
            stored = W.apply_man_input(stored, W.won_to_man(stored))
        self.assertEqual(stored, 1_234_567_890)
        self.assertEqual(W.apply_man_input(stored, 123400), 1_234_000_000)
        self.assertEqual(W.apply_man_input(1_234_000_000, 123400), 1_234_000_000)


class DocTest(unittest.TestCase):
    def test_zero_and_empty_are_kept(self):
        raw = {"property": {"mortgage": 0}, "fire": {"years": 0},
               "financial": {"retirement": {"cash": 0, "holdings": []}}}
        doc = W.normalize_doc(raw)
        self.assertEqual(doc["fire"]["years"], 0)
        self.assertEqual(doc["financial"]["retirement"]["holdings"], [])
        self.assertEqual(doc["fire"]["annual_return"], 5.0)   # 없는 키만 기본값

    def test_pension_accounts_hold_same_etf_independently(self):
        doc = W.default_doc()
        fin = doc["financial"]
        fin["retirement"]["holdings"] = W.upsert_holding([], "KR", "069500", "KODEX 200", 10)
        fin["personal"]["holdings"] = W.upsert_holding([], "KR", "069500", "KODEX 200", 3)
        doc = W.normalize_doc(json.loads(json.dumps(doc)))
        self.assertEqual(doc["financial"]["retirement"]["holdings"][0]["qty"], 10)
        self.assertEqual(doc["financial"]["personal"]["holdings"][0]["qty"], 3)
        quotes = {"KR:069500": {"close": 100_000, "currency": "KRW", "status": "ok"}}
        s = W.summarize(doc, quotes)
        self.assertEqual(s["sections"]["retirement"][0], 1_000_000)
        self.assertEqual(s["sections"]["personal"][0], 300_000)

    def test_duplicate_holdings_in_a_section_are_merged(self):
        # 직접 편집한 파일에 같은 종목이 두 줄이면 화면 입력칸 key가 겹친다
        raw = {"financial": {"stock": {"cash": 0, "holdings": [
            {"market": "US", "symbol": "SPY", "name": "SPY", "qty": 10},
            {"market": "KR", "symbol": "005930", "name": "삼성전자", "qty": 5},
            {"market": "US", "symbol": "SPY", "name": "SPY", "qty": 2.5},
        ]}}}
        holdings = W.normalize_doc(raw)["financial"]["stock"]["holdings"]
        self.assertEqual([(h["symbol"], h["qty"]) for h in holdings], [("SPY", 12.5), ("005930", 5)])

    def test_summary_formulas(self):
        doc = W.default_doc()
        doc["property"].update(value=2_000_000_000, mortgage=500_000_000, private_loan=100_000_000)
        doc["financial"]["deposit"]["cash"] = 300_000_000
        doc["financial"]["debt"] = 50_000_000
        s = W.summarize(doc, {})
        self.assertEqual(s["total"], 2_300_000_000)
        self.assertEqual(s["debt"], 650_000_000)
        self.assertEqual(s["net"], 1_650_000_000)
        self.assertEqual(s["financial_net"], 250_000_000)

    def test_incomplete_and_stale_status(self):
        doc = W.default_doc()
        doc["financial"]["stock"]["holdings"] = [
            {"market": "US", "symbol": "SPY", "name": "SPY", "qty": 2}]
        self.assertEqual(W.summarize(doc, {})["status"], "incomplete")
        q = {"US:SPY": {"close": 700.0, "currency": "USD", "fx": None, "status": "ok"}}
        self.assertEqual(W.summarize(doc, q)["status"], "incomplete")   # 환율 누락
        q["US:SPY"].update(fx=1400.0, status="stale")
        s = W.summarize(doc, q)
        self.assertEqual((s["status"], s["financial"]), ("stale", 1_960_000))

    def test_symbols(self):
        self.assertTrue(W.valid_symbol("CRYPTO", "HYPE"))
        self.assertEqual(W.CRYPTO_CATALOG["HYPE"][1], "HYPE32196-USD")
        codes = [c for _, c in W.CRYPTO_CATALOG.values()]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertFalse(W.valid_symbol("CRYPTO", "NOPE"))
        self.assertTrue(W.valid_symbol("US", "BRK.B"))
        self.assertFalse(W.valid_symbol("US", "../etc"))
        self.assertTrue(W.valid_symbol("KR", "069500"))


class QuoteTest(unittest.TestCase):
    def test_todays_bar_excluded(self):
        rows = [(date(2026, 10, 5), 85786.0), (date(2026, 10, 6), 85557.0), (date(2026, 10, 7), 83591.0)]
        self.assertEqual(W.pick_confirmed_close(rows, date(2026, 10, 7)), (date(2026, 10, 6), 85557.0))

    def test_holiday_keeps_last_trading_day_and_skips_nan(self):
        rows = [(date(2026, 10, 2), 100.0), (date(2026, 10, 6), float("nan"))]
        self.assertEqual(W.pick_confirmed_close(rows, date(2026, 10, 7)), (date(2026, 10, 2), 100.0))
        self.assertIsNone(W.pick_confirmed_close([], date(2026, 10, 7)))

    def test_fx_not_after_price_date(self):
        fx = [(date(2026, 10, 2), 1340.0), (date(2026, 10, 5), 1343.75), (date(2026, 10, 7), 1339.2)]
        self.assertEqual(W.pick_fx(fx, date(2026, 10, 4), date(2026, 10, 7)), (date(2026, 10, 2), 1340.0))
        self.assertEqual(W.pick_fx(fx, date(2026, 10, 6), date(2026, 10, 7)), (date(2026, 10, 5), 1343.75))


class PropertyTest(unittest.TestCase):
    def test_change(self):
        diff, pct, cagr = W.property_change(1_000_000_000, 1_210_000_000, "2022-03-01", "2024-03-01")
        self.assertEqual(diff, 210_000_000)
        self.assertAlmostEqual(pct, 21.0)
        self.assertAlmostEqual(cagr, 10.0, delta=0.05)   # 2년간 1.21배 → 연 10%

    def test_not_computable(self):
        self.assertEqual(W.property_change(0, 1, "2022-03-01", "2024-03-01"), (None, None, None))
        self.assertIsNone(W.property_change(100, 200, "2022-03-01", "2022-03-01")[2])

    XML = """<response><header><resultCode>000</resultCode><resultMsg>OK</resultMsg></header><body><items>
    <item><aptNm>테스트마을(가나)</aptNm><umdNm>가나동</umdNm><excluUseAr>84.93</excluUseAr><dealYear>2026</dealYear><dealMonth>9</dealMonth><dealDay>20</dealDay><dealAmount>200,000</dealAmount><floor>5</floor><cdealType> </cdealType></item>
    <item><aptNm>테스트마을(가나)</aptNm><umdNm>가나동</umdNm><excluUseAr>84.50</excluUseAr><dealYear>2026</dealYear><dealMonth>9</dealMonth><dealDay>20</dealDay><dealAmount>210,000</dealAmount><floor>9</floor></item>
    <item><aptNm>테스트마을(가나)</aptNm><umdNm>가나동</umdNm><excluUseAr>84.93</excluUseAr><dealYear>2026</dealYear><dealMonth>9</dealMonth><dealDay>20</dealDay><dealAmount>230,000</dealAmount><floor>12</floor></item>
    <item><aptNm>테스트마을(가나)</aptNm><umdNm>가나동</umdNm><excluUseAr>84.93</excluUseAr><dealYear>2026</dealYear><dealMonth>9</dealMonth><dealDay>28</dealDay><dealAmount>300,000</dealAmount><floor>3</floor><cdealType>O</cdealType><cdealDay>26.09.30</cdealDay></item>
    <item><aptNm>테스트마을(가나)</aptNm><umdNm>가나동</umdNm><excluUseAr>84.93</excluUseAr><dealYear>2026</dealYear><dealMonth>12</dealMonth><dealDay>1</dealDay><dealAmount>999,000</dealAmount><floor>3</floor></item>
    <item><aptNm>테스트마을(가나)</aptNm><umdNm>가나동</umdNm><excluUseAr>59.90</excluUseAr><dealYear>2026</dealYear><dealMonth>9</dealMonth><dealDay>25</dealDay><dealAmount>150,000</dealAmount><floor>3</floor></item>
    <item><aptNm>다른단지</aptNm><umdNm>가나동</umdNm><excluUseAr>84.93</excluUseAr><dealYear>2026</dealYear><dealMonth>9</dealMonth><dealDay>27</dealDay><dealAmount>170,000</dealAmount><floor>3</floor></item>
    <item><aptNm>테스트마을(가나)</aptNm><umdNm>가나동</umdNm><excluUseAr>abc</excluUseAr><dealYear>2026</dealYear><dealMonth>9</dealMonth><dealDay>27</dealDay><dealAmount>170,000</dealAmount></item>
    </items></body></response>"""

    def test_molit_match(self):
        trades, err = W.parse_molit_xml(self.XML)
        self.assertIsNone(err)
        self.assertEqual(len(trades), 7)   # 면적이 숫자가 아닌 항목은 버림
        m = W.match_trades(trades, "테스트마을 (가나)", "가나동", 85, date(2026, 10, 7))
        # 취소(9/28)·미래(12/1)·다른 면적·다른 단지 제외 → 9/20 3건의 중앙값
        self.assertEqual((m["date"], m["price"], m["count"]), (date(2026, 9, 20), 2_100_000_000, 3))

    def test_molit_error_and_rollback_guard(self):
        bad = "<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</errMsg></cmmMsgHeader></OpenAPI_ServiceResponse>"
        self.assertEqual(W.parse_molit_xml(bad)[0], [])
        self.assertIsNotNone(W.parse_molit_xml(bad)[1])
        self.assertIsNotNone(W.parse_molit_xml("not xml")[1])
        self.assertFalse(W.should_apply_trade(date(2025, 3, 1), "2025-03-31", None))
        self.assertFalse(W.should_apply_trade(date(2026, 1, 1), "2025-03-31", "2026-03-01"))
        self.assertTrue(W.should_apply_trade(date(2026, 3, 1), "2025-03-31", "2026-03-01"))

    def test_recent_months(self):
        ms = W.recent_months(date(2026, 2, 10))
        self.assertEqual((len(ms), ms[0], ms[2], ms[-1]), (12, "202602", "202512", "202503"))


class RecordTest(unittest.TestCase):
    def test_month_end_and_leap_year(self):
        self.assertEqual(W.month_end(2024, 2), date(2024, 2, 29))
        self.assertEqual(W.month_end(2026, 2), date(2026, 2, 28))
        self.assertEqual(W.month_end(2026, 9), date(2026, 9, 30))
        today = date(2026, 10, 7)
        self.assertFalse(W.can_record_on(W.month_end(2026, 10), today))   # 이번 달 월말은 월말부터
        self.assertTrue(W.can_record_on(W.month_end(2026, 9), today))
        self.assertTrue(W.can_record_on(W.month_end(2026, 10), date(2026, 10, 31)))

    def test_records_are_independent_numbers(self):
        doc = W.default_doc()
        doc["financial"]["stock"]["holdings"] = [{"market": "KR", "symbol": "069500", "name": "x", "qty": 10}]
        quotes = {"KR:069500": {"close": 100_000, "currency": "KRW", "status": "ok"}}
        s = W.summarize(doc, quotes)
        recs = W.upsert_record([], W.make_record("2026-09-30", s["property"], s["financial"],
                                                  s["property_debt"], s["financial_debt"]))
        # 이후 수량·시세가 바뀌어도 기록 숫자는 그대로
        doc["financial"]["stock"]["holdings"][0]["qty"] = 999
        quotes["KR:069500"]["close"] = 1
        self.assertEqual(recs[0]["financial"], 1_000_000)

    def test_upsert_delete_and_period_last(self):
        recs = []
        for d, f in (("2026-01-15", 1), ("2026-01-31", 2), ("2026-03-31", 3), ("2025-12-31", 9)):
            recs = W.upsert_record(recs, W.make_record(d, 0, f, 0, 0))
        recs = W.upsert_record(recs, W.make_record("2026-01-31", 0, 20, 0, 0))   # 같은 날짜만 수정
        self.assertEqual(len(recs), 4)
        monthly = W.period_last(recs, "M")
        self.assertEqual([(k, r["financial"]) for k, r in monthly],
                         [("2025-12", 9), ("2026-01", 20), ("2026-03", 3)])   # 2월은 추정하지 않음
        self.assertEqual([(k, r["financial"]) for k, r in W.period_last(recs, "Y")],
                         [("2025", 9), ("2026", 3)])
        self.assertEqual(len(W.delete_record(recs, "2026-03-31")), 3)


class FireTest(unittest.TestCase):
    def test_projection(self):
        # 1년차: 1000*1.1+100=1200 / 2년차: 1200*1.1+100*1.02=1422
        self.assertAlmostEqual(W.project_assets(1000, 100, 2, 2, 10), 1422.0)
        self.assertEqual(W.project_assets(1000, 100, 2, 0, 10), 1000)
        self.assertAlmostEqual(W.real_value(1.02 ** 3, 2, 3), 1.0)
        self.assertEqual(W.real_return(5, 2), 3)
        self.assertEqual(W.real_return(2, 3.5), -1.5)

    def test_monthly_spend_is_annual_withdrawal_over_12(self):
        self.assertAlmostEqual(W.monthly_spend(1_200_000_000, 4), 4_000_000)
        self.assertAlmostEqual(W.monthly_spend(1_200_000_000, W.real_return(5, 2)), 3_000_000)
        self.assertEqual(W.monthly_spend(1_200_000_000, 0), 0)
        self.assertEqual(W.monthly_spend(1_200_000_000, -3), 0)    # 실질 손실 가정이면 꺼낼 수익이 없다
        self.assertEqual(W.CONSERVATIVE_WITHDRAW_PCT, 4.0)

    def test_real_rate_withdrawal_keeps_purchasing_power(self):
        # 한 해를 명목수익률로 굴린 뒤 연초 자산의 실질수익률만큼 꺼내면 원금이 딱 물가만큼 늘어
        # 구매력이 그대로다 (실질수익률을 r-g 단순 차감으로 쓰는 것과 맞아떨어지는 가정)
        asset, r, g = 1_000_000_000, 5, 2
        left = asset
        for year in range(1, 31):
            left = left * (1 + r / 100) - W.monthly_spend(left, W.real_return(r, g)) * 12
            self.assertAlmostEqual(W.real_value(left, g, year), asset, delta=1)

    def test_years_to_target(self):
        # 인수: 시작, 저축, 물가, 명목수익률, 인출률, 목표
        self.assertEqual(W.years_to_target(1_200_000_000, 0, 2, 5, 4, 4_000_000), 0)      # 지금 가능
        n = W.years_to_target(600_000_000, 30_000_000, 2, 6, 4, 4_000_000)
        self.assertTrue(0 < n < 100)

        def spend_today(years):
            asset = W.project_assets(600_000_000, 30_000_000, 2, years, 6)
            return W.real_value(W.monthly_spend(asset, 4), 2, years)
        self.assertGreaterEqual(spend_today(n), 4_000_000)
        self.assertLess(spend_today(n - 1), 4_000_000)                                    # 처음 도달하는 해
        # 인출률이 낮을수록 같은 목표에 늦게 도달한다
        self.assertGreater(W.years_to_target(600_000_000, 30_000_000, 2, 6, 3, 4_000_000), n)
        self.assertIsNone(W.years_to_target(1_000_000, 0, 3, 4, 1, 4_000_000))            # 100년 내 불가
        self.assertIsNone(W.years_to_target(10_000_000_000, 0, 3, 3, 0, 4_000_000))       # 실질수익률 0


def _grid(cells, n_rows=30, n_cols=12):
    """{'B13': 'x', ...} → CSV를 읽은 것과 같은 2차원 목록."""
    rows = [[""] * n_cols for _ in range(n_rows)]
    for ref, v in cells.items():
        rows[int(ref[1:]) - 1][ord(ref[0]) - ord("A")] = v
    return rows


class SheetTest(unittest.TestCase):
    def test_latest_sheet_by_name_rule(self):
        html = ('items.push({name: "26-09", pageUrl: "x", gid: "11"});'
                'items.push({name: "26-10", pageUrl: "x", gid: "22"});'
                'items.push({name: "26-05_14", pageUrl: "x", gid: "33"});'
                'items.push({name: "9월 1일", pageUrl: "x", gid: "44"});'
                'items.push({name: "25-전략", pageUrl: "x", gid: "55"});'
                'items.push({name: "26-전략", pageUrl: "x", gid: "66"});'
                'items.push({name: "25-전략_6월", pageUrl: "x", gid: "77"});'
                'items.push({name: "2025", pageUrl: "x", gid: "88"});'
                'items.push({name: "2026", pageUrl: "x", gid: "99"});'
                'items.push({name: "2023_12월", pageUrl: "x", gid: "0"});')
        tabs = W.parse_sheet_tabs(html)
        self.assertEqual(len(tabs), 10)
        self.assertEqual(W.pick_latest_sheet(tabs, W.SHEET_NAME_PATTERNS["pension"]), ("26-10", "22"))
        self.assertEqual(W.pick_latest_sheet(tabs, W.SHEET_NAME_PATTERNS["crypto"]), ("26-전략", "66"))
        self.assertEqual(W.pick_latest_sheet(tabs, W.SHEET_NAME_PATTERNS["debt"]), ("2026", "99"))
        self.assertIsNone(W.pick_latest_sheet([("메모", "1")], W.SHEET_NAME_PATTERNS["stock"]))

    def test_parse_amount(self):
        self.assertEqual(W.parse_amount("₩12,300,000"), 12_300_000)
        self.assertEqual(W.parse_amount(" ₩ 4,560,000 "), 4_560_000)
        self.assertEqual(W.parse_amount("1234"), 1234)
        for bad in ("", "#N/A", "#DIV/0!", "현재 합계", None):
            self.assertIsNone(W.parse_amount(bad))

    def test_pension_sheet_rows(self):
        cells = {"A13": "듀얼 모멘텀", "B13": "가 ETF", "C13": "111110", "F13": "100",
                 "A14": "듀얼모멘텀_IRP", "B14": "나 ETF", "C14": "222220", "F14": "10",
                 "B15": "다 ETF", "C15": "333330", "F15": "20",
                 "F16": "현재 합계", "G16": " ₩ 1,000 ",                      # 소계 행은 대상이 아님
                 "A17": "LAA_DC계좌", "B17": "가 ETF", "C17": "111110", "F17": "1,000",
                 "B25": "라 ETF", "C25": "0066W0", "F25": "0",                # 수량 0 → 제외
                 "B26": "마 ETF", "C26": "0172V0", "F26": "7",
                 "B28": "나 ETF", "C28": "222220", "F28": "5",                # 같은 종목은 합산
                 "B29": "범위 밖", "C29": "999990", "F29": "9"}
        got = W.parse_pension_sheet(_grid(cells))
        personal, skipped = got["personal"]
        self.assertEqual([(h["symbol"], h["qty"]) for h in personal], [("111110", 100)])
        self.assertEqual(skipped, [])
        retirement, skipped = got["retirement"]
        self.assertEqual([(h["symbol"], h["qty"]) for h in retirement],
                         [("222220", 15), ("333330", 20), ("111110", 1000), ("0172V0", 7)])
        self.assertEqual(skipped, [])
        self.assertTrue(all(h["market"] == "KR" for h in retirement))

    def test_pension_sheet_detects_shifted_rows(self):
        with self.assertRaises(ValueError):
            W.parse_pension_sheet(_grid({"A14": "듀얼 모멘텀", "B14": "가 ETF", "C14": "111110", "F14": "1"}))

    def test_stock_sheet_takes_rows_under_header(self):
        cells = {"A7": "합계", "A9": "종목", "C9": "티커", "E9": "보유 수량",
                 "A10": "가 주식", "C10": "5930", "E10": "166",                # 앞자리 0이 빠진 코드
                 "A11": "나 ETF", "C11": "0172V0", "E11": "1,486",
                 "A12": "미국 주식", "C12": "spy", "E12": "3",
                 "A14": "코스피", "B14": "2500.12"}                           # 빈 행 뒤는 대상이 아님
        rows = _grid(cells)
        self.assertEqual(W.stock_rows(rows), [10, 11, 12])
        holdings, skipped = W.parse_stock_sheet(rows)
        self.assertEqual([(h["market"], h["symbol"], h["qty"]) for h in holdings],
                         [("KR", "005930", 166), ("KR", "0172V0", 1486), ("US", "SPY", 3)])
        self.assertEqual(skipped, [])
        # 종목이 줄어들면 줄어든 만큼만
        del cells["A11"], cells["A12"]
        self.assertEqual(len(W.parse_stock_sheet(_grid(cells))[0]), 1)
        with self.assertRaises(ValueError):
            W.stock_rows(_grid({"A1": "합계"}))

    def test_crypto_sheet_uses_raw_quantities(self):
        cells = {"A7": "코드번호", "A8": "BTC", "B8": "비트코인", "D8": "0.12",
                 "A9": "ETH", "B9": "이더리움", "D9": "3.21",
                 "A10": "ZZZ", "B10": "모르는 코인", "D10": "1.00"}
        holdings, skipped = W.parse_crypto_sheet(_grid(cells), [0.12345678, 3.21098765, 1.0])
        self.assertEqual([(h["symbol"], h["qty"]) for h in holdings], [("BTC", 0.12345678), ("ETH", 3.21098765)])
        self.assertEqual(holdings[0]["name"], "비트코인 (BTC)")
        self.assertEqual(len(skipped), 1)                                      # allowlist에 없는 코인
        # 원본 수량을 못 받은 행은 잘린 표시값으로 대신하지 않고 건너뛴다
        holdings, skipped = W.parse_crypto_sheet(_grid(cells), [None, 3.21098765, None])
        self.assertEqual([h["symbol"] for h in holdings], ["ETH"])
        self.assertEqual(len(skipped), 2)

    def test_debt_cell(self):
        self.assertEqual(W.parse_debt_sheet(_grid({"B9": "대출", "D9": "₩12,300,000"})), 12_300_000)
        self.assertEqual(W.parse_debt_sheet(_grid({"D9": "₩0"})), 0)
        with self.assertRaises(ValueError):
            W.parse_debt_sheet(_grid({"B9": "대출"}))

    def test_gviz_column(self):
        text = ('/*O_o*/\ngoogle.visualization.Query.setResponse({"version":"0.6","status":"ok","table":'
                '{"cols":[{"id":"D","type":"number"}],"rows":[{"c":[{"v":0.12345678,"f":"0.12"}]},'
                '{"c":[null]},{"c":[{"v":"합계"}]}]}});')
        self.assertEqual(W.parse_gviz_column(text), [0.12345678, None, None])
        with self.assertRaises(ValueError):
            W.parse_gviz_column('x({"status":"error"});')


class SafetyTest(unittest.TestCase):
    def test_rev_survives_roundtrip_and_defaults_to_zero(self):
        self.assertEqual(W.default_doc()["rev"], 0)
        self.assertEqual(W.normalize_doc({"financial": {}})["rev"], 0)          # rev가 없던 예전 문서
        self.assertEqual(W.normalize_doc({"rev": 7})["rev"], 7)
        self.assertEqual(W.normalize_doc({"rev": "7"})["rev"], 0)


class AddressTest(unittest.TestCase):
    TABLE = {"11140": "서울특별시 중구", "11680": "서울특별시 강남구", "26110": "부산광역시 중구",
             "41111": "경기도 수원시 장안구", "36110": "세종특별자치시", "43740": "충청북도 영동군"}

    def test_match_sigungu(self):
        m = lambda a: W.match_sigungu(a, self.TABLE)
        self.assertEqual(m("서울특별시 강남구"), "11680")
        self.assertEqual(m("서울특별시 강남구 가나로 12, 101동"), "11680")
        self.assertEqual(m("강남구 가나동"), "11680")                 # 시도 없이도 하나뿐이면
        self.assertEqual(m("수원시 장안구 다라동"), "41111")
        self.assertEqual(m("세종특별자치시 마바동"), "36110")
        self.assertEqual(m("충북 영동군"), "43740")
        self.assertIsNone(m("중구 사아동"))                           # 여러 곳이라 못 정함
        self.assertEqual(m("서울 중구 사아동"), "11140")              # 시도로 가림
        self.assertEqual(m("부산광역시 중구"), "26110")
        self.assertIsNone(m("어디에도 없는 주소"))
        self.assertIsNone(m(""))

    def test_complex_options(self):
        d = date(2026, 9, 1)
        t = lambda c, dong, area, cancelled=False: {"complex": c, "dong": dong, "area": area, "date": d,
                                                    "price": 1, "floor": "1", "cancelled": cancelled}
        opts = W.complex_options([t("나단지", "가동", 84.9), t("가단지", "가동", 59.9), t("가단지", "가동", 59.9),
                                  t("가단지", "가동", 84.5), t("가단지", "나동", 59.9),
                                  t("취소단지", "가동", 59.9, cancelled=True)])
        self.assertEqual(list(opts), ["가단지 (가동)", "가단지 (나동)", "나단지 (가동)"])   # 같은 이름도 동이 다르면 따로
        self.assertEqual(opts["가단지 (가동)"], {"complex": "가단지", "dong": "가동", "areas": [(59.9, 2), (84.5, 1)]})


if __name__ == "__main__":
    unittest.main()
