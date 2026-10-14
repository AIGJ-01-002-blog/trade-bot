# devlog 매매 봇 예제 (모의투자)

거래소 공개 시세(바이낸스·업비트 캔들)나 종가 CSV로 예제 전략을 돌려 **모의 체결**하고, 체결마다 devlog **매매 일지**(`/trades`, 이슈 #243·#255)에 기록을 남기는 작은 프로그램이에요.
실제 주문은 내지 않아요. 거래소 API 키도 쓰지 않아요(키 없이 열린 시세 주소만 불러요). 기록이 쌓이면 devlog 화면에서 날마다 메모를 남기고, 요약 임시글과 쇼츠 대본을 만들 수 있어요.

- 필요한 것: Python 3.10 이상 (추가 패키지 없음). 맥·Windows·리눅스 모두 돌아요.
- 예제 전략(`--window`일 이동평균 돌파, `--stop-loss` 손절)은 수익을 내는 전략이 아니라 기록 흐름을 보여 주는 예제예요. `devlog_trade/strategy.py`를 자기 전략으로 바꿔 쓰세요.
- 투자 권유가 아니에요. devlog에 만들어지는 글과 쇼츠에도 같은 고지가 들어가요.

## 코인 모의매매 (바이낸스·업비트)

```bash
cd app/trade-bot
python3 -m devlog_trade coin --exchange binance --dry-run              # BTCUSDT 1시간봉, 1,000 USDT 모의 계좌
python3 -m devlog_trade coin --exchange upbit --dry-run                # KRW-BTC 1시간봉, 100만 원 모의 계좌
python3 -m devlog_trade coin --exchange binance --symbol ETHUSDT --interval 15m --dry-run
```

- **현물** 모의매매예요. 레버리지·공매도·펀딩비는 없어요(바이낸스 무기한 선물이 아니라 현물 BTCUSDT 시세를 써요).
- 시세: 바이낸스 `GET /api/v3/klines`(시세 전용 주소 `data-api.binance.vision`), 업비트 `GET /v1/candles/minutes/60` 같은 공개 캔들. 봉 간격은 `1m 5m 15m 30m 1h 4h 1d`.
- **다 만들어진 봉의 종가로만** 판단하고, 그 봉이 닫힌 시각을 체결 시각으로 남겨요. 아직 진행 중인 봉으로는 거래하지 않아요.
- 처음 실행하면 받은 봉(기본 200개) 전체로 전략을 돌려요. 그 뒤로는 상태 파일에 남은 **마지막 봉 시각** 뒤의 새 봉에서만 체결해서, 같은 봉을 두 번 거래하지 않아요.
- 기본값: 시드 바이낸스 1,000 USDT · 업비트 1,000,000원, 한 번 살 때 100 USDT · 100,000원, 수수료 바이낸스 0.1% · 업비트 0.05%, 매도세 없음. `--cash`, `--budget`, `--fee-rate`로 바꿔요. 수수료는 쓰는 등급·쿠폰에 따라 다르니 확인해 넣어 주세요.
- 수량은 소수 8자리까지 버림으로 맞춰요(예: 0.00153846 BTC). 금액은 원화는 원 단위, USDT는 소수 8자리예요.
- 상태 파일은 거래소·마켓마다 따로예요(기본 `trade-state-binance-BTCUSDT.json`). 다른 거래소 계좌 파일을 주면 멈춰요.
- devlog에는 `exchange`(BINANCE·UPBIT)와 `currency`(USDT·KRW)가 함께 가요. 원화와 USDT는 매매 일지에서 따로 합산돼요.
- 바이비트는 `devlog_trade/exchanges.py`에 같은 모양의 `Exchange`를 하나 더 두면 붙어요.

매시 정각 조금 뒤에 돌리면 막 닫힌 1시간봉을 봐요.

```bash
# macOS / Linux: crontab -e (매시 1분)
1 * * * * cd /path/to/app/trade-bot && DEVLOG_TOKEN='…' python3 -m devlog_trade coin --exchange binance >> trade.log 2>&1
1 * * * * cd /path/to/app/trade-bot && DEVLOG_TOKEN='…' python3 -m devlog_trade coin --exchange upbit >> trade.log 2>&1
```

## 1. 먼저 화면에만 돌려 보기 (종가 CSV)

```bash
cd app/trade-bot
python3 -m devlog_trade run sample-prices.csv --state demo-state.json --dry-run   # Windows는 python
```

`prices.csv` 모양 (날짜 순이 아니어도 돼요):

```csv
date,symbol,name,close
2026-10-14,005930,삼성전자,71000
```

## 2. devlog에 남기기

1. devlog › **AI 연결**에서 **쓰기** 토큰을 만들어요. 토큰은 한 번만 보여요.
2. 토큰은 **환경 변수로만** 넘겨요. 파일이나 저장소에 적지 마세요.

```bash
# macOS / Linux
export DEVLOG_TOKEN='…'
python3 -m devlog_trade run prices.csv --fee-rate 0.00015 --sell-tax-rate 0.002

# Windows PowerShell
$env:DEVLOG_TOKEN = '…'
python -m devlog_trade run prices.csv --fee-rate 0.00015 --sell-tax-rate 0.002
```

- 수수료율·매도 세율은 기본이 0이에요. 쓰는 증권사와 그해 세율을 확인해 넣어 주세요(위 숫자는 모양만 보여 주는 예).
- 상태 파일(`--state`, 기본 `trade-state.json`)에 모의 현금·보유 종목·처리한 마지막 날짜가 남아요. 같은 CSV로 다시 돌려도 그 날짜 뒤의 새 날짜에서만 체결해요.
- devlog에 보내지 못한 체결은 `trade-state.pending.json`에 남았다가 다음 실행 때 다시 보내요. 체결 번호가 같아서 devlog에는 한 번만 남아요.
- 기록은 하루 500건까지예요. 넘으면 devlog가 거절하니 봇을 멈추고 전략을 확인해 주세요.
- 집에서 띄운 devlog(`app/compose.yaml`)에 남기려면 `--server http://localhost:8080`.

## 3. 매일 돌리기

장이 끝난 뒤(예: 평일 16:00) 그날 종가를 CSV에 더하고 실행하면 돼요.

```bash
# macOS / Linux: crontab -e
0 16 * * 1-5 cd /path/to/app/trade-bot && DEVLOG_TOKEN='…' python3 -m devlog_trade run prices.csv >> trade.log 2>&1
```

Windows는 작업 스케줄러에서 같은 명령을 걸고, 토큰은 사용자 환경 변수에 넣어 주세요.

## 4. devlog에서 할 일

`/trades`에서 날을 골라 **오늘 메모**를 남겨요(무엇을 배웠는지, 다음에 바꿀 것). 메모가 있어야 쇼츠 대본을 만들 수 있어요. 숫자만 읽는 영상은 양산형으로 보이기 때문이에요.

- **요약 임시글 만들기**: 체결 표·요약·메모·고지가 들어간 임시글. 읽고 고친 뒤 직접 발행해요.
- **쇼츠 대본 만들기**: 쇼츠 스튜디오로 이어져요. 영상은 렌더 워커(`app/render-worker`)가 만들고, 올리기는 직접 해요.

## 실계좌 주문

이 예제에는 없어요. `run`·`coin` 모두 `--live`를 주면 멈춰요. 실계좌 주문은 되돌릴 수 없으니, 붙이려면 증권사 API 코드를 직접 더하고 직접 켜야 해요.
실계좌 체결도 기록만 남기려면 `journal.Journal.record(fill, mode="LIVE")`로 보내면 돼요. 화면에 "실계좌"로 따로 표시돼요.

## 테스트

```bash
python3 -m unittest discover -s tests -t . -v
```
