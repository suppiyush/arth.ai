# All ten agents in one container: they reach each other on 127.0.0.1, and only the orchestrator talks to
# the outside world (through its Agentverse mailbox), so no port needs to be published.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Settings (ASI1_API_KEY, DATABASE_URL, SEED_*, CLOUDINARY_URL, ...) come from the host's environment
# variables, never from a baked-in .env.
CMD ["honcho", "start"]
