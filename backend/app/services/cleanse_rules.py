"""清洗规则版本的纯计算：字段归一化、去重键、窗口去重与版本差异。

这里不依赖 store：示例数据生成和运行时重算共用同一套规则，避免"种子结果"
与"重算结果"各算一套口径。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

WINDOWS_401 = (
    "2026-09-01~2026-09-07",
    "2026-09-08~2026-09-14",
    "2026-09-15~2026-09-21",
    "2026-09-22~2026-09-28",
)
WINDOWS_402 = (
    "2026-10-01~2026-10-07",
    "2026-10-08~2026-10-14",
    "2026-10-15~2026-10-21",
    "2026-10-22~2026-10-28",
)


@dataclass(frozen=True)
class RuleVersion:
    """一个规则版本在重算时使用的完整口径。"""

    rule_code: str
    version: str
    target_field: str
    dedup_key: str
    empty_replacement: str
    format_normalization: str
    outlier_removal: str
    normalization: str
    dedup_mode: str
    status: str
    effective_date: str
    published_at: str
    windows: tuple[str, ...]

    def as_row(self) -> dict[str, Any]:
        return {
            "rule_code": self.rule_code,
            "version": self.version,
            "target_field": self.target_field,
            "dedup_key": self.dedup_key,
            "empty_replacement": self.empty_replacement,
            "format_normalization": self.format_normalization,
            "outlier_removal": self.outlier_removal,
            "status": self.status,
            "effective_date": self.effective_date,
            "published_at": self.published_at,
            "windows": list(self.windows),
        }


RULE_VERSIONS: dict[tuple[str, str], RuleVersion] = {
    ("CLEANSE-401", "R4"): RuleVersion(
        rule_code="CLEANSE-401",
        version="R4",
        target_field="email",
        dedup_key="email",
        empty_replacement="unknown@example.com",
        format_normalization="去除首尾空白",
        outlier_removal="空值替换后保留",
        normalization="trim",
        dedup_mode="raw",
        status="历史",
        effective_date="2026-09-01",
        published_at="2026-09-01T09:00:00",
        windows=WINDOWS_401,
    ),
    ("CLEANSE-401", "R5"): RuleVersion(
        rule_code="CLEANSE-401",
        version="R5",
        target_field="email",
        dedup_key="email（归一化后）",
        empty_replacement="unknown@example.com",
        format_normalization="去除首尾空白并转小写",
        outlier_removal="空值替换后保留",
        normalization="trim_lower",
        dedup_mode="normalized",
        status="已发布",
        effective_date="2026-10-01",
        published_at="2026-10-01T09:00:00",
        windows=WINDOWS_401,
    ),
    ("CLEANSE-402", "R4"): RuleVersion(
        rule_code="CLEANSE-402",
        version="R4",
        target_field="phone_hash",
        dedup_key="phone_hash",
        empty_replacement="unknown_hash",
        format_normalization="去除首尾空白",
        outlier_removal="空值替换后保留",
        normalization="trim",
        dedup_mode="raw",
        status="历史",
        effective_date="2026-09-15",
        published_at="2026-09-15T09:00:00",
        windows=WINDOWS_402,
    ),
    ("CLEANSE-402", "R5"): RuleVersion(
        rule_code="CLEANSE-402",
        version="R5",
        target_field="phone_hash",
        dedup_key="phone_hash（归一化后）",
        empty_replacement="unknown_hash",
        format_normalization="去除首尾空白并转小写",
        outlier_removal="空值替换后保留",
        normalization="trim_lower",
        dedup_mode="normalized",
        status="已发布",
        effective_date="2026-10-15",
        published_at="2026-10-15T09:00:00",
        windows=WINDOWS_402,
    ),
}

RULE_CODES = sorted({code for code, _ in RULE_VERSIONS})


def get_rule(rule_code: str, version: str) -> RuleVersion | None:
    return RULE_VERSIONS.get((rule_code, version))


def versions_for(rule_code: str) -> list[RuleVersion]:
    """按版本号从旧到新返回规则版本。"""

    return sorted(
        (rule for (code, _), rule in RULE_VERSIONS.items() if code == rule_code),
        key=lambda rule: rule.version,
    )


def current_version(rule_code: str) -> RuleVersion:
    versions = versions_for(rule_code)
    published = [rule for rule in versions if rule.status == "已发布"]
    return (published or versions)[-1]


def previous_version(rule_code: str, version: str) -> RuleVersion | None:
    versions = versions_for(rule_code)
    for index, rule in enumerate(versions):
        if rule.version == version:
            return versions[index - 1] if index else None
    return None


def clean_value(rule: RuleVersion, value: Any) -> str:
    """按版本口径处理空值与格式；返回值就是写入清洗数据集的目标字段值。"""

    text = "" if value is None else str(value)
    if not text.strip():
        text = rule.empty_replacement
    if "trim" in rule.normalization:
        text = text.strip()
    if "lower" in rule.normalization:
        text = text.lower()
    return text


def dedup_key_value(rule: RuleVersion, value: Any) -> str:
    """去重键取值：R4 用原始值，R5 用归一化后的值。"""

    if rule.dedup_mode == "normalized":
        return clean_value(rule, value)
    return "" if value is None else str(value).strip()


def source_has_late_correction(source: dict[str, Any], rule: RuleVersion) -> bool:
    """更晚更正是否落在该版本发布之后，形成需要人工取舍的冲突。"""

    if source.get("frozen_version") == rule.version:
        return False  # 源记录冻结在这一版，之后到的更正不回溯改写历史结果
    corrected_at = source.get("corrected_at")
    return bool(
        source.get("corrected_value")
        and corrected_at
        and str(corrected_at) > rule.published_at
    )


def effective_input_value(source: dict[str, Any], rule: RuleVersion) -> str:
    """源记录输入值：冻结版本用冻结快照；后续版本按发布时点与冲突决策取值。"""

    original = "" if source.get("original_value") is None else str(source["original_value"])
    if source.get("frozen_version") == rule.version:
        return original
    corrected = source.get("corrected_value")
    if not corrected:
        return original
    if not source_has_late_correction(source, rule):
        return str(corrected)
    decision = (source.get("resolutions") or {}).get(rule.version)
    if decision == "keep_correction":
        return str(corrected)
    return original


def evaluate_window(
    rule: RuleVersion,
    sources: Iterable[dict[str, Any]],
    *,
    window: str,
) -> list[dict[str, Any]]:
    """对一个去重窗口生成唯一结果：同窗口同去重键只保留最早的一条。"""

    rows: list[dict[str, Any]] = []
    for source in sources:
        if source.get("window") != window:
            continue
        input_value = effective_input_value(source, rule)
        cleaned = clean_value(rule, input_value)
        key = dedup_key_value(rule, input_value)
        rows.append(
            {
                "source_id": source["source_id"],
                "window": window,
                "target_field": rule.target_field,
                "original_value": source.get("original_value"),
                "corrected_value": source.get("corrected_value"),
                "input_value": input_value,
                "cleaned_value": cleaned,
                "dedup_key": key,
                "removed": False,
                "removal_reason": None,
                "kept_source_id": None,
                "frozen_version": source.get("frozen_version"),
                "evaluated_version": rule.version,
                "revision": int(source.get("revision") or 1),
                "conflict": source_has_late_correction(source, rule),
            }
        )

    kept_by_key: dict[str, dict[str, Any]] = {}
    for row in sorted(rows, key=lambda item: str(item["source_id"])):
        first = kept_by_key.get(str(row["dedup_key"]))
        if first is None:
            kept_by_key[str(row["dedup_key"])] = row
            continue
        row["removed"] = True
        row["kept_source_id"] = first["source_id"]
        row["removal_reason"] = (
            f"去重键 {row['dedup_key']} 与 {first['source_id']} 相同，窗口内仅保留一条"
        )
    return rows


def window_counts(rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    rows = list(rows)
    removed = sum(1 for row in rows if row["removed"])
    return {
        "input_rows": len(rows),
        "removed_rows": removed,
        "output_rows": len(rows) - removed,
    }


def build_sources(
    rule_code: str,
    *,
    bases_per_window: tuple[int, ...],
    empties_per_window: tuple[int, ...],
    exact_dups_per_window: tuple[int, ...],
    case_variants_per_window: tuple[int, ...],
    frozen_version: str = "R4",
    value_template: str = "user{index:04d}@example.com",
) -> list[dict[str, Any]]:
    """生成源记录：每窗口若干基础值，外加空值、精确重复与大小写变体。

    精确重复在 R4/R5 都会被剔除；大小写变体只在归一化去重（R5）下落到
    同一个去重键，因此两版剔除数量不同。空值替换后同窗口只保留一条。
    """

    windows = WINDOWS_401 if rule_code == "CLEANSE-401" else WINDOWS_402
    sources: list[dict[str, Any]] = []
    offset = 0
    for index, window in enumerate(windows):
        base_count = bases_per_window[index]
        bases: list[dict[str, Any]] = []
        for i in range(base_count):
            absolute = offset + i
            if i < empties_per_window[index]:
                value = ""
            elif absolute % 11 == 0:
                value = value_template.format(index=absolute).upper()
            else:
                value = value_template.format(index=absolute)
            source = {
                "source_id": f"{rule_code}-S{len(sources) + 1:04d}",
                "window": window,
                "original_value": value,
                "frozen_version": frozen_version,
                "revision": 1,
            }
            bases.append(source)
            sources.append(source)

        non_empty = [b for b in bases if b["original_value"]]
        duplicate_targets = non_empty[: exact_dups_per_window[index]]
        duplicate_ids = {b["source_id"] for b in duplicate_targets}
        for base in duplicate_targets:
            sources.append(
                {
                    "source_id": f"{rule_code}-S{len(sources) + 1:04d}",
                    "window": window,
                    "original_value": base["original_value"],
                    "frozen_version": frozen_version,
                    "revision": 1,
                }
            )

        variant_candidates = [
            b
            for b in bases
            if b["original_value"]
            and b["source_id"] not in duplicate_ids
            and b["original_value"] == b["original_value"].lower()
        ]
        for base in variant_candidates[-case_variants_per_window[index] :]:
            sources.append(
                {
                    "source_id": f"{rule_code}-S{len(sources) + 1:04d}",
                    "window": window,
                    "original_value": base["original_value"].upper(),
                    "frozen_version": frozen_version,
                    "revision": 1,
                }
            )
        offset += base_count
    return sources


def sample_sources() -> dict[str, list[dict[str, Any]]]:
    """两条复测样例的源记录：401 目标字段合计 826 条，402 输入 1240 条。"""

    sources_401 = build_sources(
        "CLEANSE-401",
        bases_per_window=(200, 200, 200, 200),
        empties_per_window=(1, 1, 1, 1),
        exact_dups_per_window=(3, 3, 3, 3),
        case_variants_per_window=(3, 4, 3, 4),
    )
    sources_402 = build_sources(
        "CLEANSE-402",
        bases_per_window=(305, 306, 306, 306),
        empties_per_window=(0, 0, 0, 0),
        exact_dups_per_window=(2, 3, 2, 3),
        case_variants_per_window=(2, 2, 1, 2),
        value_template="hash{index:06d}",
    )

    # 已发布窗口收到更晚更正：源记录冻结在 R4，但更正发生在发布之后，形成冲突。
    # 更正落在窗口内的唯一值上，默认保留原值，因此不改变去重数量，只影响终态取值。
    for source in sources_401:
        if source["source_id"] == "CLEANSE-401-S0400":
            source["corrected_value"] = "late.fix.401@example.com"
            source["corrected_at"] = "2026-09-20T10:30:00"
            source["revision"] = 2
    for source in sources_402:
        if source["source_id"] == "CLEANSE-402-S0500":
            source["corrected_value"] = "hash-late-402"
            source["corrected_at"] = "2026-10-20T11:00:00"
            source["revision"] = 2
    return {"CLEANSE-401": sources_401, "CLEANSE-402": sources_402}
