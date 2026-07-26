# Residual Stat-Arb

## 목적

Residual Stat-Arb는 종목 수익률에서 동일 상장국가의 시장 움직임과
동일 섹터 동종업체 움직임을 제거한 뒤, 남은 잔차가 극단값에서
되돌아오기 시작할 때 진입하는 상대가치 전략이다. 기존
Price/Liquidity Reversion을 대체하지 않는 네 번째 독립 전략이다.

첫 버전은 ML, 외부 API, 미래 데이터 보정 없이 `numpy`와 `pandas`만
사용하는 결정론적 모델이다.

## 입력 계약

모든 함수는 호출자가 이미 준비한 데이터만 사용한다.

```python
price_map = {
    "AAA": aaa_ohlcv,  # DatetimeIndex + Open/High/Low/Close/Volume
    "BBB": bbb_ohlcv,
}
metadata_by_ticker = {
    "AAA": {"listing_country": "US", "sector": "Technology"},
    "BBB": {"listing_country": "US", "sector": "Technology"},
}
```

- `normalize_ohlcv(df)`: OHLCV 열 이름, 숫자형, 날짜 순서와 중복을 정규화
- `analyze_residual_stat_arb(...)`: 한 종목의 최신 또는 지정일 신호 분석
- `scan_residual_stat_arb(...)`: 공급된 유니버스의 구조화된 LONG/SHORT 결과
- `backtest_residual_stat_arb(...)`: 실제 target + peer legs 백테스트 결과
- `run_residual_stat_arb_backtest(...)`: 기존 universe aggregator용 tuple 래퍼

일반 API의 `peer_selection_as_of=None`은 각 신호일 직전 자료로 동적으로
피어를 고른다. 운영 앱은 OOS 검증과 같은 cohort를 사용하기 위해
`peer_selection_as_of=2025-12-31`을 명시한다.

이 모듈은 SQLite를 읽거나 가격·펀더멘털을 다운로드하지 않는다.

## 팩터, 피어 선택과 회귀

대상 종목과 같은 `listing_country`의 공급 종목을 동일가중하여 시장
수익률을 만든다. 대상과 `listing_country + sector`가 모두 같은 후보는
신호일 이전 수익률 상관계수 순으로 최대 5개를 선택한다.

```text
market(t)        = 같은 상장국가 종목 수익률의 동일가중 평균
peer(t)          = 선택한 동종업체 수익률의 동일가중 평균
sector_excess(t) = peer(t) - market(t)

진단 회귀:
target(t) = alpha + beta_market * market(t)
                   + beta_sector * sector_excess(t) + diagnostic_error(t)

실행 회귀:
target(t) = hedge_alpha + hedge_beta * peer(t) + executable_residual(t)
```

기본 회귀 창은 최근 120개 유효 관측치이고 최소 60개가 필요하다.
`t`일의 계수는 반드시 `t-1`일까지의 행으로만 추정한다. 현재 종가
수익률은 계수가 확정된 다음 현재 잔차 계산에만 사용한다.

시장·섹터 2요인 베타는 설명과 진단용으로 보존한다. 신호와 실제
헤지가 서로 다른 위험을 보지 않도록, 매매 신호에는 target 대
peer-basket 직접 회귀에서 얻은 `executable_residual`을 사용한다.
양수가 아닌 직접 베타는 반대 포지션으로 헤지할 수 없으므로 거래
대상에서 제외하고, 양의 베타가 비정상적으로 크면 기본 3배로 제한한다.

일반 엔진 스캔은 별도 cutoff가 없으면 신호일 직전 정보로 피어를
선정한다. Portfolio Beta 운영 앱의 현재 후보와 OOS 백테스트는 모두
2025-12-31 이전 정보로 선정한 동일 피어 cohort를 사용한다. IS/OOS
백테스트도 각 평가기간 시작 전 마지막 거래일에 피어를 한 번 선정해
해당 평가창 동안 고정한다. 따라서 평가기간 안의 미래 상관관계로
과거 피어를 다시 고르는 일은 없다.

## 신호와 체결

```text
residual_5d(t) = 최근 5일 executable_residual 합
z(t)           = residual_5d(t)를 직전 60개 residual_5d로 표준화
```

- LONG: 전일 `z <= -2`였고 현재 종가에 `z > -2`로 재진입
- SHORT: 전일 `z >= +2`였고 현재 종가에 `z < +2`로 재진입
- LONG `signal_score`: 양수
- SHORT `signal_score`: 음수
- 진입: 신호가 확인된 다음 거래일 시가

잔차가 극단에 도달한 순간이 아니라 정상 범위 안으로 되돌아오기 시작한
종가를 확인한 뒤 진입한다.

진입 시점에는 실행 회귀의 `hedge_alpha`, capped `hedge_beta`와 해당
신호의 z-score 기준 평균·표준편차를 함께 고정한다. 보유 중에는 이
고정 모델로 target과 실제 보유 peer basket의 residual을 다시 계산한다.
청산 zero/stop은 매일 새 beta로 가정한 가상 spread가 아니라 실제 보유
leg 비율과 일치하는 held-spread z-score를 사용한다.

## Multi-leg 거래

LONG target의 signed ratio 예시는 다음과 같다.

```text
AAA target       +1.00
BBB peer hedge   -0.35
CCC peer hedge   -0.35
gross             1.70
net               0.30
```

SHORT target은 모든 부호가 반대다. 동종업체 헤지 비율은 선택한 peer에
동일 배분된다. `gross_ratio`는 target과 모든 hedge leg의 절댓값 합이며,
allocator는 이 전체 묶음을 동일한 배율로 축소해야 한다.

백테스트 거래 dict는 각 leg의 `signed_ratio`, 진입·청산 시가,
기초자산 수익률과 PnL 기여도를 보존한다. `return_pct`는 leg PnL 합을
진입 gross ratio로 나눈 값이다.

## 청산

모든 청산 판단은 종가에서 확정되고 다음 거래일 시가에 전 leg를
동시에 청산한다.

- residual zero: LONG `z >= 0`, SHORT `z <= 0`
- residual stop: LONG `z <= -3.5`, SHORT `z >= +3.5`
- time exit: 진입 후 10번째 보유 종가

같은 상장국가를 강제하여 거래일 불일치를 줄인다. target의 익일 시가에
선택 peer 중 하나라도 같은 날짜 시가가 없으면 동시 진입을 건너뛴다.
청산 시 한 leg라도 예정된 익일 시가가 없으면 ticker와 예정일을
`exit_execution_failures`에 기록하고 다음 공통 시가 체결을 시도한다.
평가기간이 먼저 끝나면 모든 미청산 leg를 평가말 현재 이용 가능한
마지막 종가로 mark하여 거래 수익률과 portfolio NAV에 포함한다.

## 미래정보 차단

- peer 상관계수 표본 종료일은 신호일보다 빠르다.
- 신호일 회귀계수 표본 종료일은 신호일보다 빠르다.
- 현재 `residual_5d`의 z-score 평균·표준편차는 직전 60개 값만 사용한다.
- 진입과 신호 기반 청산은 신호일 종가가 아닌 익일 시가다.
- 운영 현재 후보는 각 `listing_country`의 최신 캐시 날짜와 signal date가
  같은 경우만 표시한다. 또한 `as_of`보다 국가 최신일이 7 calendar days
  초과 뒤처지면 해당 국가 전체를 제외한다. 제거한 stale 신호·국가 feed와
  데이터 부족 수를 보고한다.
- 명시적 `as_of`는 해당 시점을 포함해 재현한다. 기본 실행은 KST 오늘
  00:00 직전까지만 허용해 아직 확정되지 않은 당일 daily bar를 제외한다.
- neutralization과 ranking은 동일 signal date의 `OK/NO_SIGNAL` 종목만
  한 cohort로 묶고 `DATA_INSUFFICIENT` 종목은 제외한다.
- 메타데이터는 호출자가 공급한다. 과거 백테스트에서 현재 섹터 분류를
  사용할 경우 생길 수 있는 분류 생존편향은 호출자 책임이다.

## Portfolio Beta 연결

`strategy_residual_stat_arb_app.py`가 로컬 캐시와 metadata를 한 번
읽어 전략 모듈에 공급하고, 결과를 공통 `Signal` 계약으로 변환한다.
allocator는 target과 모든 hedge leg를 같은 비율로 축소하며, ledger는
각 leg의 수량·현금·short liability와 익일 시가 청산을 기록한다.
상세 내보내기에는 residual z, 베타, 피어 목록, gross/net과 일별 NAV가
포함된다.

## 제한사항

- 거래비용, 공매도 가능 여부, borrow fee와 체결 미끄러짐은 아직 미반영
- 수익률 모델은 공급 OHLCV의 raw `Close`를 사용하고 체결은 raw
  `Open`/`Close`를 사용한다. `Adj Close`로 신호만 교체하면 체결·수량과
  corporate-action 기준이 달라지므로 이번 버전에서는 변경하지 않았다.
  따라서 분할·배당이 residual과 상관계수에 미치는 영향은 명시적 한계다.
- 정적 metadata의 과거 시점 정확성은 보장하지 않음
- point-in-time universe membership과 섹터/국가 분류 이력이 없으므로
  과거 IS/OOS에도 현재 로컬 universe와 metadata를 재사용한다. 따라서
  상장폐지·편입 변화에 따른 survivorship bias와 classification look-ahead가
  남으며, 이를 제거한 백테스트로 해석하면 안 된다.
- freshness는 `as_of` 이하의 로컬 캐시 날짜를 국가별로 비교하는 결정론적
  검사다. 외부 거래소 달력이나 feed 상태를 조회하지 않으므로 국가 전체
  데이터 지연·휴장 여부까지 검증하지는 않는다.
- 구조 변화로 동종업체 관계가 붕괴할 수 있음
- 시장·섹터 2요인 beta는 diagnostics이며 실행 신호는 실제 보유 가능한
  direct-peer residual이다. 단일 peer basket만으로 시장·섹터 위험을
  완전히 동시에 제거할 수 없음
- 평가창 동안 고정한 피어는 look-ahead를 막는 대신 기업 관계 변화에
  늦게 반응할 수 있음
- 전 종목이 서로의 peer 후보와 동일가중 시장 구성원이 되므로 현재
  구현의 전체 계산량은 대략 `O(N² × T)`이다. 대규모 유니버스는 별도
  실행시간 예산과 최적화가 필요하다.
- 대규모 universe 백테스트는 호출자가 별도 실행해야 하며 모듈 import나
  scan 함수 정의 자체가 이를 자동 실행하지 않음
