from __future__ import annotations

from fastapi import FastAPI
from contextlib import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware

from .api.routes import router, task_manager
from .config import settings


@asynccontextmanager
async def lifespan(app):
    task_manager.worker.start()
    try:
        yield
    finally:
        task_manager.worker.close()


app = FastAPI(
    title="DataFlow API",
    version="2.0.0",
    description="面向短视频运营场景的自然语言问数服务",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
