"""
Q15 表头对应数据完整度 —— G-16 数据质量面(80-20 核心裁定)

判据源:本任务 2026-09-28 新增(80-20 原则:核心数据 = 表头识别 +
表头对应数据完整性);语义收敛 B3 §2 R-01"列头缺失→显式报错退出"
的历史偏差——表头识别失败由场景层转文件级 UNCERTAIN,本规则只管
"表头已识别的文件,数据格完整度是否达标"。

判定:文件级完整度(实填格/应填格)低于阈值 → FAIL(medium → REVIEW)。
阈值外置 quote_rules_config.json q15 节(缺省口径 0.8);threshold 缺失
或 confirmed=false → 全部输出 UNKNOWN 记账(fail-closed,与 Q11 同纪律)。

边界:field_facts 为空(无文件事实)不判;header_found=False 的事实跳过
(场景层 UNCERTAIN 承接);行级缺 qty/moq 不在此惩罚(Q14 行级 UNKNOWN
已承接)——本规则只管文件级汇总完整度这一件事。
"""

from __future__ import annotations

from typing import Any

from ..findings import EvidenceRef, Finding
from ..quote_records import FileFieldFacts, QuoteRecord

RULE_ID = "Q15"

_DESCRIPTION = (
    "表头对应数据完整度:文件级实填格/应填格低于阈值"
    "(80-20 核心;阈值外置,缺证即 UNKNOWN 不硬判)"
)

MANUAL_REF = "G-16 数据质量面(80-20 核心:表头识别+数据完整性;B3 §2 R-01 语义收敛)"


def evaluate(records: list[QuoteRecord], config: dict[str, Any],
             sha_of: Any, field_facts: list[FileFieldFacts] | None = None
             ) -> list[Finding]:
    """对文件级字段事实跑完整度判定。

    config: {"threshold": float|None, "confirmed": bool}
    field_facts: 场景层清洗引擎产出的每文件事实(无则不判)。
    """
    facts = field_facts or []
    if not facts:
        return []
    threshold = config.get("threshold")
    confirmed = bool(config.get("confirmed"))
    if not confirmed or threshold is None:
        # 缺证记账:每文件一条 UNKNOWN,不淘汰不兜底
        return [
            Finding(
                rule=RULE_ID, severity="info", kind="unknown",
                description=(
                    f"Q15 完整度判定未执行:阈值未定值/未确认"
                    f"(threshold={threshold!r},confirmed={confirmed})——"
                    f"按 fail-closed 记 UNKNOWN,不硬判"
                ),
                evidence_ref=EvidenceRef(
                    file=f.source_file, symbol="field_completeness",
                    line_start=f.header_row_no, line_end=f.header_row_no,
                    sha=f.sha,
                ),
                manual_ref=MANUAL_REF,
            )
            for f in facts
        ]

    findings: list[Finding] = []
    for f in facts:
        if not f.header_found:
            continue  # 表头识别失败走场景层 UNCERTAIN,不在此硬判
        if f.completeness < float(threshold):
            findings.append(Finding(
                rule=RULE_ID, severity="medium",
                description=(
                    f"文件 {f.source_file}({f.sheet_or_page})字段完整度 "
                    f"{f.completeness:.0%} 低于阈值 {float(threshold):.0%}"
                    f"(有效格 {f.cells_filled}/{f.cells_expected},"
                    f"丢弃行 {f.dropped_rows})——数据完整性影响裁决,"
                    f"进人工复核"
                ),
                evidence_ref=EvidenceRef(
                    file=f.source_file, symbol="field_completeness",
                    line_start=f.header_row_no,
                    line_end=f.header_row_no + f.data_rows,
                    sha=f.sha,
                ),
                manual_ref=MANUAL_REF,
            ))
    return findings

from ..quote_records import QuoteRuleSpec

RULE = QuoteRuleSpec(
    rule_id='Q15', title='表头对应数据完整度', severity='medium',
    ledger_entry='G-16', evaluate=evaluate,
    description=_DESCRIPTION,
)
