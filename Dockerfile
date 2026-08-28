# WebOllama — GPU monitoring collector runs on the host, app in container
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY webui ./webui
COPY .env.example .

ENV OLLAMA_URL=http://127.0.0.1:11434
ENV WEBUI_HOST=0.0.0.0
ENV WEBUI_PORT=8080
ENV DB_PATH=/app/data/webui.db
ENV LOG_FILE=/app/logs/webui.log

RUN mkdir -p /app/data /app/logs

EXPOSE 8080

CMD ["python", "-m", "webui.main"]
