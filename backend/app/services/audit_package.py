"""审计报告包领域服务：按已签发报告打包报告指标与取数凭据的可核验导出。

业务链路：审计申请受理 → 报告签发（可多次更正）→ 导出包写制/续做 → 交付 →
审计员核对 / 主管复核。

核心不变量：
1. 签发记录只增不改：更正金额 = 新签发版本（supersedes 指向旧签发号），
   旧版本行内容永不更新，任何时刻都能还原"当时实际签发了什么"。
2. 导出包创建时锁定签发版本（issuance_id 固定），正文与取数凭据必然来自
   同一签发版本；写包中途发生更正也不会混入新版本内容。
3. 已交付的包文件不可被后续更正覆盖；更正只能通过新包交付，旧下载保留
   原始摘要可供比较。
4. 写包分阶段落盘（正文 → 凭据 → 清单），任一阶段中断后重试可续做，
   同一包重试仍写同一签发版本。
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from app.database import db
from app.services.data_export import EXPORT_DIR

AUDIT_PKG_ROOT = EXPORT_DIR / "audit_packages"
AUDIT_PKG_ROOT.mkdir(parents=True, exist_ok=True)

PACKAGE_STATUSES = ["写入中", "写入失败", "已交付"]
ISSUANCE_STATUSES = ["已签发", "已被更正"]


class ExportInterrupted(RuntimeError):
    """写包过程被中断（故障注入/真实故障）；包未交付，可重试续做。"""


def _canonical(obj: Any) -> str:
    """规范化 JSON：键排序保证同一内容多次落盘字节一致，摘要可核验。"""
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_file_atomic(path: Path, content: str) -> str:
    """先写临时文件 fsync 再原子改名，返回内容 sha256。"""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return _sha256_text(content)


def _read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


class AuditPackageService:
    """审计申请 → 报告签发 → 导出包交付 的完整链路。"""

    # ------------------------------------------------------------------
    # 申请受理
    # ------------------------------------------------------------------
    def accept_application(self, title: str, applicant: str = "") -> dict[str, Any]:
        """受理一份审计申请，返回申请号。"""
        if not str(title or "").strip():
            return {"ok": False, "message": "申请标题不能为空"}
        placeholder = f"TMP-{uuid.uuid4().hex[:12]}"
        app_id = db.insert("audit_applications", {
            "application_no": placeholder,
            "title": str(title).strip(),
            "applicant": str(applicant or "").strip(),
            "status": "已受理",
        })
        application_no = f"AUD-{app_id:05d}"
        db.update("audit_applications", {"application_no": application_no}, "id=?", [app_id])
        return {
            "ok": True,
            "application_no": application_no,
            "application": db.fetchone("SELECT * FROM audit_applications WHERE id=?", [app_id]),
            "message": f"审计申请 {application_no} 已受理",
        }

    def list_applications(self) -> list[dict[str, Any]]:
        return db.fetchall("SELECT * FROM audit_applications ORDER BY id DESC")

    def get_application(self, application_no: str) -> dict[str, Any] | None:
        """申请详情：含全部签发版本（更正链）与导出包，按申请号辨认实际交出的版本。"""
        app = db.fetchone("SELECT * FROM audit_applications WHERE application_no=?", [application_no])
        if app is None:
            return None
        app["issuances"] = db.fetchall(
            "SELECT * FROM report_issuances WHERE application_id=? ORDER BY version_no",
            [app["id"]],
        )
        app["packages"] = db.fetchall(
            "SELECT * FROM export_packages WHERE application_id=? ORDER BY id",
            [app["id"]],
        )
        return app

    # ------------------------------------------------------------------
    # 报告签发（只增不改；更正 = 新版本）
    # ------------------------------------------------------------------
    def issue_report(
        self,
        application_no: str,
        amount: float | None = None,
        conclusion: str = "",
        metrics: list[dict[str, Any]] | None = None,
        issued_by: str = "报表主管",
    ) -> dict[str, Any]:
        """签发一个正式报告版本。金额更正时再次调用即产生新版本。

        取数凭据在签发时刻生成并随签发记录冻结，之后任何导出包都打包
        同一份凭据，保证"正文与凭据同属一个签发版本"。
        """
        app = db.fetchone("SELECT * FROM audit_applications WHERE application_no=?", [application_no])
        if app is None:
            return {"ok": False, "message": f"审计申请 {application_no} 不存在"}

        normalized: list[dict[str, Any]] = []
        for item in metrics or []:
            name = str(item.get("name") or item.get("指标名称") or "").strip()
            if not name:
                return {"ok": False, "message": "报告指标缺少名称"}
            try:
                value = round(float(item.get("value", item.get("数值"))), 4)
            except (TypeError, ValueError):
                return {"ok": False, "message": f"指标「{name}」数值非法"}
            normalized.append({"name": name, "value": value})

        if amount is None:
            if not normalized:
                return {"ok": False, "message": "签发金额与报告指标不能同时为空"}
            amount = round(sum(m["value"] for m in normalized), 4)
        else:
            amount = round(float(amount), 4)
            if normalized:
                total = round(sum(m["value"] for m in normalized), 4)
                if abs(total - amount) > 1e-6:
                    return {"ok": False, "message": f"指标合计 {total} 与签发金额 {amount} 不一致"}

        prev = db.fetchone(
            "SELECT * FROM report_issuances WHERE application_id=? ORDER BY version_no DESC LIMIT 1",
            [app["id"]],
        )
        version_no = int(prev["version_no"]) + 1 if prev else 1
        issuance_no = f"ISS-{application_no}-V{version_no:02d}"
        supersedes = prev["issuance_no"] if prev else None

        credentials = self._build_credentials(issuance_no, normalized, amount)
        report_output_id = self._freeze_report_output(
            app, issuance_no, version_no, amount, conclusion, normalized)

        if prev:
            db.update("report_issuances", {"status": "已被更正"}, "id=?", [prev["id"]])

        issuance_id = db.insert("report_issuances", {
            "issuance_no": issuance_no,
            "application_id": app["id"],
            "version_no": version_no,
            "amount": amount,
            "conclusion": str(conclusion or ""),
            "metrics_json": json.dumps(normalized, ensure_ascii=False),
            "credentials_json": json.dumps(credentials, ensure_ascii=False),
            "report_output_id": report_output_id,
            "supersedes_issuance_no": supersedes,
            "status": "已签发",
            "issued_by": str(issued_by or "报表主管"),
        })
        issuance = db.fetchone("SELECT * FROM report_issuances WHERE id=?", [issuance_id])
        return {
            "ok": True,
            "issuance": issuance,
            "message": (
                f"报告 {issuance_no} 已签发（金额 {amount}）"
                + (f"，更正 {supersedes}" if supersedes else "")
            ),
        }

    def list_issuances(self, application_no: str) -> list[dict[str, Any]]:
        app = db.fetchone("SELECT * FROM audit_applications WHERE application_no=?", [application_no])
        if app is None:
            return []
        return db.fetchall(
            "SELECT * FROM report_issuances WHERE application_id=? ORDER BY version_no",
            [app["id"]],
        )

    def _build_credentials(
        self, issuance_no: str, metrics: list[dict[str, Any]], amount: float
    ) -> dict[str, Any]:
        """生成取数凭据：记录每个指标的取数来源（表/SQL/参数/快照时间/行数）。

        凭据在签发时刻冻结，导出包只负责打包，不再重新取数。
        """
        entries: list[dict[str, Any]] = []
        for idx, metric in enumerate(metrics, 1):
            metric_row = db.fetchone(
                "SELECT id FROM metrics WHERE metric_name=?", [metric["name"]])
            if metric_row:
                snap = db.fetchone(
                    "SELECT snapshot_value, snapshot_time FROM metric_values "
                    "WHERE metric_id=? ORDER BY snapshot_time DESC LIMIT 1",
                    [metric_row["id"]],
                )
                source = {
                    "取数表": "metric_values",
                    "取数SQL": "SELECT snapshot_value, snapshot_time FROM metric_values "
                             "WHERE metric_id = ? ORDER BY snapshot_time DESC LIMIT 1",
                    "取数参数": {"metric_id": metric_row["id"]},
                    "快照时间": snap["snapshot_time"] if snap else None,
                    "返回行数": 1 if snap else 0,
                }
            else:
                source = {
                    "取数表": "issuance_attachment",
                    "取数SQL": "-- 指标未入指标库，按签发附件直取",
                    "取数参数": {"指标名称": metric["name"]},
                    "快照时间": None,
                    "返回行数": 1,
                }
            entries.append({
                "凭据号": f"VC-{issuance_no}-{idx:02d}",
                "指标名称": metric["name"],
                "数值": metric["value"],
                **source,
            })
        return {
            "凭据列表": entries,
            "取数合计金额": round(sum(m["value"] for m in metrics), 4) if metrics else amount,
            "取数时间": db.now(),
        }

    def _freeze_report_output(
        self,
        app: dict[str, Any],
        issuance_no: str,
        version_no: int,
        amount: float,
        conclusion: str,
        metrics: list[dict[str, Any]],
    ) -> int:
        """把本次签发的报告结果冻结进 report_outputs，供数据报告按签发号关联。"""
        report_name = f"审计报告-{app['application_no']}"
        report = db.fetchone("SELECT * FROM reports WHERE report_name=?", [report_name])
        if report is None:
            report_id = db.insert("reports", {
                "report_name": report_name,
                "metric_ids": "[]",
                "report_period": "一次性",
                "output_format": "json",
                "auto_send": 0,
                "status": "已发布",
            })
        else:
            report_id = report["id"]
        content = {
            "report_name": report_name,
            "issuance_no": issuance_no,
            "version_no": version_no,
            "generated_at": db.now(),
            "sections": [
                {"metric_name": m["name"], "current_value": m["value"]} for m in metrics
            ],
            "summary": {
                "total_metrics": len(metrics),
                "amount": amount,
                "conclusion": conclusion,
            },
        }
        return db.insert("report_outputs", {
            "report_id": report_id,
            "output_content": json.dumps(content, ensure_ascii=False),
        })

    # ------------------------------------------------------------------
    # 导出包：创建（锁定签发版本）→ 写制/续做 → 交付
    # ------------------------------------------------------------------
    def create_package(self, application_no: str, issuance_no: str | None = None) -> dict[str, Any]:
        """为指定签发版本（默认最新）创建导出包。

        同一签发版本只会有一个包：已交付则复用（不可覆盖），未交付则续做。
        """
        app = db.fetchone("SELECT * FROM audit_applications WHERE application_no=?", [application_no])
        if app is None:
            return {"ok": False, "message": f"审计申请 {application_no} 不存在"}

        if issuance_no:
            issuance = db.fetchone(
                "SELECT * FROM report_issuances WHERE issuance_no=? AND application_id=?",
                [issuance_no, app["id"]],
            )
            if issuance is None:
                return {"ok": False, "message": f"签发号 {issuance_no} 不属于申请 {application_no}"}
        else:
            issuance = db.fetchone(
                "SELECT * FROM report_issuances WHERE application_id=? ORDER BY version_no DESC LIMIT 1",
                [app["id"]],
            )
        if issuance is None:
            return {"ok": False, "message": f"申请 {application_no} 尚无已签发版本，无法打包"}

        existing = db.fetchone(
            "SELECT * FROM export_packages WHERE application_id=? AND issuance_id=?",
            [app["id"], issuance["id"]],
        )
        if existing:
            note = "该签发版本已有交付包，复用旧包（已交付文件不可覆盖）" \
                if existing["status"] == "已交付" else "该签发版本已有未完成的包，可写包续做"
            return {"ok": True, "reused": True, "package": existing, "message": note}

        package_no = f"PKG-{application_no}-V{int(issuance['version_no']):02d}"
        package_id = db.insert("export_packages", {
            "package_no": package_no,
            "application_id": app["id"],
            "application_no": application_no,
            "issuance_id": issuance["id"],
            "issuance_no": issuance["issuance_no"],
            "version_no": issuance["version_no"],
            "status": "写入中",
        })
        package = db.fetchone("SELECT * FROM export_packages WHERE id=?", [package_id])
        return {
            "ok": True,
            "reused": False,
            "package": package,
            "message": f"导出包 {package_no} 已创建，锁定签发版本 {issuance['issuance_no']}",
        }

    def get_package(self, package_no: str) -> dict[str, Any] | None:
        pkg = db.fetchone("SELECT * FROM export_packages WHERE package_no=?", [package_no])
        if pkg is None:
            return None
        pkg_dir = self._package_dir(pkg)
        pkg["package_dir"] = str(pkg_dir)
        manifest = _read_json_file(pkg_dir / "manifest.json")
        pkg["manifest"] = manifest
        return pkg

    def list_packages(self, application_no: str) -> list[dict[str, Any]]:
        app = db.fetchone("SELECT * FROM audit_applications WHERE application_no=?", [application_no])
        if app is None:
            return []
        return db.fetchall(
            "SELECT * FROM export_packages WHERE application_id=? ORDER BY id", [app["id"]])

    def write_package(self, package_no: str, fail_at: str | None = None) -> dict[str, Any]:
        """执行/续做包文件写制：正文 → 凭据 → 清单，全部落盘后才算交付。

        - 已交付：拒绝重写（已交付文件不能被之后的更正覆盖）。
        - 写入中/写入失败：按锁定的签发版本重新生成各阶段文件，可断点续做。
        - fail_at：故障注入（credentials/manifest），用于演练与回归测试。
        """
        pkg = db.fetchone("SELECT * FROM export_packages WHERE package_no=?", [package_no])
        if pkg is None:
            return {"ok": False, "message": f"导出包 {package_no} 不存在"}
        if pkg["status"] == "已交付":
            return {
                "ok": False,
                "status": "已交付",
                "package_no": package_no,
                "message": f"包 {package_no} 已交付，文件不可被后续更正覆盖；"
                           "如需交付更正版本，请签发新版本并创建新包",
            }

        app = db.fetchone("SELECT * FROM audit_applications WHERE id=?", [pkg["application_id"]])
        issuance = db.fetchone("SELECT * FROM report_issuances WHERE id=?", [pkg["issuance_id"]])
        if app is None or issuance is None:
            return {"ok": False, "message": "包关联的申请或签发版本缺失"}

        attempts = int(pkg["attempts"]) + 1
        db.update("export_packages", {"status": "写入中", "attempts": attempts},
                  "id=?", [pkg["id"]])
        pkg_dir = self._package_dir(pkg)
        pkg_dir.mkdir(parents=True, exist_ok=True)

        try:
            body = self._render_body(app, pkg, issuance)
            body_sha = _write_file_atomic(pkg_dir / "body.json", _canonical(body))

            if fail_at == "credentials":
                raise ExportInterrupted("写取数凭据阶段中断（模拟故障）")
            creds = self._render_credentials(app, pkg, issuance)
            cred_sha = _write_file_atomic(pkg_dir / "credentials.json", _canonical(creds))

            if fail_at == "manifest":
                raise ExportInterrupted("写交付清单阶段中断（模拟故障）")
            delivered_at = db.now()
            manifest = {
                "文件类型": "交付清单",
                "包号": pkg["package_no"],
                "申请号": app["application_no"],
                "签发号": issuance["issuance_no"],
                "签发版本": issuance["version_no"],
                "文件摘要": {"body.json": body_sha, "credentials.json": cred_sha},
                "交付时间": delivered_at,
            }
            manifest_sha = _write_file_atomic(pkg_dir / "manifest.json", _canonical(manifest))
        except ExportInterrupted as exc:
            db.update("export_packages", {"status": "写入失败"}, "id=?", [pkg["id"]])
            return {
                "ok": False,
                "package_no": package_no,
                "status": "写入失败",
                "attempts": attempts,
                "resumable": True,
                "message": f"{exc}；可重试续做，包仍锁定签发版本 {issuance['issuance_no']}",
            }

        db.update("export_packages", {
            "status": "已交付",
            "body_sha256": body_sha,
            "credential_sha256": cred_sha,
            "manifest_sha256": manifest_sha,
            "delivered_at": delivered_at,
        }, "id=?", [pkg["id"]])
        db.update("audit_applications", {"status": "已交付"}, "id=?", [app["id"]])

        return {
            "ok": True,
            "package_no": package_no,
            "status": "已交付",
            "attempts": attempts,
            "issuance_no": issuance["issuance_no"],
            "delivered_at": delivered_at,
            "message": f"包 {package_no} 已交付（签发版本 {issuance['issuance_no']}，第 {attempts} 次写制）",
        }

    # ------------------------------------------------------------------
    # 审计员核对 / 主管复核
    # ------------------------------------------------------------------
    def verify_package(self, package_no: str) -> dict[str, Any]:
        """审计员领取包后按交付版本核对：文件依据、版本同源、金额一致。"""
        pkg = db.fetchone("SELECT * FROM export_packages WHERE package_no=?", [package_no])
        if pkg is None:
            return {"ok": False, "message": f"导出包 {package_no} 不存在"}
        if pkg["status"] != "已交付":
            return {
                "ok": False,
                "status": pkg["status"],
                "message": f"包 {package_no} 尚未交付（{pkg['status']}），不能作为核对依据",
            }

        pkg_dir = self._package_dir(pkg)
        checks: list[dict[str, Any]] = []

        def check(name: str, ok: bool, detail: str) -> None:
            checks.append({"检查项": name, "通过": bool(ok), "说明": detail})

        body = _read_json_file(pkg_dir / "body.json")
        creds = _read_json_file(pkg_dir / "credentials.json")
        manifest = _read_json_file(pkg_dir / "manifest.json")
        check("文件齐备", all(x is not None for x in (body, creds, manifest)),
              "body.json / credentials.json / manifest.json 均存在且可解析")
        if not all(x is not None for x in (body, creds, manifest)):
            return {"ok": False, "checks": checks, "message": "包文件缺失或损坏"}

        body_sha = _sha256_text((pkg_dir / "body.json").read_text(encoding="utf-8"))
        cred_sha = _sha256_text((pkg_dir / "credentials.json").read_text(encoding="utf-8"))
        manifest_sha = _sha256_text((pkg_dir / "manifest.json").read_text(encoding="utf-8"))
        check("文件摘要与清单一致",
              manifest["文件摘要"].get("body.json") == body_sha
              and manifest["文件摘要"].get("credentials.json") == cred_sha,
              f"body={body_sha[:12]}… creds={cred_sha[:12]}…")
        check("文件摘要与交付登记一致",
              pkg["body_sha256"] == body_sha and pkg["credential_sha256"] == cred_sha
              and pkg["manifest_sha256"] == manifest_sha,
              "交付后文件未被改动")

        check("正文与凭据同属一个签发版本",
              body["签发号"] == creds["签发号"] == manifest["签发号"] == pkg["issuance_no"],
              f"签发号 {pkg['issuance_no']}")
        check("金额一致（结论 = 取数凭据合计）",
              abs(float(body["金额"]) - float(creds["取数合计金额"])) < 1e-6,
              f"正文 {body['金额']} / 凭据 {creds['取数合计金额']}")

        siblings = db.fetchall(
            "SELECT * FROM report_issuances WHERE application_id=? AND issuance_no!=? "
            "ORDER BY version_no",
            [pkg["application_id"], pkg["issuance_no"]],
        )
        other_versions = [{
            "签发号": s["issuance_no"],
            "版本": s["version_no"],
            "金额": s["amount"],
            "状态": s["status"],
            "与本包关系": "后续更正版本" if s["version_no"] > pkg["version_no"] else "先前版本",
            "更正自": s["supersedes_issuance_no"],
        } for s in siblings]

        return {
            "ok": all(c["通过"] for c in checks),
            "checks": checks,
            "交付版本": {
                "签发号": pkg["issuance_no"],
                "版本": pkg["version_no"],
                "金额": body["金额"],
                "结论": body["结论"],
                "交付时间": pkg["delivered_at"],
            },
            "文件依据": {
                "目录": str(pkg_dir),
                "正文sha256": body_sha,
                "凭据sha256": cred_sha,
                "清单sha256": manifest_sha,
            },
            "其他已签发版本": other_versions,
        }

    def review_application(self, application_no: str) -> dict[str, Any] | None:
        """主管复核：比较实际交付的签发内容与后来发出的更正。"""
        app = db.fetchone("SELECT * FROM audit_applications WHERE application_no=?", [application_no])
        if app is None:
            return None
        issuances = db.fetchall(
            "SELECT * FROM report_issuances WHERE application_id=? ORDER BY version_no",
            [app["id"]],
        )
        packages = db.fetchall(
            "SELECT * FROM export_packages WHERE application_id=? ORDER BY id", [app["id"]])
        latest = issuances[-1] if issuances else None

        comparisons: list[dict[str, Any]] = []
        for pkg in packages:
            entry: dict[str, Any] = {
                "包号": pkg["package_no"],
                "交付签发号": pkg["issuance_no"],
                "交付版本": pkg["version_no"],
                "状态": pkg["status"],
                "交付时间": pkg["delivered_at"],
            }
            if pkg["status"] == "已交付" and latest:
                body = _read_json_file(self._package_dir(pkg) / "body.json") or {}
                delivered_amount = body.get("金额")
                entry.update({
                    "交付金额": delivered_amount,
                    "当前最新签发号": latest["issuance_no"],
                    "当前最新金额": latest["amount"],
                    "金额差异": round(float(latest["amount"]) - float(delivered_amount), 4)
                               if delivered_amount is not None else None,
                    "交付后是否被更正": int(pkg["version_no"]) < int(latest["version_no"]),
                })
            comparisons.append(entry)

        return {
            "ok": True,
            "申请号": application_no,
            "申请标题": app["title"],
            "签发版本数": len(issuances),
            "最新签发": latest,
            "交付包复核": comparisons,
        }

    # ------------------------------------------------------------------
    # 文件下载（旧下载可比较原始摘要）
    # ------------------------------------------------------------------
    def package_file_path(self, package_no: str, kind: str) -> Path | None:
        pkg = db.fetchone("SELECT * FROM export_packages WHERE package_no=?", [package_no])
        if pkg is None or kind not in ("body", "credentials", "manifest"):
            return None
        path = self._package_dir(pkg) / f"{kind}.json"
        return path if path.is_file() else None

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _package_dir(self, pkg: dict[str, Any]) -> Path:
        return AUDIT_PKG_ROOT / str(pkg["package_no"])

    def _render_body(
        self, app: dict[str, Any], pkg: dict[str, Any], issuance: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "文件类型": "审计报告正文",
            "包号": pkg["package_no"],
            "申请号": app["application_no"],
            "申请标题": app["title"],
            "签发号": issuance["issuance_no"],
            "签发版本": issuance["version_no"],
            "签发人": issuance["issued_by"],
            "签发时间": issuance["issued_at"],
            "结论": issuance["conclusion"],
            "金额": issuance["amount"],
            "报告指标": json.loads(issuance["metrics_json"]),
            "更正关系": (
                {"被更正签发号": issuance["supersedes_issuance_no"]}
                if issuance["supersedes_issuance_no"] else None
            ),
        }

    def _render_credentials(
        self, app: dict[str, Any], pkg: dict[str, Any], issuance: dict[str, Any]
    ) -> dict[str, Any]:
        stored = json.loads(issuance["credentials_json"])
        return {
            "文件类型": "取数凭据",
            "包号": pkg["package_no"],
            "申请号": app["application_no"],
            "签发号": issuance["issuance_no"],
            "签发版本": issuance["version_no"],
            "凭据列表": stored["凭据列表"],
            "取数合计金额": stored["取数合计金额"],
            "取数时间": stored["取数时间"],
        }
