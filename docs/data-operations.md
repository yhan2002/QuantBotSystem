# 비밀값·데이터·Telegram 운영 안전

## 추적 금지

- `config.local.py`, `local_settings.py`, `config_backup.py`, `.env`
- Telegram token, 허용 chat ID, 인증 URL과 API key
- `data/`, SQLite·DB, 캐시, 로그
- 생성 CSV·JSON·Excel·ZIP과 런타임 export
- `active_version.local.json`, selector lock와 임시 파일

내용을 Git뿐 아니라 터미널 출력, 테스트 실패 메시지, 보고서에도 노출하지
않는다. 예제에는 자리표시자만 사용한다.

## 런타임 경계

- Classic Beta와 Portfolio Beta는 같은 Telegram token으로 동시에 polling하지
  않는다.
- 시작·종료·교체에는 루트 selector와 공통 잠금을 사용한다.
- Portfolio Beta는 Beta의 시장 DB와 비밀 설정을 공유하지만 원장·export·
  runtime state는 자체 경로를 사용한다.
- 시장 데이터 schema를 Portfolio 코드에서 임의로 migration하지 않는다.
- 데이터 갱신과 scan은 정의된 serialization·FIFO 경계를 우회하지 않는다.

## 외부·장시간 작업

다음은 구현 요청만으로 실행하지 않는다.

- Telegram bot 시작 또는 실제 메시지·파일 전송
- 최신 가격·4년 backfill·분류 metadata 다운로드
- 전체 유니버스 IS/OOS 분석
- 배포선 승격, 원격 push, 외부 회의 메모 갱신

명시적으로 승인된 경우 대상 버전, 요청량, 예상 시간, 외부 서비스,
변경되는 상태와 중단·복구 방법을 먼저 알린다.

## 안전한 검증 대안

mock, 임시 DB·디렉터리, 작은 고정 fixture, selector 단위 테스트,
`py_compile`을 우선한다. 실제 token·chat ID·시장 DB가 없어도 검증 가능한
경계를 유지한다.

