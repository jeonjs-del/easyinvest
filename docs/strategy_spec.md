Python으로 다음 자산배분 전략을 구현해줘.

전략 이름은 **"변동성 변형 듀얼모멘텀(Volatility-Adjusted Modified Dual Momentum)"** 이다.

목표는 월 단위 백테스트 프로그램을 만드는 것이며, 반드시 look-ahead bias가 발생하지 않도록 해야 한다.

## 1. 전략의 기본 구조

전략은 크게 세 단계로 구성된다.

1. 주식 위험자산 선택
2. KOSPI 선택 시 변동성에 따른 포트폴리오 조정
3. 주식이 모두 약세일 경우 안전자산 선택

모든 신호는 매월 말 종가를 이용해 계산하고, 월말 t에서 계산된 신호는 **다음 달 t+1의 포트폴리오**에 적용한다.

예를 들어 2022년 1월 31일까지의 데이터로 계산한 신호는 2022년 2월 한 달 동안 적용한다.

절대로 t+1월의 가격정보를 t월 말 신호 계산에 사용하면 안 된다.

---

# 2. 위험자산 선택

위험자산 후보는 두 개이다.

* KOSPI200
* SPY(S&P500)

각 월말 t에서 최근 **3개월 모멘텀**을 계산한다.

momentum_3m = price[t] / price[t-3] - 1

예:

KOSPI_3M = KOSPI200[t] / KOSPI200[t-3] - 1

SPY_3M = SPY[t] / SPY[t-3] - 1

판정 규칙은 다음과 같다.

### Case A

KOSPI_3M > SPY_3M 이고 KOSPI_3M > 0이면 KOSPI 선택.

### Case B

SPY_3M >= KOSPI_3M 이고 SPY_3M > 0이면 SPY 선택.

### Case C

KOSPI와 SPY의 3개월 수익률이 모두 0 이하이면 주식에 투자하지 않고 안전자산 선택 단계로 이동한다.

동률이면 구현의 재현성을 위해 SPY를 선택하도록 한다.

---

# 3. KOSPI가 선택된 경우의 변동성 오버레이

KOSPI가 위험자산으로 선택되었을 때는 KOSPI200의 최근 12개월 변동성을 계산한다.

먼저 월간 수익률:

r[t] = KOSPI200[t] / KOSPI200[t-1] - 1

최근 12개의 월간수익률을 이용하여 연환산 변동성을 계산한다.

vol_12m =
std(last 12 monthly returns, ddof=1) * sqrt(12)

즉 pandas를 사용한다면 반드시 표본표준편차와 동일하게:

series.std(ddof=1) * np.sqrt(12)

을 사용한다.

변동성 임계값은 **35%, 즉 0.35**이다.

### vol_12m < 0.35

다음 달 포트폴리오:

* KOSPI200 : 75%
* KOSPI200 IT : 25%

### vol_12m >= 0.35

다음 달 포트폴리오:

* KOSPI200 : 100%
* KOSPI200 IT : 0%

중요:
35%와 정확히 같으면 고변동성으로 처리하여 KOSPI200 100%를 사용한다.

---

# 4. SPY가 선택된 경우

SPY가 선택되면 다른 오버레이를 사용하지 않는다.

다음 달:

* SPY 100%

로 투자한다.

KOSPI 변동성은 SPY 선택 여부에 영향을 주지 않는다.

---

# 5. 안전자산 선택

KOSPI와 SPY의 최근 3개월 모멘텀이 모두 0 이하일 때만 안전자산 모듈을 실행한다.

안전자산 후보는 총 6개이다.

한국:

1. KR_BOND_SHORT : 한국 국채 단기, 약 3년
2. KR_BOND_MID : 한국 국채 중기, 약 10년
3. KR_BOND_LONG : 한국 국채 장기, 약 30년

미국 환노출:

4. US_BOND_SHORT : 미국 단기국채, 3년 이하
5. US_BOND_MID : 미국 10년 국채
6. US_BOND_LONG : 미국 20년 이상 장기국채

미국채는 반드시 **원화 투자자의 환노출 수익률**을 사용한다.

국내 상장 환노출 ETF 가격을 직접 사용할 수 있다면 그 ETF의 adjusted/total-return price를 그대로 사용한다.

해외 ETF를 proxy로 사용하는 경우에는 미국채 달러 수익률과 USD/KRW 환율을 합성한다.

KRW_return =
(1 + USD_bond_return) *
(1 + USDKRW_return) - 1

---

# 6. 안전자산 모멘텀

현재 기본 설정은 안전자산에 대해 **1개월 모멘텀**을 사용한다.

각 안전자산 i에 대해:

bond_momentum_i =
price_i[t] / price_i[t-1] - 1

6개 안전자산 중 최근 1개월 수익률이 가장 높은 자산을 찾는다.

best_bond =
argmax(momentum of six bond assets)

### 최고 안전자산 모멘텀이 양수인 경우

해당 자산 100%에 다음 달 투자한다.

### 6개 안전자산이 전부 0 이하인 경우

다음 달은:

CASH 100%

로 한다.

안전자산 모멘텀이 정확히 0이면 양의 모멘텀으로 인정하지 않는다.

동률이 발생할 경우 재현성을 위해 다음 우선순위를 사용한다.

1. KR_BOND_SHORT
2. KR_BOND_MID
3. KR_BOND_LONG
4. US_BOND_SHORT
5. US_BOND_MID
6. US_BOND_LONG

단, 이 tie-break 순서는 config에서 변경 가능하도록 만들어라.

---

# 7. 실제 연금계좌 ETF와 신호 데이터를 분리할 것

코드에서 반드시 "signal asset"과 "execution asset"을 분리해라.

예:

주식 신호:

* KOSPI200 index
* SPY

실제 매매:

* KOSPI 부분 → 국내 KOSPI200 TR ETF
* IT 부분 → KOSPI200 IT TR ETF
* S&P500 부분 → 국내 상장 환노출 S&P500 ETF

이렇게 함으로써 기존 백테스트 신호를 그대로 유지하면서 실제 연금계좌 ETF로 주문할 수 있도록 한다.

ETF ticker는 코드에 하드코딩하지 말고 config 또는 dictionary로 관리한다.

예:

LIVE_ETF = {
"KOSPI": "278530",
"KOSPI_IT": "363580",
"KR_BOND_SHORT": "114260",
"KR_BOND_MID": "148070",
"KR_BOND_LONG": "439870",
"US_BOND_SHORT": "...",
"US_BOND_MID": "...",
"US_BOND_LONG": "..."
}

미국채 ETF는 계좌 종류와 현재 연금 투자 가능 여부에 따라 변경 가능하므로 configuration으로 관리한다.

---

# 8. 장기 백테스트용 backfill

ETF 상장 이전 데이터가 없는 경우 전략 로직과 데이터 생성을 분리한다.

각 자산에 대해:

actual ETF total-return data > benchmark total-return index > closest proxy

순서로 우선한다.

각 데이터에는 source_type 컬럼을 둔다.

예:

actual_etf
benchmark_index
proxy

그리고 백테스트 결과에 각 자산이 proxy 상태에서 선택된 횟수를 반드시 표시한다.

특히 미국 장기채와 한국 30년채는 상장 이전 proxy 사용 여부가 결과에 영향을 줄 수 있으므로 따로 출력한다.

ETF가 상장된 이후에는 proxy가 아니라 실제 ETF 데이터를 우선 사용한다.

---

# 9. 분배금 처리

가능하면 adjusted close 또는 total-return index를 사용한다.

일반 close와 분배금이 별도로 있다면 분배금을 재투자한 total-return series를 만들어라.

가격수익률 ETF와 총수익률 ETF를 섞지 않도록 한다.

모멘텀 계산과 실제 투자수익 계산에는 동일한 total-return 원칙을 사용한다.

---

# 10. 거래비용

세금은 0으로 둔다.

기본 거래비용은:

ONE_WAY_COST = 0.0002

즉 편도 0.02%이다.

현금에는 매매비용을 부과하지 않는다.

포트폴리오 목표비중이 이전 달과 달라질 때 다음과 같이 계산한다.

turnover =
sum(
abs(new_weight[asset] - old_weight[asset])
for asset in non_cash_assets
)

transaction_cost =
turnover * ONE_WAY_COST

예:

SPY 100% → KOSPI 100%

turnover = 2.0

cost = 2.0 × 0.0002
= 0.0004
= 0.04%

SPY 100% → 현금 100%

turnover = 1.0

cost = 0.02%

현금 → SPY 100%

turnover = 1.0

cost = 0.02%

KOSPI100% →
KOSPI75% + IT25%

turnover =
|0.75 - 1.0| + |0.25 - 0|
= 0.50

cost = 0.01%

월 순수익률은 기본적으로:

net_return =
(1 - transaction_cost) *
(1 + gross_portfolio_return) - 1

로 계산한다.

---

# 11. 거래비용 계산 모드를 두 개 만들 것

두 가지 cost mode를 구현해라.

### target_delta

이전 달 목표비중과 이번 달 목표비중의 차이로 turnover를 계산.

이 모드는 기존 연구 백테스트 결과를 재현하기 위한 모드이다.

### drifted_weights

실제 한 달 수익률을 반영한 뒤 자연스럽게 변한 실제 비중과 새로운 목표비중의 차이로 turnover를 계산.

이 모드는 실제 투자에 더 현실적인 모드이다.

백테스트 결과에는 두 모드의 CAGR/MDD 차이를 모두 보여줘라.

---

# 12. 월간 실행 순서

각 월말 t마다 정확히 다음 순서로 실행한다.

1. t월 말까지 가격데이터 확보
2. KOSPI 3개월 모멘텀 계산
3. SPY 3개월 모멘텀 계산
4. 둘 중 하나가 양수인지 판정

5-A. KOSPI 선택:
- KOSPI 최근 12개월 월수익률 변동성 계산
- vol < 35%:
KOSPI 75%, IT 25%
- vol >= 35%:
KOSPI 100%

5-B. SPY 선택:
- SPY 100%

5-C. 두 주식 모두 음수:
- 안전자산 6개의 최근 1개월 모멘텀 계산
- 가장 높은 자산 선택
- 최고 모멘텀 > 0:
해당 채권 100%
- 최고 모멘텀 <= 0:
현금100%

6. 이전 포트폴리오와 목표 포트폴리오 비교
7. 거래비용 계산
8. t+1월 실제 자산수익률 적용
9. 월말 자산가치 기록
10. 다음 달 반복

---

# 13. 절대 금지할 look-ahead bias

반드시 아래를 검증하는 unit test를 만들어라.

2020-02-29 신호가 2020년 3월 수익률을 선택할 때:

2020년 3월 가격은 신호 계산에 절대로 포함되면 안 된다.

즉:

signal_date = 2020-02-29
holding_period = 2020-03

이어야 한다.

모든 rolling(), pct_change(), shift()의 방향을 점검해라.

---

# 14. 기존 변형 듀얼모멘텀도 benchmark로 구현

비교용으로 기존 전략을 별도 함수로 구현한다.

기존 전략:

위험자산:

* KOSPI200
* SPY

주식 모멘텀:
12개월

최근 12개월 수익률이 높은 주식을 선택.

두 주식 모두 음수이면:

* 한국 10년채
* 미국 10년채

두 자산의 최근 12개월 수익률을 비교.

높은 채권이 양수이면 그 채권 100%.

두 채권 모두 음수이면 현금100%.

KOSPI 변동성 오버레이와 IT25%는 사용하지 않는다.

거래비용은 현재 전략과 동일한 편도 0.02%를 적용한다.

---

# 15. 검증 목표값

동일한 기존 연구데이터와
2016-08 ~ 2026-05 기간을 사용하고
target_delta 거래비용 모드를 사용할 경우 대략 다음 값을 목표로 한다.

기존 변형 듀얼모멘텀:

CAGR ≈ 22.09%
MDD ≈ -20.23%
최종자산배율 ≈ 7.12

현재 변동성 변형 듀얼모멘텀:

CAGR ≈ 27.54%
MDD ≈ -20.23%
최종자산배율 ≈ 10.94

2024-12까지만 계산하면:

기존:
CAGR ≈ 8.96%
MDD ≈ -19.45%

현재:
CAGR ≈ 11.63%
MDD ≈ -10.14%

데이터 source/backfill 처리 방식에 따라 소수점 수준의 차이는 허용한다.

결과가 크게 다르면 먼저 signal shift와 total-return 데이터 처리 여부를 점검한다.

---

# 16. 출력해야 할 결과

summary dataframe:

strategy
start_date
end_date
months
CAGR
MDD
annualized_volatility
Calmar
final_multiple
total_transaction_cost
number_of_rebalances

그리고 월별 로그:

signal_date
holding_month
kospi_mom_3m
spy_mom_3m
kospi_vol_12m
risk_asset_signal
safe_asset_momentum_1m 6개
selected_asset
target_weights
gross_return
turnover
transaction_cost
net_return
portfolio_value
running_peak
drawdown
data_source_type

을 출력한다.

---

# 17. 추가 분석

다음도 자동으로 계산한다.

1. 5년(60개월) rolling CAGR
2. 5년 rolling MDD
3. 5년 rolling Calmar
4. 최저 5년 CAGR
5. CAGR 중앙값
6. MDD 중앙값
7. 자산별 선택횟수
8. 연도별 수익률
9. 2018 / 2020 / 2022 / 2026 스트레스 구간 분석
10. 기존 전략과 현재 전략의 월별 누적자산 비교

특히 2025-2026의 비정상적인 한국주식 강세가 전체 결과를 왜곡할 수 있으므로:

full period

와

end_date = 2024-12-31

두 결과를 반드시 동시에 출력한다.

---

# 18. 코드 구조

가능하면 다음과 같이 모듈화한다.

config.py
data_loader.py
indicators.py
signals.py
portfolio.py
backtest.py
metrics.py
report.py
main.py

핵심 함수 예:

calculate_momentum()
calculate_annualized_volatility()
select_risk_asset()
select_safe_asset()
build_target_weights()
calculate_turnover()
apply_transaction_cost()
run_backtest()
calculate_cagr()
calculate_mdd()
rolling_metrics()

파라미터는 하드코딩하지 말고 dataclass 또는 config 객체를 사용한다.

예:

StrategyConfig(
equity_lookback=3,
safe_lookback=1,
vol_lookback=12,
vol_threshold=0.35,
it_weight=0.25,
one_way_cost=0.0002,
tax_rate=0.0
)

향후 다음 파라미터를 쉽게 바꿔 재실행할 수 있게 설계한다.

equity_lookback = [1,3,6,9,12,18,24]

safe_lookback = [1,3,6,12]

vol_threshold = [0.25,0.30,0.35,0.40]

it_weight = [0,0.20,0.25,0.30,0.40]

---

# 19. 테스트 코드

pytest 기반 unit test도 작성한다.

최소 다음을 검증한다.

* momentum 계산
* volatility 계산(ddof=1)
* 주식 신호 선택
* 안전자산 신호 선택
* 모두 음수일 때 현금
* 35% 경계값
* 거래비용
* SPY→KOSPI 전환비용
* KOSPI100→KOSPI75/IT25 비용
* no-lookahead
* missing data
* ETF 상장 전 backfill
* ETF 상장 후 actual 데이터 우선

---

마지막에는 main.py 하나만 실행하면:

1. 데이터 로드
2. 기존 변형 듀얼모멘텀 백테스트
3. 현재 변동성 변형 듀얼모멘텀 백테스트
4. 두 전략 비교표 생성
5. 월별 로그 CSV 저장
6. rolling 결과 CSV 저장
7. 누적수익률 그래프 저장
8. drawdown 그래프 저장

까지 한 번에 실행되도록 만들어줘.

코드를 작성한 후에는 전략 규칙과 코드가 정확히 대응되는지 항목별로 self-review하고, look-ahead bias 가능성이 있는 부분을 별도로 설명해줘.
