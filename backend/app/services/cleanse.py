"""数据清洗业务规则：清洗数据集、版本重算、待处理队列与版本差异。

处理入口只有这里：源记录（冻结清洗版本）→ 按去重窗口重算 → 唯一结果写入
清洗数据集，重复剔除记录写入清洗待处理队列；数据导出只读取同一规则版本的一份
完整结果，不在导出时重新拼接多个版本。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.cleanse_rules import (
    RULE_CODES,
    RULE_VERSIONS,
    RuleVersion,
    current_version,
    effective_input_value,
    evaluate_window,
    get_rule,
    previous_version,
    sample_sources,
    source_has_late_correction,
    versions_for,
    window_counts,
)

WINDOW_PENDING = "未开始"
WINDOW_RUNNING = "处理中"
WINDOW_DONE = "已完成"
WINDOW_FAILED = "失败"

QUEUE_PENDING = "待处理"
QUEUE_DONE = "已处理"

CONFLICT_KEEP_ORIGINAL = "保留原值"
CONFLICT_KEEP_CORRECTION = "采用更正"


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _queue_sort_key(item: dict[str, Any]) -> tuple[str, str, str, str]:
    return (item["rule_code"], item["version"], item["window"], item["source_id"])


class CleanseService:
    def __init__(self) -> None:
        # 源记录一旦入库就冻结清洗版本；重算只改变评估版本，不回写源记录。
        self._sources: dict[str, list[dict[str, Any]]] = sample_sources()
        self._frozen_version: dict[str, str] = {code: "R4" for code in RULE_CODES}
        self._active: dict[str, str] = {
            code: current_version(code).version for code in RULE_CODES
        }
        self._evaluations: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = {}
        self._window_states: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        self._queue: dict[str, dict[str, Any]] = {}
        self._runs: dict[str, dict[str, Any]] = {}
        self._conflicts: dict[tuple[str, str], str] = {}
        self._seed()

    # ------------------------------------------------------------------ 种子
    def _seed(self) -> None:
        """按历史顺序重算 R4、R5，让概览启动后就能看到各版本处理量与差异。"""

        for code in RULE_CODES:
            for rule in versions_for(code):
                self._recompute_version(rule, batch=None, fail_window=None)

    # ------------------------------------------------------------ 规则版本视图
    def rule_overview(self) -> dict[str, Any]:
        rules: list[dict[str, Any]] = []
        for code in RULE_CODES:
            versions: list[dict[str, Any]] = []
            previous_counts: dict[str, int] | None = None
            for rule in versions_for(code):
                counts = self._version_counts(code, rule.version)
                states = self._states(code, rule.version)
                processed = sum(1 for state in states.values() if state["status"] == WINDOW_DONE)
                failed = [window for window, state in states.items() if state["status"] == WINDOW_FAILED]
                delta = None
                if previous_counts is not None:
                    delta = {
                        "input_rows": counts["input_rows"] - previous_counts["input_rows"],
                        "removed_rows": counts["removed_rows"] - previous_counts["removed_rows"],
                        "output_rows": counts["output_rows"] - previous_counts["output_rows"],
                    }
                versions.append(
                    {
                        **{
                            key: value
                            for key, value in rule.as_row().items()
                            if key not in {"windows", "published_at"}
                        },
                        "window_total": len(rule.windows),
                        "window_done": processed,
                        "failed_windows": failed,
                        **counts,
                        "delta_vs_previous": delta,
                    }
                )
                previous_counts = counts
            rules.append(
                {
                    "rule_code": code,
                    "target_field": versions[0]["target_field"] if versions else "",
                    "frozen_version": self._frozen_version[code],
                    "active_version": self._active[code],
                    "conflicts": sum(
                        1 for item in self.conflicts(code)
                    ),
                    "queue_pending": sum(
                        1
                        for item in self._queue.values()
                        if item["rule_code"] == code and item["status"] == QUEUE_PENDING
                    ),
                    "versions": versions,
                }
            )
        return {
            "rules": rules,
            "cards": [
                {"label": "清洗规则版本", "value": sum(len(rule["versions"]) for rule in rules)},
                {"label": "已完成窗口", "value": sum(
                    version["window_done"] for rule in rules for version in rule["versions"]
                )},
                {"label": "待处理剔除", "value": sum(
                    1 for item in self._queue.values() if item["status"] == QUEUE_PENDING
                )},
                {"label": "版本冲突", "value": len(self.conflicts())},
            ],
        }

    def overview(self) -> dict[str, Any]:
        return self.rule_overview()

    # -------------------------------------------------------------- 重算主流程
    def recompute(
        self,
        rule_code: str,
        version: str,
        *,
        batch: int | None = None,
        fail_window: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        rule = get_rule(rule_code, version)
        if rule is None:
            return None, f"清洗规则 {rule_code} 不存在版本 {version}"
        finished = self._finished_run(rule_code, version)
        if finished is not None and fail_window is None:
            # 幂等：同一版本重复提交直接复用已完成结果，不再产生剔除记录。
            return finished, f"规则 {rule_code} 版本 {version} 已完成重算，未重复剔除"
        if finished is not None and fail_window is not None:
            # 显式指定中断窗口时，先把该窗口及之后恢复到未开始，用于演练续做。
            self._reset_from_window(rule, fail_window)
        run, message = self._recompute_version(rule, batch=batch, fail_window=fail_window)
        return run, message

    def _reset_from_window(self, rule: RuleVersion, fail_window: str) -> None:
        states = self._states(rule.rule_code, rule.version)
        if fail_window not in states:
            return
        started = False
        for window in rule.windows:
            if window == fail_window:
                started = True
            if not started:
                continue
            states[window].update(
                {
                    "status": WINDOW_PENDING,
                    "input_rows": 0,
                    "removed_rows": 0,
                    "output_rows": 0,
                    "run_id": None,
                    "error": None,
                    "finished_at": None,
                }
            )
            self._evaluations.get((rule.rule_code, rule.version), {}).pop(window, None)
            for queue_id, item in list(self._queue.items()):
                if (item["rule_code"], item["version"], item["window"]) == (
                    rule.rule_code,
                    rule.version,
                    window,
                ):
                    self._queue.pop(queue_id, None)
        run = self._runs.get(f"RUN-{rule.rule_code}-{rule.version}")
        if run is not None:
            run["status"] = "处理中"
            run["finished_at"] = None

    def _recompute_version(
        self,
        rule: RuleVersion,
        *,
        batch: int | None,
        fail_window: str | None,
    ) -> tuple[dict[str, Any], str]:
        states = self._states(rule.rule_code, rule.version)
        run = self._open_run(rule)
        already_done = any(state["status"] == WINDOW_DONE for state in states.values())
        resume_from = next(
            (window for window in rule.windows if states[window]["status"] in {WINDOW_FAILED, WINDOW_PENDING}),
            None,
        )
        if resume_from and already_done:
            run["resumed_from"] = resume_from

        processed = 0
        for window in rule.windows:
            state = states[window]
            if state["status"] == WINDOW_DONE:
                continue  # 已完成的窗口不重复处理，重复提交也不会二次剔除
            if batch is not None and processed >= batch:
                break
            if fail_window == window:
                state["status"] = WINDOW_FAILED
                state["error"] = "源窗口读取超时，重算已停在当前窗口"
                run["status"] = "失败"
                run["failed_window"] = window
                run["finished_at"] = _now()
                self._refresh_run_counts(run)
                return run, f"重算在窗口 {window} 中断，可从该窗口续做"

            state["status"] = WINDOW_RUNNING
            rows = evaluate_window(rule, self._sources[rule.rule_code], window=window)
            self._evaluations.setdefault((rule.rule_code, rule.version), {})[window] = rows
            counts = window_counts(rows)
            state.update(
                {
                    "status": WINDOW_DONE,
                    "error": None,
                    "input_rows": counts["input_rows"],
                    "removed_rows": counts["removed_rows"],
                    "output_rows": counts["output_rows"],
                    "run_id": run["run_id"],
                    "finished_at": _now(),
                }
            )
            self._refresh_queue_window(rule, window, run["run_id"])
            processed += 1

        pending = [w for w in rule.windows if states[w]["status"] != WINDOW_DONE]
        self._refresh_run_counts(run)
        if pending:
            run["status"] = "处理中"
            run["failed_window"] = next(
                (w for w in pending if states[w]["status"] == WINDOW_FAILED), None
            )
            run["finished_at"] = None
            return run, f"重算未完成，剩余窗口：{'、'.join(pending)}，可继续续做"

        run["status"] = "已完成"
        run["failed_window"] = None
        run["finished_at"] = _now()
        if rule.version == current_version(rule.rule_code).version:
            self._active[rule.rule_code] = rule.version
        return run, f"规则 {rule.rule_code} 版本 {rule.version} 重算完成"

    def _open_run(self, rule: RuleVersion) -> dict[str, Any]:
        run_id = f"RUN-{rule.rule_code}-{rule.version}"
        existing = self._runs.get(run_id)
        if existing is not None:
            if existing["status"] != "已完成":
                existing["status"] = "处理中"
                existing["finished_at"] = None
            return existing
        states = self._states(rule.rule_code, rule.version)
        run = {
            "run_id": run_id,
            "rule_code": rule.rule_code,
            "version": rule.version,
            "status": "处理中",
            "started_at": _now(),
            "finished_at": None,
            "failed_window": None,
            "resumed_from": None,
            "windows": list(rule.windows),
            "counts": {"input_rows": 0, "removed_rows": 0, "output_rows": 0},
        }
        self._runs[run_id] = run
        for window in rule.windows:
            states.setdefault(
                window,
                {
                    "window": window,
                    "status": WINDOW_PENDING,
                    "input_rows": 0,
                    "removed_rows": 0,
                    "output_rows": 0,
                    "run_id": None,
                    "error": None,
                    "finished_at": None,
                },
            )
        return run

    def _refresh_run_counts(self, run: dict[str, Any]) -> None:
        run["counts"] = self._version_counts(run["rule_code"], run["version"])

    def _finished_run(self, rule_code: str, version: str) -> dict[str, Any] | None:
        run = self._runs.get(f"RUN-{rule_code}-{version}")
        if run and run["status"] == "已完成":
            return run
        return None

    def runs(self, rule_code: str | None = None) -> list[dict[str, Any]]:
        runs = list(self._runs.values())
        if rule_code:
            runs = [run for run in runs if run["rule_code"] == rule_code]
        return sorted(runs, key=lambda run: (run["rule_code"], run["version"]))

    # ------------------------------------------------------------ 窗口与计数
    def _states(self, rule_code: str, version: str) -> dict[str, dict[str, Any]]:
        rule = get_rule(rule_code, version)
        windows = rule.windows if rule else ()
        return self._window_states.setdefault(
            (rule_code, version),
            {
                window: {
                    "window": window,
                    "status": WINDOW_PENDING,
                    "input_rows": 0,
                    "removed_rows": 0,
                    "output_rows": 0,
                    "run_id": None,
                    "error": None,
                    "finished_at": None,
                }
                for window in windows
            },
        )

    def _version_counts(self, rule_code: str, version: str) -> dict[str, int]:
        evaluations = self._evaluations.get((rule_code, version), {})
        totals = {"input_rows": 0, "removed_rows": 0, "output_rows": 0}
        for rows in evaluations.values():
            counts = window_counts(rows)
            for key in totals:
                totals[key] += counts[key]
        return totals

    def windows(self, rule_code: str, version: str) -> list[dict[str, Any]]:
        return list(self._states(rule_code, version).values())

    # ------------------------------------------------------------ 待处理队列
    def _refresh_queue_window(self, rule: RuleVersion, window: str, run_id: str) -> None:
        """按窗口刷新队列：重复剔除记录入院，重复执行不产生第二条。"""

        rows = self._evaluations.get((rule.rule_code, rule.version), {}).get(window, [])
        existing = {
            queue_id: item
            for queue_id, item in self._queue.items()
            if (item["rule_code"], item["version"], item["window"])
            == (rule.rule_code, rule.version, window)
        }
        seen: set[str] = set()
        for row in rows:
            if not row["removed"]:
                continue
            queue_id = f"Q-{rule.rule_code}-{rule.version}-{row['source_id']}"
            seen.add(queue_id)
            previous = self._queue.get(queue_id)
            self._queue[queue_id] = {
                "queue_id": queue_id,
                "rule_code": rule.rule_code,
                "version": rule.version,
                "run_id": (previous or {}).get("run_id") or run_id,
                "window": window,
                "source_id": row["source_id"],
                "target_field": row["target_field"],
                "original_value": row["original_value"],
                "corrected_value": row["corrected_value"],
                "input_value": row["input_value"],
                "cleaned_value": row["cleaned_value"],
                "dedup_key": row["dedup_key"],
                "removed": True,
                "removal_reason": row["removal_reason"],
                "kept_source_id": row["kept_source_id"],
                "frozen_version": row["frozen_version"],
                "status": (previous or {}).get("status") or QUEUE_PENDING,
                "created_at": (previous or {}).get("created_at") or _now(),
                "handled_at": (previous or {}).get("handled_at"),
            }
        for queue_id in existing:
            if queue_id not in seen:
                self._queue.pop(queue_id, None)

    def list_queue(
        self,
        *,
        rule_code: str | None = None,
        version: str | None = None,
        window: str | None = None,
        reason: str | None = None,
        run_id: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        items = list(self._queue.values())
        if rule_code:
            items = [item for item in items if item["rule_code"] == rule_code]
        if version:
            items = [item for item in items if item["version"] == version]
        if window:
            items = [item for item in items if item["window"] == window]
        if reason:
            items = [item for item in items if reason in str(item.get("removal_reason") or "")]
        if run_id:
            items = [item for item in items if item["run_id"] == run_id]
        if status:
            items = [item for item in items if item["status"] == status]
        items = sorted(items, key=_queue_sort_key)
        total = len(items)
        start = max(page - 1, 0) * size
        return items[start : start + size], total

    def queue_detail(
        self,
        *,
        rule_code: str | None = None,
        version: str | None = None,
        window: str | None = None,
        reason: str | None = None,
        run_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """导出详情：每条剔除记录都带上前后版本的取舍差异。"""

        items, _ = self.list_queue(
            rule_code=rule_code,
            version=version,
            window=window,
            reason=reason,
            run_id=run_id,
            status=status,
            page=1,
            size=200,
        )
        return [self.queue_item_detail(item) for item in items]

    def queue_item_detail(self, item: dict[str, Any]) -> dict[str, Any]:
        """单条剔除记录 + 前后版本差异，队列列表与导出详情共用。"""
        rule = get_rule(item["rule_code"], item["version"])
        before_rule = previous_version(item["rule_code"], item["version"]) if rule else None
        before = self._source_snapshot(item["rule_code"], before_rule, item["source_id"])
        after = self._source_snapshot(item["rule_code"], rule, item["source_id"])
        return {
            **item,
            "version_diff": {
                "from_version": before_rule.version if before_rule else None,
                "to_version": item["version"],
                "before": before,
                "after": after,
                "changed_fields": [
                    {
                        "field": "规则版本",
                        "before": before_rule.version if before_rule else None,
                        "after": item["version"],
                    },
                    {
                        "field": "去重数量",
                        "before": self._version_counts(item["rule_code"], before_rule.version)["removed_rows"]
                        if before_rule
                        else 0,
                        "after": self._version_counts(item["rule_code"], item["version"])["removed_rows"],
                    },
                ],
            },
        }

    def _source_snapshot(
        self, rule_code: str, rule: RuleVersion | None, source_id: str
    ) -> dict[str, Any] | None:
        if rule is None:
            return None
        rows = self._evaluations.get((rule_code, rule.version), {})
        for window_rows in rows.values():
            for row in window_rows:
                if row["source_id"] == source_id:
                    return {
                        "removed": row["removed"],
                        "cleaned_value": row["cleaned_value"],
                        "dedup_key": row["dedup_key"],
                        "removal_reason": row["removal_reason"],
                    }
        return None

    def handle_queue_item(self, queue_id: str, action: str) -> tuple[dict[str, Any] | None, str]:
        item = self._queue.get(queue_id)
        if item is None:
            return None, f"待处理记录 {queue_id} 不存在或不属于当前清洗数据集"
        if action not in {"标记已处理", "恢复待处理"}:
            return None, f"动作「{action}」不属于清洗待处理队列可执行范围"
        item["status"] = QUEUE_DONE if action == "标记已处理" else QUEUE_PENDING
        item["handled_at"] = _now() if action == "标记已处理" else None
        return item, f"待处理记录已{action}"

    # ------------------------------------------------------------ 清洗数据集
    def dataset(self, rule_code: str, version: str | None = None) -> dict[str, Any]:
        rule = get_rule(rule_code, version or self._active.get(rule_code, ""))
        if rule is None:
            raise ValueError(f"清洗规则 {rule_code} 不存在版本 {version}")
        self._require_complete(rule)
        evaluations = self._evaluations.get((rule_code, rule.version), {})
        items: list[dict[str, Any]] = []
        for window in rule.windows:
            for row in evaluations.get(window, []):
                if row["removed"]:
                    continue
                items.append(
                    {
                        "source_id": row["source_id"],
                        "window": row["window"],
                        "target_field": row["target_field"],
                        "original_value": row["original_value"],
                        "cleaned_value": row["cleaned_value"],
                        "dedup_key": row["dedup_key"],
                        "frozen_version": row["frozen_version"],
                        "rule_version": rule.version,
                        "conflict": row["conflict"],
                    }
                )
        counts = self._version_counts(rule_code, rule.version)
        return {
            "rule_code": rule_code,
            "target_field": rule.target_field,
            "rule_version": rule.version,
            "frozen_version": self._frozen_version[rule_code],
            "windows": list(rule.windows),
            "counts": counts,
            "items": items,
        }

    def _require_complete(self, rule: RuleVersion) -> None:
        states = self._states(rule.rule_code, rule.version)
        pending = [window for window, state in states.items() if state["status"] != WINDOW_DONE]
        if pending:
            raise ValueError(
                f"规则 {rule.rule_code} 版本 {rule.version} 尚未完成全部窗口（{ '、'.join(pending) }），"
                "导出只允许读取完整的一套口径"
            )

    def export_dataset(self, rule_code: str, version: str | None = None) -> dict[str, Any]:
        dataset = self.dataset(rule_code, version)
        versions = {item["rule_version"] for item in dataset["items"]}
        if versions and versions != {dataset["rule_version"]}:
            raise ValueError("清洗数据集混入了多个规则版本，已停止导出")
        return {
            "module": "cleanse",
            "dataset": "cleanse_dataset",
            "rule_code": dataset["rule_code"],
            "rule_version": dataset["rule_version"],
            "target_field": dataset["target_field"],
            "counts": dataset["counts"],
            "total": len(dataset["items"]),
            "items": dataset["items"],
        }

    # ------------------------------------------------------------ 源记录与冲突
    def find_source(self, source_id: str) -> dict[str, Any] | None:
        for rule_code, sources in self._sources.items():
            for source in sources:
                if source["source_id"] == source_id:
                    versions = []
                    for rule in versions_for(rule_code):
                        states = self._states(rule_code, rule.version)
                        if states and all(
                            state["status"] == WINDOW_DONE for state in states.values()
                        ):
                            versions.append(
                                {
                                    "version": rule.version,
                                    "evaluation": self._source_snapshot(
                                        rule_code, rule, source_id
                                    ),
                                }
                            )
                    return {
                        **source,
                        "rule_code": rule_code,
                        "target_field": RULE_VERSIONS[(rule_code, source["frozen_version"])].target_field,
                        "conflict": any(
                            source_has_late_correction(source, rule)
                            for rule in versions_for(rule_code)
                        ),
                        "resolutions": {
                            version: self._conflicts.get((source_id, version))
                            for version in [rule.version for rule in versions_for(rule_code)]
                        },
                        "versions": versions,
                    }
        return None

    def conflicts(self, rule_code: str | None = None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for code, sources in self._sources.items():
            if rule_code and code != rule_code:
                continue
            for source in sources:
                for rule in versions_for(code):
                    if not source_has_late_correction(source, rule):
                        continue
                    items.append(
                        {
                            "rule_code": code,
                            "version": rule.version,
                            "source_id": source["source_id"],
                            "window": source["window"],
                            "target_field": rule.target_field,
                            "frozen_version": source.get("frozen_version"),
                            "original_value": source.get("original_value"),
                            "corrected_value": source.get("corrected_value"),
                            "corrected_at": source.get("corrected_at"),
                            "published_at": rule.published_at,
                            "window_state": self._states(code, rule.version)
                            .get(source["window"], {})
                            .get("status"),
                            "decision": self._conflicts.get(
                                (source["source_id"], rule.version)
                            )
                            or CONFLICT_KEEP_ORIGINAL,
                            "effective_value": effective_input_value(source, rule),
                        }
                    )
        return sorted(
            items, key=lambda item: (item["rule_code"], item["version"], item["source_id"])
        )

    def resolve_conflict(
        self, source_id: str, version: str, decision: str
    ) -> tuple[dict[str, Any] | None, str]:
        if decision not in {CONFLICT_KEEP_ORIGINAL, CONFLICT_KEEP_CORRECTION}:
            return None, f"取舍「{decision}」不在允许范围内"
        source = None
        rule_code = None
        for code, sources in self._sources.items():
            for item in sources:
                if item["source_id"] == source_id:
                    source = item
                    rule_code = code
                    break
            if source:
                break
        if source is None or rule_code is None:
            return None, f"源记录 {source_id} 不存在"
        rule = get_rule(rule_code, version)
        if rule is None:
            return None, f"规则 {rule_code} 不存在版本 {version}"
        if not source_has_late_correction(source, rule):
            return None, f"源记录 {source_id} 在版本 {version} 下没有更晚更正冲突"
        self._conflicts[(source_id, version)] = decision
        source.setdefault("resolutions", {})[version] = (
            "keep_correction" if decision == CONFLICT_KEEP_CORRECTION else "keep_original"
        )
        # 取舍会改变终态：只重算受影响窗口，队列与数据集同步刷新。
        states = self._states(rule_code, version)
        for window, state in states.items():
            if state["status"] == WINDOW_DONE and source["window"] == window:
                rows = evaluate_window(rule, self._sources[rule_code], window=window)
                self._evaluations[(rule_code, version)][window] = rows
                counts = window_counts(rows)
                state.update(
                    {
                        "input_rows": counts["input_rows"],
                        "removed_rows": counts["removed_rows"],
                        "output_rows": counts["output_rows"],
                        "finished_at": _now(),
                    }
                )
                self._refresh_queue_window(
                    rule, window, state.get("run_id") or f"RUN-{rule_code}-{version}"
                )
        conflict = next(
            (
                item
                for item in self.conflicts(rule_code)
                if item["source_id"] == source_id and item["version"] == version
            ),
            None,
        )
        return conflict, f"源记录 {source_id} 在版本 {version} 的冲突已按「{decision}」处理"

    # -------------------------------------------------------------- 版本差异
    def diff(
        self, rule_code: str, from_version: str, to_version: str
    ) -> dict[str, Any] | None:
        before_rule = get_rule(rule_code, from_version)
        after_rule = get_rule(rule_code, to_version)
        if before_rule is None or after_rule is None:
            return None
        for rule in (before_rule, after_rule):
            if not self._evaluations.get((rule_code, rule.version)):
                return None
        before_counts = self._version_counts(rule_code, from_version)
        after_counts = self._version_counts(rule_code, to_version)
        before_rows = self._rows_by_source(rule_code, from_version)
        after_rows = self._rows_by_source(rule_code, to_version)

        changes: list[dict[str, Any]] = []
        for source_id in sorted(set(before_rows) | set(after_rows)):
            before = before_rows.get(source_id)
            after = after_rows.get(source_id)
            if before is None or after is None:
                continue
            removed_before = bool(before["removed"])
            removed_after = bool(after["removed"])
            value_changed = before["cleaned_value"] != after["cleaned_value"]
            key_changed = before["dedup_key"] != after["dedup_key"]
            if not (removed_before != removed_after or value_changed or key_changed):
                continue  # 未被规则命中的记录不进入差异，更不算作重复剔除
            if removed_after and not removed_before:
                change = "新增去重剔除"
            elif removed_before and not removed_after:
                change = "恢复保留"
            elif removed_before and removed_after:
                change = "去重结果不变"
            else:
                change = "版本规范化"
            changes.append(
                {
                    "source_id": source_id,
                    "window": after["window"],
                    "target_field": after["target_field"],
                    "original_value": after["original_value"],
                    "before_value": before["cleaned_value"],
                    "after_value": after["cleaned_value"],
                    "before_dedup_key": before["dedup_key"],
                    "after_dedup_key": after["dedup_key"],
                    "before_removed": removed_before,
                    "after_removed": removed_after,
                    "kept_source_id": after["kept_source_id"],
                    "change": change,
                }
            )

        new_removals = [item for item in changes if item["after_removed"] and not item["before_removed"]]
        return {
            "rule_code": rule_code,
            "target_field": after_rule.target_field,
            "from_version": from_version,
            "to_version": to_version,
            "counts": {
                "before": before_counts,
                "after": after_counts,
                "delta": {
                    key: after_counts[key] - before_counts[key] for key in before_counts
                },
            },
            # 仅规则版本变化时，字段级差异只有版本与去重数量；
            # 未命中规则字段的记录不计入重复剔除。
            "changed_fields": [
                {
                    "field": "规则版本",
                    "before": from_version,
                    "after": to_version,
                },
                {
                    "field": "去重数量",
                    "before": before_counts["removed_rows"],
                    "after": after_counts["removed_rows"],
                },
            ],
            "new_removal_count": len(new_removals),
            "changed_count": len(changes),
            "items": changes,
        }

    def _rows_by_source(self, rule_code: str, version: str) -> dict[str, dict[str, Any]]:
        evaluations = self._evaluations.get((rule_code, version), {})
        return {
            row["source_id"]: row
            for rows in evaluations.values()
            for row in rows
        }

    # -------------------------------------------------------------- 规则清单
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """规则清单来自版本口径表，保持原有列表接口的字段结构。"""

        rows = [self._rule_list_row(rule) for rule in self._all_rules()]
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start : start + size], total

    def _all_rules(self) -> list[RuleVersion]:
        return [rule for code in RULE_CODES for rule in versions_for(code)]

    def _rule_list_row(self, rule: RuleVersion) -> dict[str, Any]:
        return {
            "id": self._rule_id(rule),
            "规则编号": rule.rule_code,
            "目标字段": rule.target_field,
            "空值替换": rule.empty_replacement,
            "格式规范化": rule.format_normalization,
            "异常值剔除": rule.outlier_removal,
            "去重键": rule.dedup_key,
            "规则版本": rule.version,
            "生效日期": rule.effective_date,
            "status": rule.status,
            "pending": rule.version != self._active[rule.rule_code],
            "abnormal": False,
        }

    def _rule_id(self, rule: RuleVersion) -> int:
        return self._all_rules().index(rule) + 1

    def find_rule_entry(self, entry_id: int) -> dict[str, Any] | None:
        rules = self._all_rules()
        if 1 <= entry_id <= len(rules):
            return self._rule_list_row(rules[entry_id - 1])
        return None


cleanse_service = CleanseService()
