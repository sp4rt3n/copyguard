FROM python:3.12-slim

# System deps: curl (scraping), ffmpeg + fpcalc (audio fingerprinting)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ffmpeg \
    libchromaprint-tools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer cache)
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend source
COPY backend/ ./backend/

# Copy frontend
COPY frontend/ ./frontend/

# Persistent data lives here (mount a volume at /app/data)
RUN mkdir -p /app/data/uploads /app/data/screenshots

# Create .env from example if not present — actual values injected via env vars
COPY backend/.env.example ./backend/.env.example

# Run as non-root user
RUN useradd -r -s /bin/false appuser \
    && chown -R appuser:appuser /app/data
USER appuser

WORKDIR /app/backend

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
