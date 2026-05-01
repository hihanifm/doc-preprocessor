#!/bin/bash
set -e

echo "============================================"
echo " Docs Garage - Starting..."
echo "============================================"

# .env is optional (no required settings today). Create from example if you want overrides.
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    cp .env.example .env
    echo "[info] Created .env from .env.example (optional overrides)."
fi

# Create venv if it doesn't exist
if [ ! -f ".venv/bin/activate" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

# Activate venv
source .venv/bin/activate

# Install / update dependencies
echo "Installing dependencies..."
pip install -q -r requirements.txt

echo ""
echo "============================================"
echo " Server running at http://localhost:5000"
echo " Press Ctrl+C to stop"
echo "============================================"
echo ""

python app.py
