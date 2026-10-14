# research — 세 겹 거름망 복리법 검증

봇(`devlog_trade`)과 따로 두는 **검증 전용** 폴더예요. 봇은 이 폴더를 읽지 않고, Docker 이미지에도 들어가지 않아요.
설계는 프로젝트 노트 `trade-bot-compound-method-2026-10-15.md`, 도구 선택은 `trade-bot-harness-research-2026-10-14.md`를 따라요.

| 파일 | 하는 일 | 라이선스 |
|---|---|---|
| `fetch_data.py` | 2021-01 ~ 2026-09 1시간봉과 선물 펀딩비, 공포·탐욕 지수 받기 (바이낸스는 data.binance.vision, 업비트는 공개 API) | 봇과 같음 |
| `ladder.py` | 기법 규칙과 계좌 장부(엔진). 지금 봇 규칙(비교 기준)도 같은 장부로 | 봇과 같음 |
| `run_backtest.py` | 앞 구간에서 조합 고르기 → 뒤 구간 확인 → 이웃 조합·몬테카를로 → 보고서. 현물은 vectorbt로 장부 대조 | 봇과 같음 |
| `freqtrade/` | 같은 기법을 Freqtrade 전략으로 옮긴 것(바이낸스 현물·선물) | **GPL-3.0** (`freqtrade/LICENSE`) |

## GPL을 봇에서 떼어 놓는 방법
- Freqtrade는 GPL-3.0이라 그 위에서 도는 전략 파일은 `research/freqtrade/` 안에만 두고 그 폴더에 GPL-3.0을 붙였어요.
- `devlog_trade`와 `research/ladder.py`는 Freqtrade를 import하지 않고, Freqtrade 쪽도 그 둘을 import하지 않아요. 규칙은 두 곳에 따로 적고, 결과를 나란히 놓고 맞춰 봐요.

## 돌리기

GitHub에서는 `research/`가 바뀐 PR이나 Actions의 **backtest** 워크플로를 손으로 돌리면 돌아요. 결과는 워크플로 요약과 아티팩트에 남아요.

로컬에서는:
```bash
python -m venv .venv && . .venv/bin/activate
pip install -r research/requirements.txt
python research/fetch_data.py --out research/data --start 2020-08 --end 2026-09   # 처음 한 번, 몇 분
python research/run_backtest.py --data research/data --out research/results/engine

pip install -r research/freqtrade/requirements.txt   # Freqtrade 대조(선택)
python research/freqtrade/run_offline.py --data research/data --out research/results/freqtrade
```

`run_offline.py`는 거래소 API 없이 돌도록 시장 정보를 가짜로 넣어요(바이낸스 API는 미국 GitHub 러너에서 451로 막혀서). 한국 맥에서
진짜 시장 정보로 확인하려면 Docker로:
```bash
cd research/freqtrade
docker run --rm -v "$PWD:/freqtrade/user_data_root" -w /freqtrade/user_data_root freqtradeorg/freqtrade:2026.9 \
  download-data -c config-futures.json --userdir user_data --timeframes 1h 4h 1d --timerange 20200801-20261001
docker run --rm -v "$PWD:/freqtrade/user_data_root" -w /freqtrade/user_data_root freqtradeorg/freqtrade:2026.9 \
  backtesting -c config-futures.json --userdir user_data --strategy LadderStrategy --timerange 20240101-20261001
```

## 정한 것과 뺀 것
- 앞 구간 2021-01 ~ 2023-12에서 `atr_mult`(1.2·1.5·2.0) × `breakout_bars`(10·20) × `regime_sma`(50·100) 12개 조합 중 수익 ÷ 최대 낙폭이 가장 좋은 것을 고르고(거래 30건 미만은 제외), 뒤 구간 2024-01 ~ 2026-09에서 확인해요.
- 통과 기준(설계 8장): 뒤 구간 수익 플러스, 최대 낙폭 15% 이하, 청산 0건, 최악의 하루 -3% 이내, 거래 30건 이상.
- 백테스트에서 뺀 것: 뉴스 거부권(과거 자료 없음), AI 확신도(1.0으로 고정), 코어 바구니 적립(트레이딩 바구니만 평가). 모의 4주에서 봐요.
- 그림자 거래: 켈리 값이 0 이하이면 돈을 넣지 않지만, 그 신호도 같은 규칙으로 따라가 R만 기록해요. 이게 없으면 거래 30건째에
  켈리가 0 이하로 나온 순간 기록이 멈춰 다시는 거래하지 않아요(첫 실행에서 세 계좌 모두 뒤 구간 거래가 딱 30건에서 멈춘 원인).
  봇에 넣을 때도 같은 방식으로 해요(이슈 #13). Freqtrade 전략은 그림자 거래를 따로 두지 않아서 이 부분은 엔진과 달라요.
- 해석: 진입은 신호 봉 종가 + 미끄러짐 0.05%, 한 봉에서 손절과 익절이 같이 닿으면 손절 먼저, 1시간 EMA20 추적 정리는 1R 익절 뒤부터.
