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
