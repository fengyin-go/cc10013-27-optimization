"""清洗重算回归脚本：不依赖 pytest，直接跑 python -m app.check_cleanse。

覆盖：
1. CLEANSE-401 R4→R5 目标字段 826 条，差异只含版本与去重数量；
2. CLEANSE-402 R5 phone_hash：input_rows=1240、removed=17、output_rows=1223；
3. 中断后续做、重复提交不二次剔除；
4. 待处理队列按重算结果筛选，导出详情带版本差异且只含一套口径。
"""
from __future__ import annotations

import sys

from app.services.cleanse import cleanse_service as service
from app.services.cleanse_rules import current_version


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"ok - {message}")


def main() -> int:
    overview = service.rule_overview()
    rules = {rule["rule_code"]: rule for rule in overview["rules"]}

    version_401 = {version["version"]: version for version in rules["CLEANSE-401"]["versions"]}
    check(version_401["R5"]["input_rows"] == 826, "CLEANSE-401 目标字段总计 826 条")
    check(version_401["R4"]["input_rows"] == 826, "CLEANSE-401 前后两版输入一致")
    check(
        version_401["R5"]["removed_rows"] > version_401["R4"]["removed_rows"],
        "R5 归一化去重比 R4 多剔除",
    )
    check(
        version_401["R5"]["delta_vs_previous"]["input_rows"] == 0,
        "仅规则版本变化时输入量不变",
    )
    check(rules["CLEANSE-401"]["frozen_version"] == "R4", "源记录冻结在首次清洗版本 R4")

    diff = service.diff("CLEANSE-401", "R4", "R5")
    check(diff is not None, "CLEANSE-401 可对比 R4 与 R5")
    fields = [item["field"] for item in diff["changed_fields"]]
    check(fields == ["规则版本", "去重数量"], "差异字段只有版本与去重数量")
    removed_401 = {version_401["R5"]["removed_rows"], version_401["R4"]["removed_rows"]}
    check(
        {item["before"] for item in diff["changed_fields"] if item["field"] == "去重数量"}
        | {item["after"] for item in diff["changed_fields"] if item["field"] == "去重数量"}
        == removed_401,
        "差异里的去重数量与版本处理量一致",
    )
    check(
        all(item["original_value"] is not None for item in diff["items"]),
        "差异明细能回到目标字段原值",
    )
    check(
        diff["new_removal_count"]
        == version_401["R5"]["removed_rows"] - version_401["R4"]["removed_rows"],
        "新增剔除数量等于两版剔除差",
    )

    version_402 = {version["version"]: version for version in rules["CLEANSE-402"]["versions"]}
    assert_402 = version_402["R5"]
    check(assert_402["input_rows"] == 1240, "CLEANSE-402 R5 input_rows=1240")
    check(assert_402["removed_rows"] == 17, "CLEANSE-402 R5 removed=17")
    check(assert_402["output_rows"] == 1223, "CLEANSE-402 R5 output_rows=1223")

    exported = service.export_dataset("CLEANSE-402", "R5")
    check(exported["counts"]["output_rows"] == 1223, "导出输出与数据集口径一致")
    check(
        {item["rule_version"] for item in exported["items"]} == {"R5"},
        "导出只包含 R5 一套口径",
    )

    queue_all = service.list_queue(rule_code="CLEANSE-402", version="R5")[1]
    check(queue_all == 17, "待处理队列完整收录 R5 的 17 条重复剔除")
    queue_running = service.list_queue(
        rule_code="CLEANSE-402", version="R5", run_id="RUN-CLEANSE-402-R5"
    )[1]
    check(queue_running == 17, "待处理队列可按重算批次筛选")
    details = service.queue_detail(rule_code="CLEANSE-402", version="R5")
    check(
        all(item["version_diff"]["to_version"] == "R5" for item in details),
        "队列导出详情跟随版本差异",
    )
    check(
        details[0]["version_diff"]["changed_fields"][0]["field"] == "规则版本",
        "导出详情的字段差异从规则版本开始",
    )

    # 中断 → 续做 → 重复提交
    run, message = service.recompute(
        "CLEANSE-402", "R5", fail_window="2026-10-15~2026-10-21"
    )
    check(run["status"] == "失败" and run["failed_window"] == "2026-10-15~2026-10-21", "重算可在失败窗口中断")
    mid_queue = service.list_queue(rule_code="CLEANSE-402", version="R5")[1]
    check(mid_queue < 17, "中断时只保留已完成窗口的剔除记录")
    try:
        service.export_dataset("CLEANSE-402", "R5")
    except ValueError:
        print("ok - 中断期间拒绝导出半套口径")
    else:
        raise AssertionError("中断期间不应允许导出半套口径")

    run, message = service.recompute("CLEANSE-402", "R5")
    check(run["status"] == "已完成", "从失败窗口续做直至完成")
    resumed_queue = service.list_queue(rule_code="CLEANSE-402", version="R5")[1]
    check(resumed_queue == 17, "续做后剔除数量回到 17")
    run, message = service.recompute("CLEANSE-402", "R5")
    check(
        service.list_queue(rule_code="CLEANSE-402", version="R5")[1] == 17,
        "重复提交不会二次剔除",
    )
    check(service.export_dataset("CLEANSE-402", "R5")["total"] == 1223, "续做后导出仍为 1223 条")

    # 已发布窗口的更晚更正：取舍影响终态
    conflicts = [item for item in service.conflicts("CLEANSE-402") if item["version"] == "R5"]
    check(len(conflicts) == 1, "CLEANSE-402 R5 存在一条更晚更正冲突")
    source_id = conflicts[0]["source_id"]
    before_export = service.export_dataset("CLEANSE-402", "R5")
    before_value = next(
        item["cleaned_value"] for item in before_export["items"] if item["source_id"] == source_id
    )
    service.resolve_conflict(source_id, "R5", "采用更正")
    after_export = service.export_dataset("CLEANSE-402", "R5")
    after_value = next(
        item["cleaned_value"] for item in after_export["items"] if item["source_id"] == source_id
    )
    check(before_value != after_value, "冲突取舍改变终态取值")
    check(
        after_export["counts"] == before_export["counts"],
        "更正落在唯一值上，不改变去重数量",
    )
    service.resolve_conflict(source_id, "R5", "保留原值")
    restored = service.export_dataset("CLEANSE-402", "R5")
    check(
        next(item["cleaned_value"] for item in restored["items"] if item["source_id"] == source_id)
        == before_value,
        "切回保留原值后终态恢复",
    )

    print("\n全部检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
