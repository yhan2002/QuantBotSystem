# QuantBot

QuantBot은 Long/Short 종목선정과 백테스트를 위한 Python 프로젝트입니다.

## 코드 라인

- `QuantBotSystem/`: 안정화된 배포·롤백 기준선입니다.
- `QuantBotSystem_Portfolio_Beta/`: 최신 전략 관찰선이며 Classic 전략과 포트폴리오 기능을 합친 단일 Beta 실행선입니다.
- `QuantBotSystem_Beta/`: 기존 로컬 설정·시장 DB와 롤백 호환을 위한 레거시 보존선입니다.

## 현재 전략

- Momentum: 5/20/60 이동평균 정렬과 동일 방향 기울기 확인
- 거래대금 확인: 252거래일 평균 대비 거래대금 3배 이상 급증 조건
- Box Range V2: 40개 완료 봉의 횡보·상승·하락 채널별 reclaim/pullback 후보
- Reversion: 가격·유동성 충격 이후 평균회귀 후보

Beta의 기존 촉매 점수 직접 가산 실험은 제거했습니다. 현재 전략 결과를 변경하는 실험은 별도 A/B 테스트 후 반영합니다.

## 준비

각 코드 라인은 자기 폴더의 로컬 설정과 데이터 캐시를 사용합니다. 새 기능은 Beta에서 검증하고, 승격 시 런타임 소스만 배포 버전에 동기화합니다.

```powershell
cd ".\QuantBot\QuantBotSystem_Portfolio_Beta"
```

통합 Beta selector는 기존 `..\QuantBotSystem_Beta\config.local.py`와 시장 DB를
그대로 재사용하므로 비밀 설정을 복사하지 않습니다.

API 키, Telegram 설정 등 비밀값은 `config.local.py`, `local_settings.py`, 환경변수에만 보관하고 Git에 추가하지 않습니다.

가격 데이터는 Beta의 로컬 SQLite 캐시에 저장됩니다. 일상 갱신은 Admin의
`Update Selected/All (Latest)`를 사용하며, 각 종목의 마지막 캐시 날짜에서
10일을 겹쳐 최신분만 받습니다. 신규 종목 또는 과거 중간 공백 복구가 필요할
때만 확인 화면을 거쳐 `Backfill Selected/All (4Y)`를 사용합니다.

## 실행

배포 버전:

```powershell
cd ".\QuantBot\QuantBotSystem"
python .\RUN_telegram_bot_server.py
```

다음 업데이트는 `QuantBotSystem_Portfolio_Beta/`에서 검증합니다. 기존 `telegram_bot_server.py` 실행명도 배포 호환용으로 유지하지만, 새 표준 진입점은 `RUN_telegram_bot_server.py`입니다.

## 거래대금 alpha A/B 백테스트

스캘핑 데이터가 아니라 가격·거래대금 기반 보조 alpha를 기존 Momentum과 비교합니다.

```powershell
cd ".\QuantBot\QuantBotSystem_Portfolio_Beta"
python .\test_experiment_momentum_volume_alpha_ab.py
```

주요 옵션:

```powershell
python .\test_experiment_momentum_volume_alpha_ab.py `
  --windows 5,20,60 `
  --holding-days 20 `
  --lookback 252 `
  --turnover-multiple 3 `
  --alpha-weight 0.5 `
  --top-n 5
```

기본 신호는 당일 종가에서 확정하고 다음 거래일 시가부터 진입합니다. 생성되는 SQLite·CSV·캐시 결과는 로컬 산출물이며 Git에 추가하지 않습니다.

## 테스트

```powershell
cd ".\QuantBot\QuantBotSystem_Portfolio_Beta"
python -m unittest test_core_market_data_store.py
python -m unittest discover -s . -p "test_*.py"
python -m py_compile RUN_telegram_bot_server.py strategy_ma_cross.py core_market_data_store.py
```

## 변경 이력

주요 변경사항은 [CHANGELOG.md](./CHANGELOG.md)에 날짜별로 기록합니다. 운영에 영향을 주는 변경은 먼저 Beta에서 검증한 뒤 배포 버전에 반영합니다.

## Git 작업

```powershell
git status
git add <변경 파일>
git commit -m "설명"
git push origin main
```

로컬 설정, API 키, SQLite 데이터베이스, 캐시, 로그, 생성 결과 파일은 커밋하지 않습니다.
