FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*
RUN git config --global --add safe.directory /app/vault
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY docs/PRINCIPLES.md ./docs/PRINCIPLES.md
COPY config.yaml ./config.yaml
CMD ["python", "-m", "src.main"]
