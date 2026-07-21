FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agents/ ./agents/
COPY lambdas/__init__.py ./lambdas/__init__.py
COPY lambdas/webhook_receiver/ ./lambdas/webhook_receiver/
COPY lambdas/enrichment/ ./lambdas/enrichment/
EXPOSE 8011
CMD ["python", "-m", "lambdas.enrichment.handler"]
