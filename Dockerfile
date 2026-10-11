FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY web ./web

ENV LLM_MODE=mock
EXPOSE 8000
# non-root (허브 Kyverno restricted 정책 대비)
RUN useradd -u 10001 -m analyzer
USER 10001

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
