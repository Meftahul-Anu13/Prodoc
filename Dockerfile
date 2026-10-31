# Optional deployment path: Docker is unavailable on the verified Windows host.
FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 FASTEMBED_CACHE_PATH=/opt/models/embeddings HF_HOME=/opt/models/huggingface
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend ./backend
COPY prompts ./prompts
COPY scripts ./scripts
RUN python -m scripts.setup_models --embeddings && useradd --uid 10001 --create-home docintel && mkdir -p /data/originals /opt/models && chown -R docintel:docintel /data /app/var /opt/models
USER docintel
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "backend.app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
