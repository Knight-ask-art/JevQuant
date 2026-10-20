FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN addgroup --system jev && adduser --system --ingroup jev jev

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

RUN mkdir -p /app/data && chown -R jev:jev /app
USER jev

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=4s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

CMD ["jev", "serve", "--host", "0.0.0.0", "--port", "8000"]
