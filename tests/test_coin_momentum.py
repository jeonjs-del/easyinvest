"""코인 듀얼모멘텀 계산 로직(coin_momentum.py) 오프라인 검증. 네트워크 불필요.

    python -m unittest tests.test_coin_momentum -v
"""
import os
import sys
import unittest
from datetime import date

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import coin_momentum as CM  # noqa: E402


def _m(sym, mcap, price=10.0, name=None):
    return {"symbol": sym.lower(), "name": name or sym, "market_cap": mcap, "current_price": price}


class ExclusionTest(unittest.TestCase):
    def test_listed_categories(self):
        self.assertEqual(CM.exclusion_reason(_m("USDT", 1, 1.0)), "stablecoin")
        self.assertEqual(CM.exclusion_reason(_m("WBTC", 1, 80000)), "wrapped")
        self.assertEqual(CM.exclusion_reason(_m("PAXG", 1, 4000)), "gold")
        self.assertEqual(CM.exclusion_reason(_m("USYC", 1, 1.14)), "tokenized_fund")
        self.assertEqual(CM.exclusion_reason(_m("LEO", 1, 9.0)), "hard_to_trade")
        self.assertEqual(CM.exclusion_reason(_m("XMR", 1, 500)), "hard_to_trade")

    def test_stable_rule(self):
        self.assertEqual(CM.exclusion_reason(_m("NEWX", 1, 1.001, name="New USD")), "stable_rule")
        self.assertEqual(CM.exclusion_reason(_m("XUSD", 1, 0.98)), "stable_rule")
        # 이름에 usd가 있어도 가격이 1달러 근처가 아니면 스테이블이 아니다
        self.assertIsNone(CM.exclusion_reason(_m("XUSD", 1, 1.20)))
        self.assertIsNone(CM.exclusion_reason(_m("ADA", 1, 1.0, name="Cardano")))

    def test_select_universe_skips_and_fills(self):
        markets = [_m("BTC", 100), _m("USDT", 90, 1.0), _m("ETH", 80), _m("WETH", 70), _m("SOL", 60),
                   _m("NOCAP", None)]
        kept, excluded = CM.select_universe(markets, top_n=2)
        self.assertEqual([m["symbol"] for m in kept], ["btc", "eth"])
        self.assertEqual([r for _, r in excluded], ["stablecoin", "wrapped"])

    def test_hard_to_trade_replaced_by_next(self):
        markets = [_m("BTC", 100), _m("WBT", 95), _m("ETH", 90), _m("LEO", 85), _m("AVAX", 80), _m("SUI", 70)]
        kept, excluded = CM.select_universe(markets, top_n=4)
        self.assertEqual([m["symbol"] for m in kept], ["btc", "eth", "avax", "sui"])
        self.assertEqual([(m["symbol"], r) for m, r in excluded], [("wbt", "hard_to_trade"), ("leo", "hard_to_trade")])

    def test_hard_to_trade_from_live_listings(self):
        listings = {"upbit": {"BTC"}, "okx": {"BTC", "OKB", "LEO", "NEW"}, "bybit": {"BTC", "SUI"}}
        vol = {"OKB": 7_000_000, "LEO": 135, "NEW": 2_000_000}
        hard = CM.hard_to_trade_symbols(["BTC", "OKB", "LEO", "SUI", "XMR", "new"], listings, vol)
        self.assertEqual(hard, {"LEO", "XMR"})
        # 고정 목록에 없던 코인도 상장목록에서 빠지면 바로 잡힌다
        self.assertEqual(CM.hard_to_trade_symbols(["ETH"], listings, vol), {"ETH"})

    def test_hard_to_trade_fallback_when_listing_missing(self):
        listings = {"upbit": {"BTC"}, "okx": None, "bybit": {"BTC"}}
        self.assertEqual(CM.hard_to_trade_symbols(["BTC", "LEO", "ETH"], listings), {"LEO"})

    def test_unlisted_stable_stays_stable(self):
        self.assertEqual(CM.exclusion_reason(_m("NUSD", 1, 1.0), hard={"NUSD"}), "stable_rule")

    def test_select_universe_live_hard_set(self):
        markets = [_m("BTC", 100), _m("OKB", 95), _m("ETH", 90), _m("AVAX", 80)]
        kept, excluded = CM.select_universe(markets, top_n=2, hard={"OKB"})
        self.assertEqual([m["symbol"] for m in kept], ["btc", "eth"])
        self.assertEqual(excluded[0][1], "hard_to_trade")

    def test_select_universe_reorders_by_market_cap(self):
        kept, _ = CM.select_universe([_m("A", 5), _m("B", 9), _m("C", 7)], top_n=3)
        self.assertEqual([m["symbol"] for m in kept], ["b", "c", "a"])


class SmaFilterTest(unittest.TestCase):
    def test_completed_closes_drops_today(self):
        idx = pd.date_range("2026-10-10", periods=6, freq="D")
        s = pd.Series(range(6), index=idx, dtype=float)
        out = CM.completed_closes(s, date(2026, 10, 15))
        self.assertEqual(out.index[-1], pd.Timestamp("2026-10-14"))
        self.assertEqual(len(out), 5)

    def test_sma_values(self):
        s = pd.Series([float(i) for i in range(1, 121)],
                      index=pd.date_range("2026-01-01", periods=120, freq="D"))
        sig = CM.sma_filter(s, 120)
        self.assertAlmostEqual(sig["sma"], 60.5)
        self.assertEqual(sig["close"], 120.0)
        self.assertTrue(sig["above"])
        self.assertAlmostEqual(sig["gap"], 120 / 60.5 - 1)

    def test_sma_uses_last_window_only(self):
        s = pd.Series([1000.0] * 10 + [10.0] * 119 + [5.0])
        sig = CM.sma_filter(s, 120)
        self.assertAlmostEqual(sig["sma"], (10 * 119 + 5) / 120)
        self.assertFalse(sig["above"])

    def test_sma_live_price_decides(self):
        s = pd.Series([100.0] * 120)
        sig = CM.sma_filter(s, 120, price=101.0)
        self.assertTrue(sig["above"] and sig["live"])
        self.assertAlmostEqual(sig["gap"], 0.01)
        self.assertEqual(sig["close"], 100.0)
        self.assertFalse(CM.sma_filter(s, 120, price=99.0)["above"])

    def test_sma_insufficient(self):
        self.assertIsNone(CM.sma_filter(pd.Series([1.0] * 119), 120))

    def test_equal_is_not_above(self):
        self.assertFalse(CM.sma_filter(pd.Series([5.0] * 120), 120)["above"])


class MaFilterTest(unittest.TestCase):
    def _closes(self):
        # 앞 50개 200, 뒤 50개 100 → SMA100=150, SMA50=100
        return pd.Series([200.0] * 50 + [100.0] * 50,
                         index=pd.date_range("2026-01-01", periods=100, freq="D"))

    def test_above_both_invests(self):
        sig = CM.ma_filter(self._closes(), price=160.0)
        self.assertTrue(sig["above"])
        self.assertAlmostEqual(sig["mas"][50]["sma"], 100.0)
        self.assertAlmostEqual(sig["mas"][100]["sma"], 150.0)
        self.assertAlmostEqual(sig["mas"][100]["gap"], 160 / 150 - 1)

    def test_below_either_is_cash(self):
        sig = CM.ma_filter(self._closes(), price=120.0)  # 50MA 위, 100MA 아래
        self.assertFalse(sig["above"])
        self.assertTrue(sig["mas"][50]["above"])
        self.assertFalse(sig["mas"][100]["above"])
        closes = pd.Series([100.0] * 50 + [200.0] * 50)  # SMA100=150, SMA50=200
        sig = CM.ma_filter(closes, price=170.0)          # 100MA 위, 50MA 아래
        self.assertFalse(sig["above"])
        self.assertFalse(sig["mas"][50]["above"])

    def test_insufficient_for_longest(self):
        self.assertIsNone(CM.ma_filter(pd.Series([1.0] * 99)))
        self.assertEqual(list(CM.ma_filter(pd.Series([1.0] * 100))["mas"]), [50, 100])


class RankTest(unittest.TestCase):
    def test_lookback_return(self):
        self.assertAlmostEqual(CM.lookback_return([100, 110, 121], 2), 0.21)
        self.assertIsNone(CM.lookback_return([100, 110], 2))

    def test_rank_order_and_flags(self):
        rows = [{"symbol": "A", "ret": 0.05}, {"symbol": "B", "ret": 0.20}, {"symbol": "C", "ret": None},
                {"symbol": "D", "ret": -0.01}, {"symbol": "E", "ret": -0.30}]
        df = CM.rank_momentum(rows)
        self.assertEqual(df["symbol"].tolist(), ["B", "A", "D", "E", "C"])
        self.assertEqual(df["rank"].tolist()[:4], [1, 2, 3, 4])
        self.assertTrue(pd.isna(df["rank"].iloc[4]))
        self.assertEqual(df["is_pick"].tolist(), [True, True, True, False, False])
        self.assertEqual(df["neg_momentum"].tolist(), [False, False, True, False, False])
        self.assertEqual(CM.note_for(df.iloc[0]), "🟢 매수 후보")
        self.assertEqual(CM.note_for(df.iloc[2]), "⚠ 모멘텀 음수(현금 보유 고려)")
        self.assertEqual(CM.note_for(df.iloc[3]), "")

    def test_missing_ret_never_pick(self):
        df = CM.rank_momentum([{"symbol": "A", "ret": None}, {"symbol": "B", "ret": 0.1}])
        self.assertEqual(df["is_pick"].tolist(), [True, False])

    def test_close_before_uses_close_time(self):
        idx = pd.to_datetime(["2026-10-01 00:00", "2026-10-02 00:00", "2026-10-03 00:00"], utc=True)
        s = pd.Series([1.0, 2.0, 3.0], index=idx)
        self.assertEqual(CM.close_before(s, pd.Timestamp("2026-10-02 12:00", tz="UTC")), 2.0)
        self.assertEqual(CM.close_before(s, pd.Timestamp("2026-10-03 00:00", tz="UTC")), 3.0)
        self.assertIsNone(CM.close_before(s, pd.Timestamp("2026-09-30", tz="UTC")))

    def test_live_return(self):
        self.assertAlmostEqual(CM.live_return(110, 100), 0.10)
        self.assertIsNone(CM.live_return(None, 100))
        self.assertIsNone(CM.live_return(110, None))

    def test_latest_update(self):
        ms = [{"last_updated": "2026-10-15T12:16:32.123Z"}, {"last_updated": "2026-10-15T12:10:00Z"},
              {"last_updated": None}, {}]
        self.assertEqual(CM.latest_update(ms), pd.Timestamp("2026-10-15 12:16:32.123", tz="UTC"))
        self.assertIsNone(CM.latest_update([{}]))

    def test_today_utc(self):
        self.assertEqual(CM.today_utc_date(pd.Timestamp("2026-10-15 08:30", tz="Asia/Seoul")),
                         date(2026, 10, 14))


if __name__ == "__main__":
    unittest.main()
