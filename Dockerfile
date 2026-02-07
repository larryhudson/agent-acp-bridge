FROM python:3.12-slim-bookworm

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 22 via NodeSource
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install ACP agent adapters globally
RUN npm install -g @zed-industries/claude-code-acp @zed-industries/codex-acp

# Set up Python app
WORKDIR /app

# Copy project files
COPY pyproject.toml .
COPY app/ app/
COPY main.py .

# Install Python dependencies
RUN python3 -m pip install --break-system-packages .

# Create data directory
RUN mkdir -p /data/projects

# Configure Codex CLI for autonomous operation (no permission prompts)
RUN mkdir -p /root/.codex && printf '%s\n' \
    'approval_policy = "never"' \
    'sandbox_mode = "danger-full-access"' \
    > /root/.codex/config.toml

EXPOSE 8000

ENTRYPOINT ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
