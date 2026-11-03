# Incident Copilot API image — multi-stage, pinned base, non-root runtime.
FROM python:3.12.11-slim-bookworm AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/usr/local -r requirements.txt

FROM python:3.12.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN useradd --create-home --uid 10001 copilot

WORKDIR /app
COPY --from=builder /usr/local /usr/local
COPY app/ ./app/
COPY bot/ ./bot/
COPY collectors/ ./collectors/

USER copilot
EXPOSE 8000

# The compose file overrides this for the bot / docker-events services.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
