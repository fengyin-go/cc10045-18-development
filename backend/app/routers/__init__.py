"""业务路由汇总：资源路由 + 管道路由 + 审计报告包路由。"""
from __future__ import annotations

from app.routers import resource
from app.routers import pipeline
from app.routers import audit

ROUTERS = [resource]
PIPELINE_ROUTER = pipeline.router
AUDIT_ROUTER = audit.router