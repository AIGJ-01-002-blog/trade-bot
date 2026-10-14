# 매매 봇 Changelog

매매 봇은 devlog와 **따로 버전을 매겨요**([Semantic Versioning](https://semver.org/lang/ko/)). 버전은 `devlog_trade/__init__.py`의 `__version__` 하나에서 읽어요(`python -m devlog_trade --version`).

- 1.0.0 전(0.x)에는 기능이 늘면 Minor, 고치기만 하면 Patch를 올려요.
- **1.0.0은 실계좌 주문이 바이낸스 테스트넷 확인을 통과했을 때** 내요. 그때부터 실제 돈이 움직일 수 있어서예요.
- 봇이 별도 저장소 `trade-bot`으로 옮겨 가면 그 저장소에 `v버전` 태그와 릴리스를 만들어요. 그 전에는 이 파일과 devlog의 draft PR로만 기록해요.
- devlog 릴리스(목표 단위 Git Flow)와는 묶지 않아요. 봇 버전마다 필요한 devlog 버전을 아래에 적어요.

## [Unreleased]

## [0.1.0] - 2026-10-14

**필요한 devlog**: `record_trade`가 `exchange`·`currency`·소수 수량을 받는 버전(#254, PR #257). 그 전 devlog에서는 통화가 원화로 남아요. 선물 필드(`market`·`position_side`·`leverage`·`interval`, #260)는 아직 보내지 않고, 선물 체결은 이름·이유 꼬리표("비트코인 선물", "[선물 숏 진입 3x]")로 구분해요.

### 추가
- 코인 모의매매 `coin` 명령: 바이낸스·업비트 공개 캔들(API 키 없음), 코인 5종(BTC·ETH·XRP·SOL·DOGE), 봉 1m·5m·15m·30m·1h·4h·1d. 다 닫힌 봉으로만 판단하고 종목별 마지막 봉 시각으로 같은 봉을 두 번 거래하지 않음 (#255)
- 바이낸스 USDT-M 선물 모의매매: 롱·숏, 격리 증거금, 1~7배(처음 3배에서 민서님 결정으로 7배까지 올림), 강제 청산. 펀딩비는 아직 없음 (#255)
- AI 매매 판단 켜고 끄기(`--ai on`/`AI_DECISION`): 15분봉 이상, 손절은 규칙이 먼저, 하루 호출·월 예산 상한 (#263, PR #269)
- `jobs` 명령과 `jobs.conf`, 맥북 launchd 등록(`macos/install.sh`, 키체인 토큰, caffeinate), 집 PC용 Dockerfile·compose.yaml
- `--version`

### 그대로
- 종가 CSV 모드 `run`(증권사 종목, 원화)
- 실계좌 주문은 거절해요(`--live` → 종료 코드 2). 실계좌 연결(#264, PR #270)은 다음 버전에서 들어가요.
