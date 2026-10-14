# devlog 매매 봇 예제 (모의투자)

거래소 공개 시세(바이낸스·업비트 캔들)나 종가 CSV로 예제 전략을 돌려 **모의 체결**하고, 체결마다 devlog **매매 일지**(`/trades`, 이슈 #243·#255)에 기록을 남기는 작은 프로그램이에요.
실제 주문은 내지 않아요. 거래소 API 키도 쓰지 않아요(키 없이 열린 시세 주소만 불러요). 기록이 쌓이면 devlog 화면에서 날마다 메모를 남기고, 요약 임시글과 쇼츠 대본을 만들 수 있어요.

- 버전: 봇은 devlog와 따로 버전을 매겨요. 지금 0.1.0이고, 바뀐 점과 필요한 devlog 버전은 [CHANGELOG.md](CHANGELOG.md)에 있어요(`python3 -m devlog_trade --version`).
- 필요한 것: Python 3.10 이상 (추가 패키지 없음). 맥·Windows·리눅스 모두 돌아요.
- 예제 전략(`--window`일 이동평균 돌파, `--stop-loss` 손절)은 수익을 내는 전략이 아니라 기록 흐름을 보여 주는 예제예요. `devlog_trade/strategy.py`를 자기 전략으로 바꿔 쓰세요.
- 투자 권유가 아니에요. devlog에 만들어지는 글과 쇼츠에도 같은 고지가 들어가요.

## 코인 모의매매 (바이낸스·업비트)

```bash
cd app/trade-bot
python3 -m devlog_trade coin --exchange binance --dry-run                                   # 현물, 코인 5종, 1시간봉
python3 -m devlog_trade coin --exchange upbit --interval 15m --dry-run                      # 업비트 원화 마켓 15분봉
python3 -m devlog_trade coin --exchange binance --market futures --leverage 3 --dry-run     # USDT-M 선물, 3배
python3 -m devlog_trade coin --exchange binance --coins BTC,ETH --interval 4h --dry-run      # 코인 골라서
```

| 고를 것 | 값 | 기본 |
|---|---|---|
| 거래소·시장 | 바이낸스 현물·USDT-M 선물, 업비트 현물(업비트는 선물이 없어요) | 바이낸스 현물 |
| 코인 `--coins` | BTC, ETH, XRP, SOL, DOGE | 5종 모두 |
| 봉 간격 `--interval` | 1m, 5m, 15m, 30m, 1h, 4h, 1d | 1h |
| 시드 `--cash` | 바이낸스 1,000 USDT · 업비트 1,000,000원 (계좌 하나에 코인 5종이 같이 들어요) | |
| 한 번 들어갈 금액 `--budget` | 바이낸스 100 USDT · 업비트 100,000원 (선물은 증거금) | |
| 수수료 `--fee-rate` | 바이낸스 현물 0.1% · 선물 0.05% · 업비트 0.05%, 매도세 없음 | |
| 레버리지 `--leverage` | 선물만, 1~3배 | 1배 |

- 시세는 API 키 없이 공개 캔들만 받아요. 바이낸스 현물 `data-api.binance.vision/api/v3/klines`, 선물 `fapi.binance.com/fapi/v1/klines`, 업비트 `api.upbit.com/v1/candles/…`. 선물 시세 주소는 나라에 따라 막힐 수 있어요.
- **다 만들어진 봉의 종가로만** 판단하고, 그 봉이 닫힌 시각을 체결 시각으로 남겨요. 진행 중인 봉으로는 거래하지 않아요.
- 처음 실행하면 받은 봉(종목마다 기본 200개) 전체로 전략을 돌려요. 그 뒤로는 상태 파일에 종목마다 남은 **마지막 봉 시각** 뒤의 새 봉에서만 체결해서 같은 봉을 두 번 거래하지 않아요. `--dry-run`도 상태 파일을 앞으로 옮겨요. 처음부터 다시 하려면 상태 파일을 지우세요.
- 상태 파일은 거래소·시장·봉 간격마다 따로예요(기본 `trade-state-binance-spot-1h.json`). 다른 계좌 파일을 주면 멈춰요.
- 수량은 소수 8자리에서 버려요. 금액은 원화는 원 단위, USDT는 소수 8자리예요.
- devlog에는 `exchange`(BINANCE·UPBIT)와 `currency`(USDT·KRW)가 함께 가요. 원화와 USDT는 매매 일지에서 따로 합산돼요.
- 1분·5분봉에 코인 5종을 돌리면 체결이 많아져요. devlog 매매 일지는 하루 500건까지라, 짧은 봉은 코인을 줄이거나 `--window`를 키워 주세요.

### 선물 (바이낸스 USDT-M 무기한)

- 종가가 이동평균선을 위로 뚫으면 **롱**, 아래로 뚫으면 **숏**으로 들어가요. 반대 신호가 오면 먼저 정리하고 반대로 들어가요. 손절선(`--stop-loss`, 기본 3%)에 닿으면 정리해요.
- 증거금 = 명목 금액 ÷ 레버리지, 종목마다 따로(격리)예요. 봉 종가에서 손실이 증거금을 다 쓰면 강제 청산으로 남겨요. 유지 증거금과 **펀딩비는 아직 계산하지 않아요**.
- 매매 일지에는 롱 진입 BUY·정리 SELL, 숏 진입 SELL·정리 BUY로 남고, 종목 이름이 "비트코인 선물", 이유 앞에 `[선물 숏 진입 3x]`처럼 붙어요.

### 정해 둔 작업을 한 번에 (jobs.conf)

`jobs.conf`에 줄마다 `coin` 옵션을 적고 `python3 -m devlog_trade jobs jobs.conf`로 차례로 돌려요. `--every 300`을 붙이면 5분마다 계속 돌아요. 맥 launchd와 Docker가 이 파일을 써요. 처음 받은 파일은 모두 `--dry-run`이라, 화면을 보고 괜찮으면 지워 주세요.

### 맥북 덮개를 닫고 돌리기

1. 토큰을 키체인에 넣어요(파일에 적지 않아요).
   ```bash
   security add-generic-password -a "$USER" -s devlog-trade-bot -w   # 물으면 devlog 쓰기 토큰을 붙여 넣어요
   ```
2. launchd에 등록해요. 5분마다 `jobs.conf`를 돌리고, 도는 동안은 `caffeinate`가 잠들지 않게 잡아 줘요. 1분봉을 쓰면 `60`을 주세요.
   ```bash
   ./macos/install.sh 300          # 내리기: ./macos/install.sh --uninstall
   tail -f ~/Library/Logs/devlog-trade-bot.log
   ```
3. 덮개를 닫아도 깨어 있게 해요. 둘 중 하나예요.
   - **외장 모니터가 있으면**: 전원 어댑터와 외장 모니터·키보드를 연결한 채 덮개를 닫으면 맥이 클램셸 모드로 계속 돌아요(애플이 지원하는 방법).
   - **모니터 없이**: 전원을 꽂고 `sudo pmset -a disablesleep 1`을 한 번 실행하면 덮개를 닫아도 잠들지 않아요. 가방에 넣으면 뜨거워지니, 쓰지 않을 땐 `sudo pmset -a disablesleep 0`으로 되돌려 주세요.
- 맥이 잠들었다 깨면 launchd가 놓친 실행을 바로 한 번 돌리고, 봇은 그 사이 닫힌 봉을 차례로 따라잡아요.

### 집 PC로 옮길 때 (Docker)

```bash
cd app/trade-bot
echo "DEVLOG_TOKEN=…" > .env       # git에 올라가지 않아요
docker compose up -d --build       # 5분마다 jobs.conf를 돌려요. 상태 파일은 ./data에 남아요
docker compose logs -f
```

맥에서 쓰던 `trade-state-*.json`을 `./data`로 옮기면 이어서 돌아요. Docker 없이 Windows에서 돌리려면 작업 스케줄러에 `python -m devlog_trade jobs jobs.conf`를 5분마다 걸어도 돼요.

## AI 매매 판단 켜고 끄기 (#263)

코인 모드에서 매매 판단을 AI에게 맡길 수 있어요. **기본은 꺼져 있어요.**

```bash
export GEMINI_API_KEY='…'          # Google AI Studio에서 만든 키. 파일·저장소에 적지 마세요
AI_DECISION=on python3 -m devlog_trade coin --exchange binance --interval 1h --dry-run
python3 -m devlog_trade coin --exchange binance --market futures --interval 4h --ai on --dry-run   # 플래그가 환경 변수보다 먼저
```

- 15분봉 이상(`15m 30m 1h 4h 1d`)에서 봉이 닫힐 때만 AI에게 최근 종가 60개·포지션·평균 단가(진입가)·쓸 수 있는 현금을 주고 행동과 이유 한 줄을 받아요. 현물은 BUY·SELL·HOLD, 선물은 LONG·SHORT·CLOSE·HOLD예요. 1분·5분봉은 켜 두어도 규칙(이동평균·손절)으로 매매해요.
- 손절선과 강제 청산은 AI에게 묻지 않고 규칙대로 정리해요.
- AI 답이 형식에 맞지 않거나 늦으면(Gemini 20초) **관망**해요. 규칙으로 대신 사거나 팔지 않아요.
- 하루 호출 상한(`--ai-daily-calls`, 기본 2,000회)이나 월 예산(`--ai-monthly-krw`, 기본 30,000원)을 넘으면 그날(그달)은 규칙 매매로 내려가요. 사용량은 지금 폴더의 `trade-ai-usage.json`에 모이고, 모든 코인·거래소·봉이 같은 파일을 써서 상한이 합쳐져요(`--ai-usage`로 바꿔요).
- 매매 일지에는 이유가 `AI: …`로, 전략 이름이 `ma-cross+ai:gemini:gemini-2.5-flash-lite`처럼 남아요.
- 기본 AI는 Gemini `gemini-2.5-flash-lite`(devlog 서버가 쓰는 모델과 같아요). 비용은 100만 토큰당 입력 0.10달러·출력 0.40달러, 1달러 1,400원으로 어림해요. 한 번 묻는 데 1원이 안 들어요(입력 1,500토큰 안팎).
- 맥북에서 Ollama를 띄웠으면 돈 없이 돌릴 수 있어요: `AI_PROVIDER=ollama AI_MODEL=qwen2.5:7b` (주소는 `OLLAMA_BASE_URL`, 기본 `http://localhost:11434`). 모델은 `AI_MODEL`이나 `--ai-model`로 바꿔요.
- `jobs.conf`에서는 줄마다 `--ai on`을 붙여 켜요. 맥 launchd는 Gemini 키를 키체인에서 읽어요: `security add-generic-password -a "$USER" -s devlog-trade-bot-gemini -w`. Docker는 `.env`에 `GEMINI_API_KEY=…`.
- 켰는데 키가 없으면 매매하지 않고 멈춰요(종료 코드 2).

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
