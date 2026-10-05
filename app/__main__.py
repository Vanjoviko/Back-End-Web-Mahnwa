"""Jalankan worker: `python -m app` (bind default 127.0.0.1:8000)."""
import uvicorn

from app.config import Settings
from app.main import create_app

if __name__ == "__main__":
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.worker_bind, port=settings.worker_port)
