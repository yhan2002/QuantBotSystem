# 버전·모듈 아키텍처

## 코드 라인

| 위치 | 역할 | 수정 원칙 |
| --- | --- | --- |
| `QuantBotSystem/` | 검증된 배포선 | 명시적 승인과 Beta 검증 없이 직접 수정하지 않는다 |
| `QuantBotSystem_Beta/` | Classic Beta 개발·롤백선 | 기존 전략·UI의 실험과 회귀 검증 |
| `QuantBotSystem_Portfolio_Beta/` | 포트폴리오 개발선 | 점수·비중·원장·비동기 스캔·Portfolio 보고 |
| `RUN_quantbot_version_switch.py` | Beta 선택·프로세스 잠금 | 두 poller 동시 실행을 막는 루트 경계 |

버전 폴더는 코드 복제본이지만 자동 동기화 대상이 아니다. 변경 목적과
테스트 근거를 확인한 뒤 필요한 파일만 승격한다. 로컬 설정과 데이터는
복사·승격하지 않는다.

Portfolio Beta의 상세 불변식과 저장 경계는
[`ARCHITECTURE.md`](../QuantBotSystem_Portfolio_Beta/ARCHITECTURE.md),
운영 명령은
[`RUNBOOK.md`](../QuantBotSystem_Portfolio_Beta/RUNBOOK.md)를 따른다.

## 버전 내부 역할

파일 접두사는 책임 경계를 나타낸다.

- `RUN_*`: 사용자가 실행하는 프로세스 진입점
- `app_*`: Telegram·스케줄러·세션·보고 orchestration
- `strategy_*`: 전략별 신호와 진입·청산 규칙
- `core_*`: 시장 데이터, 체결, 중립화, 백테스트, 성과 계산
- `config.py`: 추적 가능한 기본 설정
- `config.local.py`, `local_settings.py`: 추적 금지 로컬 비밀 설정
- `analyze_*`: 개발자용 분석·장시간 검증 진입점
- `test_*`: 해당 버전의 회귀·구조 계약

## 의존성 방향

```text
RUN_*  ─────▶  app_*
app_*  ─────▶  strategy_* + core_*
strategy_* ─▶  core_*와 추적 가능한 설정
core_* ─────▶  더 낮은 core_*·표준 라이브러리·외부 패키지
```

- core 모듈은 Telegram UI·세션에 의존하지 않는다.
- 버전 내부 코드는 다른 버전 폴더의 Python 모듈을 직접 import하지 않는다.
- 공유 시장 데이터와 비밀 설정은 selector가 환경·경로로 연결한다.
- 배포 호환 래퍼는 유지하되 신규 표준 진입점은
  `RUN_telegram_bot_server.py`다.

## 자동 강제

`tools/check_architecture.py`는 애플리케이션 모듈을 import하지 않고 AST로
세 버전을 각각 검사한다.

| 계층 | 파일 |
| --- | --- |
| 0: 설정 | `config*`, `runtime_settings` |
| 1: 기반 | `core_*`, `market_data_store` 호환 래퍼 |
| 2: 전략 | `strategy_*`, 전략 호환 래퍼, `portfolio_*` 정책 |
| 3: 앱 | `app_*`, bot·scheduler 호환 래퍼 |
| 4: 진입·분석 | `RUN_*`, `analyze_*`, `experiment_*` |

각 모듈은 같은 계층 또는 더 낮은 계층만 import할 수 있다. 검사기는 다음도
강제한다.

- 세 버전 사이 Python 모듈 직접 import 금지
- 버전 내부 import cycle 금지
- 상대 import로 계층 검사를 우회하는 것 금지
- 새 production 파일의 계층 분류 필수
- 각 버전의 공통 app/core/strategy 계약과 Portfolio 필수 모듈 존재
- root selector가 버전 구현 모듈을 직접 import하지 않을 것

기존 배포 호환 래퍼는 이름별 계층에 명시적으로 분류한다. 파일명 규칙으로
표현할 수 없는 새 모듈은 검사 대상에서 제외하지 말고 계층 분류를 추가한다.

## 시간 계약

- 신호는 해당 거래일 종가까지의 정보만 사용한다.
- 신규 포지션은 다음 거래일 시가보다 빨리 체결하지 않는다.
- IS/OOS 경계 밖의 가격과 미래 청산 결과를 현재 평가에 섞지 않는다.
- 직접 성과 계산 경로도 평가 종료 시점을 명시적으로 mark한다.
