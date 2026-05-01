#!/bin/bash
set -e

echo "============================================"
echo " Test Plan Converter - Starting..."
echo "============================================"

# Check .env exists
if [ ! -f ".env" ]; then
    echo ""
    echo "[ERROR] .env file not found."
    echo "Copy .env.example to .env and fill in your settings:"
    echo "  cp .env.example .env"
    echo ""
    exit 1
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
