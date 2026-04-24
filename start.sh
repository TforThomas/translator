#!/bin/bash

echo "Starting OmniTranslate Production Server..."

# Create necessary directories
mkdir -p backend/data/uploads
mkdir -p backend/data/exports

# Install Python dependencies if needed
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
else
    source venv/bin/activate
fi

# Install Node dependencies if needed
if [ ! -d "node_modules" ]; then
    echo "Installing Node dependencies..."
    pnpm install
fi

# Build frontend
echo "Building frontend..."
pnpm build

# Start backend with Gunicorn
echo "Starting backend server..."
gunicorn backend.main:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000 --timeout 120
