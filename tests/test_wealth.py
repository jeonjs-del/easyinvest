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
        self.assertEqual(doc["fire"]["withdraw_rate"], 4.0)   # 없는 키만 기본값

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
        self.assertAlmostEqual(W.monthly_spend(1_200_000_000, 4), 4_000_000)
        self.assertAlmostEqual(W.real_value(1.02 ** 3, 2, 3), 1.0)

    def test_years_to_target(self):
        self.assertEqual(W.years_to_target(1_200_000_000, 0, 2, 5, 4, 4_000_000), 0)      # 지금 가능
        n = W.years_to_target(600_000_000, 30_000_000, 2, 6, 4, 4_000_000)
        self.assertTrue(0 < n < 100)
        prev = W.real_value(W.monthly_spend(W.project_assets(600_000_000, 30_000_000, 2, n - 1, 6), 4), 2, n - 1)
        self.assertLess(prev, 4_000_000)                                                  # 처음 도달하는 해
        self.assertIsNone(W.years_to_target(1_000_000, 0, 3, 1, 4, 4_000_000))            # 100년 내 불가


if __name__ == "__main__":
    unittest.main()
