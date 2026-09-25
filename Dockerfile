FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY vozviva ./vozviva
COPY config.example.yaml config.demo.yaml agenda.example.yaml agenda.demo.yaml ./
ENV PYTHONUNBUFFERED=1 VOZVIVA_CONFIG=/app/config.yaml
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"
CMD ["python", "-m", "vozviva", "serve"]
