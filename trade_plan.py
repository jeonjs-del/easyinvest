"""매수 탭 · 매매계획 — R(리스크 단위) 기반 첫 매수·불타기(피라미딩) 계획의 순수 계산.

Streamlit·네트워크 비의존. 화면·시세·환율 조회는 app.py의 매매계획 하위탭이 맡는다.
모든 금액은 종목 통화(원 또는 달러) 기준 — 원화 투자자산을 달러로 바꾸는 건 호출부 몫.
"""
import math

# 줄여가기: 첫 매수 금액 대비 2차 100% → 3차 50% → 4차 25%
TAPER_RATIOS = (1.0, 0.5, 0.25)

SELL_MODES = ("step", "trail", "ma20", "price")


def floor_lot(x, lot=1):
    """lot 단위로 버림. 주식 lot=1(정수 주), 코인 lot=1e-8.
    7e8/7=1e8 같은 나눗셈이 99999999.99999999로 떨어져 한 단위를 잃지 않게 아주 작은 여유를 둔다."""
    if x <= 0:
        return 0 if lot == 1 else 0.0
    units = math.floor(x / lot * (1 + 1e-12))
    if lot == 1:
        return int(units)
    return round(units * lot, 10)


def add_ratios(mode, adds, custom=None):
    """불타기 n회 각각의 '첫 매수 금액 대비' 비율 목록."""
    if adds <= 0:
        return []
    if mode == "taper":
        return [TAPER_RATIOS[min(i, len(TAPER_RATIOS) - 1)] for i in range(adds)]
    ratio = {"100": 1.0, "50": 0.5}.get(mode, custom)
    if ratio is None or ratio <= 0:
        raise ValueError("불타기 비율이 없습니다")
    return [float(ratio)] * adds


def build_plan(price, asset, loss_pct, r, step_pct=0.0, ratios=(), sell_mode="step",
               sell_price=None, lot=1):
    """첫 매수와 불타기 단계별 계획.

    price: 첫 매수가, asset: 투자자산(종목 통화), loss_pct: 한 종목 최대 손실(0.01=1%),
    r: 1R = 매수가 − 손절가(주당 금액), step_pct: 불타기 간격(매수가 대비, 0.14=+14%),
    ratios: 불타기 차수별 첫 매수 대비 금액 비율 — 금액을 고정하므로 수량은 그 차수 매수가로 나눈 값
      (주가가 오른 만큼 수량이 줄어든다), sell_mode: 첫 추가매수 이후 매도선 방식
      step  = 마지막 매수가 − 1R(계단식)
      trail = 최고 종가 − 1R — 계획 시점엔 최고 종가를 그 차수 매수가로 본 최소값(계단식과 같음)
      ma20  = 20일 이평(미래 값을 알 수 없어 손익은 None)
      price = sell_price 고정
    1차(첫 매수) 매도선은 언제나 손절가다. 손익은 그 시점 누적 수량 전부를 매도선에서 판다고 본 값.
    """
    if price <= 0 or asset <= 0 or loss_pct <= 0 or r <= 0 or r >= price:
        return None
    if sell_mode not in SELL_MODES:
        raise ValueError(sell_mode)
    stop = price - r
    q1 = floor_lot(asset * loss_pct / r, lot)
    if q1 <= 0:
        return {"r": r, "stop": stop, "q1": q1, "rows": [], "worst_loss": 0.0, "worst_loss_pct": 0.0,
                "total_cost": 0.0}

    first_amount = price * q1
    entries = [(price, 1.0)] + [(price * (1 + step_pct * (i + 1)), ratio)
                                for i, ratio in enumerate(ratios)]
    rows = []
    cum_q, cum_cost = 0, 0.0
    for n, (p, ratio) in enumerate(entries, start=1):
        q = q1 if n == 1 else floor_lot(first_amount * ratio / p, lot)
        cum_q = cum_q + q if lot == 1 else round(cum_q + q, 10)
        cum_cost += p * q
        if n == 1:
            line = stop
        elif sell_mode in ("step", "trail"):
            line = p - r
        elif sell_mode == "price":
            line = sell_price
        else:
            line = None
        pnl = None if line is None or cum_q <= 0 else line * cum_q - cum_cost
        rows.append({
            "n": n, "price": p, "ratio": ratio, "qty": q, "amount": p * q,
            "cum_qty": cum_q, "cum_cost": cum_cost,
            "avg": cum_cost / cum_q if cum_q else None,
            "line": line, "pnl": pnl, "pnl_pct": None if pnl is None else pnl / asset,
        })
    worst = q1 * r
    return {"r": r, "stop": stop, "q1": q1, "rows": rows, "worst_loss": worst,
            "worst_loss_pct": worst / asset, "total_cost": cum_cost}
