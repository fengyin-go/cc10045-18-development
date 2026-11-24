"""审计报告包验收场景模拟（隔离环境运行，不污染开发数据）。

场景主线（对应需求）：
  审计申请受理 → 初版签发金额 100 → 导出包创建并开始写文件
  → 第一次写取数凭据时中断（模拟故障）
  → 报表主管发出更正金额的正式版本 90
  → 重试续做完成交付 → 审计员领取包并按交付版本核对金额
  → 检查结论与取数凭据同源（同为 100 或同为 90）
  → 更正版本只能通过新包交付；旧下载保留原始摘要供比较
  → 数据报告按导出文件记录的签发号关联那次的报告结果

运行：cd backend && .venv/bin/python scripts/simulate_audit_package.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

# 隔离环境：临时数据库与导出目录，必须在 import app 之前设置
WORK_DIR = Path(tempfile.mkdtemp(prefix="audit_pkg_sim_"))
os.environ["CC10045_DB_DIR"] = str(WORK_DIR / "db")
os.environ["CC10045_EXPORT_DIR"] = str(WORK_DIR / "exports")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import db  # noqa: E402
from app.services.audit_package import AuditPackageService  # noqa: E402
from app.services.data_report import DataReportService  # noqa: E402

svc = AuditPackageService()
report_svc = DataReportService()

RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    RESULTS.append((bool(condition), name, detail))
    mark = "✅" if condition else "❌"
    print(f"  {mark} {name}" + (f" — {detail}" if detail else ""))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def pkg_dir(package_no: str) -> Path:
    from app.services.audit_package import AUDIT_PKG_ROOT
    return AUDIT_PKG_ROOT / package_no


print(f"工作目录（隔离）: {WORK_DIR}\n")

# ============================================================================
print("== 场景一：写凭据中断 + 更正签发，重试续做后按交付版本核对 ==\n")

# 1. 受理审计申请
app = svc.accept_application("2026年度营收审计", applicant="审计员-王")
app_no = app["application_no"]
print(f"1. 受理申请: {app_no}（{app['message']}）")
check("申请已受理", app["ok"] and app["application"]["status"] == "已受理")

# 2. 初版签发：金额 100
v1 = svc.issue_report(
    app_no, amount=100, conclusion="确认营收金额为 100 万元",
    metrics=[{"name": "营收金额", "value": 100}], issued_by="报表主管-李")
iss1 = v1["issuance"]["issuance_no"]
print(f"\n2. 初版签发: {iss1}（金额 100）")
check("初版签发成功", v1["ok"] and v1["issuance"]["version_no"] == 1)

# 3. 创建导出包（锁定 V01）
pkg1 = svc.create_package(app_no)
pkg1_no = pkg1["package"]["package_no"]
print(f"\n3. 创建导出包: {pkg1_no}（{pkg1['message']}）")
check("包锁定初版签发号", pkg1["package"]["issuance_no"] == iss1)

# 4. 第一次写包：写取数凭据时中断（模拟故障）
r1 = svc.write_package(pkg1_no, fail_at="credentials")
d1 = pkg_dir(pkg1_no)
print(f"\n4. 第一次写包（模拟写凭据中断）: {r1['message']}")
check("写包中断，状态=写入失败", not r1["ok"] and r1["status"] == "写入失败")
check("正文已落盘、凭据未落盘",
      (d1 / "body.json").is_file() and not (d1 / "credentials.json").is_file())
check("中断可续做", r1.get("resumable") is True)

# 5. 导出写文件尚未完成，主管发出更正金额的正式版本：100 → 90
v2 = svc.issue_report(
    app_no, amount=90, conclusion="更正：确认营收金额为 90 万元",
    metrics=[{"name": "营收金额", "value": 90}], issued_by="报表主管-李")
iss2 = v2["issuance"]["issuance_no"]
v1_row = db.fetchone("SELECT * FROM report_issuances WHERE issuance_no=?", [iss1])
print(f"\n5. 更正签发: {iss2}（金额 90），{iss1} 状态 → {v1_row['status']}")
check("更正版为新签发号", v2["ok"] and iss2 != iss1 and v2["issuance"]["version_no"] == 2)
check("更正链完整（V02 更正自 V01）", v2["issuance"]["supersedes_issuance_no"] == iss1)
check("旧版本标记已被更正且金额仍是 100",
      v1_row["status"] == "已被更正" and v1_row["amount"] == 100)

# 6. 重试续做：完成交付
r2 = svc.write_package(pkg1_no)
print(f"\n6. 重试续做: {r2['message']}")
check("重试后交付成功", r2["ok"] and r2["status"] == "已交付" and r2["attempts"] == 2)

body1 = read_json(d1 / "body.json")
creds1 = read_json(d1 / "credentials.json")
print(f"   正文签发号={body1['签发号']} 金额={body1['金额']}；"
      f"凭据签发号={creds1['签发号']} 合计={creds1['取数合计金额']}")
check("正文与凭据同属一个签发版本",
      body1["签发号"] == creds1["签发号"] == iss1,
      f"同为 {iss1}，未被更正版 {iss2} 混入")
check("结论与取数凭据金额一致（同为 100 或同为 90）",
      body1["金额"] == creds1["取数合计金额"] == 100,
      f"同为 {body1['金额']}")

# 7. 审计员领取包，按交付版本核对金额
verify1 = svc.verify_package(pkg1_no)
print("\n7. 审计员核对:")
for c in verify1["checks"]:
    print(f"   {'✅' if c['通过'] else '❌'} {c['检查项']}: {c['说明']}")
check("核对全部通过", verify1["ok"])
check("按交付版本核对金额为 100", verify1["交付版本"]["金额"] == 100)
check("文件依据可核验（sha256 齐备）",
      all(len(v) == 64 for k, v in verify1["文件依据"].items() if k.endswith("sha256")))
other = verify1["其他已签发版本"]
check("可见另一已签发版本与本次申请的关系",
      len(other) == 1 and other[0]["签发号"] == iss2
      and other[0]["金额"] == 90 and other[0]["与本包关系"] == "后续更正版本",
      f"{other[0]['签发号']} 金额 {other[0]['金额']}（{other[0]['与本包关系']}）" if other else "无")

# 8. 已交付的包不能被之后的更正覆盖
r3 = svc.write_package(pkg1_no)
print(f"\n8. 已交付包再次写制: {r3['message']}")
check("已交付包拒绝重写", not r3["ok"] and r3["status"] == "已交付")
pkg1_hashes = {f: sha(d1 / f) for f in ("body.json", "credentials.json", "manifest.json")}

# 9. 更正版本只能通过新包交付（先模拟写清单中断再续做）
pkg2 = svc.create_package(app_no, issuance_no=iss2)
pkg2_no = pkg2["package"]["package_no"]
r4 = svc.write_package(pkg2_no, fail_at="manifest")
r5 = svc.write_package(pkg2_no)
d2 = pkg_dir(pkg2_no)
body2 = read_json(d2 / "body.json")
creds2 = read_json(d2 / "credentials.json")
print(f"\n9. 更正版新包: {pkg2_no}；写清单中断续做后 {r5['message']}")
check("更正只能走新包", pkg2_no != pkg1_no and pkg2["package"]["issuance_no"] == iss2)
check("写清单中断也可续做", not r4["ok"] and r4["status"] == "写入失败" and r5["ok"])
check("新包正文与凭据同为 90",
      body2["金额"] == creds2["取数合计金额"] == 90 and body2["签发号"] == iss2)

# 10. 旧下载未被覆盖，可比较原始摘要
pkg1_hashes_now = {f: sha(d1 / f) for f in ("body.json", "credentials.json", "manifest.json")}
print(f"\n10. 旧包摘要比较: V01 金额 {body1['金额']} vs V02 金额 {body2['金额']}")
check("旧包文件交付后未被改动", pkg1_hashes == pkg1_hashes_now)
check("原始摘要可比较（100 → 90，差异 -10）",
      body1["金额"] - body2["金额"] == 10)

# 11. 数据报告按导出文件记录的签发号关联那次的报告结果
manifest1 = read_json(d1 / "manifest.json")
out1 = report_svc.output_by_issuance(manifest1["签发号"])
out2 = report_svc.output_by_issuance(iss2)
print(f"\n11. 数据报告关联: 导出文件记录签发号 {manifest1['签发号']} → "
      f"report_output #{out1.get('report_output_id')}")
check("按签发号关联到初版报告结果（金额 100）",
      out1["ok"] and out1["content"]["summary"]["amount"] == 100)
check("按签发号关联到更正版报告结果（金额 90）",
      out2["ok"] and out2["content"]["summary"]["amount"] == 90)
check("两次签发各自冻结了报告输出",
      out1["report_output_id"] != out2["report_output_id"])

# 12. 主管复核：比较实际交付内容与后来发出的更正
review = svc.review_application(app_no)
print(f"\n12. 主管复核（申请 {app_no}）:")
for item in review["交付包复核"]:
    print(f"   包 {item['包号']}: 交付 {item.get('交付签发号')} 金额 {item.get('交付金额')}"
          f" → 最新 {item.get('当前最新签发号')} 金额 {item.get('当前最新金额')}"
          f" 差异 {item.get('金额差异')} 被更正={item.get('交付后是否被更正')}")
rev1 = review["交付包复核"][0]
check("复核可见交付后被更正及金额差异",
      rev1["交付后是否被更正"] is True and rev1["金额差异"] == -10)
check("复核可见两个交付包", len(review["交付包复核"]) == 2)

# ============================================================================
print("\n== 场景二：更正先于打包，重试后正文与凭据同为 90 ==\n")

app2 = svc.accept_application("2026年度成本审计", applicant="审计员-赵")
app2_no = app2["application_no"]
svc.issue_report(app2_no, amount=100, conclusion="初版 100",
                 metrics=[{"name": "成本金额", "value": 100}])
svc.issue_report(app2_no, amount=90, conclusion="更正 90",
                 metrics=[{"name": "成本金额", "value": 90}])
pkg3 = svc.create_package(app2_no)  # 默认锁定最新签发版本（90）
pkg3_no = pkg3["package"]["package_no"]
svc.write_package(pkg3_no, fail_at="credentials")  # 写凭据中断
svc.write_package(pkg3_no)  # 重试续做
d3 = pkg_dir(pkg3_no)
body3 = read_json(d3 / "body.json")
creds3 = read_json(d3 / "credentials.json")
print(f"包 {pkg3_no} 锁定 {pkg3['package']['issuance_no']}；"
      f"正文 {body3['金额']} / 凭据 {creds3['取数合计金额']}")
check("中断重试后结论与取数凭据同为 90",
      body3["金额"] == creds3["取数合计金额"] == 90)
check("正文与凭据签发号同源",
      body3["签发号"] == creds3["签发号"] == pkg3["package"]["issuance_no"])
verify3 = svc.verify_package(pkg3_no)
check("场景二核对全部通过", verify3["ok"])

# ============================================================================
print("\n== 汇总 ==")
failed = [r for r in RESULTS if not r[0]]
print(f"通过 {len(RESULTS) - len(failed)}/{len(RESULTS)} 项检查")
if failed:
    for _, name, detail in failed:
        print(f"  失败: {name} {detail}")
    sys.exit(1)
print("全部验收检查通过 ✅")
print(f"（隔离数据保留于 {WORK_DIR}，可手工查验包文件）")
