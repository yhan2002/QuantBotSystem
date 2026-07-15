# QuantBot

QuantBot은 Long/Short 종목선정과 백테스트를 위한 Python 프로젝트입니다.

## 코드 라인

- `QuantBotSystem/`: 배포 버전. 안정화된 원본으로 취급합니다.
- `QuantBotSystem_Beta/`: 업데이트·실험 버전. 새 전략과 검증은 여기서 진행합니다.

## Beta 주요 전략

- MA Momentum: 5/20/60 이동평균 정렬과 동일 방향 기울기 확인
- 거래대금 확인: 252거래일 평균 대비 거래대금 3배 이상 급증 조건
- Value Reversion: 가격·펀더멘털 기반 평균회귀 후보
- Box Range: 박스권 반등·이탈 상태 진단

Beta의 기존 촉매 점수 직접 가산 실험은 제거했습니다. 현재 전략 결과를 변경하는 실험은 별도 A/B 테스트 후 반영합니다.

## 준비

Beta 폴더에서 필요한 패키지를 설치하고, 로컬 설정 파일을 준비합니다.

```powershell
cd ".\QuantBot\QuantBotSystem_Beta"
Copy-Item .\config.local.example.py .\config.local.py
```

API 키, Telegram 설정 등 비밀값은 `config.local.py`, `local_settings.py`, 환경변수에만 보관하고 Git에 추가하지 않습니다.

가격 데이터는 Beta의 로컬 SQLite 캐시에 저장됩니다. 캐시가 없거나 오래된 경우 Beta UI의 `Preload 2Y Market Data` 또는 데이터 갱신 기능을 사용합니다.

## 거래대금 alpha A/B 백테스트

스캘핑 데이터가 아니라 가격·거래대금 기반 보조 alpha를 기존 Momentum과 비교합니다.

```powershell
cd ".\QuantBot\QuantBotSystem_Beta"
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
cd ".\QuantBot\QuantBotSystem_Beta"
python -m unittest test_core_market_data_store.py
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
