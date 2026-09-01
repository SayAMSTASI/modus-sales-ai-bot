FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
COPY config ./config
COPY scripts/check_keycloak_config.py scripts/register_webhook.py ./scripts/
RUN pip install --no-cache-dir .

RUN addgroup --system app && adduser --system --ingroup app app \
    && chown -R app:app /app

USER app

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
