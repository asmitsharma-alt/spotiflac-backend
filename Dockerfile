FROM python:3.12-slim

# Install system dependencies: FFmpeg, Node.js (for extension bridges), curl, and openssl
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
    curl \
    openssl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Ensure UTF-8 output encoding and SpotiFLAC extension registry
ENV PYTHONUNBUFFERED=1
ENV PYTHONIOENCODING=utf-8
ENV PYTHONUTF8=1
ENV SPOTIFLAC_REGISTRIES="https://raw.githubusercontent.com/zarzet/SpotiFLAC-Extension/main/registry.json"

# Pre-install SpotiFLAC audio provider extensions into container image (purge soundcloud)
RUN python -c "from SpotiFLAC.extensions import ExtensionManager; em = ExtensionManager(); em.ensure_download_providers(); (em.uninstall('soundcloud') if em.get_installed('soundcloud') else None); print('Pre-installed extensions:', [e.name for e in em.list_installed()])"

ENV PORT=8000
EXPOSE $PORT

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port $PORT"]
