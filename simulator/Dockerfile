FROM python:3.12-slim

WORKDIR /srv
COPY simulator.py .

ENV INCOMING_DIR=/data/incoming \
    WRITE_INTERVAL_SECONDS=1.0 \
    ROTATE_SECONDS=45 \
    PARTIAL_EVERY_N_LINES=7 \
    LATE_LINES_AFTER_ROTATION=2

CMD ["python", "-u", "simulator.py"]
