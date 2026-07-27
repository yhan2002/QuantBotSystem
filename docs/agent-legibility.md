# DOM·스크린샷 기반 에이전트 가독성

## 목적과 범위

QuantBot의 사용자 표면은 Telegram 메뉴·보고서와 버전 선택기 콘솔이다.
실제 bot token, chat ID, 시장 캐시 또는 운영 프로세스를 사용하지 않고도
메시지 계층, 상태 라벨, 후보 표와 안전 경계를 검토할 수 있도록
`harness/agent-view.json`의 합성 상태를 정적 HTML로 렌더링한다.

이 화면은 Telegram 클라이언트 자체나 전략 성과를 재현하지 않는다. 실제
메시지 포맷은 가까운 mock 테스트로 별도 검증한다.

## 생성 명령

```powershell
python .\tools\build_agent_view.py --output-dir .\.agent-artifacts
python .\tools\build_agent_view.py --output-dir .\.agent-artifacts --screenshot
```

생성물:

- `agent-view.dom.html`: CSP로 네트워크를 차단한 정적 DOM
- `agent-view.dom.json`: landmark, 카드, 표, fixture·DOM 해시
- `agent-view.png`: 1440×1100 기준 브라우저 캡처

`.agent-artifacts/`는 커밋하지 않는다. CI는 세 파일을
`agent-view-quantbot` 아티팩트로 제공한다.

## 변경 시 확인

Telegram 메뉴·전략명·상태 문구·버전 선택기 출력이 바뀌면 합성 fixture와
미리보기를 함께 갱신한다.

```powershell
python -m unittest test_agent_view.py -v
```

검토자는 다음을 확인한다.

- 합성 데이터 배너가 첫 화면에 분명한가
- 배포선, Beta, Portfolio Beta 상태가 혼동되지 않는가
- LONG·SHORT, READY·REVIEW·SKIP이 색상 없이도 구분되는가
- 실제 token, chat ID, DB·캐시 경로, 종목 데이터가 포함되지 않았는가
- 전송·스캔 같은 외부 동작이 `BLOCKED`로 명시되는가
