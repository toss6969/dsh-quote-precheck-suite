"""
Q11 多供应商报价横向异常 —— E-11 判据(PRC,★5)

判据源:登记册 E-11"多供应商报价异常/横向比较"(快照锚点
0_selection/out/snapshot.json entries[E-11],源
02_条目登记册/E_异常检测/2_价格报价异常.md:6)。

判定:同一物料跨供应商单价离散度(最高/最低比值)超过阈值 → FAIL。
阈值数值**登记册行未载**,外置于 quote_rules_config.json;
threshold 为空或 confirmed=false → 本规则全部输出 UNKNOWN 记账
(fail-closed:缺证不装懂,复刻选品域 ruler_config 治理态纪律)。
"""

from __future__ import annotations

from typing import Any

from ..findings import EvidenceRef, Finding
from ..quote_records import QuoteRecord

RULE_ID = "Q11"

_DESCRIPTION = (
    "多供应商报价横向异常:同物料跨供应商单价离散度超阈值"
    "(E-11;阈值外置,缺证即 UNKNOWN 不硬判)"
)

MANUAL_REF = "登记册 E-11(快照 entries[E-11];2_价格报价异常.md:6)"


def evaluate(records: list[QuoteRecord], config: dict[str, Any],
             sha_of: Any, field_facts: Any = None) -> list[Finding]:
    """对归一记录跑 E-11 横向比较。

    config: {"threshold": float|None, "confirmed": bool}
    sha_of(record) -> str: 证据文件 sha(由场景层备好,规则不做 IO)。
    field_facts: 文件级字段事实(Q15 消费;本规则忽略)。

    口径守卫(内生,无新阈值):同物料组内显式标注的 currency 或 tax_basis
    混杂(如 $ vs ¥ / 含税 vs 未税)→ 直接比值是假异常,整组转 UNKNOWN
    不硬判;口径一致或未标注(空串)→ 照常比较。
    """
    threshold = config.get("threshold")
    confirmed = bool(config.get("confirmed"))
    if not confirmed or threshold is None:
        # 缺证记账:每物料一条 UNKNOWN,不淘汰不兜底
        by_item: dict[str, list[QuoteRecord]] = {}
        for r in records:
            by_item.setdefault(r.key(), []).append(r)
        return [
            Finding(
                rule=RULE_ID, severity="info", kind="unknown",
                description=(
                    f"E-11 横向比较未执行:阈值未定值/未确认"
                    f"(threshold={threshold!r},confirmed={confirmed})——"
                    f"按 fail-closed 记 UNKNOWN,不硬判"
                ),
                evidence_ref=EvidenceRef(
                    file=rs[0].source_file, symbol=rs[0].item,
                    line_start=rs[0].row_no, line_end=rs[0].row_no,
                    sha=sha_of(rs[0]),
                ),
                manual_ref=MANUAL_REF,
            )
            for rs in by_item.values() if len(rs) >= 2
        ]

    findings: list[Finding] = []
    by_item: dict[str, list[QuoteRecord]] = {}
    for r in records:
        by_item.setdefault(r.key(), []).append(r)
    for item, rs in by_item.items():
        bad_price = [r for r in rs if r.unit_price <= 0]
        if bad_price:
            # 非正单价(0/负):已进对照表但不能参与比值——按数据异常逐条点名,
            # 不静默滤除(2026-09-28 反演审查 R5/R6 修复:原实现 continue 直接跳过,
            # 脏价无任何提示地留在对照表里,且注释声称的"数据质量通道"并不存在)。
            for r in bad_price:
                findings.append(Finding(
                    rule=RULE_ID, severity="high",
                    description=(
                        f"物料 {r.item} 供应商 {r.supplier} 单价非正数"
                        f"({r.unit_price:g})——脏数据,该物料横向比价不成立,"
                        f"请人工核对源文件"
                    ),
                    evidence_ref=EvidenceRef(
                        file=r.source_file, symbol=r.item,
                        line_start=r.row_no, line_end=r.row_no,
                        sha=sha_of(r),
                    ),
                    manual_ref=MANUAL_REF,
                ))
            continue  # 含脏价的组不做比值(比了也是假结论)
        if len(rs) < 2:
            continue  # 单一供应商无从横向比较,不是异常
        currencies = {r.currency for r in rs if r.currency}
        tax_bases = {r.tax_basis for r in rs if r.tax_basis}
        if len(currencies) > 1 or len(tax_bases) > 1:
            # 口径不可比:币种/含税口径混杂,直接比值是假异常——转人工
            findings.append(Finding(
                rule=RULE_ID, severity="info", kind="unknown",
                description=(
                    f"物料 {rs[0].item} 跨 {len(rs)} 家供应商比价口径混杂"
                    f"(币种:{sorted(currencies) or ['-']} / "
                    f"含税口径:{sorted(tax_bases) or ['-']})——"
                    f"不可直接比值,记 UNKNOWN 转人工"
                ),
                evidence_ref=EvidenceRef(
                    file=rs[0].source_file, symbol=rs[0].item,
                    line_start=rs[0].row_no, line_end=rs[0].row_no,
                    sha=sha_of(rs[0]),
                ),
                manual_ref=MANUAL_REF,
            ))
            continue
        prices = [r.unit_price for r in rs]
        low, high = min(prices), max(prices)
        ratio = high / low
        if ratio > threshold:
            top = max(rs, key=lambda r: r.unit_price)
            findings.append(Finding(
                rule=RULE_ID, severity="high",
                description=(
                    f"物料 {top.item} 跨 {len(rs)} 家供应商单价离散 "
                    f"max/min={ratio:.2f} 超阈值 {threshold}"
                    f"(高:{top.supplier} {top.unit_price} / "
                    f"低:{min(rs, key=lambda r: r.unit_price).unit_price})"
                ),
                evidence_ref=EvidenceRef(
                    file=top.source_file, symbol=top.item,
                    line_start=top.row_no, line_end=top.row_no,
                    sha=sha_of(top),
                ),
                manual_ref=MANUAL_REF,
            ))
    return findings

from ..quote_records import QuoteRuleSpec

RULE = QuoteRuleSpec(
    rule_id='Q11', title='多供应商报价横向异常', severity='high',
    ledger_entry='E-11', evaluate=evaluate,
    description=_DESCRIPTION,
)
