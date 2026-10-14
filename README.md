# devlog 매매 봇 예제 (모의투자)

종가 CSV로 예제 전략을 돌려 **모의 체결**하고, 체결마다 devlog **매매 일지**(`/trades`, 이슈 #243)에 기록을 남기는 작은 프로그램이에요.
실제 주문은 내지 않아요. 기록이 쌓이면 devlog 화면에서 날마다 메모를 남기고, 요약 임시글과 쇼츠 대본을 만들 수 있어요.

- 필요한 것: Python 3.10 이상 (추가 패키지 없음). 맥·Windows·리눅스 모두 돌아요.
- 예제 전략(`--window`일 이동평균 돌파, `--stop-loss` 손절)은 수익을 내는 전략이 아니라 기록 흐름을 보여 주는 예제예요. `devlog_trade/strategy.py`를 자기 전략으로 바꿔 쓰세요.
- 투자 권유가 아니에요. devlog에 만들어지는 글과 쇼츠에도 같은 고지가 들어가요.

## 1. 먼저 화면에만 돌려 보기

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

이 예제에는 없어요. `--live`를 주면 멈춰요. 실계좌 주문은 되돌릴 수 없으니, 붙이려면 증권사 API 코드를 직접 더하고 직접 켜야 해요.
실계좌 체결도 기록만 남기려면 `journal.Journal.record(fill, mode="LIVE")`로 보내면 돼요. 화면에 "실계좌"로 따로 표시돼요.

## 테스트

```bash
python3 -m unittest discover -s tests -t . -v
```
