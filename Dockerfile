FROM python:3.12-slim

# Install system dependencies including FFmpeg, Node.js (required by SpotiFLAC extensions)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    curl \
    ca-certificates \
    gnupg && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" | tee /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-install SpotiFLAC download provider extensions during build
RUN python -c "from SpotiFLAC.extensions import ExtensionManager; em = ExtensionManager(); em.ensure_download_providers()"

# Copy application source code
COPY . .

# Ensure UTF-8 output encoding to prevent logging crashes
ENV PYTHONUNBUFFERED=1
ENV PYTHONIOENCODING=utf-8

ENV PORT=8000
EXPOSE $PORT

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port $PORT"]
