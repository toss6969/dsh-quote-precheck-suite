"""
Q13 报价单内部一致性 —— E-13 判据(SAL,★5)

判据源:登记册 E-13"报价单一致性/报价异常提示"(快照锚点
0_selection/out/snapshot.json entries[E-13],源
02_条目登记册/E_异常检测/2_价格报价异常.md:8;"一致性形态→见B章备注")。

判定:同一报价单(同一来源文件)内,同物料重复出现的单价必须一致——
相等性判定**内生**,无需外置阈值(confirmed=true 恒成立)。
不一致 → FAIL(high)。
"""

from __future__ import annotations

from typing import Any

from ..findings import EvidenceRef, Finding
from ..quote_records import QuoteRecord

RULE_ID = "Q13"

_DESCRIPTION = "报价单内部一致性:同单同物料单价必须一致(E-13,相等性内生判定)"

MANUAL_REF = "登记册 E-13(快照 entries[E-13];2_价格报价异常.md:8)"


def evaluate(records: list[QuoteRecord], config: dict[str, Any],
             sha_of: Any, field_facts: Any = None) -> list[Finding]:
    """config 预留扩展位;相等性判定不消费阈值。field_facts 为 Q15 预留。"""
    by_file_item: dict[tuple[str, str], list[QuoteRecord]] = {}
    for r in records:
        by_file_item.setdefault((r.source_file, r.key()), []).append(r)

    findings: list[Finding] = []
    for (src, _item), rs in by_file_item.items():
        if len(rs) < 2:
            continue
        prices = {r.unit_price for r in rs}
        if len(prices) > 1:
            first = rs[0]
            second = max(rs, key=lambda r: abs(r.unit_price - first.unit_price))
            findings.append(Finding(
                rule=RULE_ID, severity="high",
                description=(
                    f"报价单 {src} 内物料 {first.item} 单价不一致:"
                    f"{sorted(prices)}(行 {first.row_no} vs 行 {second.row_no})"
                ),
                evidence_ref=EvidenceRef(
                    file=src, symbol=first.item,
                    line_start=first.row_no, line_end=second.row_no,
                    sha=sha_of(first),
                ),
                manual_ref=MANUAL_REF,
            ))
    return findings

from ..quote_records import QuoteRuleSpec

RULE = QuoteRuleSpec(
    rule_id='Q13', title='报价单内部一致性', severity='high',
    ledger_entry='E-13', evaluate=evaluate,
    description=_DESCRIPTION,
)
