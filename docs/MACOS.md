# M1 Mac 설치 및 실행 범위

Apple Silicon M1의 **arm64 Python 3.11**을 기준으로 한 안내입니다. macOS 버전은
아직 확인하지 않았습니다. 이 안내는 Windows의 격리 체크아웃에서 코드·오프라인
테스트를 확인한 결과이며, 실제 M1 설치·의존성 wheel·봇 실행은 검증하지 않았습니다.

## 1. 프로젝트 전용 가상환경

먼저 `sw_vers -productVersion`으로 macOS 버전을 확인하고
[공통 M1 준비 안내](https://github.com/yhan2002/Market-Lens/blob/codex/macos-m1-20261012/docs/MACOS-M1-START.md)를 참고합니다.
Python 3.11과 Git 설치를 마친 터미널에서 실행합니다. 저장소 접근 권한이 있는
개인 GitHub 계정으로 인증하고 회사 PC의 가상환경·비밀 설정·시장 DB는 복사하지 않습니다.
Python은 [공식 macOS 안내](https://docs.python.org/3.11/using/mac.html)를 참고해 준비합니다.

```sh
git clone --branch codex/macos-m1-20261012 https://github.com/yhan2002/QuantBotSystem.git
cd QuantBotSystem
uname -m
python3.11 -c "import platform, sys; print(sys.version); print(platform.machine()); assert platform.machine() == 'arm64', 'Use native arm64 Python on M1'"
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install numpy pandas requests schedule yfinance pyTelegramBotAPI
python -m pip check
```

위 clone 명령은 검토 중인 M1 작업 브랜치를 받습니다. main에 병합된 뒤에는 `--branch`를 생략할 수 있습니다.

`uname -m`과 Python 출력 모두 `arm64`인지 확인합니다. `x86_64`면 해당 Python이나
터미널의 실행 방식을 먼저 확인하고, Rosetta 환경을 기본값으로 사용하지 않습니다.
이 저장소에는 기본 런타임의 버전 잠금 파일이 없습니다. 위 패키지는 기존 README의
필수 목록이며 새 버전으로 전체 의존성을 갱신했다는 뜻이 아닙니다. M1에서 설치 오류가
나면 해당 패키지명·Python/macOS 버전을 확인해야 하며, 자동 소스 빌드 성공을 가정하지 않습니다.

## 2. 먼저 오프라인 검증

새 clone에서 토큰과 시장 데이터 없이 아래 검사만 실행합니다. 기존 테스트는
합성 데이터·임시 파일·mock을 사용합니다. 전체 `unittest discover`나 연구 스크립트를
대신 실행하지 않습니다.

```sh
python -m unittest test_architecture.py test_agent_view.py test_review_gate.py test_runtime_settings.py -v
python tools/check_architecture.py
python -m unittest test_RUN_quantbot_version_switch.py test_cache_restatement.py -v
(cd QuantBotSystem_Portfolio_Beta && ../.venv/bin/python -m unittest test_runtime_settings.py test_app_bot_reports.py -v)
python RUN_quantbot_version_switch.py --help
```

필요하면 합성 UI 미리보기 HTML도 만들 수 있습니다. 실제 Telegram과 연결하지 않습니다.

```sh
python tools/build_agent_view.py --output-dir .agent-artifacts
open .agent-artifacts/agent-view.dom.html
```

PNG 생성은 선택 사항입니다. Chromium 계열 브라우저가 PATH에 없으면 실제 설치된
브라우저 실행 파일을 `--browser`로 지정합니다. 예를 들어 Chrome의 기본 앱 경로는
`/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`입니다. 브라우저 설치나
스크린샷 생성은 이 M1에서 아직 확인하지 않았습니다.

## 3. macOS에서 가능한 범위와 제한

| 기능 | 현재 상태 |
| --- | --- |
| 구조·설정·합성 캐시·리포트 테스트 | Windows에서 통과, M1에서 위 명령 재검증 필요 |
| Python 전략·DB 경로 처리 | `pathlib`/표준 라이브러리 사용, Windows COM·pywin32 필요 없음 |
| 루트 selector의 프로세스 관리 | Windows API 또는 Linux `/proc`에 의존하여 macOS 미지원 |
| Beta 직접 foreground 실행 | 아래 참고 경로 사용 가능하나 M1 실기 및 외부 서비스 미검증 |
| 예약 갱신·리포트 | 별도 비공개 freshness 설치·설정·검증된 데이터가 있어야 함 |
| HMM·연구 분석 | 선택 설치, M1 의존성/성능 미검증 |

selector의 `run`·`stop`·`--replace`는 Mac에서 설정·파일·프로세스를 변경하기 전에
명시적인 오류로 차단됩니다. PID 재사용을 구별하는
프로세스 생성 식별자를 Mac에서 아직 구현하지 않았습니다. 단순 PID나 초 단위 실행시각으로
안전 검증을 약화하지 않습니다. 직접 실행은 selector의 중복 실행 잠금을 사용하지 않으므로
같은 봇을 두 번 실행하지 않고 해당 터미널의 `Ctrl+C`로 종료합니다.

## 4. 개인 설정과 수동 실행 참고

이 단계는 설치 확인용 명령이 아닙니다. 실행하면 Telegram 연결과 **내장 예약 스레드**가
함께 시작됩니다. 실제 봇 실행은 별도로 준비하고, 회사의 토큰·chat ID·DB·포트폴리오
기록·freshness 비밀 파일을 개인 Mac으로 옮기지 않습니다.

- 개인용 설정은 [예제](../QuantBotSystem_Portfolio_Beta/config.local.example.py)의 필드만
  참고해 `QuantBotSystem_Portfolio_Beta/config.local.py`에 새로 작성합니다. 이 파일은
  Git에서 제외됩니다. `config.local.py`는 Python 코드로 실행되므로 신뢰하는 파일만 사용합니다.
- Beta 직접 실행은 `QUANTBOT_CONFIG_PATH` → Portfolio Beta의 `config.local.py` →
  legacy `QuantBotSystem_Beta/config.local.py` 순으로 설정을 찾습니다.
- 기본 시장 DB는 `QuantBotSystem_Beta/data/market_data.sqlite`, Beta의 상태·export는
  `QuantBotSystem_Portfolio_Beta/data/` 아래입니다. `.env`는 자동으로 읽지 않습니다.
- 명시적으로 필요한 경우 `QUANTBOT_CONFIG_PATH`, `QUANTBOT_DATA_DIR`,
  `QUANTBOT_MARKET_DB_PATH`, `QUANTBOT_RUNTIME_DIR`, `QUANTBOT_EXPORT_DIR`,
  `QUANTBOT_STATE_DIR` 환경 변수에 **Mac 경로**를 지정할 수 있습니다.
- 예약 갱신과 예약 리포트 검증에는 저장소 외부 `freshness/runner.py` 및
  `operational_freshness/config.private.json`이 필요합니다. 해당 외부 구현의 macOS
  지원은 확인할 수 없으며, 이 저장소에 대체 파이프라인을 추가하지 않았습니다.
  관련 구성·검증된 시장 데이터가 없으면 갱신/예약 리포트가 차단됩니다.
- 내장 스케줄러는 시간대 인자를 지정하지 않은
  [`schedule`](https://schedule.readthedocs.io/en/stable/timezones.html)을 사용합니다. Mac의 로컬 시간으로
  갱신 08:00/19:00, 평일 스캔 19:10을 해석하므로 실행 전 시스템 시간대를 확인합니다.
  이 안내는 `launchd`/cron이나 자동 시작을 등록하지 않습니다.

개인 설정·데이터 권한·외부 freshness 호환성과 실제 외부 통신 범위를 모두 확인한 뒤,
필요한 경우에만 저장소 루트에서 수동으로 실행합니다.

```sh
source .venv/bin/activate
python QuantBotSystem_Portfolio_Beta/RUN_telegram_bot_server.py
```

`--help`나 `--dry-run`을 봇 엔트리포인트에 붙여도 오프라인 모드가 되지 않습니다.
그 옵션을 처리하는 CLI가 없습니다. 오프라인 검증은 위의 지정 테스트만 사용합니다.
Stable/legacy/Portfolio Beta 폴더를 합치거나 운영 설정을 서로 덮어쓰지 않습니다.

## 5. 선택 연구 의존성과 검증 기록

HMM은 [requirements-hmm.txt](../QuantBotSystem_Portfolio_Beta/requirements-hmm.txt),
연구 분석은 [requirements-research.txt](../QuantBotSystem_Portfolio_Beta/requirements-research.txt)에
별도로 선언되어 있습니다. 기본 설치에는 포함하지 않습니다. 필요할 때 별도 연구용
가상환경에서 해당 파일을 설치하고, macOS/arm64 지원과 데이터·다운로드 범위를 확인합니다.

2026-10-12 기준 검증 범위: Windows x64/Python 3.11의 지정 단위 테스트와 구조 검사,
문서의 저장소 내부 링크 확인. M1의 패키지 설치·브라우저·Telegram·가격 공급자·외부
freshness 실행·예약 시각·장시간 백테스트는 미검증입니다.
