# QuantBot Agent Guide

이 파일은 짧은 진입점이다. 세부 구조·검증·운영 규칙은 `docs/`와 각
버전 문서에 유지한다.

## 시작 순서

1. 저장소 루트, 브랜치, origin, `git status --short`를 확인한다.
2. [README.md](README.md)와 [최근 변경 이력](CHANGELOG.md)을 확인한다.
3. 아래 목차에서 현재 작업과 관련된 문서만 읽는다.
4. 대상 코드 라인과 가까운 테스트를 확인한 뒤 최소 범위로 수정한다.
5. `python -m unittest test_architecture.py -v`와 집중 검증을 실행하고
   결과·미검증 범위·남은 위험을 보고한다.

## 컨텍스트 목차

- [버전·모듈 아키텍처](docs/architecture.md)
- [개발·승격·기록 절차](docs/development-workflow.md)
- [테스트·성과 판정 규칙](docs/validation.md)
- [비밀값·데이터·Telegram 운영 안전](docs/data-operations.md)
- [Portfolio Beta 상세 아키텍처](QuantBotSystem_Portfolio_Beta/ARCHITECTURE.md)
- [Portfolio Beta 실행 Runbook](QuantBotSystem_Portfolio_Beta/RUNBOOK.md)

## 필수 규칙

- `QuantBotSystem/`은 배포선이다. 명시적 승인 없이 직접 수정하지 않는다.
- Classic 변경은 `QuantBotSystem_Beta/`, 포트폴리오·원장 변경은
  `QuantBotSystem_Portfolio_Beta/`에서 먼저 검증한다.
- 사용자 변경을 보존하고 버전 폴더 사이를 무조건 덮어쓰지 않는다.
- 모듈 계층이나 새 파일 접두사를 추가하면 `tools/check_architecture.py`의
  분류 계약도 함께 갱신한다.
- 신호는 종가까지의 정보만 사용하고 신규 진입은 다음 시가보다 빠를 수 없다.
- 비밀 설정, SQLite, 캐시, 로그, 생성 CSV·JSON·Excel을 커밋하지 않는다.
- 전체 백테스트·대량 다운로드·Telegram 발송·봇 실행은 현재 요청에서
  명시적으로 승인받은 경우에만 수행한다.

## 완료 기준

- 관련 집중 테스트와 문법 검사가 통과했다.
- 전략·검증·릴리스·위험 변경은 `CHANGELOG.md`에 기록했다.
- 배포선 변경은 Beta 검증 근거와 승격 범위가 명확하다.
- 변경 파일, 검증 결과, 실행하지 않은 작업, 남은 위험을 인계한다.
