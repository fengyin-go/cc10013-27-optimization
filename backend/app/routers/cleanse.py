"""数据清洗接口：规则清单、清洗数据集、版本重算、待处理队列与版本差异。

路由顺序上把 /export、/queue 这类固定路径放在 /{entry_id} 之前，避免被当成
整数主键解析（旧实现里 /export 就因此返回 422）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.cleanse import (
    RULE_CODES,
    CleanseService,
    cleanse_service,
)
from app.services.cleanse_rules import current_version, versions_for

router = APIRouter(prefix="/api/cleanse", tags=["数据清洗"])

service: CleanseService = cleanse_service

LIST_FIELDS = ["规则编号", "目标字段", "空值替换", "格式规范化", "异常值剔除", "去重键", "规则版本", "生效日期"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


def _resolve_version(rule_code: str, version: str | None) -> str:
    if rule_code not in RULE_CODES:
        raise HTTPException(status_code=404, detail=f"清洗规则 {rule_code} 不存在或已归档")
    if version:
        if version not in {rule.version for rule in versions_for(rule_code)}:
            raise HTTPException(
                status_code=404, detail=f"清洗规则 {rule_code} 不存在版本 {version}"
            )
        return version
    return current_version(rule_code).version


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤数据清洗规则；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


# ------------------------------------------------------------------ 版本与重算
@router.get("/versions", response_model=dict)
def version_overview() -> dict[str, Any]:
    """概览：各规则版本的窗口处理量、与上一版本的前后差异，以及冲突/队列数量。"""
    return service.rule_overview()


@router.get("/runs", response_model=dict)
def list_runs(rule_code: str | None = None) -> dict[str, Any]:
    """重算批次列表：看到每个版本做到哪一步、停在哪个失败窗口。"""
    return {"items": service.runs(rule_code)}


@router.get("/windows", response_model=dict)
def list_windows(rule_code: str = Query(...), version: str | None = None) -> dict[str, Any]:
    """按去重窗口查看处理状态与处理量，便于定位中断窗口。"""
    resolved = _resolve_version(rule_code, version)
    return {
        "rule_code": rule_code,
        "version": resolved,
        "items": service.windows(rule_code, resolved),
    }


@router.post("/recompute", response_model=ActionResult)
def recompute(payload: EntryPayload) -> ActionResult:
    """触发重算：可只跑 batch 个窗口或指定失败窗口；重复提交幂等。"""
    rule_code = str(payload.values.get("rule_code") or "").strip()
    version = str(payload.values.get("version") or "").strip() or None
    batch = payload.values.get("batch")
    fail_window = payload.values.get("fail_window")
    if not rule_code:
        return ActionResult(ok=False, message="缺少规则编号，无法重算")
    resolved = _resolve_version(rule_code, version)
    try:
        batch_size = int(batch) if batch not in (None, "") else None
    except (TypeError, ValueError):
        return ActionResult(ok=False, message=f"窗口批次「{batch}」不是整数")
    run, message = service.recompute(
        rule_code,
        resolved,
        batch=batch_size,
        fail_window=str(fail_window) if fail_window else None,
    )
    if run is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=run["status"] == "已完成", message=message, entry=run)


# ------------------------------------------------------------------ 数据集
@router.get("/dataset", response_model=dict)
def dataset(
    rule_code: str = Query(...), version: str | None = None, page: int = 1, size: int = 50
) -> dict[str, Any]:
    """按单一规则版本读取清洗数据集；未完成全部窗口时拒绝返回半套口径。"""
    resolved = _resolve_version(rule_code, version)
    try:
        payload = service.dataset(rule_code, resolved)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    start = max(page - 1, 0) * size
    return {
        **{key: value for key, value in payload.items() if key != "items"},
        "items": payload["items"][start : start + size],
        "total": len(payload["items"]),
        "page": page,
        "size": size,
    }


@router.get("/dataset/export", response_model=dict)
def export_dataset(rule_code: str = Query(...), version: str | None = None) -> dict[str, Any]:
    """数据导出：只读取一个规则版本的完整清洗结果，不拼接其他版本。"""
    resolved = _resolve_version(rule_code, version)
    try:
        return service.export_dataset(rule_code, resolved)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/export", response_model=dict)
def export_entries(rule_code: str = Query(...), version: str | None = None) -> dict[str, Any]:
    """导出清洗数据集；与数据集读取使用同一规则版本。"""
    resolved = _resolve_version(rule_code, version)
    try:
        return service.export_dataset(rule_code, resolved)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


# ------------------------------------------------------------------ 待处理队列
@router.get("/queue", response_model=PageResult[dict])
def list_queue(
    rule_code: str | None = None,
    version: str | None = None,
    window: str | None = None,
    reason: str | None = None,
    run_id: str | None = None,
    status: str | None = Query(default=None, description="待处理、已处理"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """清洗待处理队列：筛选重算产生的重复剔除记录，可按版本/窗口/批次过滤。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_queue(
        rule_code=rule_code,
        version=version,
        window=window,
        reason=reason,
        run_id=run_id,
        status=status,
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/queue/export", response_model=dict)
def export_queue(
    rule_code: str | None = None,
    version: str | None = None,
    window: str | None = None,
    reason: str | None = None,
    run_id: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """导出队列详情：每条剔除记录都跟随展示前后规则版本的差异。"""
    items = service.queue_detail(
        rule_code=rule_code,
        version=version,
        window=window,
        reason=reason,
        run_id=run_id,
        status=status,
    )
    return {
        "module": "cleanse",
        "dataset": "cleanse_queue",
        "rule_code": rule_code,
        "rule_version": version,
        "total": len(items),
        "items": items,
    }


@router.get("/queue/{queue_id}", response_model=dict)
def queue_item(queue_id: str) -> dict[str, Any]:
    """单条剔除记录详情：目标字段原值与前后版本差异。"""
    for item in service.list_queue(page=1, size=100000)[0]:
        if item["queue_id"] == queue_id:
            return service.queue_item_detail(item)
    raise HTTPException(status_code=404, detail=f"待处理记录 {queue_id} 不存在")


@router.post("/queue/{queue_id}/actions", response_model=ActionResult)
def queue_action(queue_id: str, payload: EntryPayload) -> ActionResult:
    """标记或恢复待处理记录；不存在的记录给出可读说明。"""
    action = str(payload.values.get("action") or "").strip()
    item, message = service.handle_queue_item(queue_id, action)
    if item is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=item)


# ------------------------------------------------------------------ 版本差异
@router.get("/diff", response_model=dict)
def version_diff(
    rule_code: str = Query(...),
    from_version: str | None = None,
    to_version: str | None = None,
) -> dict[str, Any]:
    """对比两个规则版本：字段级差异只列版本与去重数量，明细可回到目标字段原值。"""
    resolved_to = _resolve_version(rule_code, to_version)
    if from_version:
        resolved_from = _resolve_version(rule_code, from_version)
    else:
        versions = [rule.version for rule in versions_for(rule_code)]
        index = versions.index(resolved_to)
        if index == 0:
            raise HTTPException(status_code=400, detail="没有更早的规则版本可供对比")
        resolved_from = versions[index - 1]
    payload = service.diff(rule_code, resolved_from, resolved_to)
    if payload is None:
        raise HTTPException(
            status_code=409,
            detail=f"规则 {rule_code} 的版本 {resolved_from} 或 {resolved_to} 尚未完成重算",
        )
    return payload


# ------------------------------------------------------------------ 冲突取舍
@router.get("/conflicts", response_model=dict)
def list_conflicts(rule_code: str | None = None) -> dict[str, Any]:
    """已发布清洗窗口收到更晚更正形成的冲突，以及当前取舍。"""
    items = service.conflicts(rule_code)
    return {"items": items, "total": len(items)}


@router.post("/conflicts/resolve", response_model=ActionResult)
def resolve_conflict(payload: EntryPayload) -> ActionResult:
    """对单条冲突选择保留原值或采用更正；取舍会重算受影响窗口的终态。"""
    source_id = str(payload.values.get("source_id") or "").strip()
    version = str(payload.values.get("version") or "").strip()
    decision = str(payload.values.get("decision") or "").strip()
    if not source_id or not version:
        return ActionResult(ok=False, message="缺少源记录编号或规则版本")
    item, message = service.resolve_conflict(source_id, version, decision)
    if item is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=item)


@router.get("/sources/{source_id}", response_model=dict)
def get_source(source_id: str) -> dict[str, Any]:
    """回看源记录：冻结版本、各版本清洗结果与目标字段原值。"""
    source = service.find_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail=f"源记录 {source_id} 不存在")
    return source


# ------------------------------------------------------------------ 规则明细
@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条清洗规则明细；不存在时给出可读的错误说明。"""
    entry = service.find_rule_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"清洗规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条清洗规则，缺字段时说明原因而不是静默丢弃。"""
    values = payload.values
    missing = [
        field
        for field in ["规则编号", "目标字段", "空值替换"]
        if not str(values.get(field) or "").strip()
    ]
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(
        ok=False,
        message="清洗规则登记入口尚未接入审批流，示例数据只读",
    )


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """规则动作：示例数据只读，重算请走 /recompute。"""
    entry = service.find_rule_entry(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=f"清洗规则 {entry_id} 不存在或已归档")
    action = str(payload.values.get("action") or "").strip()
    return ActionResult(
        ok=False,
        message=f"动作「{action}」不影响已冻结的源记录；请在清洗数据集页发起重算",
        entry=entry,
    )
