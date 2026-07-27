# 테스트·성과 판정 규칙

## 빠른 검증

변경한 파일만 먼저 문법 검사한다.

```powershell
python -m py_compile <변경한 파일.py>
```

selector 변경:

```powershell
python -m unittest .\test_RUN_quantbot_version_switch.py
```

Classic Beta 또는 Portfolio Beta 변경:

```powershell
Set-Location .\QuantBotSystem_Beta
python -m unittest <가까운 test_파일.py>

Set-Location ..\QuantBotSystem_Portfolio_Beta
python -m unittest <가까운 test_파일.py>
```

## 버전 전체 단위 테스트

관련 변경이 넓을 때만 해당 버전 하나를 실행한다.

```powershell
Set-Location .\QuantBotSystem_Beta
python -m unittest discover -s . -p "test_*.py"

Set-Location ..\QuantBotSystem_Portfolio_Beta
python -m unittest discover -s . -p "test_*.py"
```

테스트가 외부 데이터·Telegram·긴 백테스트를 실제 호출하지 않는지 먼저
확인한다.

## 전략 시간 불변식

- 신호 계산은 신호일 종가 이후 정보를 읽지 않는다.
- 진입은 다음 거래일 시가 또는 그 이후다.
- 데이터 분할, 중립화 cohort, 벤치마크 종료일에 미래 정보가 섞이지 않는다.
- 실험은 no-lookahead 회귀 테스트를 포함한다.

## 성과 PASS/FAIL

개발자 보고서의 1차 기준은 동일 구간의 양의 iShares `ACWI` ETF 조정가격
ROI다.

- `Strategy ROI > ACWI ROI`: `PASS`
- 동률 또는 하회: `FAIL`
- ACWI 누락·부실·0 이하 수익률: `REVIEW`

Universe EW는 종목선택 진단이며 대체 벤치마크가 아니다. 승률 58%,
CAGR, MDD, Sharpe는 보조 진단이고 PASS/FAIL을 바꾸지 않는다.

## 승인 필요한 검증

전체 유니버스 분석, 장시간 백테스트, 가격 backfill, 대량 metadata 갱신,
실제 봇 실행과 Telegram 전송은 현재 요청에서 명시적으로 승인받은 경우에만
수행한다. 실행 전 예상 유니버스·기간·시간·외부 호출·생성 파일을 알린다.

