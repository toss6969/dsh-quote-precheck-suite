"""
Q14 MOQ/包装数量异常 —— E-14 判据(TRD,★5)

判据源:登记册 E-14"MOQ/包装数量异常检查"(快照锚点
0_selection/out/snapshot.json entries[E-14],源
02_条目登记册/E_异常检测/2_价格报价异常.md:9;"包装单位检查同条")。

判定(内生,无需外置阈值):
  ① 报价数量 < MOQ → FAIL(低于起订量)
  ② 数量非 MOQ 整数倍 → FAIL(不满足整倍起订)
  ③ 同单同物料包装单位不一致 → FAIL("包装单位检查同条")
  ④ MOQ 字段缺失(moq=0) → 该行 UNKNOWN 记账(缺证不硬判)
"""

from __future__ import annotations

from typing import Any

from ..findings import EvidenceRef, Finding
from ..quote_records import QuoteRecord

RULE_ID = "Q14"

_DESCRIPTION = "MOQ/包装数量异常:低于起订/非整倍/包装单位混乱(E-14,内生判定)"

MANUAL_REF = "登记册 E-14(快照 entries[E-14];2_价格报价异常.md:9)"


def evaluate(records: list[QuoteRecord], config: dict[str, Any],
             sha_of: Any, field_facts: Any = None) -> list[Finding]:
    findings: list[Finding] = []
    for r in records:
        if r.qty <= 0:
            # 数量缺失(归一层落 0)不是"低于起订",是缺证——转人工不硬判
            findings.append(Finding(
                rule=RULE_ID, severity="info", kind="unknown",
                description=(
                    f"{r.supplier} 对 {r.item} 的报价数量未提供——"
                    f"起订量判定缺证,记 UNKNOWN 转人工"
                ),
                evidence_ref=EvidenceRef(
                    file=r.source_file, symbol=r.item,
                    line_start=r.row_no, line_end=r.row_no, sha=sha_of(r),
                ),
                manual_ref=MANUAL_REF,
            ))
            continue
        if r.moq <= 0:
            findings.append(Finding(
                rule=RULE_ID, severity="info", kind="unknown",
                description=(
                    f"{r.supplier} 对 {r.item} 的 MOQ 未提供——"
                    f"起订量判定缺证,记 UNKNOWN 转人工"
                ),
                evidence_ref=EvidenceRef(
                    file=r.source_file, symbol=r.item,
                    line_start=r.row_no, line_end=r.row_no, sha=sha_of(r),
                ),
                manual_ref=MANUAL_REF,
            ))
            continue
        if r.qty < r.moq:
            findings.append(Finding(
                rule=RULE_ID, severity="high",
                description=(
                    f"{r.supplier} 对 {r.item} 报价数量 {r.qty:g} "
                    f"低于 MOQ {r.moq:g}"
                ),
                evidence_ref=EvidenceRef(
                    file=r.source_file, symbol=r.item,
                    line_start=r.row_no, line_end=r.row_no, sha=sha_of(r),
                ),
                manual_ref=MANUAL_REF,
            ))
        elif r.qty % r.moq != 0:
            findings.append(Finding(
                rule=RULE_ID, severity="medium",
                description=(
                    f"{r.supplier} 对 {r.item} 报价数量 {r.qty:g} "
                    f"非 MOQ {r.moq:g} 的整数倍(倍数 "
                    f"{r.qty / r.moq:.2f})"
                ),
                evidence_ref=EvidenceRef(
                    file=r.source_file, symbol=r.item,
                    line_start=r.row_no, line_end=r.row_no, sha=sha_of(r),
                ),
                manual_ref=MANUAL_REF,
            ))

    # ③ 同单同物料包装单位混乱
    by_file_item: dict[tuple[str, str], list[QuoteRecord]] = {}
    for r in records:
        if r.package_unit:
            by_file_item.setdefault((r.source_file, r.key()), []).append(r)
    for (src, _item), rs in by_file_item.items():
        units = {r.package_unit for r in rs}
        if len(units) > 1:
            first = rs[0]
            findings.append(Finding(
                rule=RULE_ID, severity="medium",
                description=(
                    f"报价单 {src} 内物料 {first.item} 包装单位不一致:"
                    f"{sorted(units)}"
                ),
                evidence_ref=EvidenceRef(
                    file=src, symbol=first.item,
                    line_start=first.row_no, line_end=rs[-1].row_no,
                    sha=sha_of(first),
                ),
                manual_ref=MANUAL_REF,
            ))
    return findings

from ..quote_records import QuoteRuleSpec

RULE = QuoteRuleSpec(
    rule_id='Q14', title='MOQ/包装数量异常', severity='high',
    ledger_entry='E-14', evaluate=evaluate,
    description=_DESCRIPTION,
)
