# AGENTS.md

easyinvest 저장소에서 작업하는 AI 코딩 에이전트(Claude Code, OpenAI Codex 등) 공용 가이드.
다른 도구 전용 파일(`CLAUDE.md` 등)이 생기면 이 문서를 우선 참조하게 하고, 그쪽엔 도구 전용
설정만 남긴다. 지금은 이 파일이 유일한 가이드다.

## 1. 프로젝트 개요
개인용 투자 대시보드(Streamlit 웹앱). easyinvesting.app 사이트의 기능(추세추종·계절성
스캐너 등)을 참고해 재현/확장하는 작업이 진행 중이다. 배포는 Streamlit Community Cloud —
`main` 브랜치에 push하면 자동 재배포된다. 사용자는 1인 운영자로, 요구사항을 상세히
지정하고 매 라운드 코드 변경 후 검증과 git add/commit/push까지 완료하는 것을 기대한다.

## 2. 파일 구조
- `app.py` — 대시보드 본체. 모든 탭 UI + 경량 계산(전략 백테스트, 코인 API 호출 등)이 한
  파일에 있다(1300줄+, 대용량 단일 파일 — 새 탭도 이 파일에 이어서 추가하는 게 기존 관례).
- `strategies.py` — 동적자산배분 17개 전략. `STRATEGIES` dict(표시명→함수), 각 전략은
  `strat_*(mp, t, ctx) -> {티커: 비중}` 형태.
- `lwc_local.py` — `streamlit-lightweight-charts-ntf` 패치 래퍼(줌 상태 복원 + dblclick
  지원). 건드릴 일 거의 없음.
- `scanner/` — 오프라인 배치 스캐너(로컬에서만 수동 실행, Cloud에서는 실행 안 됨):
  - `universe.py` — 한국(KOSPI200 근사)/미국(NASDAQ+NYSE+AMEX 전종목, 네이버
    marketValue API) 유니버스 구성. `build_universe(us_min_market_cap_usd=...)`로 호출부마다
    시총 하한을 다르게 줄 수 있음(추세추종 $50M / 계절성 $5M).
  - `trend_scan.py` — 추세추종 신호 스캔(이평눌림목/박스돌파/신고가돌파) →
    `data/trend.parquet` + `data/trend_meta.json`.
  - `seasonality_scan.py` — 계절성 스캔(월중 n번째 거래일 패턴) →
    `data/seasonality.parquet` + `data/seasonality_meta.json`.
- `data/*.parquet`, `data/*_meta.json` — 스캐너 결과물. `app.py`는 이 파일들을 읽기만 하고
  재계산하지 않는다. git에 커밋해야 Cloud에 반영됨.
- `tests/test_calibration.py` — 추세추종 신호 계산을 원사이트 공개 수치와 대조하는
  캘리브레이션 테스트(unittest, 실가격 네트워크 조회 필요, 로컬 전용, CI에는 없음).
- `watchlist.json`, `ma_tab_indices.json`, `ma_tab_periods.json` — Gist 미설정 시 로컬
  폴백 저장 파일. Cloud는 재시작마다 파일시스템이 초기화되므로 이것만으로는 영구저장 안 됨.
- `.streamlit/secrets.toml` — Gist 토큰 등 비밀값(`.gitignore`에 등록, 커밋 금지).
  `.streamlit/secrets.toml.example`이 템플릿.
- `docs/strategy_spec.md` — 동적자산배분 전략 규칙 명세.
- `requirements.txt` — 의존성. 패키지를 새로 쓰면 여기도 갱신할 것.

## 3. 실행·개발
```
.venv\Scripts\activate          # Windows. 유닉스는 source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```
스캐너 배치(로컬에서 수동 실행, 유니버스 전체 규모라 20~60분 이상 걸릴 수 있음 — 백그라운드
실행 전 동일 스캔이 이미 돌고 있지 않은지 먼저 확인할 것, 중복 실행으로 리소스 낭비한 전례 있음):
```
python scanner/trend_scan.py
python scanner/seasonality_scan.py
```
결과 갱신 후 `data/*.parquet`·`data/*_meta.json`을 커밋해야 앱(특히 Cloud)에 반영된다.

테스트:
```
python -m unittest tests.test_calibration -v
```

## 4. 탭 구성
1. **차트** — 종목/지수 검색, SMA/EMA 오버레이, 구간 클릭 등락률 측정.
2. **관심목록** — 등록 종목 기간별 수익률 랭킹.
3. **동적자산배분** — `strategies.py`의 17개 전략, 현재 포지션·CAGR·MDD·Sharpe.
4. **프리미엄** — 김치프리미엄(업비트 vs Coinbase BTC), 금치프리미엄(KRX금현물 ETF vs GLD).
5. **매수** — 하위탭 3개: 계절성(`seasonality_scan.py` 결과, 매수창 필터) · 추세추종
   (`trend_scan.py` 결과, 신호유형별 백분위 별점) · 매매계획(포지션 사이징·손절/익절 계산기).
6. **이동평균** — 지수/종목 이동평균 대비 이격도 표. 사용자가 지수·이평 목록을 추가/삭제
   가능(Gist 저장).
7. **코인** — 하위탭 3개: 랭킹(CoinGecko 시세 + 다중소스 과거일봉으로 RS 계산) · 글로벌
   24-7(Hyperliquid `xyz` 퍼프 DEX의 24시간 거래 전통자산) · 온체인(미구현, "준비 중").

## 5. 데이터 소스 & 함정 (반드시 읽을 것)
- **FinanceDataReader** — 국내/미국 주식·ETF·지수 기본 소스.
- **CoinGecko** `coins/markets`, `market_chart` — 코인 시세·시총. 무료 티어 레이트리밋
  있음(과호출 주의), `market_chart` 히스토리는 365일까지만 된다.
- **업비트/Coinbase** — 프리미엄 탭 김치프리미엄 계산에 사용.
- **Hyperliquid** 공개 info API — 코인 탭 글로벌 24-7(빌더 배포 퍼프 DEX `xyz`가 전통자산 담당).
- **FRED** — LAA/RAA 전략의 실업률(UNRATE), FDR 경유로 조회.
- ⚠️ **Binance(`api.binance.com`)는 Streamlit Community Cloud(AWS 미국 리전)에서 지역
  차단(HTTP 451)될 수 있다.** 로컬(한국)에서는 정상 동작해 이 문제를 놓치기 쉽다. 코인
  랭킹의 과거 일봉은 `app.py`의 `_fetch_coin_daily_history` 폴백 체인(Binance→
  data-api.binance.vision→Coinbase→Bybit→OKX→CoinGecko)을 통해서만 받을 것 — Binance
  단독 호출 코드를 새로 추가하지 말 것.
- ⚠️ **네이버금융 페이지 직접 스크레이핑은 차단된다.** `scanner/universe.py`가 쓰는
  `api.stock.naver.com`의 marketValue JSON API는 페이지 스크레이핑이 아니라 되는 것이니
  혼동하지 말 것.
- ⚠️ **Streamlit Community Cloud는 파일시스템이 재시작마다 초기화된다.** 사용자가 저장해야
  하는 값(관심목록, 이동평균 탭 설정 등)은 로컬 JSON 파일에만 의존하면 안 되고
  `st.secrets["gist"]`(GitHub Gist)에 저장 + 로컬 파일은 폴백으로만 쓰는 패턴을 따를 것
  (`load_json_list`/`save_json_list` 참고 — 새 저장 항목 추가 시 이 패턴 재사용).
- ⚠️ **무거운 스캔(전체 유니버스 순회)은 절대 앱 안에서 실시간 계산하지 말 것.**
  `scanner/*.py`로 오프라인 배치 실행 → `data/*.parquet` 저장 → `app.py`는 그 결과 파일을
  읽기만 하는 구조를 지킬 것. 깨면 Cloud에서 타임아웃난다.

### 변동성 변형 듀얼모멘텀 데이터 소스 (2026-09-20 조사, 2026-09-20 배당/backfill 점검 반영)
`strat_vol_dm`(`strategies.py`)이 실제로 쓰는 심볼과 특성. 전부 `load_monthly_panel()`이
`fdr.DataReader(code, "1990-01-01")`로 조회해 월말 종가로 리샘플한 값이며, 시작일은 그
심볼로 실제 조회되는 첫 거래일 기준(로컬 조사 시점).

| 자산 | 실제 심볼 | 데이터 종류 | 데이터 시작일 | 총수익(배당) 반영 |
|---|---|---|---|---|
| KOSPI200 | `278530`(KODEX 200TR) + 상장전 `KS200` 소급 → `KOSPI_BF` | 실제 ETF(KRX) + 벤치마크 지수 | 1993-04(패널 전체 시작, KS200 자체는 1990~) / 278530 실거래는 2017-11-21부터 | 부분적 — 278530 구간(2017-11~)은 TR·배당반영, KS200 소급 구간(~2017-11)은 가격지수라 배당 미반영 |
| KOSPI200 IT | `363580` (KODEX 200IT TR) | 실제 ETF(KRX) | 2020-09-25(backfill 없음) | Yes — TR형 |
| SPY | `SPY` | 실제 ETF(Yahoo) | 1993-01-29 | Yes — `Adj Close` 사용(배당 재투자 반영) |
| 한국 국채 3년 | `114260` (KODEX 국고채3년) | 실제 ETF(KRX) | 2014-07-01 | **No** — 연 1회(12월) 분배금 확인됨(최근 1년 분배율 1.31%), `Close`만 사용해 미반영 |
| 한국 국채 10년 | `148070` (KIWOOM 국고채10년) | 실제 ETF(KRX) | 2014-07-01 | **No** — 연 1회(12월) 분배금 확인됨(최근 1년 분배율 3.62%), `Close`만 사용해 미반영 |
| 한국 국채 30년 | `439870` (KODEX 국고채30년액티브) | 실제 ETF(KRX) | 2022-08-23(backfill 없음) | **No** — 연 1회(12월) 분배금 확인됨(최근 1년 분배율 1.96%), `Close`만 사용해 미반영 |
| 미국 단기국채 | `SHY` → 합성 컬럼 `US_BOND_SHORT_KRW` | 실제 ETF(Yahoo) + 환노출 합성 proxy | 합성 시리즈는 USD/KRW 시작일에 걸려 2003-12-01부터 유효 | Yes — SHY `Adj Close` 기준 |
| 미국 중기국채 | `IEF` → `US_BOND_MID_KRW` | 위와 동일 구조 | 합성 2003-12-01부터 | Yes |
| 미국 장기국채 | `TLT` → `US_BOND_LONG_KRW` | 위와 동일 구조 | 합성 2003-12-01부터 | Yes |
| USD/KRW | `USD/KRW` (FDR→Yahoo `KRW=X`) | 실제 환율(현물) | 2003-12-01 | 해당없음(환율) |

#### [1순위] 한국 채권 ETF 배당(분배금) 처리 — 확인 완료, 코드로는 미해결
- **114260/148070/439870 전부 분배금을 지급한다**(funetf.co.kr 공시 확인, 2025-12 기준
  최근 1년 분배율 각각 1.31% / **3.62%** / 1.96%, 모두 연 1회 12월 지급). 세 종목 다
  "총수익 지수"를 벤치마크로 표방하지만 ETF 자체는 그 총수익 일부를 현금으로 분배하므로
  `mp`의 `Close` 가격만으로는 총수익이 아니다(스펙 9항 위배 확인).
- **왜곡 크기**: 안전자산 6종 비교는 1개월 모멘텀이라, 매년 12월 한 달만 국채3종 가격이
  분배락만큼(1.3~3.6%) 계단식으로 빠진다 — 미국채(SHY/IEF/TLT, Adj Close라 분배락 없이
  매끈)와 비교할 때 12월엔 국채3종이 구조적으로 불리하고, 나머지 11개월은 반대로 소폭
  유리(분배 전 가격에 이자수익이 그대로 얹혀있는 상태)해진다. 148070(10년물)이 분배율
  3.62%/년으로 가장 크다 — 10년물이 안전자산 후보로 뽑히는 달의 손익 비교가 가장 민감.
- **TR형 국고채 ETF 존재 여부**: `fdr.StockListing('ETF/KR')`로 "국고채" 포함 23개 상품을
  전수 확인했으나 "TR"·"누적형" 표기 상품은 하나도 없다 — 국내엔 국고채 TR ETF 자체가
  없어 티커 교체로 통일 불가능.
- **pykrx로 분배금 시계열 확보 시도 → 실패**: `pykrx.stock.get_etf_ohlcv_by_date` 등
  ETF/지수 관련 조회가 전부 KRX가 반환하는 `LOGOUT`(HTTP 400)으로 실패한다.
  `data.krx.co.kr`의 `MDCSTAT00301` 등 report bld가 최근 로그인(KRX_ID/KRX_PW) 없이는
  차단되도록 바뀐 것으로 보이며, 이 환경엔 그 계정이 없어 재현 불가 확인(순수 `requests`로
  동일 파라미터를 보내도 동일하게 `LOGOUT` 응답).
- **결론/조치**: 신뢰할 수 있는 무료 total-return 재구성 데이터원이 없어 코드로 배당을
  되살리는 건 보류. 대신 `strategies.py`의 `VOLDM_ASSET_META`에 자산별
  `total_return: True/False`와 근거(분배율 수치)를 명시했고, 앱 "동적자산배분" 탭 →
  변동성 변형 듀얼모멘텀 상세 → "데이터 출처 상세" expander에 표로 노출한다.
- **KOSPI200(278530)·IT(363580)는 반대로 무분배 TR형이 맞다**(펀드명 자체가 TR) — 위
  왜곡은 국채 3종에만 해당.

#### [2순위] backfill로 시작 구간 확대 — KOSPI200만 가능, IT/30년채는 확인 후 보류
- **KOSPI200**: `278530` 상장(2017-11-21) 이전 구간을 KRX가 무료·비로그인으로 제공하는
  `KS200`(코스피200 가격지수, FinanceDataReader의 GitHub 캐시 `fdr_krx_data_cache`,
  1990-01~) 수익률로 소급 연결했다. 방식은 가격 레벨을 그대로 붙이는 게 아니라
  `composite = index × (etf[상장일] / index[상장일])`로 **상장일 값에 정확히 맞물리게
  스케일링한 수익률 체이닝**(`strategies._backfill_with_index`). 이음매 점검: 2017-11
  월수익률 -2.49%는 전체 표본 z-score -0.41로 이상치 아님, splice 앞(지수 구간) 월변동성
  8.28% vs 뒤(ETF 구간) 8.60%로 자연스럽게 이어짐. 합성 컬럼명은 `KOSPI_BF`
  (`VOLDM_TICKERS["KOSPI"]`가 이걸 가리키도록 변경), 출처 태그는 `KOSPI_BF_SRC`
  컬럼(`actual_etf`/`benchmark_index`)에 매달 기록된다.
  **한계**: KS200은 가격지수라 배당 미반영 — 2017-11 이전 구간은 KOSPI 배당수익률만큼
  실제보다 낮게 나온다(스펙 8항이 요구하는 "benchmark **total-return** index"엔 못 미침,
  현재 KRX가 KOSPI200 TR지수를 무료·비로그인으로 공개하지 않아 차선책).
- **KOSPI200 IT(363580, 2020-09-25 상장)**: backfill 불가 확인, 현행(상장 전 IT 오버레이
  생략, KOSPI200 100%로 대체) 유지. 조사한 근거 — `pykrx`의 지수 코드 검색
  (`주가지수검색`)으로 "코스피 200 정보기술"(그룹 `indIdx=1`, 코드 `indIdx2=155`) 자체는
  찾았지만, 실제 시세 조회 bld(`MDCSTAT00301`)가 로그인 필요로 차단되어(위 1순위 참고)
  받아올 방법이 없다. FDR의 무료 GitHub 캐시엔 KS11/KQ11/KS200 세 지수만 있고 업종지수는
  없다. 네이버/Yahoo 등 대체 소스에도 코스피200 정보기술 업종지수 이력은 없다.
- **국고채 30년(439870, 2022-08-23 상장)**: backfill 불가 확인, 현행(상장 전 안전자산
  후보 제외) 유지. ECOS(한국은행) 100대 지표엔 국고채(3년)/(5년) 금리만 있고 30년물이
  없으며, 정확한 통계표 코드 없이는 ECOS 전체 지표에서 30년물을 찾기 어려웠다. Yahoo에도
  `KR30YT=RR` 류의 한국 장기금리 티커가 없다. 10년물(148070)로 duration을 스케일링한
  proxy는 컨벡시티·커브 형태 차이로 오차가 커서 채택하지 않았다(사용자 지시대로 현행
  유지 + 사유 보고).
- **source_type / proxy 표시**: `strategies.voldm_source_type(mp, key, t)`가 매 시점
  `actual_etf`/`benchmark_index`/`proxy`를 반환한다(US_BOND_*_KRW는 존재하는 전 구간이
  proxy 취급 — 국내 상장 환노출 ETF가 아니라 상시 합성값이기 때문). 앱 상세 화면의
  "데이터 출처 상세" expander가 전체 유효 개월 수 대비 이 자산이 선택된 달 수/비율과
  자산별 내역을 보여준다.

#### [3순위] backfill 후 검증 (2016-08~2026-05, target_delta 거래비용 0.02% 편도 모드)
스펙 15항 목표값과 대조(스펙 14항 "기존 변형 듀얼모멘텀"은 strategies.py에 없어 검증용
스크립트에서 스펙 정의 그대로 별도 구현, turnover=목표비중 차이 합, cost=turnover×0.02%).

| 구간 | 지표 | 목표(스펙) | 실측(backfill 후) | 차이 |
|---|---|---|---|---|
| 2016-08~2026-05, 기존 변형 DM | CAGR/MDD/배율 | 22.09% / -20.23% / 7.12 | 21.96% / -20.11% / 7.04 | 거의 일치 |
| 2016-08~2026-05, 변동성 변형 DM | CAGR/MDD/배율 | 27.54% / -20.23% / 10.94 | 26.08% / -20.12% / 9.76 | CAGR -1.5%p |
| ~2024-12, 기존 변형 DM | CAGR/MDD | 8.96% / -19.45% | 9.24% / -19.83% | 거의 일치 |
| ~2024-12, 변동성 변형 DM | CAGR/MDD | 11.63% / -10.14% | 9.97% / -18.12% | MDD 큰 차이 |

- **"기존 변형 DM"이 거의 정확히 재현된다는 점이 중요** — KOSPI_BF backfill과 미국채
  환노출 합성 파이프라인 자체는 스펙과 정합적이라는 뜻. 변동성 변형 DM의 CAGR 갭
  (~1.5%p, 2016-08~2026-05 기준)은 방향과 크기 모두 위 1순위 배당 미반영 문제로 설명
  가능한 범위다(6개 안전자산을 더 자주/세밀하게 오가는 구조라 국채3종 배당누락이 누적).
- **~2024-12 구간의 MDD 괴리(-18.12% vs -10.14%)는 배당 문제로 설명이 안 되는 규모다.**
  원인 추적 결과 peak(2018-09)~trough(2020-02) 구간에서 SPY↔KOSPI_BF↔국채를 매달
  갈아타는 휘핑쏘 패턴이 확인됐고, 이 시퀀스 자체가 원 연구와 다르게 나온 것으로 보인다
  (정확한 원인 미확정 — 월말 기준일/모멘텀 윈도 계산 방식 등 원 백테스트의 세부 구현
  차이 가능성). **목표값에 억지로 맞추지 않고 미해결 이슈로 남긴다.**
- target_delta 거래비용(0.02% 편도)은 현재 `app.py`의 `backtest()`엔 구현돼 있지 않다
  (무비용) — 위 표의 실측치는 검증용 스크립트에서만 별도 계산한 값이며, 화면에 보이는
  CAGR/MDD와는 다르다. 거래비용 모드(target_delta/drifted_weights) 자체를 앱에 넣는
  일은 이번 작업 범위 밖이라 별도로 하지 않았다.

#### 기타
- **미국채 원화환노출 처리**: 국내 상장 환노출 ETF를 쓰는 게 아니라 `strategies.py`의
  `augment_panel()`이 `KRW_return = (1+USD채권월수익률)×(1+USD/KRW월수익률) - 1`을 매월
  복리 누적해 `*_KRW` 파생 컬럼을 합성한다(원본 SHY/IEF/TLT는 패널에 그대로 남고, 전략은
  `_KRW` 컬럼만 참조). 실재하는 상장 상품이 아니라 계산상 합성 시리즈다.
- **백테스트 실제 시작 시점(현재 앱, backfill 반영 후)**: KOSPI_BF가 1990년대까지
  확장되면서 병목이 SPY(1993-01) 쪽으로 옮겨갔다 — 실제 신호는 1993-04, 첫 실현 월수익률은
  1993-05부터 나온다(이전엔 278530 상장일 제약으로 2018-03이 시작이었음).
- **CASH 처리**: 안전자산 6종이 모두 1개월 수익률 음수/데이터 없음이면
  `{"CASH": 1.0}`을 반환하지만 "CASH"는 `mp`에 없는 티커라 `backtest()`가 그 비중의
  수익률을 그냥 0으로 취급한다(이자 없는 현금 보유 근사, `strategies.py:6` 주석 참고,
  변경 없음).
- **화면 표시**: "동적자산배분" 탭 → 전략 선택 → 상세 화면에 "백테스트 구간: YYYY-MM ~
  YYYY-MM"과 전략별 데이터 한계 캡션이 항상 뜨고, 변동성 변형 듀얼모멘텀을 선택하면
  "데이터 출처 상세" expander에 총수익 여부 표 + benchmark_index/proxy 선택 비율이
  추가로 뜬다(`app.py` `_STRAT_DATA_NOTES` / VOLDM 관련 블록).

## 6. 코딩 규칙
- **matplotlib 의존 금지.** 색상 배경은 `_color_scale_zero`/`_apply_bg`(app.py) 같은 순수
  CSS 헬퍼로 만든다. `Styler.background_gradient`도 matplotlib 의존이라 쓰지 않는다.
- 수익률/이격도 등 증감 색상은 **0 기준 대칭**: 양수=녹색, 음수=빨강, 진하기는 상한(cap)
  대비 절대값 비율.
- 외부 API 호출은 `@st.cache_data(ttl=...)`로 캐시하고, 여러 종목/코인 조회는
  `ThreadPoolExecutor`로 병렬화한다.
- 외부 호출 실패는 **그 영역만** 안내 문구를 띄우고 넘어가야 한다 — 앱 전체가 죽으면 안 됨.
  실패 원인(HTTP 상태코드·응답 본문 등)은 `print()`로 남겨 Cloud "Manage app" 로그에서
  진단 가능하게 한다.
- `.streamlit/secrets.toml`은 **절대 커밋 금지**(이미 `.gitignore`에 있음, `git add -f`로
  강제 추가하지 말 것).
- 주석은 "왜"만 남긴다 — 코드가 이미 설명하는 당연한 내용은 쓰지 않는다.

## 7. 동적자산배분 전략 추가/수정
- `strategies.py`의 `STRATEGIES` dict에 표시명→함수로 등록.
- 함수 시그니처: `strat_XXX(mp, t, ctx) -> {티커: 비중}` (mp=월간 가격 패널 DataFrame,
  t=현재 시점 인덱스, ctx=`build_ctx`가 만든 시장상태 컨텍스트).
- 규칙 상세는 `docs/strategy_spec.md` 참고.

## 8. 작업 완료 기준
요청받은 변경을 코드 수정 → 검증(가능하면 직접 실행/테스트, 브라우저 자동화가 안 되면
`streamlit.testing.v1.AppTest`로 대체 검증) → **git add, commit, push까지 완료**하는 것을
한 세트로 취급한다. 커밋 메시지는 사용자가 지정하면 그대로 쓰고, 안 지정하면 변경 요지를
한국어로 간결하게 쓴다.

## 9. 진행 중 / 남은 과제
- **추세추종**: 별점을 신호유형별 백분위로 바꿔 신고가돌파 쏠림은 완화했지만, 정렬 2차
  기준(RS)이 여전히 신고가/모멘텀형 종목에 유리한 구조적 편향이 남아있음(의도적으로 범위
  밖에 둔 상태).
- **계절성**: `tests/test_calibration.py`의 Oil-Dri 대표보유기간 테스트는 10년 lookback
  창이 매일 하루씩 밀리며 표본수가 자연 변동해 실패할 수 있다 — 코드 버그 아님, 필요시
  테스트 허용오차를 넓히는 걸 검토.
- **코인 탭**: Binance 폴백 체인을 로컬에서는 검증했지만, Cloud 배포본에서 실제 어떤
  소스로 성공하는지는 사용자가 "Manage app" 로그로 확인해야 한다(에이전트가 직접 못 봄).
- **온체인 하위탭**: 미구현. 어떤 지표가 어떤 무료 API로 가능한지 조사한 결과표만 코인 탭
  안에 있음 — 구현 우선순위는 사용자 지시 대기.
- 원 사이트(easyinvesting.app)와의 수치 대조는 계속 진행형이다. 계산 방법론 차이(배당
  미반영, 고정보유기간 vs 손절/트레일링 추정 등)로 완전히 일치하지 않는 항목들이 있고,
  알려진 한계는 각 스캐너 파일 상단 docstring에 기록돼 있으니 새 대조 작업 전에 먼저 읽을 것.
