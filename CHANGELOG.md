# QuantBot 변경 이력

프로젝트의 주간 변경사항을 배포판과 베타판으로 구분해 기록합니다.

## 2026-08-10

### Unified Beta runtime

- Classic Beta 전용 실행 소스가 없고 Portfolio Beta가 공통 전략의 상위집합임을
  확인해 `QuantBotSystem_Portfolio_Beta`를 단일 Beta 실행선으로 통합했습니다.
- selector의 새 표준 이름은 `BETA`이며 기존 `CLASSIC_BETA`와 `PORTFOLIO_BETA`는
  저장된 명령·예약 작업 호환을 위해 모두 같은 통합 런타임으로 연결됩니다.
- 비밀 설정과 기존 시장 DB는 이동하지 않고 레거시 Classic 경로를 재사용합니다.

### Portfolio Beta Reversion BRAIN alpha A/B

- 기존 Reversion의 충격·종가확인·next-open·부분청산·손절·시간청산을 유지한 채,
  공통 signal-score 경계에 기본 OFF인 국가별 BRAIN alpha filter를 추가했습니다.
- CH/JP/KR은 국가별 20종목 이상 신호일 cohort에서 원식 재현형
  `APAC_EXACT_FILTER`와 수익률 단위 안전형 `APAC_UNIT_SAFE_FILTER`를 분리했습니다.
  US는 기존 Reversion을 그대로 사용하며 252일 이력·회귀 조건 부족 시 baseline으로
  통과합니다.
- 로컬 캐시 전용 `BASELINE / APAC_EXACT_FILTER / APAC_UNIT_SAFE_FILTER` 비교 명령을
  추가했습니다. 전체 유니버스 A/B, 데이터 다운로드, Telegram 실행은 수행하지
  않았습니다.

## 2026-08-04

- Portfolio Beta에 시장 국면별 전략 자본 배분을 연구하는 2-state Gaussian HMM overlay를 추가했습니다. ACWI 수익률·실현변동성·추세 강도와 유니버스 breadth·dispersion을 종가 시점 정보만으로 계산하고, expanding walk-forward 재학습으로 `TREND / NON_TREND` 확률을 산출합니다.
- Momentum과 Box Range·Reversion의 같은 방향 진입 cohort 안에서 기존 요청 gross를 보존하며 확률적으로 비중을 재배분합니다. 기존 신호·청산·보유수량과 name/side/gross cap은 변경하지 않으며, 데이터가 부족한 날짜는 baseline 비중을 그대로 사용합니다.
- 개발자 전용 비교 명령은 로컬 캐시만 읽고 `BASELINE / RULE / HMM`을 동일 IS/OOS 조건에서 비교합니다. Telegram·예약 스캔·Classic Beta·배포판에는 연결하지 않았고 장시간 비교는 명시적인 `--confirm-long-run` 없이는 실행되지 않습니다.

## 2026-08-04

- Nasdaq Top 30 구성은 2026-08-03 조회 시가총액 순위로 교정하고 Classic Beta와 배포판을 다시 동기화했습니다. 저장된 세션 호환성을 위해 기존 `NASDAQ_TOP_30_20260731` 식별자는 유지합니다.

## 주간 요약: 2026-08-03 ~ 2026-08-10

### 배포판 변경

- `Nasdaq Top 30` 구성 종목을 2026-08-03 시가총액 순위 기준으로 교정했습니다.
- 배포판의 유니버스 구성을 Classic Beta와 다시 일치시켰습니다.
- 저장된 사용자 세션 호환성을 위해 `NASDAQ_TOP_30_20260731` 그룹 식별자는 유지했으며 전략·실행 로직 변경은 없습니다.

### 베타판 변경

- Classic Beta의 `Nasdaq Top 30` 구성을 동일한 2026-08-03 스냅샷으로 교정했습니다.
- 유니버스 선택·리포트 경로의 회귀 테스트를 갱신해 배포판과 종목 구성이 일치하는지 확인했습니다.
- Portfolio Beta와 Momentum·Box Range·Reversion 전략 로직에는 변경이 없습니다.

### GitHub 커밋 요약

- `9316d0a`: 로컬 분석 산출물이 Git에 포함되지 않도록 ignore 규칙 보강
- `e82a78b`: 배포판과 Classic Beta의 Nasdaq Top 30 구성 동기화

## 2026-07-31

- Classic Beta에서 검증한 40개 완료 봉 기반 Box Range V2를 배포판에 승격했습니다. 채널 품질·방향 판정, 종가 신호와 다음 시가 진입, 고정 투영 목표 및 보수적 stop-first 체결 규칙을 동일하게 적용합니다.
- 2026-07-31 나스닥 상장 시가총액 스냅샷에서 Alphabet은 `GOOGL`만 유지하고 중복 클래스 `GOOG` 대신 31위 `SNDK`를 포함한 30개 티커를 `NASDAQ_TOP_30_20260731` 그룹으로 Classic Beta와 배포판에 연결했습니다.

## 주간 요약: 2026-07-27 ~ 2026-08-03

### 배포판 변경

- Classic Beta에서 검증한 Box Range V2를 `QuantBotSystem/`에 승격해 40개 완료 봉 기반의 횡보·상승·하락 채널별 reclaim/pullback 규칙을 배포판에 반영했습니다.
- 배포판과 Classic Beta에 `Nasdaq Top 30` 선택 유니버스를 추가하고 스캔·백테스트·세션 경로에서 동일하게 사용할 수 있게 했습니다.
- Nasdaq 유니버스의 Alphabet 중복 노출을 피하기 위해 `GOOG`를 제거하고 `GOOGL` 한 종목만 유지했습니다. Portfolio RL 연구 기능은 배포판에 연결하지 않았습니다.

### 베타판 변경

- Classic Beta의 Box Range V2와 Nasdaq Top 30 구성을 배포판과 동기화하고 관련 세션·리포트 테스트를 보강했습니다.
- Portfolio Beta의 1차 성과 기준을 양의 동일구간 iShares `ACWI` ETF 조정가격 ROI 초과로 단순화하고, 평가 종료일·잔여 포지션 MTM을 공통 ACWI 거래일에 맞췄습니다.
- Portfolio Beta의 내보내기는 `See Detail` 조회와 분리해 요청 시에만 ZIP/CSV를 백그라운드 생성하고 보관 시간·개수·용량 한도를 적용했습니다.
- Box 신호 기반 model-free 연속 비중 RL 연구 오버레이를 추가해 방향별 gross와 5% 종목 한도를 유지하면서 같은 방향 후보 사이의 자본 재배분을 비교할 수 있게 했습니다.
- RL 비교에 `CSQ_UNIV ∪ US Top 100` 결합 유니버스와 LONG/SHORT 50% side cap을 추가하고 미배분 자본을 실제 진입 신호에 재분배하도록 수정했습니다.
- Residual Stat-Arb는 비용 0 가정에서도 IS·OOS 성과가 부진해 코드·메뉴·분석 경로에서 제거했으며 Telegram 운영과 배포판에는 연결하지 않았습니다.

### GitHub 커밋 요약

- `7aeff90`: Portfolio Beta 독립 개발선과 Box Range V2·관리 기능을 포함한 주간 소스 게시
- `6ce7ebe` 및 `a332210`~`f5abfa9`: 실패한 Residual Stat-Arb 제거와 ACWI 벤치마크·평가 종료일 정비
- `3716460`, `5439f25`, `d58a2f0`: 포트폴리오 RL 비중 연구, 결합 유니버스, 진입 신호 자본 재분배 추가
- `727e9c1`~`046fb6e`: 아키텍처 계약, 독립 리뷰 게이트, 에이전트 가독성 스냅샷과 캡처 안정화
- `6efb38d`, `333bfc2`: Box Range V2·Nasdaq Top 30 배포 승격과 Alphabet 단일 클래스 정리

## 주간 요약: 2026-07-20 ~ 2026-07-27

### 배포판 변경

- 7월 20일 검증된 Beta 런타임을 `QuantBotSystem/`에 승격해 포지션 방향 설정, 고정 IS/OOS 백테스트, 횡단면 중립화, 거래 CSV 및 메타데이터 기능을 배포선에 반영했습니다.
- 표준 실행 파일을 `RUN_telegram_bot_server.py`로 통일하고 app/core/strategy 모듈 구조를 적용했으며, 기존 파일명은 호환 래퍼로 유지했습니다.
- 이후 Reversion 기대손익 필터와 Box Range V2 등 신규 전략 실험은 Beta에만 한정했으며 배포판 전략 규칙은 추가 변경하지 않았습니다.

### 베타판 변경

- Portfolio Beta의 RL 연구기에 `CSQ_UNIV ∪ US Top 100`을 중복 제거한 단일 포트폴리오로 평가하는 결합 유니버스 모드를 추가했습니다. Box 원형과 동일 신호·진입·청산을 유지하고, 양방향 50% side cap의 `LONG_SHORT_NEUTRAL` 정책을 baseline·학습·검증에 동일하게 고정했습니다.
- 이전의 거래별 `ENTER/SKIP` RL 파일럿을 운영에 복원하지 않고, Portfolio Beta에 독립적인 model-free 연속 비중 RL 연구 오버레이를 추가했습니다. 초기 정책은 baseline 비중과 정확히 같으며, 학습 정책은 각 진입 코호트의 LONG·SHORT 요청 gross를 각각 보존한 채 같은 방향 후보 간 자본만 재배분합니다. 종목 비중은 상향·하향할 수 있지만 5% name cap과 기존 원장 한도를 우회할 수 없습니다. 실제 현금·수량 원장의 ACWI 대비 로그수익과 일별 CVaR 제약으로 학습하고, 재배분 turnover와 방향별 자본 보존율을 출력합니다. Telegram·예약 스캔·배포판에는 연결하지 않았습니다.
- Portfolio Beta의 `See Detail`은 텍스트만 표시하고, 별도 `Download Export`를 눌렀을 때만 해당 스캔의 ZIP/CSV를 백그라운드에서 임시 생성·전송한 뒤 정리하도록 세 전략의 동작을 통일했습니다. 저장된 내보내기 스냅샷은 압축·불변화하고 24시간/채팅당 8건·32MB의 보관 한도를 적용했습니다.
- Portfolio Beta 개발자 PASS/FAIL 기준을 기존 2배 목표에서 양의 동일구간 iShares `ACWI` ETF 조정가격 ROI를 엄격히 초과하는 기준으로 변경했습니다. 전략·Universe EW·ACWI 성과의 종료일은 최신 공통 ACWI 거래일로 자동 정렬하며, 동률은 `FAIL`, ACWI 누락·0·음수는 `REVIEW`입니다. 승률 58%와 CAGR·MDD·Sharpe는 보조 진단으로 유지합니다.
- ACWI 공통 종료일보다 뒤에 있던 엔진 평가종료 청산이 포트폴리오 원장에서 미처리되어 Box 스캔이 실패하던 문제를 수정했습니다. 종목별 백테스트 전에 유효 ACWI 종료일을 적용하고, 직접 성과 계산 경로는 종료일 이후 잔여 포지션을 명시적 `evaluation-end mark`로 평가해 미래 청산 수익이 승률·구조별 통계에 섞이지 않도록 했습니다.
- Portfolio Beta의 1차 벤치마크를 `ACWI`로 단순화했습니다. Admin의 `Refresh Benchmark`가 Yahoo에서 고정 IS 이전부터 최신일까지 전체 가격을 자동 갱신하며, 성과 비교에는 배당·분할을 반영한 `Adj Close`를 우선 사용합니다. 부분 응답은 기간·최신성·영업일 커버리지로 차단하고 기존 CSV·인증 URL 설정은 제거했습니다.
- Portfolio Beta의 1차 시장 벤치마크를 exact MSCI ACWI(`MIWD00000PUS`) 경계 종가 ROI로 분리하고, Universe EW는 종목선택 초과성과 진단으로 유지했습니다. ACWI 데이터가 없으면 ETF 대체 없이 `N/A/REVIEW`로 처리하며, 로컬 Date/Close CSV 원자적 적재와 검산용 benchmark summary export를 추가했습니다.
- Portfolio Beta Admin에 `Refresh Benchmark`를 추가해 승인된 로컬/HTTPS `Date,Close` CSV의 exact MSCI ACWI를 백그라운드에서 검증·적재하도록 했습니다. 공개 페이지 스크래핑이나 ETF 대체는 사용하지 않습니다.
- 성과 PASS/FAIL의 1차 기준을 1년 누적 포트폴리오 ROI와 양의 동일구간 벤치마크 ROI의 2배 비교로 통일하고, 승률 58%는 보조 진단으로 분리했습니다.
- Box Range를 40개 완료 봉 기반의 횡보·상승·하락 채널 V2로 교체하고 채널 품질, 다음 시가 진입, 고정 목표 경로와 계산 캐시를 보강했습니다.
- Admin 데이터 작업을 마지막 캐시일 기준 10일 중첩 최신분 갱신과 확인이 필요한 4년 전체 backfill로 분리했습니다.
- Reversion의 계획 기대손익 필터를 제한적으로 시험했으나 채택 근거가 부족해 코드·테스트·문서를 실험 전 상태로 롤백했습니다.
- `QuantBotSystem_Portfolio_Beta/` 독립 개발선을 추가해 점수 기반 비중 배분, 현금·수량 원장, 포트폴리오 한도 및 Residual Stat-Arb를 구현했습니다.
- Residual Stat-Arb는 거래비용 0 가정에서도 IS CAGR -1.26%, OOS CAGR -11.25%로 가설 지지 근거가 없어 Portfolio Beta의 메뉴·실행·분석 코드에서 제거했습니다. 공용 점수·포트폴리오 원장 인프라는 유지합니다.
- Classic Beta와 Portfolio Beta를 선택 실행하는 버전 선택기와 프로세스 잠금을 추가하고, 오래된 독립 추세선 실험 파일은 제거했습니다.

### GitHub 커밋 요약

- `fcbe1e3`: Beta 포트폴리오 성과 검증과 전략 분석 기반 추가
- `6812a11`: 검증된 Beta 런타임을 배포판으로 승격
- `67d9381`: 배포·검증 변경 이력과 실행 문서 정리
- `6efc1a0` → `43c0e52`: Reversion 기대손익 필터 실험 후 전량 롤백

## 2026-07-23

### Beta 확률 진입 실험 전량 보류 및 롤백

- 기대손익 경로 필터, GPR/GBM 연속확률 필터, RL 진입 정책의 Beta 코드·전략 연결·실험 테스트를 제거하고 Box Range와 Reversion을 실험 전 로직으로 복원했습니다.
- 배포판과 Telegram 운영 설정은 변경하지 않았으며, 생성된 실험 CSV는 연구 감사 기록으로 로컬에 보존합니다.

## 2026-07-22

### 개발자 성과평가 기준 정리

- 공통 최상위 PASS/FAIL 기준을 `1Y cumulative ROI가 양의 동일구간 벤치마크 ROI의 2배 이상`으로 통일했습니다. 승률 58%는 계속 표시하지만 하드게이트가 아닌 보조 품질지표로 사용합니다.
- 성과 큐브의 최소 30거래 신뢰성 가드와 양수가 아닌 벤치마크의 `REVIEW` 처리는 유지하며, 승률 58%는 보조 품질지표로 표시합니다. Telegram 일반 스캔은 변경하지 않습니다.

## 2026-07-20

### Beta → 배포 승격

- 현재 Beta의 공통 UI, 전체 유니버스 고정 IS/OOS 백테스트, 포지션 방향 설정, 횡단면 중립화, CSV 상세 내보내기, 데이터 메타데이터, 세 전략 런타임을 `QuantBotSystem/` 배포선에 승격했습니다.
- 배포 표준 진입점을 `RUN_telegram_bot_server.py`와 app/core/strategy 모듈명으로 통일하고, 기존 실행 명령과 import가 즉시 깨지지 않도록 예전 파일명은 호환 래퍼로 전환했습니다.
- 토큰·채팅 ID·로컬 설정·SQLite·캐시·생성 결과는 Git 승격 대상에서 제외합니다. 배포 인증 설정은 무시된 `config.local.py`에서 별도로 초기화하고, 데이터 캐시는 배포와 Beta가 각자 유지합니다. `QuantBotSystem_Beta/`는 동일 런타임 기준점에서 다음 업데이트 개발선으로 계속 사용합니다.

### Beta 포지션 방향 설정

- Telegram Settings에 `Long Only`, `Long / Short Neutral`, `Short Only` 포지션 모드를 추가하고 당일 후보, 고정 IS/OOS 백테스트, 예약 스캔에 동일하게 전달되도록 연결했습니다. 기본값은 기존 양방향 동작을 보존하는 `Long / Short Neutral`입니다.
- Git의 과거 베타 박스 구현을 기준으로 상단 장중 돌파 후 종가 박스 복귀 및 상단 반등 2회 이상의 SHORT reclaim 신호를 복원했습니다. 다음 시가는 중심선과 상단 사이에서만 진입하며, 기존 중심 50%·반대 경계 50% 익절과 +7% 절반·+10% 전량 손절을 대칭 적용합니다.
- Settings와 일반·예약·Quick Check 보고서의 사용자 노출 전략명을 `Momentum`, `Box Range`, `Reversion`으로 통일했습니다. 각 전략의 룰과 진입·청산 설명은 유지했습니다.

### Beta 전략 성과 큐브

- Momentum·Box Range·Reversion을 CSQ·JP·CH·US Top 100·Primary Combined 유니버스와 LONG·SHORT·LS 방향별로 독립 재실행하는 개발자 분석기를 추가했습니다. 고정 IS(2024-01~2025-12)와 OOS(2026-01~로컬 최신일)를 동일 기준으로 비교하며, 거래 통계·일별 동일가중 전략 NAV·동일 유니버스 벤치마크·ROI·MDD·Sharpe를 발표용 Excel로 출력합니다.
- 2026-07-17 기준 90개 구간 조합을 검증한 결과 OOS 목표(양의 벤치마크 수익률의 2배, 승률 58% 이상, 거래 30건 이상)는 Reversion의 CSQ LONG과 CSQ LS만 통과했습니다. 다만 IS와 OOS에서 동시에 양의 ROI·승률 58%·거래 30건을 충족한 조합은 없어 운영 규칙 변경 근거로 사용하지 않습니다.

## 2026-07-16

### Beta 개발자 성과 검증

- 활성 거래 슬리브를 매일 동일비중으로 재조정하고 부분청산 현금은 해당 슬리브에 유지하는 일별 전략 NAV 산출기를 추가했습니다.
- 동일 유니버스 원시 종가 기준의 일별 동일가중 벤치마크와 1Y ROI, CAGR, MDD, Sharpe, 초과 CAGR 및 목표 PASS/FAIL 계산을 추가했습니다. 휴장은 전일 종가를 유지하되 7일 초과 가격 공백은 구성 종목에서 제외하고 일별 커버리지를 기록합니다.
- 세 전략의 거래 상세에 실제 진입가와 필요한 최종 청산가를 포함해 다음 시가 진입 및 단계적 청산 손익을 일별로 재현할 수 있게 했습니다.
- 이 성과 지표는 개발자 검증 전용이며 Telegram 일반 스캔·일일 스케줄러 출력에는 연결하지 않았습니다.
- 채팅별 백테스트 상세 숨김 설정을 제거하고, 모든 대화형 스캔에서 요약은 본문에, 상세는 `See Detail` 버튼에 동일하게 보관하도록 단일화했습니다. 예약 스캔은 버튼이 없으므로 기존처럼 요약만 발송합니다.
- IS/OOS 거래 원문이 길어지는 문제를 해소하기 위해 `See Detail`은 구간별 LONG/SHORT 통계만 표시하고, 전체 진입·청산 이벤트(분할청산 포함)는 Excel 호환 CSV 첨부로 분리했습니다.
- Admin에서 전체 종목의 상장국가·기업국가·sector·industry 메타데이터를 별도 갱신해 SQLite에 캐시할 수 있게 했고, 거래 이벤트 CSV에도 분류 라벨을 포함했습니다.
- Settings에 WorldQuant식 횡단면 그룹 평균 제거 방식의 Market/Country/Sector/Industry 후보 점수 중립화를 추가했습니다. 당일 후보와 과거 각 진입일의 IS/OOS 후보 선택에 동일하게 적용하며, 청산 규칙은 원전략을 유지합니다.
- 중립화 IS/OOS는 기존 종목별 비중첩 거래 스트림을 진입일 횡단면에서 후처리하는 실험치이며, BRAIN의 완전한 일별 book-size scaling·beta-neutral 포트폴리오와 동일하지 않습니다.
- Country 중립화 기준을 기업국가가 아닌 상장시장 기준의 US/CH/JP/KR 4개 그룹으로 변경했습니다. CH는 Hong Kong·Shanghai·Shenzhen 상장을 포함하며, ADR은 미국 상장이면 US로 분류합니다.
- Sector/Industry 메타데이터 갱신에서 Yahoo Invalid Crumb(401)를 감지하면 쿠키·crumb 상태를 초기화해 1회 재시도하고, 반복 실패 시 나머지 요청을 중단한 뒤 Telegram에 재시도 안내와 실패 표본만 표시하도록 보강했습니다.
- 홈 화면·콜백·레거시 명령·리포트·전략 비교 래퍼에서 Box TP/SL Test 기능을 제거했습니다.
- 박스 스캔은 종목별 4년 OHLCV를 한 번만 읽어 당일 후보·IS·OOS에서 재사용하고, 20일 박스·반등·거래대금 조건을 벡터 사전 계산해 반복 Pandas 윈도우 연산 병목을 줄였습니다. 진입·청산 및 다음 시가 체결 규칙은 유지했습니다.
- 세 전략에 기본값을 바꾸지 않는 체결 벤치마크 옵션(익일 시가, 익일 OHLC4 실행 프록시, 불리한 방향 2% 초과 갭 미체결)을 추가했습니다. OHLC4는 실제 VWAP이 아니며 실행일 고가·저가 청산을 금지해 일봉 룩어헤드를 방지합니다.
- CSQ/JP/CH 343종목의 고정 IS/OOS 체결 비교에서 모멘텀 2% 갭 필터만 IS와 OOS 모두 평균수익·승률이 개선됐습니다. OOS는 익일 시가 대비 거래 258→239, 평균 +3.27%→+3.99%, 승률 48.8%→50.6%, ROI 26.02%→33.32%, Sharpe 1.86→2.37이었습니다. 박스와 리버전은 체결 변경 효과가 일관되지 않아 기본 익일 시가를 유지합니다.
- 공통 거래 이벤트 CSV와 개발자 체결 비교 CSV에 신호일 종가, 익일 시가, 체결 방식·가격, 종가→시가 및 종가→체결 방향수익을 기록합니다.

## 2026-07-27

### Portfolio Beta Telegram 메뉴 안정화

- 내부 선택자 키 `PORTFOLIO_BETA`는 유지하되 Telegram Markdown 메시지에 표시되는
  버전명을 `Portfolio Beta`로 분리해 밑줄 문자가 메뉴 전송을 중단시키던 오류를
  수정했습니다.

### Portfolio Beta 공통 진행률·백그라운드 작업

- Momentum, Box Range, Reversion, Residual Stat-Arb 수동 스캔과 Admin의 최신 가격,
  4년 backfill, 국가·섹터·산업 다운로드를 단일 FIFO 백그라운드 작업자로 전환했습니다.
  Telegram은 단계, 종목 진행 수, 경과시간을 포함한 1~100% 로딩 메시지를 5% 단위로
  갱신하며, 같은 채팅의 중복 실행은 차단합니다.
- 대기 중 설정 변경이 실행 중 스캔에 섞이지 않도록 유니버스·전략·중립화·포지션 모드를
  제출 시점의 불변 요청으로 고정했습니다.
- Residual Stat-Arb의 피어 순위·상관관계와 residual regression feature에 작업 내
  메모리 및 Portfolio Beta 전용 SQLite 캐시를 추가했습니다. 설정·cutoff·메타데이터·
  Close 데이터 fingerprint가 달라지면 자동 무효화하고, 캐시 장애 시 정상 계산으로
  fallback합니다. 오래된 fingerprint는 주기적으로 정리해 최신 약 4,000개만 유지합니다.
- Portfolio Beta 표시 빌드를 `2026.07.27`로 갱신했습니다. 배포판과 Classic Beta는
  변경하지 않았습니다.

### Portfolio Beta CAGR 중심 요약

- IS/OOS 스캔 요약의 `ALL avg`를 실제 점수 비중·현금·동시 포지션을 반영한 일별
  포트폴리오 NAV 기반 CAGR로 교체했습니다. CAGR은 252거래일 기준으로 연환산합니다.
- LONG/SHORT 및 Quick Check의 동일거래 가중 평균은 포트폴리오 수익률과 혼동하지
  않도록 `avg trade`로 명시했습니다. 개발자 PASS/FAIL의 1차 ROI 기준은 변경하지
  않았습니다.

## 2026-07-24

### Beta Box Range V2

- Beta의 고정 20일 박스 reclaim 엔진을 가격 구조 기반 V2로 전면 교체했습니다. 배포 폴더의 기존 엔진은 변경하지 않았습니다.
- 직전 40개 완료 봉의 평행 채널을 `FLAT`, `RISING`, `FALLING`, `TRANSITION`으로 분류합니다. 횡보 채널은 양방향 false-break reclaim, 상승 채널은 LONG 눌림 반등, 하락 채널은 SHORT 반등 실패를 사용하며 실제 방향은 포지션 모드 설정을 따릅니다.
- 채널 체류율 85% 이상, 폭 4~12 ATR, 5거래일 이상 떨어진 진입 방향 경계 접촉 2회 이상, 거래대금 20일 평균 2배 미만을 품질 조건으로 적용합니다.
- 신호일 종가까지만 채널을 계산하고 다음 시가가 지지선과 1차 목표 사이에 있을 때만 진입합니다. 보유 중 목표선은 신호일에 고정한 기울기로만 연장합니다.
- 거래 상세에 구조·설정 유형, 기울기 비율, 채널 체류율, ATR 폭, 신호 점수와 진입 시점 예상 손익비를 기록합니다.

### Beta Admin·성능 정비

- Admin 가격 갱신을 선택/전체 유니버스별 `최신분 갱신`과 `4년 전체 backfill`로 분리했습니다. 최신분은 종목별 마지막 캐시 날짜에서 10일을 겹쳐 받고, 캐시가 없는 종목만 4년을 초기 적재합니다.
- 4년 backfill은 별도 확인 버튼을 거쳐야 실행되며, 캐시 상태 조회의 종목별 반복 SQL을 단일 그룹 조회로 줄였습니다.
- Box Range V2의 40일 채널 후보를 종목당 한 번만 계산해 IS/OOS에서 재사용하고, 이미 정제된 내부 윈도우의 중복 DataFrame 정규화를 제거했습니다. 합성 4년 일봉 기준 100종목 예상 CPU 시간은 약 164초에서 42초로 감소했습니다.
- Beta 최상위 Python 37개를 문법 검사하고 전체 단위 테스트 66개를 통과했습니다. 외부 다운로드·Telegram 발송·전체 유니버스 백테스트는 실행하지 않았습니다.

### Portfolio Beta 독립 개발선

- 현재 `QuantBotSystem_Beta`를 보존하고 코드 전용 복제본
  `QuantBotSystem_Portfolio_Beta`를 별도 개발선으로 추가했습니다. 토큰·DB·CSV·
  캐시·로컬 설정은 복제하지 않았습니다.
- 신규 버전은 종목별 신호를 방향별 횡단면 percentile로 변환하고, 점수 크기에
  따라 현재 NAV의 최대 5%까지 요청 비중을 차등 부여합니다.
- 기존 일별 동일가중 가상 sleeve 대신 현금·실제 수량·숏 부채·부분청산·고정
  수량을 기록하는 원장을 추가했습니다. 신규 진입 시 총 gross 100%, 종목 5%,
  L/S long 50%·short 50% 한도를 적용하며 신호가 부족하면 현금을 유지합니다.
- Box Range는 동일 엔진 안에서 FLAT 평균회귀와 RISING/FALLING TREND 채널의
  거래 통계 및 NAV 손익 기여를 분리해 기록합니다.
- 기존 Reversion은 유지하고, 동일 상장국가·섹터 peer basket 대비 잔차가
  ±2σ 밖에서 복귀할 때 실제 beta-adjusted 다중 leg로 진입하는
  `Residual Stat-Arb`를 네 번째 전략으로 추가했습니다. 회귀계수와 peer 선택은
  신호 시점 이전 데이터만 사용하고 모든 신호 기반 진입·청산은 다음 시가입니다.
- `RUN_quantbot_version_switch.py`로 `CLASSIC_BETA`와 `PORTFOLIO_BETA`를 서로
  다른 프로세스로 선택 실행하며, 공통 잠금으로 selector를 통한 중복 Telegram
  polling을 차단합니다. 시장 DB와 비밀 설정은 기존 Beta를 공유하고 Portfolio
  원장·exports·runtime state는 별도 경로를 사용합니다.
- See Detail 포트폴리오 export는 거래 이벤트, 실제 leg별 원장, 일별 NAV·현금·
  gross/net을 하나의 ZIP으로 제공하며 거래와 leg 원장은 같은 `trade_id`로
  연결할 수 있습니다.
- 평가창 종료 시 아직 보유 중인 거래는 마지막 종가로 MTM해 NAV에서 누락하지
  않으며, 과거 중립화 구성 종목은 해당 진입일에 실제 가격 이력이 존재한 종목으로
  제한했습니다. 벤치마크 시작가격도 거래소별 직전 거래일을 각각 보존합니다.
- 수동·예약 스캔과 Admin 데이터 갱신은 한 프로세스 안에서 직렬화하고, See Detail
  버튼은 실행별 고유 ID에 연결했습니다. selector는 프로세스 생성 identity까지
  확인해 시작·종료 경쟁과 PID 재사용 시 오종료를 방지합니다.
- 외부 다운로드·Telegram 발송·전체 유니버스 백테스트는 실행하지 않았습니다.

## 2026-07-15

### Beta 운영·백테스트·보안

- 세 전략의 후보 선정 근거는 2024-01~2025-12 In-sample, 평가는 2026-01~07 고정 OOS로 분리했습니다. 각 구간은 실제 다음 시가 진입일 기준으로 절단합니다.
- Admin 전체/선택 유니버스 Refresh는 4년 가격 이력을 full backfill해 2024년 IS 초반의 252일 거래대금 워밍업까지 로컬 DB에 저장합니다.
- 스캔 백테스트를 현재 후보 종목이 아닌 선택 유니버스 전체의 최근 1년 완료 거래 기준으로 집계하고, 실제 진입·청산일은 `See Detail`에서 확인하도록 분리했습니다.
- 스캔 상단에는 각 전략의 진입·청산 규칙과 간단한 전체 유니버스 요약만 표시하도록 정리했습니다.
- `US Top 100` 유니버스 선택 항목을 추가했습니다.
- 베타 모듈을 app/core/strategy 역할 기준 이름으로 정리하고 관련 문서·테스트·실험 파일명을 갱신했습니다.
- 텔레그램 토큰과 허용 채팅 ID를 Git 추적 `config.py`에서 제거하고, 무시되는 `config.local.py` 오버라이드로 분리했습니다.

### Beta Price/Liquidity Reversion v2

- LONG은 전일 대비 상승 외에 당일 양봉 마감을 확인하도록 보강했습니다.
- ±0.5σ 부분익절 뒤 잔여 물량의 손절을 진입가로 올려, 부분익절 후 큰 ATR 손실을 제한했습니다. 갭 발생 시 보수적 손절 가격 규칙은 유지합니다.

## 2026-07-14

### 베타 전략 개편

- Beta에서 기존 고정 촉매 점수 및 관련 실험 모듈을 정리하고, 촉매 점수 직접 가산을 제거했습니다.
- MA Momentum은 5/20/60 이동평균 정렬·동일 방향 기울기에 더해, 252거래일 평균 대비 거래대금 3배 이상 급증을 확인 조건으로 적용합니다.
- 2년 가격 이력 backfill·캐시 갱신 흐름과, 거래대금 급증 alpha를 기존 Momentum과 비교하는 A/B 스크립트 및 단위 테스트를 추가했습니다.
- Value Reversion과 Box Range도 독립 전략 점수만으로 후보를 정렬하도록 정리했습니다.

### 신호 엔진 정교화

- Box Range를 단순 경계 터치 대신 false-break reclaim·거래대금 veto·단계적 청산 구조로 개편했습니다.
- Value Reversion을 60일 추세, 5일 충격 반전, 거래대금 조건을 사용하는 Price/Liquidity Reversion v2로 교체했습니다.
- 보고서·세션·일일 스케줄러 표기를 새 전략 규칙에 맞추고, 두 전략의 회귀 테스트를 추가했습니다.

## 2026-07-13

### 운영·추적 설정

- 배포판(`QuantBotSystem/`)과 베타판(`QuantBotSystem_Beta/`)을 하나의 Git 저장소에서 추적하도록 정비했습니다.
- 기존 GitHub 이력을 보존해 병합하고, 로컬 설정·데이터·백업 파일은 추적에서 제외했습니다.
- 주간 GitHub·Notion 회의자료 자동화와 VS Code 발행 작업을 추가했습니다.

<!-- 새 주간 기록은 이 줄 위에 추가합니다. -->
