FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /srv
COPY requirements.txt .
# Playwright's bundled Node needs libatomic1, which the slim image lacks.
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m playwright install-deps chromium && rm -rf /var/lib/apt/lists/*
RUN python -m playwright install chromium

# Non-root user; pre-create volume mount points so named volumes inherit ownership.
RUN useradd --create-home --uid 1000 bee \
 && mkdir -p /profile /public /debug \
 && chown -R bee:bee /profile /public /debug

COPY app ./app
USER bee

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
