#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/backend"

# Check fpcalc
if ! command -v fpcalc &>/dev/null; then
  echo "Installing libchromaprint-tools (needed for audio fingerprinting)..."
  sudo apt-get install -y libchromaprint-tools
fi

# Check ffmpeg
if ! command -v ffmpeg &>/dev/null; then
  echo "Installing ffmpeg..."
  sudo apt-get install -y ffmpeg
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo ""
  echo "┌─────────────────────────────────────────────────────┐"
  echo "│  Add your free AcoustID key to backend/.env         │"
  echo "│  Get it at: https://acoustid.org/api-key (free)     │"
  echo "└─────────────────────────────────────────────────────┘"
  echo ""
fi

if [ ! -d venv ]; then
  echo "Creating virtual environment..."
  python3 -m venv venv
fi

source venv/bin/activate
pip install -q -r requirements.txt

echo ""
echo "  CopyGuard running at http://localhost:8000"
echo "  Dashboard:           http://localhost:8000/app"
echo ""

uvicorn main:app --host 0.0.0.0 --port 8000 --reload
