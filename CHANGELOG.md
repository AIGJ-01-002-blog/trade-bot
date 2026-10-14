# 매매 봇 Changelog

매매 봇은 devlog와 **따로 버전을 매겨요**([Semantic Versioning](https://semver.org/lang/ko/)). 버전은 `devlog_trade/__init__.py`의 `__version__` 하나에서 읽어요(`python -m devlog_trade --version`).

- 1.0.0 전(0.x)에는 기능이 늘면 Minor, 고치기만 하면 Patch를 올려요.
- **1.0.0은 실계좌 주문이 바이낸스 테스트넷 확인을 통과했을 때** 내요. 그때부터 실제 돈이 움직일 수 있어서예요.
- 버전마다 이 저장소에 `v버전` 태그를 달아요. #번호는 devlog 저장소의 이슈·PR이에요(봇이 2026-10-14에 devlog `app/trade-bot`에서 옮겨 왔어요).
- devlog 릴리스(목표 단위 Git Flow)와는 묶지 않아요. 봇 버전마다 필요한 devlog 버전을 아래에 적어요.

## [Unreleased]

### 추가
- 실계좌 주문 모듈 `devlog_trade/live.py` (#264): 바이낸스 현물·USDT-M 선물(롱·숏), 업비트 원화 현물에 시장가 주문. 모의 `PaperBroker`·`FuturesBroker`와 같은 모양의 `LiveBroker`·`LiveFuturesBroker`이고, 체결은 실제 체결가·수량·수수료·체결 시각으로 `mode=LIVE` 기록. `coin --live`로 켜고, 처음 보는 종목은 지난 봉으로 주문하지 않아요. 실거래소 `--live`는 `--dry-run`과 함께 못 쓰고, 테스트넷은 `--dry-run`으로 화면에만 볼 수 있어요.
- 안전장치: 메인넷은 `DEVLOG_TRADE_LIVE=1`과 `--live` 둘 다(테스트넷은 `--live --testnet`), 출금 권한 키 거절, 주문당 금액·손실 상한과 하루 손실 상한(기본값 없음), 선물 1~7배와 격리 마진, 같은 봉 중복 주문 막기(주문 번호를 종목·방향·봉 시각으로)
- `python -m devlog_trade.live check`(주문 없이 키·권한·잔고 확인), `resume`(손실 상한으로 멈춘 진입 다시 열기). 사용법은 `LIVE.md`
- 공개 저장소 대비: CI에 gitleaks 비밀값 검사를 넣고, `.gitignore`에 키·인증서·로그 파일을 더했다. README에 키는 기기 비밀 저장소에만 두고 거래소 키는 출금 권한 없이 만든다고 적었다.

- AI 판단 모델 고르기: `--ai-provider anthropic`(기본 `claude-opus-5-5`)·`openai`(기본 `gpt-5`)를 더했다. 키는 `ANTHROPIC_API_KEY`·`OPENAI_API_KEY`(맥은 키체인 `devlog-trade-bot-anthropic`·`devlog-trade-bot-openai`), 표에 없는 모델 가격은 `--ai-price-in`·`--ai-price-out`. 기본은 그대로 Gemini. README에 모델별 비용과 모의 계좌 비교 방법을 적었다.
- 매매 봇 MCP 서버 `python -m devlog_trade.mcp`(stdio, 표준 라이브러리만): `trade_bot_prices`(시세·이동평균), `trade_bot_accounts`(계좌·포지션·평가 손익·AI 사용량), `trade_bot_order`(기본 모의 주문, 실계좌는 서버 `--live`와 상한·`live=true`가 함께 있어야), `trade_bot_ai`(AI 판단 켜고 끄기). 체결은 봇과 같은 길로 `mcp-manual` 전략 이름으로 기록하고, 기본은 devlog에 보내지 않는다(`--record`).
- `trade-control.json`의 AI 켜고 끄기가 `--ai` 플래그와 `AI_DECISION`보다 먼저 적용된다.
- 같은 상태 파일을 봇 작업과 대화 주문이 동시에 고치지 않게 잠근다(fcntl, 윈도는 잠그지 않음).

### 바뀜
- 선물 레버리지 상한을 7배에서 5배로 낮췄다(모의·실계좌 같은 값, 민서님 결정 2026-10-15 세 겹 거름망 복리법 Q9). 6배 이상은 거절한다.

### 고침
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
