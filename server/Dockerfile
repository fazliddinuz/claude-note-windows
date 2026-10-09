FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py db.py alerts.py users.py ./
COPY templates ./templates
COPY static ./static

# Baza va cookie kaliti konteynerdan tashqarida - volume da.
# Konteyner qayta qurilganda ma'lumot yo'qolmasin.
RUN useradd -r -u 10001 -m cnote && mkdir -p /data && chown cnote /data
VOLUME ["/data"]
USER cnote

EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# --proxy-headers: oldida nginx turadi, mijozning haqiqiy sxemasi shundan bilinadi
# (cookie ni Secure qilib qo'yish uchun kerak).
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
