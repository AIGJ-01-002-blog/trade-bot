# 매매 봇 Changelog

매매 봇은 devlog와 **따로 버전을 매겨요**([Semantic Versioning](https://semver.org/lang/ko/)). 버전은 `devlog_trade/__init__.py`의 `__version__` 하나에서 읽어요(`python -m devlog_trade --version`).

- 1.0.0 전(0.x)에는 기능이 늘면 Minor, 고치기만 하면 Patch를 올려요.
- **1.0.0은 실계좌 주문이 바이낸스 테스트넷 확인을 통과했을 때** 내요. 그때부터 실제 돈이 움직일 수 있어서예요.
- 버전마다 이 저장소에 `v버전` 태그를 달아요. #번호는 devlog 저장소의 이슈·PR이에요(봇이 2026-10-14에 devlog `app/trade-bot`에서 옮겨 왔어요).
- devlog 릴리스(목표 단위 Git Flow)와는 묶지 않아요. 봇 버전마다 필요한 devlog 버전을 아래에 적어요.

## [Unreleased]

### 추가
- devlog 자동매매 화면(`devlog.life/trade`, devlog spec 101) 알림: `jobs`가 한 바퀴 뒤 `report_trade_bot`으로 모의 계좌·적용 중인 규칙·새 모의 체결·AI 진입 판단(허락·막음)·지난 날 하루 요약을 보낸다. 실계좌는 빼고, 실패해도 매매와 종료 코드는 그대로이며 사건은 `trade-bot-report.json`에 두었다가 다시 보낸다. `--no-report`·`TRADE_REPORT=off`로 끈다. devlog v2.2.0(PR #297) 이후 운영 서버에서 받는다.
- 대화 주문(MCP `trade_bot_order`)의 체결도 같은 길로 자동매매 화면에 간다.
- MCP `trade_bot_rules`(규칙형): 대화하는 AI가 모의 계좌마다 코인·이동평균 봉 수·손절·한 번 금액·멈춤을 정하면 봇이 다음 바퀴부터 `jobs.conf` 대신 그 값으로 돈다. 상한(코인 10개, 2~60봉, 손절 0.5~20%, 금액은 처음 현금의 절반)은 코드가 지키고, 실계좌는 받지 않는다. 자동매매 화면 규칙에 "기준을 정한 곳"과 이유가 붙는다.

### 고침
- MCP `trade_bot_accounts`가 테스트넷 계좌를 실계좌(LIVE)로 보이던 것: 실계좌 상태 파일에 `testnet`을 남기고 TESTNET으로 보인다. 테스트넷 장부를 실거래소에서(또는 반대로) 다시 쓰려 하면 멈춘다.
- 세 겹 거름망의 보조 장부(`*.ladder.json`)가 계좌 목록에 "읽지 못했어요"로 끼던 것.

## [1.0.0] - 2026-10-15

바이낸스 현물 테스트넷에서 실계좌 주문을 확인했어요(민서님 M1 맥, 2026-10-15). 1분봉 BTC로 매수·매도·매수 3건이 테스트넷에서 체결됐고, 봇 장부의 현금 9,980.57812 USDT가 거래소 잔고와 같았어요. 주문당 20 USDT 상한도 걸렸어요. 메인넷 주문은 여전히 `DEVLOG_TRADE_LIVE=1`과 `--live`가 함께 있어야 하고, 지금 `jobs.conf`는 모두 모의(`--dry-run`)예요.

### 추가
- 실계좌 주문 모듈 `devlog_trade/live.py` (#264): 바이낸스 현물·USDT-M 선물(롱·숏), 업비트 원화 현물에 시장가 주문. 모의 `PaperBroker`·`FuturesBroker`와 같은 모양의 `LiveBroker`·`LiveFuturesBroker`이고, 체결은 실제 체결가·수량·수수료·체결 시각으로 `mode=LIVE` 기록. `coin --live`로 켜고, 처음 보는 종목은 지난 봉으로 주문하지 않아요. 실거래소 `--live`는 `--dry-run`과 함께 못 쓰고, 테스트넷은 `--dry-run`으로 화면에만 볼 수 있어요.
- 안전장치: 메인넷은 `DEVLOG_TRADE_LIVE=1`과 `--live` 둘 다(테스트넷은 `--live --testnet`), 출금 권한 키 거절, 주문당 금액·손실 상한과 하루 손실 상한(기본값 없음), 선물 1~5배와 격리 마진, 같은 봉 중복 주문 막기(주문 번호를 종목·방향·봉 시각으로)
- `python -m devlog_trade.live check`(주문 없이 키·권한·잔고 확인), `resume`(손실 상한으로 멈춘 진입 다시 열기). 사용법은 `LIVE.md`
- 공개 저장소 대비: CI에 gitleaks 비밀값 검사를 넣고, `.gitignore`에 키·인증서·로그 파일을 더했다. README에 키는 기기 비밀 저장소에만 두고 거래소 키는 출금 권한 없이 만든다고 적었다.

- AI 판단 모델 고르기: `--ai-provider anthropic`(기본 `claude-opus-5-5`)·`openai`(기본 `gpt-5`)를 더했다. 키는 `ANTHROPIC_API_KEY`·`OPENAI_API_KEY`(맥은 키체인 `devlog-trade-bot-anthropic`·`devlog-trade-bot-openai`), 표에 없는 모델 가격은 `--ai-price-in`·`--ai-price-out`. 기본은 그대로 Gemini. README에 모델별 비용과 모의 계좌 비교 방법을 적었다.
- 매매 봇 MCP 서버 `python -m devlog_trade.mcp`(stdio, 표준 라이브러리만): `trade_bot_prices`(시세·이동평균), `trade_bot_accounts`(계좌·포지션·평가 손익·AI 사용량), `trade_bot_order`(기본 모의 주문, 실계좌는 서버 `--live`와 상한·`live=true`가 함께 있어야), `trade_bot_ai`(AI 판단 켜고 끄기). 체결은 봇과 같은 길로 `mcp-manual` 전략 이름으로 기록하고, 기본은 devlog에 보내지 않는다(`--record`).
- `trade-control.json`의 AI 켜고 끄기가 `--ai` 플래그와 `AI_DECISION`보다 먼저 적용된다.
- 같은 상태 파일을 봇 작업과 대화 주문이 동시에 고치지 않게 잠근다(fcntl, 윈도는 잠그지 않음).
- 세 겹 거름망 복리법 `coin --rule ladder` (1시간봉 모의 전용, 이슈 #11·#12·#13): 1일 방향·4시간 되돌림·1시간 돌파, ATR 손절과 1/3씩 분할 익절, 켈리 크기와 그림자 거래, 거래마다 레버리지, 하루·주 손실 브레이크, 코어 적립. 캔들에 시가·고가·저가를 더했고, 모의 선물에 일부 정리(`FuturesBroker.reduce`)를 더했다. `jobs.conf`에 비교용 세 계좌 줄을 넣었다.

### 바뀜
- 선물 레버리지 상한을 7배에서 5배로 낮췄다(모의·실계좌 같은 값, 민서님 결정 2026-10-15 세 겹 거름망 복리법 Q9). 6배 이상은 거절한다.

### 고침
- 실계좌 안전장치(리뷰 반영): 업비트 키 확인은 `out_of_scope`가 아니면 거절(429·5xx·인증 오류에 통과하지 않게), 한 번 손실 상한이 같은 손실로 걸린 하루 상한에 가려지지 않게, 선물을 같은 봉에 정리하고 뒤집을 때 두 주문의 번호를 따로, 거래소에서 사라진 선물 포지션(강제 청산)은 증거금만큼 잃은 것으로 셈해 상한에 넣는다.
- Claude 대체 모델이 답하면 `usage.iterations`로 시도마다 그 모델 값으로 비용을 더하고, 거절돼도 청구된 비용을 예산에 넣는다.
- 세 겹 거름망: 익절 1/3이 체결되지 않으면(수량 0) 익절 표시와 본전 손절을 미룬다. MCP 시세는 닫힌 봉이 없으면 도구 오류로 알린다. 맥 `run-bot.sh`는 키체인에 없는 키로 이미 받은 환경 변수를 지우지 않는다.
- backtest 워크플로: 체크아웃 토큰을 남기지 않고, Freqtrade 실패가 `grep`에 가려지지 않게 했다.
- 업비트 시세가 `jobs`에서 429(요청 너무 많음)로 빠지던 것: 요청 사이를 0.15초 띄우고 429면 1초·2초 쉬고 다시 묻는다. 한 번에 200개까지라 1일봉 250개(SMA200 과열 판단)를 `to`로 이어 받는다.
- 맥 launchd가 기본 `/usr/bin/python3`(3.9)를 잡아 봇이 돌지 않던 문제: `macos/find-python.sh`가 Homebrew·python.org의 3.10 이상을 찾고, 없으면 `install.sh`가 설치 안내와 함께 멈춘다.

## [0.1.0] - 2026-10-14

**필요한 devlog**: `record_trade`가 `exchange`·`currency`·소수 수량을 받는 버전(#254, PR #257). 그 전 devlog에서는 통화가 원화로 남아요. 선물 필드(`market`·`position_side`·`leverage`·`interval`, #260)는 아직 보내지 않고, 선물 체결은 이름·이유 꼬리표("비트코인 선물", "[선물 숏 진입 3x]")로 구분해요.

### 추가
- 코인 모의매매 `coin` 명령: 바이낸스·업비트 공개 캔들(API 키 없음), 코인 5종(BTC·ETH·XRP·SOL·DOGE), 봉 1m·5m·15m·30m·1h·4h·1d. 다 닫힌 봉으로만 판단하고 종목별 마지막 봉 시각으로 같은 봉을 두 번 거래하지 않음 (#255)
- 바이낸스 USDT-M 선물 모의매매: 롱·숏, 격리 증거금, 1~7배(처음 3배에서 민서님 결정으로 7배까지 올림), 강제 청산. 펀딩비는 아직 없음 (#255)
- AI 매매 판단 켜고 끄기(`--ai on`/`AI_DECISION`): 15분봉 이상, 손절은 규칙이 먼저, 하루 호출·월 예산 상한 (#263, PR #269)
- `jobs` 명령과 `jobs.conf`, 맥북 launchd 등록(`macos/install.sh`, 키체인 토큰, caffeinate), 집 PC용 Dockerfile·compose.yaml
- `--version`
- 별도 비공개 저장소로 옮김: devlog `app/trade-bot`의 커밋 기록과 CI(3개 OS 테스트·실제 시세 확인·Docker 빌드)를 그대로 가져왔어요 (#259)

### 그대로
- 종가 CSV 모드 `run`(증권사 종목, 원화)
- 실계좌 주문은 거절해요(`--live` → 종료 코드 2). 실계좌 연결(#264, PR #270)은 다음 버전에서 들어가요.
