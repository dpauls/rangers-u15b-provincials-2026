FROM python:3.12-slim

WORKDIR /app

# Install git for pushing updates
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY data/ data/
COPY docs/ docs/
COPY scripts/ scripts/

# API key and git config injected at runtime via environment variables:
#   ANTHROPIC_API_KEY  - for Claude narrative generation
#   GIT_USER_NAME      - for git commit author
#   GIT_USER_EMAIL     - for git commit author email
# Git push credentials: mount your .gitconfig and credential store, or use GH_TOKEN

ENV PYTHONUNBUFFERED=1

# Default: run the generator once (daemon.py will be the entrypoint later)
CMD ["python3", "src/generate.py"]
