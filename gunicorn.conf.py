wsgi_app = "backend.main:app"
workers = 4
worker_class = "uvicorn.workers.UvicornWorker"
bind = "0.0.0.0:8000"
timeout = 120
keepalive = 5
graceful_timeout = 30
