@echo off
echo Starting OmniTranslate Production Server...

REM Create necessary directories
if not exist backend\data\uploads mkdir backend\data\uploads
if not exist backend\data\exports mkdir backend\data\exports

REM Install Python dependencies if needed
if not exist venv (
    echo Creating virtual environment...
    python -m venv venv
    call venv\Scripts\activate
    pip install -r requirements.txt
) else (
    call venv\Scripts\activate
)

REM Install Node dependencies if needed
if not exist node_modules (
    echo Installing Node dependencies...
    pnpm install
)

REM Build frontend
echo Building frontend...
pnpm build

REM Start backend
echo Starting backend server...
gunicorn backend.main:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000 --timeout 120
