"""매매계획 R 기반 불타기 계산(trade_plan.py) 오프라인 검증. 네트워크 불필요.

    python -m unittest tests.test_trade_plan -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trade_plan as TP  # noqa: E402


class TestFloorLot(unittest.TestCase):
    def test_stock_integer(self):
        self.assertEqual(TP.floor_lot(204.99, 1), 204)
        self.assertIsInstance(TP.floor_lot(204.99, 1), int)

    def test_coin_8_decimals(self):
        self.assertAlmostEqual(TP.floor_lot(0.123456789, 1e-8), 0.12345678, places=10)

    def test_float_division_not_lose_unit(self):
        self.assertEqual(TP.floor_lot(0.7 / 0.007, 1), 100)


class TestAddRatios(unittest.TestCase):
    def test_modes(self):
        self.assertEqual(TP.add_ratios("100", 3), [1.0, 1.0, 1.0])
        self.assertEqual(TP.add_ratios("50", 2), [0.5, 0.5])
        self.assertEqual(TP.add_ratios("taper", 3), [1.0, 0.5, 0.25])
        self.assertEqual(TP.add_ratios("taper", 1), [1.0])
        self.assertEqual(TP.add_ratios("custom", 2, 0.3), [0.3, 0.3])
        self.assertEqual(TP.add_ratios("100", 0), [])


class TestBtcExample(unittest.TestCase):
    """요청서 검증 예시: BTC-USD 78438.01, 700억원, 1%, 7%, 2R, 3회, 100%, 계단식, 1USD=1,339원."""

    def setUp(self):
        self.fx = 1339.0
        self.price = 78438.01
        self.r = self.price * 0.07
        self.plan = TP.build_plan(self.price, 70_000_000_000 / self.fx, 0.01, self.r,
                                  step_pct=2 * 0.07, ratios=TP.add_ratios("100", 3),
                                  sell_mode="step", lot=1e-8)

    def test_r_and_stop(self):
        self.assertAlmostEqual(self.plan["r"], 5490.66, places=2)
        self.assertAlmostEqual(self.plan["stop"], 72947.35, places=2)

    def test_second_buy_price_and_line(self):
        row1, row2 = self.plan["rows"][:2]
        self.assertAlmostEqual(row2["price"], 89419.33, places=2)
        self.assertAlmostEqual(row2["line"], 83928.67, places=2)
        # 금액 고정: 2차 금액 = 1차 금액, 수량은 1/1.14
        self.assertAlmostEqual(row2["amount"], row1["amount"], delta=row2["price"] * 1e-8)
        self.assertAlmostEqual(row2["qty"], row1["qty"] / 1.14, places=7)
        # 매도선 도달 시 = 1차 금액 × (1.07 × (1 + 1/1.14) − 2) ≈ +0.86% — 본전보다 약간 위
        expect = row1["amount"] * (1.07 * (1 + 1 / 1.14) - 2)
        self.assertAlmostEqual(row2["pnl"], expect, delta=1.0)
        self.assertGreater(row2["pnl"], 0)

    def test_first_buy_about_10b_krw(self):
        krw = self.plan["rows"][0]["amount"] * self.fx
        self.assertTrue(9_990_000_000 < krw <= 10_000_000_000, krw)

    def test_coin_quantity_is_fractional(self):
        q1 = self.plan["q1"]
        self.assertNotEqual(q1, int(q1))
        self.assertAlmostEqual(q1 * 1e8, round(q1 * 1e8), places=4)

    def test_worst_loss_is_1pct(self):
        self.assertAlmostEqual(self.plan["worst_loss_pct"], 0.01, places=6)


class TestPyramidPnl(unittest.TestCase):
    def test_taper_step(self):
        p = TP.build_plan(100, 100_000, 0.01, 10, step_pct=0.2,
                          ratios=TP.add_ratios("taper", 3), sell_mode="step")
        # 금액 10000 → 10000·5000·2500을 120·140·160에 산다(정수 주 버림)
        self.assertEqual([r["qty"] for r in p["rows"]], [100, 83, 35, 15])
        self.assertEqual([r["line"] for r in p["rows"]], [90, 110, 130, 150])
        self.assertEqual([round(r["pnl"], 6) for r in p["rows"]], [-1000, 170, 3480, 7690])
        self.assertAlmostEqual(p["total_cost"], 27260)

    def test_half_ratio_changes_pnl(self):
        p = TP.build_plan(100, 100_000, 0.01, 10, step_pct=0.2,
                          ratios=TP.add_ratios("50", 1), sell_mode="step")
        self.assertEqual([r["qty"] for r in p["rows"]], [100, 41])
        self.assertAlmostEqual(p["rows"][1]["pnl"], 110 * 141 - 14920)

    def test_fixed_price_and_ma20(self):
        p = TP.build_plan(100, 100_000, 0.01, 10, step_pct=0.2,
                          ratios=[1.0], sell_mode="price", sell_price=105)
        self.assertEqual(p["rows"][0]["line"], 90)
        self.assertAlmostEqual(p["rows"][1]["pnl"], 105 * 183 - 19960)
        p = TP.build_plan(100, 100_000, 0.01, 10, step_pct=0.2, ratios=[1.0], sell_mode="ma20")
        self.assertIsNone(p["rows"][1]["pnl"])
        self.assertAlmostEqual(p["rows"][0]["pnl"], -1000)

    def test_krw_stock_integer_shares(self):
        p = TP.build_plan(70_000, 100_000_000, 0.01, 4_900)
        self.assertEqual(p["q1"], 204)
        self.assertEqual(len(p["rows"]), 1)

    def test_invalid(self):
        self.assertIsNone(TP.build_plan(100, 1000, 0.01, 0))
        self.assertIsNone(TP.build_plan(100, 1000, 0.01, 100))
        self.assertEqual(TP.build_plan(100, 10, 0.01, 7)["q1"], 0)


if __name__ == "__main__":
    unittest.main()
