# QuantBot

Long/Short 종목 신호, 로컬 가격 캐시와 포트폴리오 백테스트를 관리하는 Python 프로젝트입니다.
Telegram을 통한 수동·예약 스캔을 지원하며, 거래소 주문 실행기는 포함하지 않습니다.

## 현재 구조

| 경로 | 역할 |
| --- | --- |
| `QuantBotSystem/` | Beta에서 검증한 런타임을 승격한 안정화 배포선 |
| `QuantBotSystem_Portfolio_Beta/` | 신규 검증과 전략 관찰을 수행하는 단일 통합 Beta |
| `QuantBotSystem_Beta/` | 기존 비밀 설정·시장 DB와 호환을 유지하는 레거시 보존선 |
| `RUN_quantbot_version_switch.py` | 통합 Beta 선택·프로세스 잠금·실행 관리 |
| `docs/`, `harness/`, `tools/` | 운영·개발 규칙, 합성 UI fixture, 구조·리뷰 검사 |

세 버전 폴더는 역할이 다릅니다. 작업 폴더나 데이터를 서로 덮어쓰지 않습니다.
현재 전략은 Momentum, Box Range V2, 가격·유동성 Reversion이며, 통합 Beta에는
ACWI 시장 통제와 동일 국가·섹터 hedge를 사용하는 Relative Equilibrium도 있습니다.
과거 촉매 점수 직접 가산 실험은 제거됐습니다. 자세한 기준과 변경 내역은
[아키텍처](docs/architecture.md), [Beta 구조](QuantBotSystem_Portfolio_Beta/ARCHITECTURE.md),
[CHANGELOG](CHANGELOG.md)에 기록합니다.

## 설치와 오프라인 검증

명령은 저장소 루트에서 실행합니다. CI의 기본 Python은 3.11입니다.
새 clone에서는 먼저 빈 템플릿으로 로컬 설정을 준비합니다. 이 명령은 기존 설정을 덮어쓰지
않고, 봇·API·스케줄러를 실행하지 않습니다. 이후 아래 표준 라이브러리 검사를 실행합니다.

```powershell
python tools/prepare_local_config.py
python -m unittest test_architecture.py test_agent_view.py test_review_gate.py test_runtime_settings.py -v
python tools/check_architecture.py
```

런타임의 외부 패키지는 `numpy`, `pandas`, `requests`, `schedule`, `yfinance`,
`pyTelegramBotAPI`입니다. 전체 운영 환경을 재현하는 잠금 파일은 아직 없으므로,
기존 운영 환경과 분리한 가상환경에서 설치·검증합니다.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install numpy pandas requests schedule yfinance pyTelegramBotAPI
.\.venv\Scripts\python.exe -m unittest test_cache_restatement.py -v
```

통합 Beta의 설정·보고서 집중 검사:

```powershell
Push-Location QuantBotSystem_Portfolio_Beta
..\.venv\Scripts\python.exe -m unittest test_runtime_settings.py test_app_bot_reports.py -v
Pop-Location
```

HMM과 연구 분석은 선택 기능입니다. 별도 연구 환경에서
[requirements-hmm.txt](QuantBotSystem_Portfolio_Beta/requirements-hmm.txt),
[requirements-research.txt](QuantBotSystem_Portfolio_Beta/requirements-research.txt)를 사용합니다.
전체 테스트나 분석 스크립트 실행 전 외부 다운로드·장시간 백테스트 여부를 확인합니다.
검증 범위는 [테스트 규칙](docs/validation.md)을 따릅니다.

## 비밀 설정과 데이터

`QuantBotSystem/config.py`와 `QuantBotSystem_Beta/config.py`는 Git에서 제외합니다.
기존 [배포선 예제](QuantBotSystem/config.local.example.py)와
[레거시 Beta 예제](QuantBotSystem_Beta/config.local.example.py)를 완전한 로컬 설정
템플릿으로 사용합니다. 이름을 달리한 중복 예제는 만들지 않습니다. 토큰·채팅 ID·개인
워크북 경로는 비어 있고 공개 기본 유니버스와 필수 구조는 유지합니다.
`python tools/prepare_local_config.py`는 이 두 예제를 같은 폴더의 `config.py`로
복사하되 이미 있는 파일은 그대로 둡니다. 비밀값은 기존처럼 Git에서 제외되는
`config.local.py`에서 관리할 수 있습니다. [통합 Beta 비밀 설정 예제](QuantBotSystem_Portfolio_Beta/config.local.example.py)는 별도 런타임 경로를 유지합니다.

이미 사용하는 clone에서 업데이트할 때는 먼저 해당 두 `config.py`의 로컬 사본을
보관하세요. 추적 삭제 커밋을 pull하면 변경되지 않은 추적 파일이 작업 폴더에서도
삭제될 수 있습니다. 현재 회사 PC 원본이나 실행 중인 봇에는 이 변경을 적용하지 않았습니다.

- 통합 Beta는 selector를 통해 기존 `QuantBotSystem_Beta/config.local.py`와
  `QuantBotSystem_Beta/data/market_data.sqlite`를 공유합니다.
- 배포선은 자체 `QuantBotSystem/config.local.py`와 `QuantBotSystem/data/`를 사용합니다.
- `QUANTBOT_CONFIG_PATH`는 명시적 비밀 설정 파일 경로입니다. 유효하지 않은 경로는 실패하며,
  비어 있는 `QUANTBOT_MARKET_DB_PATH`는 기존 기본 DB 경로를 사용합니다.
- 위 두 `config.py`, `config.local.py`, 옛 이름 `config_local.py`, `local_settings.py`, `.env`,
  `*.private.json`, 데이터·로그·캐시·실계좌/포지션 내역은 커밋하지 않습니다.

예약 갱신·리포트에는 별도로 설치된 로컬 freshness 파이프라인과
`operational_freshness/config.private.json`이 필요합니다. 이 비공개 폴더와 외부 설치물은
저장소에 포함되지 않습니다. 설정·검증된 데이터가 없으면 예약 갱신/리포트가 차단됩니다.
깨끗한 clone만으로 운영 환경이 완성되지는 않습니다.

## 운영 실행

아래 명령은 실제 봇 연결과 예약 작업을 시작합니다. 운영 설정과 외부 실행을 승인한 환경에서만
사용하고, 동일 토큰을 쓰는 봇을 동시에 실행하지 않습니다.

```powershell
# 통합 Beta: foreground 실행, 종료는 Ctrl+C
.\.venv\Scripts\python.exe RUN_quantbot_version_switch.py run BETA

# 안정화 배포선
.\.venv\Scripts\python.exe QuantBotSystem/RUN_telegram_bot_server.py
```

버전 선택·잠금·데이터 위치는 [Beta Runbook](QuantBotSystem_Portfolio_Beta/RUNBOOK.md),
외부 다운로드·메시지 전송 경계는 [운영 안전 규칙](docs/data-operations.md)을 확인합니다.
전체 유니버스 백테스트·backfill·봇 재시작·Telegram 발송은 오프라인 검증에 포함되지 않습니다.

## 개발과 연구

변경은 통합 Beta에서 검증하고 작업 브랜치와 PR로 검토합니다. 배포선 승격과 운영 반영은
별도 승인 범위입니다. `test_experiment_*`, `experiment_*`, `analyze_*`는 연구용 경로이며,
연구 결과를 자동으로 운영 전략에 채택하지 않습니다.

API 키·토큰·개인 경로·연락처, 운영 데이터와 노트북 실행 출력은 게시 전에 제거합니다.
이미 Git 이력에 들어간 값은 현재 파일 삭제나 `.gitignore` 추가만으로 제거되지 않습니다.
작업 절차는 [AGENTS.md](AGENTS.md), [개발 규칙](docs/development-workflow.md),
[독립 리뷰 규칙](docs/review-loop.md)을 따릅니다.
