# devlog 매매 봇 (모의투자)

거래소 공개 시세(바이낸스·업비트 캔들)나 종가 CSV로 예제 전략을 돌려 **모의 체결**하고, 체결마다 devlog **매매 일지**(`/trades`)에 기록을 남기는 작은 프로그램이에요.
기본은 모의매매라 실제 주문을 내지 않고 거래소 API 키도 쓰지 않아요(키 없이 열린 시세 주소만 불러요). 실계좌 주문은 `--live`로 직접 켜야 나가요([LIVE.md](LIVE.md)). 기록이 쌓이면 devlog 화면에서 날마다 메모를 남기고, 요약 임시글과 쇼츠 대본을 만들 수 있어요.

- 이 저장소는 [devlog](https://github.com/AIGJ-01-002-blog/devlog)의 `app/trade-bot`을 커밋 기록째 옮겨 온 거예요. 커밋·CHANGELOG에 적힌 #번호는 devlog 저장소의 이슈·PR이에요.
- 일하는 방식은 devlog와 같은 목표 단위 Git Flow예요: `develop`에서 딴 `feature/*`로 작업해 `develop`에 PR, 버전을 낼 때 `develop`을 `main`에 합치고 `v버전` 태그를 달아요.
- 버전: 봇은 devlog와 따로 버전을 매겨요. 지금 0.1.0이고, 바뀐 점과 필요한 devlog 버전은 [CHANGELOG.md](CHANGELOG.md)에 있어요(`python3 -m devlog_trade --version`).
- 필요한 것: Python 3.10 이상 (추가 패키지 없음). 맥·Windows·리눅스 모두 돌아요. 맥 기본 `python3`는 3.9라 `brew install python`이나 python.org 설치본이 필요해요(`macos/install.sh`가 알아서 찾아요).
- 예제 전략(`--window`일 이동평균 돌파, `--stop-loss` 손절)은 수익을 내는 전략이 아니라 기록 흐름을 보여 주는 예제예요. `devlog_trade/strategy.py`를 자기 전략으로 바꿔 쓰세요.
- 투자 권유가 아니에요. devlog에 만들어지는 글과 쇼츠에도 같은 고지가 들어가요.

## 비밀값 다루기

이 저장소는 공개될 수 있어요. 토큰·키는 저장소에 절대 넣지 않아요.

- devlog 쓰기 토큰, Gemini 키, 거래소 API 키는 **기기의 비밀 저장소**(맥 키체인, Docker `.env`, Windows 사용자 환경 변수)에만 둬요. 파일로 커밋하지 않아요(`.env`·`*.key`·`*.pem`·로그는 `.gitignore`에 있어요).
- 실계좌용 거래소 API 키는 **출금 권한 없이** 만들어요. 주문(거래) 권한만 켜고, 가능하면 IP 제한도 걸어요.
- 커밋마다 CI가 [gitleaks](https://github.com/gitleaks/gitleaks)로 전체 기록에서 비밀값을 찾아요. 걸리면 CI가 실패해요.

## 코인 모의매매 (바이낸스·업비트)

```bash
cd trade-bot
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
| 레버리지 `--leverage` | 선물만, 1~5배 (실계좌와 같은 상한) | 1배 |

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
cd trade-bot
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

### 상위 모델 고르기

`--ai-provider`(환경 변수 `AI_PROVIDER`)와 `--ai-model`(`AI_MODEL`)로 판단 모델을 바꿔요. 기본은 그대로 Gemini `gemini-2.5-flash-lite`예요.

| 제공자 | 기본 모델 | 키 (환경 변수 / 맥 키체인 서비스 이름) | 100만 토큰당 입력·출력 |
|---|---|---|---|
| `gemini` | `gemini-2.5-flash-lite` | `GEMINI_API_KEY` / `devlog-trade-bot-gemini` | 0.10·0.40달러 |
| `anthropic` | `claude-opus-5-5` (`claude-sonnet-5-5`, `claude-haiku-5-5`) | `ANTHROPIC_API_KEY` / `devlog-trade-bot-anthropic` | Opus 4·20, Sonnet 2·10, Haiku 0.10·0.50달러 |
| `openai` | `gpt-5` (`gpt-5-mini`, `gpt-5-nano`) | `OPENAI_API_KEY` / `devlog-trade-bot-openai` | 1.25·10, mini 0.25·2, nano 0.05·0.40달러 |
| `ollama` | `qwen2.5:7b` | 없음 (맥북에서 무료) | 0 |

- **ChatGPT Plus·Claude 구독으로는 API를 쓸 수 없어요.** 각 회사 개발자 콘솔에서 종량제 API 키를 따로 만들어야 해요.
- Claude는 생각을 짧게(`effort=low`), GPT-5는 `reasoning_effort=low`로 물어요. 생각 토큰도 출력으로 과금돼서 한 번에 1,500~3,000토큰 안팎이 들어요. 답이 늦을 수 있어 제한 시간은 90초예요.
- 표에 없는 모델(예: 새로 나온 모델)은 가격을 넣어 주세요: `--ai-model gpt-5.5 --ai-price-in 5 --ai-price-out 30`. 넣지 않으면 그 회사의 비싼 값으로 어림해서 예산을 일찍 막아요.
- 월 예산(기본 30,000원)을 넘으면 규칙 매매로 내려가니, 비싼 모델은 4시간봉처럼 호출이 적은 봉에 쓰는 게 좋아요. 코인 5개 기준 한 계좌 4시간봉은 하루 30번, 1시간봉은 120번이에요.

### 모델끼리 모의매매로 비교하기

같은 캔들을 모델마다 다른 상태 파일로 돌리면 같은 조건에서 비교돼요. 매매 일지의 전략 이름(`ma-cross+ai:openai:gpt-5` 등)으로 모델이 나뉘어요.

```text
# jobs.conf — 4시간봉 바이낸스 현물, 모델마다 모의 계좌 하나씩
--exchange binance --market spot --interval 4h --state trade-state-cmp-rule.json --dry-run
--exchange binance --market spot --interval 4h --state trade-state-cmp-gemini.json --ai on --dry-run
--exchange binance --market spot --interval 4h --state trade-state-cmp-gpt5.json --ai on --ai-provider openai --dry-run
--exchange binance --market spot --interval 4h --state trade-state-cmp-opus.json --ai on --ai-provider anthropic --dry-run
```

규칙만 쓰는 계좌를 기준으로 2주 돌린 뒤 실현 손익, 승률, 최대 낙폭, 수수료, AI 비용을 함께 봐요. 위 네 줄을 2주 돌리면 AI 비용은 2만 5천 원 안팎으로 어림돼요(대부분 Opus). 모든 계좌가 같은 `trade-ai-usage.json`을 써서 월 예산은 합쳐서 지켜지고, 첫날 이 파일로 실제 비용을 확인해요. 비교가 끝나면 고른 모델 줄만 남겨요.

## 대화로 봇 다루기 (MCP)

Claude Desktop·Claude Code·Codex CLI에 봇을 MCP 서버로 붙이면 대화로 시세와 계좌를 보고 주문할 수 있어요. 구독(Claude Pro, ChatGPT Plus) 범위 안에서 돌아요. 다만 대화는 사람이 말을 걸 때만 움직여서, 봉마다 혼자 판단하는 자동 매매는 그대로 `jobs.conf`와 위의 AI 판단이 맡아요.

| 도구 | 하는 일 |
|---|---|
| `trade_bot_prices` | 최근 종가, 5·20봉 이동평균, 등락률, 고가·저가 |
| `trade_bot_accounts` | 계좌마다 현금, 보유 종목·포지션, 지금 가격 기준 평가 손익, AI 켜짐 여부와 이번 달 AI 비용 |
| `trade_bot_order` | 시장가 주문. 현물 BUY·SELL, 선물 LONG·SHORT·CLOSE. **기본은 모의 계좌만** |
| `trade_bot_ai` | AI 판단 켜기·끄기(`trade-control.json`). `jobs.conf`의 `--ai`보다 먼저 적용돼요 |

- 도구 이름은 `trade_bot_`으로 시작해요. 매매 일지 조회·기록은 devlog MCP(`record_trade` 등)가 맡고, 이 서버는 봇 계좌만 다뤄요. 대화로 낸 체결은 봇과 같은 길로 기록되고 전략 이름이 `mcp-manual`, 이유가 `[대화 주문] …`이에요.
- 서버는 기본으로 체결을 devlog에 보내지 않아요. 보내려면 `--record`를 주고 서버 환경 변수에 `DEVLOG_TOKEN`을 넣어요(설정 파일에 토큰을 적지 말고 키체인에서 읽어 넘겨요).
- 실계좌 주문은 서버를 `--live --max-order … --max-order-loss … --max-daily-loss …`로 띄우고(실거래소는 `DEVLOG_TRADE_LIVE=1`과 `--record`도) 도구에서 `live: true`를 줄 때만 나가요. 봇과 같은 주문당·하루 손실 상한을 거쳐요. 대화하는 AI는 서버 설정을 바꿀 수 없어요.
- 봇 작업과 대화 주문이 같은 상태 파일을 동시에 고치지 않게 잠가요(맥·리눅스).

맥에서 붙이기 (`PY`는 `macos/find-python.sh`가 찾은 3.10 이상 Python):

```bash
cd ~/trade-bot && PY="$(./macos/find-python.sh)"
# Claude Code
claude mcp add trade-bot -e PYTHONPATH="$PWD" -- "$PY" -m devlog_trade.mcp --dir "$PWD"
```

Claude Desktop은 `~/Library/Application Support/Claude/claude_desktop_config.json`에:

```json
{"mcpServers": {"trade-bot": {"command": "/opt/homebrew/bin/python3",
  "args": ["-m", "devlog_trade.mcp", "--dir", "/Users/<나>/trade-bot"],
  "env": {"PYTHONPATH": "/Users/<나>/trade-bot"}}}}
```

```toml
# Codex CLI: ~/.codex/config.toml
[mcp_servers.trade-bot]
command = "/opt/homebrew/bin/python3"
args = ["-m", "devlog_trade.mcp", "--dir", "/Users/<나>/trade-bot"]
env = { PYTHONPATH = "/Users/<나>/trade-bot" }
```

ChatGPT 앱은 인터넷에 열린 원격 MCP 서버만 붙일 수 있어서, 봇을 밖에 열지 않는 지금 구조에서는 쓰지 않아요.

## 1. 먼저 화면에만 돌려 보기 (종가 CSV)

```bash
cd trade-bot
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
- 집에서 띄운 devlog(devlog 저장소의 `app/compose.yaml`)에 남기려면 `--server http://localhost:8080`.

## 3. 매일 돌리기

장이 끝난 뒤(예: 평일 16:00) 그날 종가를 CSV에 더하고 실행하면 돼요.

```bash
# macOS / Linux: crontab -e
0 16 * * 1-5 cd /path/to/trade-bot && DEVLOG_TOKEN='…' python3 -m devlog_trade run prices.csv >> trade.log 2>&1
```

Windows는 작업 스케줄러에서 같은 명령을 걸고, 토큰은 사용자 환경 변수에 넣어 주세요.

## 4. devlog에서 할 일

`/trades`에서 날을 골라 **오늘 메모**를 남겨요(무엇을 배웠는지, 다음에 바꿀 것). 메모가 있어야 쇼츠 대본을 만들 수 있어요. 숫자만 읽는 영상은 양산형으로 보이기 때문이에요.

- **요약 임시글 만들기**: 체결 표·요약·메모·고지가 들어간 임시글. 읽고 고친 뒤 직접 발행해요.
- **쇼츠 대본 만들기**: 쇼츠 스튜디오로 이어져요. 영상은 렌더 워커(devlog 저장소의 `app/render-worker`)가 만들고, 올리기는 직접 해요.

## 실계좌 주문

기본은 꺼져 있어요. `coin` 명령에 `--live`와 세 가지 손실 상한(`--max-order`·`--max-order-loss`·`--max-daily-loss`)을 주고, 실거래소는 환경 변수 `DEVLOG_TRADE_LIVE=1`까지 있어야 주문이 나가요. 바이낸스 현물·선물(1~5배), 업비트 현물을 받고, 키는 출금 권한 없이 만들어 기기 비밀 저장소에만 둬요. 자세한 건 [LIVE.md](LIVE.md)에 있어요.
종가 CSV 모드(`run`)는 모의 체결만 해요.

## 테스트

```bash
python3 -m unittest discover -s tests -t . -v
```
