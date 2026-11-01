"""FastAPI application entrypoint for the Dual-Hub homelab."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.database import init_all_databases
from backend.notes.router import router as notes_router
from backend.learning.router import router as learning_router

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

ENVIRONMENT = os.getenv("ENVIRONMENT", "production")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await init_all_databases()
        logger.info("Databases initialised.")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Database init failed: %s", exc)
    yield


app = FastAPI(title="Dual-Hub Homelab", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(notes_router)
app.include_router(learning_router)

storage_dir = Path(os.getenv("STORAGE_DIR", str(Path(__file__).resolve().parent.parent / "storage")))
if storage_dir.exists():
    app.mount("/storage", StaticFiles(directory=str(storage_dir)), name="storage")


@app.get("/api/health")
async def health():
    return JSONResponse({"status": "ok", "environment": ENVIRONMENT})


frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
