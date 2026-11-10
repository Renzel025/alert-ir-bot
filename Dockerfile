FROM python:3.12-slim

WORKDIR /srv/alert-ir-bot
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# Claude Code CLI for the AI notes (app/ai_notes.py runs `claude -p` headless; there is no API key).
# Auth comes from CLAUDE_CODE_OAUTH_TOKEN in .env (`claude setup-token` on a logged-in machine).
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://claude.ai/install.sh | bash \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
ENV CLAUDE_BIN=/root/.local/bin/claude

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY policy.yaml .

# state (sqlite) and policy.yaml are mounted from the host — see docker-compose.yml
ENV DATABASE_PATH=/data/alert_ir.sqlite3 POLICY_PATH=/data/policy.yaml
VOLUME ["/data"]

EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --retries=3 CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
