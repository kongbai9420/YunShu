"""命令巡检接口（4.4 P21）。

权限：只读账号可以查看；运维与管理员可以建巡检任务、立即执行；
自定义模板只有管理员能建改删，保存时拦截危险命令并要输入模板名称确认；内置模板只读。
巡检任务里的设备必须都在发起人的资产范围内；结果按查看人的资产范围过滤。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit
from app.api.deps import admin_only, editor, get_db, reader
from app.command_inspection import VERDICTS, job_assets, next_run, start_run, validate_schedule
from app.inspection_templates import CHECK_TYPES, PLATFORMS, validate_items
from app.models import Asset, InspectionJob, InspectionResult, InspectionRun, InspectionTemplate, User
from app.scope import can_see, require_asset, restrict, scope_for

router = APIRouter(prefix="/api/command-inspection", tags=["command-inspection"])
MAX_ASSETS = 200


def _dt(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def _who(user: User) -> str:
    return user.display_name or user.username


# ---- 模板 ----


def _template_out(t: InspectionTemplate, *, items: bool = True) -> dict[str, Any]:
    out = {"id": str(t.id), "name": t.name, "platform": t.platform, "platform_label": PLATFORMS.get(t.platform, t.platform),
           "description": t.description, "builtin": t.builtin_key is not None, "item_count": len(t.items), "revision": t.revision,
           "updated_by": t.updated_by, "updated_at": _dt(t.updated_at)}
    if items:
        out["items"] = t.items
    return out


def _template(db: Session, template_id: uuid.UUID) -> InspectionTemplate:
    t = db.get(InspectionTemplate, template_id)
    if t is None or t.deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "巡检模板不存在")
    return t


@router.get("/templates")
def list_templates(_: User = Depends(reader), db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.scalars(select(InspectionTemplate).where(InspectionTemplate.deleted.is_(False)))
    order = list(PLATFORMS)
    # 内置在前，按平台（Linux、PVE、华为、H3C）；自定义按名称
    rows = sorted(rows, key=lambda t: (t.builtin_key is None, order.index(t.platform) if t.builtin_key and t.platform in order else 0, t.name))
    return {"items": [_template_out(t) for t in rows], "platforms": PLATFORMS, "check_types": list(CHECK_TYPES)}


class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    platform: str
    description: str = Field(default="", max_length=1000)
    items: list[dict[str, Any]] = Field(max_length=50)
    confirm: str = ""
    revision: int | None = None


def _checked(body: TemplateIn) -> list[dict[str, Any]]:
    items, problems = validate_items(body.items, body.platform)
    if problems:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"message": "模板没有保存：" + problems[0], "problems": problems})
    if body.confirm.strip() != body.name.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"message": "请输入模板名称确认：自定义命令会在设备上执行"})
    return items


@router.post("/templates", status_code=status.HTTP_201_CREATED)
def create_template(body: TemplateIn, actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> dict[str, Any]:
    items = _checked(body)
    t = InspectionTemplate(name=body.name.strip(), platform=body.platform, description=body.description.strip(), items=items,
                           updated_by=_who(actor), updated_at=datetime.now(UTC))
    db.add(t)
    db.flush()
    audit.record(db, actor, "inspection.template.create", target_type="inspection_template", target_id=t.id,
                 summary=f"新建巡检模板“{t.name}”（{len(items)} 条检查项）", detail={"commands": [i["command"] for i in items]})
    return _template_out(t)


@router.put("/templates/{template_id}")
def update_template(template_id: uuid.UUID, body: TemplateIn, actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> dict[str, Any]:
    t = _template(db, template_id)
    if t.builtin_key:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "内置模板只读：请复制一份再修改")
    if body.revision is not None and body.revision != t.revision:
        raise HTTPException(status.HTTP_409_CONFLICT, "模板已被别人修改，请刷新后再改")
    items = _checked(body)
    t.name, t.platform, t.description, t.items = body.name.strip(), body.platform, body.description.strip(), items
    t.revision, t.updated_by, t.updated_at = t.revision + 1, _who(actor), datetime.now(UTC)
    audit.record(db, actor, "inspection.template.update", target_type="inspection_template", target_id=t.id,
                 summary=f"修改巡检模板“{t.name}”（{len(items)} 条检查项）", detail={"commands": [i["command"] for i in items]})
    return _template_out(t)


@router.delete("/templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_template(template_id: uuid.UUID, actor: User = Depends(admin_only), db: Session = Depends(get_db)) -> None:
    t = _template(db, template_id)
    if t.builtin_key:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "内置模板不能删除")
    used = db.scalar(select(func.count()).select_from(InspectionJob).where(InspectionJob.template_id == t.id, InspectionJob.deleted.is_(False)))
    if used:
        raise HTTPException(status.HTTP_409_CONFLICT, f"还有 {used} 个巡检任务在用这个模板，请先删除或改用其他模板")
    t.deleted = True
    audit.record(db, actor, "inspection.template.delete", target_type="inspection_template", target_id=t.id, summary=f"删除巡检模板“{t.name}”")


# ---- 巡检任务 ----


def _assets_in_scope(db: Session, actor: User, ids: list[uuid.UUID]) -> list[Asset]:
    scope = scope_for(db, actor)
    for aid in ids:
        require_asset(db, scope, aid)
    return list(db.scalars(select(Asset).where(Asset.id.in_(ids)).order_by(Asset.name))) if ids else []


def _job_out(db: Session, j: InspectionJob, visible: Any) -> dict[str, Any]:
    t = db.get(InspectionTemplate, j.template_id)
    ids = [uuid.UUID(x) for x in j.asset_ids]
    # 回收站里的资产仍留在任务里（恢复后继续巡检），名称后注明；执行时按生命周期跳过
    names = {i: n + ("（在回收站）" if d is not None else "") for i, n, d in db.execute(
        select(Asset.id, Asset.name, Asset.deleted_at).where(Asset.id.in_(ids)))} if ids else {}
    assets = [{"id": str(a), "name": names.get(a, "（已删除）")} for a in ids if visible(a)]
    return {"id": str(j.id), "name": j.name, "template_id": str(j.template_id), "template_name": t.name if t else "",
            "platform": t.platform if t else "", "assets": assets, "asset_total": len(ids), "schedule": j.schedule, "enabled": j.enabled,
            "next_run_at": _dt(j.next_run_at), "last_run_at": _dt(j.last_run_at), "created_by": j.created_by, "revision": j.revision}


def _visible(db: Session, user: User):  # type: ignore[no-untyped-def]
    scope = scope_for(db, user)
    return (lambda _a: True) if scope.all else (lambda a: can_see(db, scope, a) or can_see(db, scope, a, deleted=True))


@router.get("/jobs")
def list_jobs(user: User = Depends(reader), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    visible = _visible(db, user)
    jobs = db.scalars(select(InspectionJob).where(InspectionJob.deleted.is_(False)).order_by(InspectionJob.created_at.desc()))
    out = [_job_out(db, j, visible) for j in jobs]
    return [j for j in out if j["assets"] or scope_for(db, user).all]


class JobIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    template_id: uuid.UUID
    asset_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_ASSETS)
    schedule: dict[str, Any]
    enabled: bool = True
    revision: int | None = None


def _apply_job(db: Session, actor: User, j: InspectionJob, body: JobIn, now: datetime) -> tuple[InspectionTemplate, list[Asset]]:
    t = _template(db, body.template_id)
    assets = _assets_in_scope(db, actor, list(dict.fromkeys(body.asset_ids)))
    try:
        schedule = validate_schedule(body.schedule)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    if schedule["kind"] == "once" and schedule.get("at") and next_run(schedule, now) is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "指定的执行时间已经过去")
    j.name, j.template_id, j.asset_ids, j.schedule, j.enabled = body.name.strip(), t.id, [str(a.id) for a in assets], schedule, body.enabled
    j.next_run_at = next_run(schedule, now) if body.enabled else None
    return t, assets


@router.post("/jobs", status_code=status.HTTP_201_CREATED)
def create_job(body: JobIn, actor: User = Depends(editor), db: Session = Depends(get_db)) -> dict[str, Any]:
    now = datetime.now(UTC)
    j = InspectionJob(created_by=_who(actor), created_at=now)
    t, assets = _apply_job(db, actor, j, body, now)
    db.add(j)
    db.flush()
    run_id = None
    if j.schedule["kind"] == "once" and not j.schedule.get("at") and j.enabled:
        # 单次、立即执行
        run_id = start_run(db, t, [a for a in assets if a.lifecycle == "active"], by=_who(actor), job=j, actor=actor, now=now).id
        j.enabled, j.last_run_at, j.next_run_at = False, now, None
    audit.record(db, actor, "inspection.job.create", target_type="inspection_job", target_id=j.id,
                 summary=f"新建巡检任务“{j.name}”（模板“{t.name}”，{len(assets)} 台）")
    return _job_out(db, j, lambda _a: True) | {"run_id": str(run_id) if run_id else None}


def _job(db: Session, job_id: uuid.UUID) -> InspectionJob:
    j = db.get(InspectionJob, job_id)
    if j is None or j.deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "巡检任务不存在")
    return j


def _require_job_scope(db: Session, actor: User, j: InspectionJob) -> None:
    scope = scope_for(db, actor)
    # 范围内回收站里的设备也算范围内，否则删除一台后任务就改不了了
    if not scope.all and not all(can_see(db, scope, uuid.UUID(a)) or can_see(db, scope, uuid.UUID(a), deleted=True) for a in j.asset_ids):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "这个巡检任务包含你范围外的设备，不能修改或执行")


@router.put("/jobs/{job_id}")
def update_job(job_id: uuid.UUID, body: JobIn, actor: User = Depends(editor), db: Session = Depends(get_db)) -> dict[str, Any]:
    j = _job(db, job_id)
    _require_job_scope(db, actor, j)
    if body.revision is not None and body.revision != j.revision:
        raise HTTPException(status.HTTP_409_CONFLICT, "巡检任务已被别人修改，请刷新后再改")
    t, assets = _apply_job(db, actor, j, body, datetime.now(UTC))
    j.revision += 1
    audit.record(db, actor, "inspection.job.update", target_type="inspection_job", target_id=j.id,
                 summary=f"修改巡检任务“{j.name}”（模板“{t.name}”，{len(assets)} 台，{'启用' if j.enabled else '停用'}）")
    return _job_out(db, j, lambda _a: True)


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job(job_id: uuid.UUID, actor: User = Depends(editor), db: Session = Depends(get_db)) -> None:
    j = _job(db, job_id)
    _require_job_scope(db, actor, j)
    j.deleted, j.enabled, j.next_run_at = True, False, None
    audit.record(db, actor, "inspection.job.delete", target_type="inspection_job", target_id=j.id, summary=f"删除巡检任务“{j.name}”")


@router.post("/jobs/{job_id}/run", status_code=status.HTTP_202_ACCEPTED)
def run_job(job_id: uuid.UUID, actor: User = Depends(editor), db: Session = Depends(get_db)) -> dict[str, Any]:
    j = _job(db, job_id)
    _require_job_scope(db, actor, j)
    t = _template(db, j.template_id)
    run = start_run(db, t, job_assets(db, j), by=_who(actor), job=j, actor=actor)
    j.last_run_at = run.created_at
    return {"run_id": str(run.id)}


class RunIn(BaseModel):
    template_id: uuid.UUID
    asset_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_ASSETS)


@router.post("/run", status_code=status.HTTP_202_ACCEPTED)
def run_now(body: RunIn, actor: User = Depends(editor), db: Session = Depends(get_db)) -> dict[str, Any]:
    t = _template(db, body.template_id)
    assets = [a for a in _assets_in_scope(db, actor, list(dict.fromkeys(body.asset_ids))) if a.lifecycle == "active"]
    if not assets:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "所选设备都不在“在用”状态")
    return {"run_id": str(start_run(db, t, assets, by=_who(actor), actor=actor).id)}


# ---- 执行与结果 ----


def _run_out(r: InspectionRun) -> dict[str, Any]:
    return {"id": str(r.id), "job_id": str(r.job_id) if r.job_id else None, "name": r.name, "template_name": r.template_name,
            "platform": r.platform, "triggered_by": r.triggered_by, "created_at": _dt(r.created_at), "finished_at": _dt(r.finished_at),
            "status": r.status, "asset_total": r.asset_total, "assets_done": r.assets_done, "counts": r.counts}


@router.get("/runs")
def list_runs(job_id: uuid.UUID | None = None, limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0),
              user: User = Depends(reader), db: Session = Depends(get_db)) -> dict[str, Any]:
    stmt = select(InspectionRun)
    scope = scope_for(db, user)
    if not scope.all:
        stmt = stmt.where(InspectionRun.id.in_(restrict(select(InspectionResult.run_id), InspectionResult.asset_id, scope)))
    if job_id:
        stmt = stmt.where(InspectionRun.job_id == job_id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(InspectionRun.created_at.desc()).limit(limit).offset(offset))
    return {"total": total, "items": [_run_out(r) for r in rows]}


@router.get("/runs/{run_id}")
def get_run(run_id: uuid.UUID, verdict: str = "", user: User = Depends(reader), db: Session = Depends(get_db)) -> dict[str, Any]:
    r = db.get(InspectionRun, run_id)
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "巡检记录不存在")
    stmt = restrict(select(InspectionResult), InspectionResult.asset_id, scope_for(db, user)).where(InspectionResult.run_id == run_id)
    if verdict in VERDICTS:
        stmt = stmt.where(InspectionResult.verdict == verdict)
    results = db.scalars(stmt.order_by(InspectionResult.asset_name, InspectionResult.observed_at)).all()
    order = {i["key"]: n for n, i in enumerate(r.items)}
    results = sorted(results, key=lambda x: (x.asset_name, order.get(x.item_key, -1)))
    return _run_out(r) | {"results": [{
        "id": str(x.id), "asset_id": str(x.asset_id), "asset_name": x.asset_name, "item_key": x.item_key, "item_name": x.item_name,
        "command": x.command, "verdict": x.verdict, "severity": x.severity, "detail": x.detail, "exit_code": x.exit_code,
        "observed_at": _dt(x.observed_at), "output_size": len(x.output)} for x in results]}


@router.get("/results/{result_id}")
def get_result(result_id: uuid.UUID, user: User = Depends(reader), db: Session = Depends(get_db)) -> dict[str, Any]:
    x = db.get(InspectionResult, result_id)
    if x is None or not can_see(db, scope_for(db, user), x.asset_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "巡检结果不存在")
    prev = db.scalar(select(InspectionResult).join(InspectionRun, InspectionRun.id == InspectionResult.run_id)
                     .where(InspectionResult.asset_id == x.asset_id, InspectionResult.item_key == x.item_key, InspectionResult.item_key != "",
                            InspectionResult.observed_at < x.observed_at,
                            InspectionRun.template_id == select(InspectionRun.template_id).where(InspectionRun.id == x.run_id).scalar_subquery())
                     .order_by(InspectionResult.observed_at.desc()).limit(1))
    return {"id": str(x.id), "asset_name": x.asset_name, "item_name": x.item_name, "command": x.command, "output": x.output,
            "exit_code": x.exit_code, "verdict": x.verdict, "severity": x.severity, "detail": x.detail, "observed_at": _dt(x.observed_at),
            "previous": {"output": prev.output, "verdict": prev.verdict, "observed_at": _dt(prev.observed_at)} if prev else None}
