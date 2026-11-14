FROM python:3.12-slim

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY db.py collector.py voucher.py main.py .

ENV DB_PATH=/data/db/measurements.db \
    INCOMING_DIR=/data/incoming \
    VOUCHER_DIR=/data/vouchers

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
