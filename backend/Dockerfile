FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg default-libmysqlclient-dev build-essential pkg-config libmagic1 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt
COPY . .
RUN addgroup --system nexora && adduser --system --ingroup nexora nexora && chown -R nexora:nexora /app
USER nexora
EXPOSE 8000
CMD ["gunicorn","config.asgi:application","-k","uvicorn.workers.UvicornWorker","-c","gunicorn.conf.py"]
