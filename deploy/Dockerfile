FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app
COPY src /app/src

RUN groupadd --gid 10001 stock \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin stock \
    && mkdir -p /var/lib/stock \
    && chown -R stock:stock /app /var/lib/stock

USER stock:stock
ENTRYPOINT ["python", "-m", "stock_assistant"]
CMD ["serve", "--host", "0.0.0.0", "--port", "9120", "--database", "/var/lib/stock/stock-assistant.sqlite3", "--secret-file", "/run/secrets/stock-api-key"]

