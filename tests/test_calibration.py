"""추세추종 스캐너 캘리브레이션 테스트.

목적: scanner/trend_scan.py의 신호 판정·통계 계산이 원사이트(easyinvesting.app)
추세추종 결과와 얼마나 일치하는지, 사용자가 준 두 종목의 공개 수치를 정답지 삼아
검증한다. 실가격을 FinanceDataReader로 받아오는 네트워크 의존 테스트라 로컬에서
직접 실행한다(CI에는 올리지 않음):
    python -m pytest tests/test_calibration.py -v -s
    또는: python tests/test_calibration.py

정답지 (사용자 제공, 원사이트 화면 값)
------------------------------------
Oil-Dri Corporation Of America(ODC) / EMA40 장기 근접 / 대표 보유기간 126일
    승률 60.2% · 터치수 121 · 평균이익 +20.6% · 평균손실 -3.0% · 손익비 6.89
    (참고) 2일 44.5%/+2.9%/-1.6%, 20일 53.6%/+13.9%/-6.3%,
           252일 85.6%/+34.1%/-9.6%(표본 114)
Park Aerospace Corp(PKE) / 박스돌파 장기 / 대표 보유기간 2일
    승률 61.0% · 돌파수 55 · 평균이익 +3.4% · 평균손실 -0.6% · 손익비 5.87
    (참고) 20일 39.9%/+13.4%/-6.3%, 63일 71.9%(표본 53), 252일 29.8%(표본 51)

캘리브레이션 과정 요약
----------------------
근접임계값 1.5~5%, 상승추세 판정기간 0~50일, 박스 변동폭 상한 15~100%,
통계 창 3~20년/전체 구간(1990년~)을 그리드로 돌려 표본수·승률이 정답지에
가장 가까운 조합을 찾았다. 그 결과 scanner/trend_scan.py의 MA_NEAR_TOLERANCE를
0.03->0.02로, BOX_MAX_RANGE를 0.30->0.50으로 조정했고, "장기" 통계 창은 10년으로
정했다(15~16년 근방에서 우연히 더 잘 맞는 조합도 있었지만, 그 근방에서만 국소적으로
좋아지고 바로 옆 연도에서는 다시 나빠지는 등 종목 1개에 과적합된 우연으로 판단해
채택하지 않았다).

이 조합(scanner/trend_scan.py의 실제 설정값)으로 계산한 결과:
  * Oil-Dri EMA40 터치수 91 (정답 121, 75%) · 대표 보유기간 126일 (정답과 일치) ·
    평균이익 +21.8% (정답 +20.6%, 거의 일치) · 승률 51.6% (정답 60.2%) ·
    평균손실 -7.65% (정답 -3.0%, 약 2.5배)
  * Park Aerospace 박스돌파(60일, 변동폭<=50%) 돌파수 55 (정답 55, 정확히 일치) ·
    평균이익 +3.25% (정답 +3.4%, 거의 일치) · 승률 50.9% (정답 61.0%) ·
    평균손실 -1.81% (정답 -0.6%, 약 3배) · 대표 보유기간은 252일로 선택되어
    정답(2일)과는 불일치

결론 (사용자가 준 진단 기준 적용: "표본수 맞으면 신호판정 정답, 표본수는 맞는데
승률/수익률이 다르면 수익률 계산 문제")
--------------------------------------------------------------------------
표본수는 두 종목 모두 정답지의 75~100% 수준까지 근접했고(돌파수는 정확히 일치),
평균이익도 두 사례 모두 오차 10% 안팎으로 거의 일치했다 -> 신호 판정(어떤 날이
눌림목/돌파인지, 승/패로 갈리는 지점) 자체는 크게 틀리지 않았다고 볼 수 있다.
반면 평균손실은 근접임계값·박스 변동폭·통계 창을 넓은 범위로 바꿔봐도 항상
정답지보다 2~3배 크게 나왔다 — 즉 파라미터를 더 조정해도 좁혀지지 않는 구조적
차이다. 이는 원사이트가 "H일 뒤 종가 청산" 같은 고정 보유기간 방식이 아니라
손절/트레일링 스탑처럼 손실 쪽을 조기에 제한하는 백테스트 엔진을 쓰고 있을
가능성을 시사한다(Park Aerospace의 대표 보유기간이 2일로 짧게 나오는 것도, 손실을
빠르게 끊는 규칙이 있다면 짧은 보유기간의 손익비가 유독 좋아 보이는 것과 부합).
이 부분은 원사이트 방법론을 직접 확인하지 않는 한 완전히 재현하기 어렵다.

이 테스트는 위 결론에 맞춰 두 단계로 검증한다:
1) "근접 가능" 항목(표본수, 평균이익, 짧은 보유기간 승률, Oil-Dri의 대표 보유기간)
   은 여유 있는 오차범위로 assert한다.
2) "구조적으로 못 맞추는" 항목(평균손실 크기, 장기 보유 승률, Park Aerospace의
   대표 보유기간)은 정보 출력만 하고 assert하지 않는다 — 억지로 맞추려 파라미터를
   과적합하면 다른 종목에 대한 일반성을 해치기 때문이다.
"""
import sys
import unittest
from pathlib import Path

import FinanceDataReader as fdr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scanner import trend_scan as ts


def _stats_by_hold(close_arr, positions, window_years):
    n = len(close_arr)
    window_start = max(0, n - window_years * ts.TRADING_DAYS_PER_YEAR)
    term_positions = positions[positions >= window_start]
    rows = ts._hold_stats(close_arr, term_positions, ts.HOLD_PERIODS)
    return len(term_positions), {r["hold_days"]: r for r in rows}


def _fmt_n(n, target_n):
    if target_n is None:
        return f"표본 {n:>4}"
    return f"표본 {n:>4}(정답 {target_n})"


def _print_row(h, r, target_wr, target_aw, target_al, target_n=None):
    if r is None:
        print(f"  h={h:>3}일  (표본 없음)")
        return
    print(f"  h={h:>3}일  {_fmt_n(r['n_samples'], target_n):<16}"
          f"승률 {r['win_rate']:.1%}(정답 {target_wr:.1%})  "
          f"평균이익 {r['avg_win']:+.1%}(정답 {target_aw:+.1%})  "
          f"평균손실 {r['avg_loss']:+.1%}(정답 {target_al:+.1%})")


class TestOilDriEMA40(unittest.TestCase):
    """Oil-Dri Corporation Of America(ODC) / EMA40 장기 근접."""

    @classmethod
    def setUpClass(cls):
        df = fdr.DataReader("ODC", "1990-01-01")
        close, low = df["Close"], df["Low"]
        cls.close_arr = close.to_numpy()
        positions, _ma = ts.detect_ma_near_positions(close, low, "EMA", 40)
        cls.total_occ, cls.by_h = _stats_by_hold(cls.close_arr, positions, ts.STAT_TERMS["장기"])

    def test_touch_count_close_to_target(self):
        target = 121
        print(f"\n[Oil-Dri EMA40 장기] 터치수: 우리 {self.total_occ} vs 정답 {target}"
              f" (비율 {self.total_occ / target:.0%})")
        ratio = self.total_occ / target
        self.assertGreater(ratio, 0.6, "터치수가 정답지의 60% 미만 — 신호 판정이 크게 어긋남")
        self.assertLess(ratio, 1.6, "터치수가 정답지의 160% 초과 — 신호 판정이 크게 어긋남")

    def test_representative_hold_period_matches(self):
        rep = ts._pick_representative(list(self.by_h.values()), ts.MIN_SAMPLE, self.total_occ,
                                       ts.MIN_SAMPLE_RATIO, ts.REPRESENTATIVE_METRIC)
        self.assertIsNotNone(rep, "대표 보유기간 후보가 없음(최소 표본수/비율 조건 미달)")
        print(f"[Oil-Dri EMA40 장기] 대표 보유기간: 우리 {rep['hold_days']}일 vs 정답 126일")
        self.assertEqual(rep["hold_days"], 126)

    def test_hold126_avg_win_close_to_target(self):
        r = self.by_h[126]
        print(f"[Oil-Dri EMA40 장기] h=126 평균이익: 우리 {r['avg_win']:+.1%} vs 정답 +20.6%")
        self.assertAlmostEqual(r["avg_win"], 0.206, delta=0.06)

    def test_hold2_win_rate_close_to_target(self):
        """가장 짧은 보유기간(2일)은 손절/트레일링 효과가 거의 없어 정답지와 가장 잘 맞는다."""
        r = self.by_h[2]
        print(f"[Oil-Dri EMA40 장기] h=2 승률: 우리 {r['win_rate']:.1%} vs 정답 44.5%")
        self.assertAlmostEqual(r["win_rate"], 0.445, delta=0.12)

    def test_full_comparison_report(self):
        """참고용 전체 비교표 출력 — 평균손실/장기 보유 승률은 구조적 한계로 assert하지 않는다."""
        targets = {
            2: (0.445, 0.029, -0.016, None),
            20: (0.536, 0.139, -0.063, None),
            126: (0.602, 0.206, -0.030, 121),
            252: (0.856, 0.341, -0.096, 114),
        }
        print(f"\n[Oil-Dri EMA40 장기 근접] 전체 비교 (터치수 {self.total_occ}, 정답 121):")
        for h, (twr, taw, tal, tn) in targets.items():
            _print_row(h, self.by_h.get(h), twr, taw, tal, tn)


class TestParkAerospaceBox(unittest.TestCase):
    """Park Aerospace Corp(PKE) / 박스돌파(60일) 장기."""

    @classmethod
    def setUpClass(cls):
        df = fdr.DataReader("PKE", "1990-01-01")
        close, high, low = df["Close"], df["High"], df["Low"]
        cls.close_arr = close.to_numpy()
        positions, _box_top = ts.detect_box_breakout_positions(close, high, low, 60)
        cls.total_occ, cls.by_h = _stats_by_hold(cls.close_arr, positions, ts.STAT_TERMS["장기"])

    def test_breakout_count_matches_target(self):
        target = 55
        print(f"\n[Park Aerospace 박스돌파(60일) 장기] 돌파수: 우리 {self.total_occ} vs 정답 {target}")
        self.assertEqual(self.total_occ, target)

    def test_hold2_avg_win_close_to_target(self):
        r = self.by_h[2]
        print(f"[Park Aerospace 박스돌파(60일) 장기] h=2 평균이익: 우리 {r['avg_win']:+.1%} vs 정답 +3.4%")
        self.assertAlmostEqual(r["avg_win"], 0.034, delta=0.015)

    def test_hold2_win_rate_within_wide_margin(self):
        r = self.by_h[2]
        print(f"[Park Aerospace 박스돌파(60일) 장기] h=2 승률: 우리 {r['win_rate']:.1%} vs 정답 61.0%")
        self.assertAlmostEqual(r["win_rate"], 0.610, delta=0.15)

    def test_representative_hold_period_is_known_mismatch(self):
        """대표 보유기간은 정답(2일)과 불일치(우리는 252일) — 알려진 한계.
        원인: 우리 계산은 손절을 모델링하지 않아 h=2의 평균손실이 부풀려지고,
        그 결과 손익비/기대값 모두 h=252를 더 높게 평가한다(모듈 docstring의
        손절/트레일링 가설 참고). 실패시키지 않고 기록만 한다."""
        rep = ts._pick_representative(list(self.by_h.values()), ts.MIN_SAMPLE, self.total_occ,
                                       ts.MIN_SAMPLE_RATIO, ts.REPRESENTATIVE_METRIC)
        self.assertIsNotNone(rep)
        match = "일치" if rep["hold_days"] == 2 else "불일치(구조적 한계로 예상된 결과)"
        print(f"[Park Aerospace 박스돌파(60일) 장기] 대표 보유기간: "
              f"우리 {rep['hold_days']}일 vs 정답 2일 -> {match}")

    def test_full_comparison_report(self):
        targets = {
            2: (0.610, 0.034, -0.006, 55),
            20: (0.399, 0.134, -0.063, None),
            63: (0.719, None, None, 53),
            252: (0.298, None, None, 51),
        }
        print(f"\n[Park Aerospace 박스돌파(60일) 장기] 전체 비교 (돌파수 {self.total_occ}, 정답 55):")
        for h, (twr, taw, tal, tn) in targets.items():
            r = self.by_h.get(h)
            if r is None:
                print(f"  h={h:>3}일  (표본 없음)")
                continue
            taw = r["avg_win"] if taw is None else taw
            tal = r["avg_loss"] if tal is None else tal
            _print_row(h, r, twr, taw, tal, tn)


if __name__ == "__main__":
    unittest.main(verbosity=2)
