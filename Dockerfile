# 집 PC(Windows·리눅스)에서 봇을 계속 돌릴 때. 표준 라이브러리만 써서 설치할 패키지가 없다.
FROM python:3.12-slim
ENV PYTHONUTF8=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY devlog_trade ./devlog_trade
COPY jobs.conf ./jobs.conf
# 상태 파일(trade-state-*.json)은 /data에 남긴다. 컨테이너를 다시 만들어도 이어진다
WORKDIR /data
ENV PYTHONPATH=/app
CMD ["python", "-m", "devlog_trade", "jobs", "/app/jobs.conf", "--every", "300"]
