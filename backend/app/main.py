"""API数据聚合采集中心 后端服务入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8000
健康检查：GET /api/health
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import ROUTERS
from app.services.cleanse import cleanse_service
from app.store import store

app = FastAPI(title="API数据聚合采集中心", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in ROUTERS:
    app.include_router(module.router)


@app.get("/api/health")
def health() -> dict[str, object]:
    """健康检查：确认服务已经监听、示例数据已经就绪。"""
    return {"ok": True, "app": settings.app_name, "modules": len(store.module_names())}


@app.get("/api/overview")
def overview() -> dict[str, object]:
    """运营概览：各业务模块总量 + 数据清洗各版本处理量与前后差异。

    数据清洗的口径来自清洗数据集本身（规则版本数、待处理剔除、版本冲突），
    避免沿用规则清单的静态计数。
    """
    payload = store.overview()
    cleanse = cleanse_service.rule_overview()
    for item in payload["modules"]:
        if item["name"] != "cleanse":
            continue
        item["created"] = sum(len(rule["versions"]) for rule in cleanse["rules"])
        item["pending"] = sum(rule["queue_pending"] for rule in cleanse["rules"])
        item["abnormal"] = sum(rule["conflicts"] for rule in cleanse["rules"])
    payload["cleanse"] = cleanse
    return payload
