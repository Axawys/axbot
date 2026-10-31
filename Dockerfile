FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/axbot.sqlite3 \
    MPLCONFIGDIR=/tmp/matplotlib

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY axbot ./axbot

RUN useradd --system --uid 1000 axbot && mkdir /data && chown axbot /data
USER axbot
VOLUME /data

CMD ["python", "-m", "axbot"]
