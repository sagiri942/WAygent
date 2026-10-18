"""FastAPI 应用入口。

路由：
  POST /api/conversations/* — 持久化旅行对话
  POST /api/runs/*          — 后台规划、修改与续接
  POST /api/auth/*      — 注册 / 登录
  GET  /api/history     — 历史行程列表
  GET  /api/history/:id — 历史行程详情
  GET  /api/health      — 健康检查
  GET  /api/config      — 前端高德 JS Key
  GET  /                — 前端首页
"""

from __future__ import annotations

import os
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.core.cache import close_cache
from app.core.env import load_local_env

load_local_env()

from app.api.auth_routes import router as auth_router
from app.api.history_routes import router as history_router
from app.api.profile_routes import router as profile_router
from app.api.plan_routes import router as plan_router
from app.api.sweep_routes import router as sweep_router
from app.api.runtime_routes import router as runtime_router
from app.runtime.container import start_runtime, stop_runtime
from app.storage_runtime import (
    bind_production_backends,
    create_production_storage,
    verify_checkpoint_storage,
)

# ─── 应用 ────────────────────────────────────────────────────


def create_app(*, storage_factory=None) -> FastAPI:
    from app.core.log_safety import install_log_redaction
    install_log_redaction()

    @asynccontextmanager
    async def lifespan(application):
        mode = os.getenv("WAYGENT_STORAGE_MODE", "mysql").strip().lower()
        storage = None
        runtime_started = False
        application.state.ready = False
        try:
            if mode == "sqlite-test":
                if not os.getenv("PYTEST_CURRENT_TEST"):
                    raise RuntimeError("sqlite-test storage mode is only available inside pytest")
                from app.core.database import get_db_path, init_db
                from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

                init_db()
                checkpoint_factory = lambda: AsyncSqliteSaver.from_conn_string(
                    os.getenv("RUNTIME_CHECKPOINT_DB", str(get_db_path().with_name("checkpoints.db")))
                )
            elif mode == "mysql":
                storage = (storage_factory or create_production_storage)()
                await verify_checkpoint_storage(storage)
                bind_production_backends(storage)
                checkpoint_factory = storage.checkpoint_context_factory
            else:
                raise RuntimeError("WAYGENT_STORAGE_MODE must be mysql")
            await start_runtime(checkpoint_context_factory=checkpoint_factory)
            runtime_started = True
            application.state.ready = True
            yield
        finally:
            application.state.ready = False
            try:
                if runtime_started:
                    await stop_runtime()
            finally:
                try:
                    if storage is not None:
                        storage.close()
                finally:
                    close_cache()

    application = FastAPI(
        title="AI 旅游规划助手", version="0.1.0", lifespan=lifespan
    )
    application.include_router(auth_router)
    application.include_router(history_router)
    application.include_router(profile_router)
    application.include_router(plan_router)
    application.include_router(sweep_router)
    application.include_router(runtime_router)

    @application.get("/api/health")
    def health():
        return {"status": "ok"}

    @application.get("/api/ready")
    def readiness():
        if not getattr(application.state, "ready", False):
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready"},
            )
        return {"status": "ready"}

    return application


app = create_app()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/config")
def config():
    """暴露前端地图所需的高德 JS API 密钥（不含敏感 REST Key）。"""
    return {
        "amap_js_key":           os.getenv("AMAP_JS_KEY", ""),
        "amap_js_security_code": os.getenv("AMAP_JS_SECURITY_CODE", ""),
    }
# ─── 静态文件（前端）—— 必须在 API 路由之后挂载 ─────────────

_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


def _frontend_index_response() -> FileResponse:
    # 前端没有构建产物指纹；HTML 必须每次取新版本，否则它会继续引用旧的
    # ChatState/pages 脚本并静默丢弃服务端新增的 PlanningBrief 字段。
    return FileResponse(
        _FRONTEND_DIR / "index.html",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


class RevalidatingStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


@app.get("/")
def index():
    return _frontend_index_response()


@app.get("/history")
def history_page():
    return _frontend_index_response()


@app.get("/profile")
def profile_page():
    return _frontend_index_response()


app.mount("/", RevalidatingStaticFiles(directory=str(_FRONTEND_DIR)), name="frontend")
