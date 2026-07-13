# Point-in-time Catalyst Overlay A/B Backtest

`run_catalyst_overlay_ab.py`는 과거 `fixed_catalyst_score`를 사용하지 않는다. 스캘핑 이벤트는 QuantBot MA Long/Short 후보의 순위만 제한적으로 보정하며, 독립 진입 전략으로 쓰지 않는다.

## 실행

```powershell
cd QuantBotSystem_Beta
python run_catalyst_overlay_ab.py "C:\path\to\scalping_research.xlsx" --top-n 5 --cost-bps 10
```

선택적으로 `ticker,sector[,country]` 열을 가진 로컬 CSV를 전달하면 섹터 익스포저를 구분한다.

```powershell
python run_catalyst_overlay_ab.py "C:\path\to\scalping_research.xlsx" --sector-map "C:\path\to\ticker_sector.csv"
```

## Point-in-time 규칙

- 이벤트 신호는 당일 종가 후 확정되고, 진입은 다음 거래일 시가다.
- 작성자 성과는 20 거래일 보유 종료일이 현재 신호일보다 엄격히 과거일 때만 쓴다.
- 완료 표본 10건 미만 작성자는 0점이며, 20건 전체 평균 shrinkage, 중앙값 0 정렬, `[-10, 10]` clip을 적용한다.
- `estimate_up`이면서 7% 급등·거래대금 Top 10이 아닌 종목만 tier 1/2가 될 수 있다. 52주 고점·저점은 진단값일 뿐 점수에는 쓰지 않는다.

## 결과 해석

JSON의 `segments.train`, `validation`, `test`에서 `A_baseline`을 기준으로 B(Long tilt), C(Short penalty), D(둘 다)의 `incremental_vs_baseline`을 비교한다. `cagr`, `sharpe`의 양의 증분과 `mdd`, `turnover`의 악화 여부를 함께 확인한다. tier별 수익률·국가/섹터 기여도는 과적합 진단용이며, validation과 test에서 동시에 유지되는 조합만 채택한다.
