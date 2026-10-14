# 실계좌 주문 (#264)

`devlog_trade/live.py`는 모의 계좌(`PaperBroker`)와 같은 모양으로 **진짜 거래소에 시장가 주문**을 내는 `LiveBroker`예요.
바이낸스 현물(`binance`), 바이낸스 USDT-M 선물(`binance-futures`), 업비트 원화 마켓 현물(`upbit`)을 받아요. 바이비트는 같은 `Trader` 틀로 붙여요.

실계좌 주문은 되돌릴 수 없어요. 그래서 **기본은 모의매매**이고, 아래 조건이 모두 맞아야 주문이 나가요.

## 켜는 조건과 안전장치

| 장치 | 내용 |
| --- | --- |
| 기본 꺼짐 | `--live`(확인 플래그)가 없으면 모의매매예요. |
| 실거래소 이중 확인 | 메인넷은 환경 변수 `DEVLOG_TRADE_LIVE=1`(설정)과 `--live`가 **둘 다** 있어야 해요. 테스트넷은 `--live --testnet`만으로 돼요. |
| 출금 권한 거절 | 주문 전에 키 권한을 봐요. 바이낸스는 `GET /sapi/v1/account/apiRestrictions`의 `enableWithdrawals`가 `false`여야 하고, 업비트는 출금 조회(`/v1/withdraws/chance`)가 막힌 키여야 해요. |
| 주문당 금액 상한 | `--max-order`: 한 번 살 때 쓸 최대 금액. 넘으면 이 금액으로 줄여서 사요. |
| 주문당 손실 상한 | `--max-order-loss`: 한 번 청산에서 이만큼 잃으면 **새 매수를 멈춰요**. `python -m devlog_trade.live resume <상태 파일>`로 직접 풀어요. |
| 하루 손실 상한 | `--max-daily-loss`: 한국 시간 하루 동안 잃은 금액(실현 손익 + 매수 수수료)이 이만큼이면 그날 새 매수를 멈추고, 다음 날 풀려요. |
| 레버리지 상한 | 선물은 `--leverage` 1~7배(정수)만 받아요(민서님 결정, 2026-10-14). 8배 이상은 거래소에 묻기 전에 거절해요. |
| 격리 마진 | 선물은 종목마다 첫 주문 전에 격리 마진(ISOLATED)으로 맞춰요. 강제 청산돼도 그 포지션 증거금까지만 잃어요. 바꾸지 못하면(열린 주문이 있을 때 등) 진입하지 않아요. |
| 키는 환경 변수로만 | 파일·저장소·로그에 쓰지 않아요. 테스트넷 키는 이름이 따로라 실계좌 키와 섞이지 않아요. |

- 세 상한은 기본값이 없어요. 실계좌는 상한을 직접 정해야 돌아요.
- 청산(매도)은 위험을 줄이는 쪽이라 멈춘 동안에도 나가요.
- 같은 봉을 다시 처리해도 주문이 두 번 나가지 않아요. 주문 번호(바이낸스 `newClientOrderId`, 업비트 `identifier`)를 종목·방향·봉 시각으로 정하고, 주문 전에 그 번호로 낸 주문이 있는지 먼저 찾아요. 주문 직후 연결이 끊겨도 번호로 찾아서 결과를 이어 받아요.
- 기록은 실제 체결가·체결 수량·수수료로 남겨요. 체결 번호는 `live-<거래소>-<주문번호>`예요.

## 처음 상한 추천값 (시드 20만~50만 원)

민서님이 처음 실계좌를 20만~50만 원으로 시작할 계획이라(2026-10-14) 그 금액에 맞춘 추천값이에요. 계좌(상태 파일)마다 따로 적용돼요.

| 상한 | 기준 | 업비트 현물 (원) | 바이낸스 (USDT) |
| --- | --- | --- | --- |
| `--max-order` 한 번 진입 금액 | 시드의 10% | 20,000 ~ 50,000 | 현물 15 ~ 35 · 선물 명목 100 |
| `--max-order-loss` 한 번 청산 손실 | 시드의 2% | 4,000 ~ 10,000 | 3 ~ 7 |
| `--max-daily-loss` 하루 손실 | 시드의 5% | 10,000 ~ 25,000 | 7 ~ 18 |

- USDT는 1 USDT ≈ 1,400원으로 어림했어요. 바이낸스에 넣은 금액을 USDT로 보고 같은 비율로 잡으면 돼요.
- 10%·2%·5%로 잡은 이유: 한 번에 시드의 10%만 들어가면 코인 5종을 동시에 들어도 절반은 현금으로 남아요. 전략 손절(-3%)로 끝나면 한 번 손실은 시드의 0.3% 정도라, 청산 손실 상한(2%)은 한 종목이 20% 넘게 빠지는 사고 때만 걸려서 직접 `resume`할 때까지 멈춰요. 하루 손실 상한(5%)은 평범한 손절이 여러 번 쌓인 나쁜 날을 그날로 끊어요. 시드를 다 잃으려면 20일 넘게 매일 상한까지 잃어야 해요.
- 업비트 최소 주문은 5,000원이라 2만 원부터면 충분히 주문이 나가요.
- **선물과 7배:** `--max-order`는 선물에서 **명목 금액**(증거금 × 레버리지) 기준이라, 레버리지를 올려도 들어가는 크기는 늘지 않고 묶이는 증거금만 줄어요. 예: 명목 100 USDT에 7배면 증거금 약 14 USDT, 3배면 약 33 USDT예요. 바이낸스 BTC 선물은 최소 주문 명목이 100 USDT 안팎이라(종목마다 달라요, 거래소 화면에서 확인) 선물만 100으로 잡았어요.
- 격리 마진이라 강제 청산돼도 그 증거금까지만 잃어요. 7배 명목 100이면 약 13% 반대로 가서 약 14 USDT를 잃는데, 이건 청산 손실 상한(7)을 넘어서 그 한 번으로 새 진입이 멈춰요. 그래서 **처음에는 2~3배로 시작하고**, 일주일쯤 기록을 보고 올리는 걸 추천해요.

```bash
# 업비트 현물, 시드 30만 원
python3 -m devlog_trade coin --exchange upbit --live --max-order 30000 --max-order-loss 6000 --max-daily-loss 15000
# 바이낸스 선물, 시드 약 200 USDT, 3배로 시작
python3 -m devlog_trade coin --exchange binance --market futures --leverage 3 --live --max-order 100 --max-order-loss 5 --max-daily-loss 12
```

## 키 만들기

| 거래소 | 환경 변수 | 고를 권한 |
| --- | --- | --- |
| 바이낸스 (메인넷) | `BINANCE_API_KEY`, `BINANCE_API_SECRET` | Enable Reading + Spot & Margin Trading(현물) / Futures(선물). **Withdrawals는 끄고**, IP 제한을 걸어 주세요. |
| 바이낸스 현물 테스트넷 | `BINANCE_TESTNET_API_KEY`, `BINANCE_TESTNET_API_SECRET` | testnet.binance.vision에서 만든 키 |
| 바이낸스 선물 테스트넷 | `BINANCE_FUTURES_TESTNET_API_KEY`, `BINANCE_FUTURES_TESTNET_API_SECRET` | testnet.binancefuture.com에서 만든 키 |
| 업비트 | `UPBIT_ACCESS_KEY`, `UPBIT_SECRET_KEY` | 자산 조회 · 주문 조회 · 주문하기만. **출금 조회·출금하기는 고르지 마세요**. 업비트는 테스트넷이 없어요. |

## 키 확인 (주문 없음)

```bash
BINANCE_TESTNET_API_KEY='…' BINANCE_TESTNET_API_SECRET='…' python3 -m devlog_trade.live check --market binance --testnet
UPBIT_ACCESS_KEY='…' UPBIT_SECRET_KEY='…' python3 -m devlog_trade.live check --market upbit
```

출금 권한이 켜진 키면 여기서 멈춰요.

## 현물·선물 브로커와 기록 모양

- 현물 `LiveBroker`는 모의 `PaperBroker`와 같은 `buy`·`sell`, 선물 `LiveFuturesBroker`는 모의 `FuturesBroker`와 같은 `open(symbol, name, side, …)`·`close(symbol, price, reason, at)`를 써요. 봇은 `broker.market`(spot·futures)으로 가르고, `broker.mode`가 `LIVE`예요.
- 선물 방향: 롱 진입 BUY·정리 SELL, 숏 진입 SELL·정리 BUY(정리는 reduceOnly). 단방향 포지션 기준이라 같은 종목에 롱과 숏을 함께 들지 않아요. 레버리지는 종목마다 처음 주문하기 직전에 거래소에 맞춰요.
- 선물 주문 상한(`--max-order`)은 명목 금액 기준이에요. 강제 청산은 거래소가 하고, 거래소에서 사라진 포지션은 다음 정리 때 장부에서만 지워요.
- devlog 기록: 거래소는 `BINANCE`·`UPBIT`, 선물은 이유 앞에 `[선물 숏 진입 3x]`·`[선물 숏 정리]`처럼 모의와 같은 꼬리표를 붙여요. 체결 시각은 봉이 닫힌 시각이 아니라 거래소가 알려 준 실제 체결 시각이에요. 실계좌 체결은 `mode=LIVE`로 보내요.
- 실계좌 상태 파일 기본 이름은 `trade-live-<거래소>-<시장>-<봉>.json`이에요(`live.live_state_name`). 처음 보는 종목은 받은 봉으로 전략만 데우고 다음 봉부터 주문해요.

## 봇으로 돌리기

`coin` 명령에 `--live`를 주면 같은 전략으로 실계좌 주문을 내요. 먼저 테스트넷으로 확인하세요.

```bash
# 테스트넷 (바이낸스만). 키는 환경 변수로만
python3 -m devlog_trade coin --exchange binance --coins BTC --interval 1h \
  --live --testnet --max-order 20 --max-order-loss 5 --max-daily-loss 10

# 실거래소: 설정과 확인 플래그가 둘 다 있어야 해요
DEVLOG_TRADE_LIVE=1 python3 -m devlog_trade coin --exchange binance --market futures --leverage 2 --coins BTC \
  --live --max-order 20 --max-order-loss 5 --max-daily-loss 10
```

- 상태 파일 기본 이름은 `trade-live-<거래소>-<시장>-<봉>.json`이라 모의 계좌(`trade-state-…`)와 섞이지 않아요.
- 처음 도는 종목은 받은 봉으로 전략만 데우고, 그다음 닫히는 봉부터 주문해요. 지난 봉으로 주문하지 않아요.
- devlog 매매 일지에는 `mode=LIVE`로 남아 화면에 "실계좌"로 따로 보여요. 실거래소 `--live`는 `--dry-run`과 함께 쓸 수 없어요. 테스트넷은 `--dry-run`을 붙이면 devlog에 보내지 않고 화면에만 보여요.
- 켜는 조건·상한·키·출금 권한 중 하나라도 맞지 않으면 주문 없이 멈춰요(종료 코드 2).

## 테스트

```bash
python3 -m unittest discover -s tests -t . -v                 # 가짜 거래소 HTTP만 써요. 진짜 주문은 없어요
DEVLOG_TRADE_TESTNET_IT=1 BINANCE_TESTNET_API_KEY='…' BINANCE_TESTNET_API_SECRET='…' \
  python3 -m unittest tests.test_live.BinanceTestnetIT -v       # 바이낸스 현물 테스트넷에 20 USDT 이하로 사고팔아요
```

테스트는 메인넷 주소로 가지 않아요. 테스트넷 확인도 `testnet=True`로 고정돼 있어요.
