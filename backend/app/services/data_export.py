"""分析结果导出领域服务：将分析结果按条件导出为文件。

真实数据流：定义导出任务（查询条件/格式）→ 从对应表提取数据 → 序列化
→ 写入 JSON/CSV 文件 → 记录导出元数据到 exports 表。
"""
from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
from typing import Any

from app.database import db
from app.services.base import BaseModuleService

EXPORT_DIR = Path(os.environ.get("CC10045_EXPORT_DIR", Path(__file__).resolve().parent.parent.parent.parent / "exports"))
EXPORT_DIR.mkdir(parents=True, exist_ok=True)


class DataExportService(BaseModuleService):
    MODULE = "data_export"
    ENTITY = "导出任务"
    OPERATION_TERM = "导出"
    LIST_FIELDS = ["导出名称", "查询条件", "导出格式", "排队状态", "文件大小"]
    REQUIRED_FIELDS = ["导出名称", "导出格式"]
    STATUS_ORDER = ["排队中", "导出中", "已完成", "导出失败"]
    ACTION_RULES = {"开始导出": "已完成", "重试导出": "导出中"}
    NEGATIVE_ACTIONS = []

    def start_export(self, export_id: int) -> dict[str, Any]:
        """执行导出任务。"""
        task = db.fetchone("SELECT * FROM exports WHERE id=?", [export_id])
        if task is None:
            return {"ok": False, "message": f"导出任务 {export_id} 不存在"}

        db.update("exports", {"status": "导出中"}, "id=?", [export_id])

        query_condition_raw = task.get("query_condition", "{}")
        export_format = task.get("export_format", "csv")
        export_name = task.get("export_name", f"export_{export_id}")

        try:
            conditions = json.loads(query_condition_raw) if isinstance(query_condition_raw, str) else query_condition_raw
        except (json.JSONDecodeError, TypeError):
            conditions = {}

        module = conditions.get("module", "")
        if module:
            table_map = {
                "events": "events",
                "metric_values": "metric_values",
                "aggregation_results": "aggregation_results",
                "segment_members": "segment_members",
                "retention_results": "retention_results",
            }
            table = table_map.get(module, "events")
        else:
            table = "events"

        rows = db.fetchall(f"SELECT * FROM {table} LIMIT 1000")

        ext = export_format if not export_format.startswith('.') else export_format
        file_path = EXPORT_DIR / f"{export_name}.{ext}"

        if export_format == "json":
            content = json.dumps(rows, ensure_ascii=False, indent=2)
            file_path.write_text(content, encoding="utf-8")
        else:
            output = io.StringIO()
            output.write("\ufeff")
            if rows:
                writer = csv.writer(output, lineterminator="\n")
                writer.writerow(list(rows[0].keys()))
                for row in rows:
                    writer.writerow(list(row.values()))
            file_path.write_text(output.getvalue(), encoding="utf-8")

        file_size = file_path.stat().st_size
        db.update("exports", {
            "status": "已完成",
            "file_size_bytes": file_size,
            "output_path": str(file_path),
        }, "id=?", [export_id])

        return {
            "ok": True,
            "export_name": export_name,
            "format": export_format,
            "row_count": len(rows),
            "file_size_bytes": file_size,
            "output_path": str(file_path),
        }

    def list_files(self) -> list[dict[str, Any]]:
        """列出导出目录中的文件。"""
        files = []
        for f in EXPORT_DIR.iterdir():
            if f.is_file():
                files.append({"name": f.name, "size": f.stat().st_size, "modified": f.stat().st_mtime})
        return files

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["total_exports"] = db.count("exports")
        snapshot["completed_exports"] = db.count("exports", "status='已完成'")
        # 审计报告包：按已签发报告打包指标与取数凭据的可核验导出
        snapshot["audit_packages"] = db.count("export_packages")
        snapshot["audit_packages_delivered"] = db.count("export_packages", "status='已交付'")
        return snapshot