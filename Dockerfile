FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MEDIKTEST_NO_BROWSER=1 \
    MEDIKTEST_HOST=0.0.0.0 \
    MEDIKTEST_PORT=8765 \
    MEDIKTEST_DATA_DIR=/app/data

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY medik_pilot ./medik_pilot

RUN mkdir -p /app/data
EXPOSE 8765

CMD ["python", "-m", "medik_pilot"]