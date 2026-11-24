"""审计报告包接口：申请受理、报告签发、导出包写制/交付/核对/复核。

状态流转与业务判断都在 app/services/audit_package.py，路由只做参数传递。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.services import service_for
from app.services.audit_package import AuditPackageService

router = APIRouter(prefix="/api/audit", tags=["审计报告包"])
service = AuditPackageService()


class AcceptPayload(BaseModel):
    title: str
    applicant: str = ""


class IssuePayload(BaseModel):
    amount: float | None = None
    conclusion: str = ""
    metrics: list[dict[str, Any]] = Field(default_factory=list)
    issued_by: str = "报表主管"


class CreatePackagePayload(BaseModel):
    issuance_no: str | None = None


class WritePayload(BaseModel):
    # 故障注入点：credentials / manifest，用于演练"写文件中断后重试续做"
    fail_at: str | None = None


@router.post("/applications")
def accept_application(payload: AcceptPayload) -> dict:
    """受理审计申请，返回申请号。"""
    result = service.accept_application(payload.title, payload.applicant)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@router.get("/applications")
def list_applications() -> dict:
    return {"ok": True, "items": service.list_applications()}


@router.get("/applications/{application_no}")
def get_application(application_no: str) -> dict:
    """申请详情：签发版本链 + 导出包，按申请号辨认实际交出的报告版本。"""
    app = service.get_application(application_no)
    if app is None:
        raise HTTPException(status_code=404, detail=f"审计申请 {application_no} 不存在")
    return {"ok": True, "application": app}


@router.post("/applications/{application_no}/issuances")
def issue_report(application_no: str, payload: IssuePayload) -> dict:
    """签发正式报告版本；金额更正时再次调用即产生新版本（旧版本只读保留）。"""
    result = service.issue_report(
        application_no,
        amount=payload.amount,
        conclusion=payload.conclusion,
        metrics=payload.metrics,
        issued_by=payload.issued_by,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@router.get("/applications/{application_no}/issuances")
def list_issuances(application_no: str) -> dict:
    """签发版本列表：查看其他已签发版本与本次申请的关系（更正链）。"""
    return {"ok": True, "items": service.list_issuances(application_no)}


@router.post("/applications/{application_no}/packages")
def create_package(application_no: str, payload: CreatePackagePayload) -> dict:
    """按签发版本创建导出包（默认最新版本）；同一版本只会有一个包。"""
    result = service.create_package(application_no, payload.issuance_no)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@router.get("/applications/{application_no}/packages")
def list_packages(application_no: str) -> dict:
    return {"ok": True, "items": service.list_packages(application_no)}


@router.get("/applications/{application_no}/review")
def review_application(application_no: str) -> dict:
    """主管复核：比较实际交付的签发内容与后来发出的更正。"""
    result = service.review_application(application_no)
    if result is None:
        raise HTTPException(status_code=404, detail=f"审计申请 {application_no} 不存在")
    return result


@router.post("/packages/{package_no}/write")
def write_package(package_no: str, payload: WritePayload) -> dict:
    """执行/续做包文件写制。已交付的包拒绝重写。"""
    result = service.write_package(package_no, fail_at=payload.fail_at)
    if not result.get("ok") and "不存在" in result.get("message", ""):
        raise HTTPException(status_code=404, detail=result["message"])
    return result


@router.post("/packages/{package_no}/verify")
def verify_package(package_no: str) -> dict:
    """审计员核对：文件依据、版本同源、金额一致、其他已签发版本关系。"""
    result = service.verify_package(package_no)
    if "不存在" in result.get("message", ""):
        raise HTTPException(status_code=404, detail=result["message"])
    return result


@router.get("/packages/{package_no}")
def get_package(package_no: str) -> dict:
    pkg = service.get_package(package_no)
    if pkg is None:
        raise HTTPException(status_code=404, detail=f"导出包 {package_no} 不存在")
    return {"ok": True, "package": pkg}


@router.get("/packages/{package_no}/files/{kind}")
def download_package_file(package_no: str, kind: str) -> FileResponse:
    """下载包内文件（body/credentials/manifest）；旧下载可比较原始摘要。"""
    path = service.package_file_path(package_no, kind)
    if path is None:
        raise HTTPException(status_code=404, detail=f"包 {package_no} 的 {kind} 文件不存在")
    return FileResponse(path, media_type="application/json", filename=path.name)


@router.get("/packages/{package_no}/report-output")
def package_report_output(package_no: str) -> dict:
    """数据报告从导出文件记录的签发号，关联那一次的报告结果。"""
    pkg = service.get_package(package_no)
    if pkg is None:
        raise HTTPException(status_code=404, detail=f"导出包 {package_no} 不存在")
    report_service = service_for("data_report")
    result = report_service.output_by_issuance(pkg["issuance_no"])
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result["message"])
    return result
