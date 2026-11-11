"""
verdict.py —— 裁决计算（判定权归代码）

裁决优先级写死在这里，先到先得，只产出一个 verdict：

    critical > 0            → BLOCKED   阻断，修完是新指纹新运行
    存在降级源               → DEGRADED  知情放行，降级不洗白
    存在任何非 info finding  → REVIEW    进人工卡点
    否则                     → PASS      直通

"error 级 := critical"：只有"运行时确定出错"才配 critical
（当前规则面仅 D3 幽灵引用）。high 恒 REVIEW 起、进不进卡点由人判——
把 high 也升成阻断会让工具变成"狼来了"，人很快就学会无脑放行。

不外部化成 YAML：判断标准可以改，但改它必须是一次显式的代码变更，
而不是运行期被一份配置文件（或一段提示词）说服。
"""

from __future__ import annotations

PASS = "PASS"
REVIEW = "REVIEW"
DEGRADED = "DEGRADED"
BLOCKED = "BLOCKED"

_RANK = {PASS: 0, REVIEW: 1, DEGRADED: 2, BLOCKED: 3}

# 会把裁决推到 REVIEW 的 severity（info 只是记录，不推）
_NON_INFO = ("critical", "high", "medium", "low")


def compute_verdict(counts: dict[str, int], degraded: bool = False) -> str:
    """按优先级算裁决。counts 是各 severity 的条数。"""
    if counts.get("critical", 0) > 0:
        return BLOCKED
    if degraded:
        return DEGRADED
    if any(counts.get(s, 0) > 0 for s in _NON_INFO):
        return REVIEW
    return PASS


def verdict_rank(verdict: str) -> int:
    return _RANK.get(verdict, 0)


def gate_required(verdict: str) -> bool:
    """卡点 = 放行权：只有"即将放行"的裁决需要人。

    BLOCKED 没有放行可作（必须修），PASS 不需要放行——都不设卡点。
    不设卡点 ≠ 不可见：两者的报告照常归档。
    """
    return verdict in (REVIEW, DEGRADED)


def verdict_action(verdict: str) -> str:
    return {
        PASS: "none（直通，无需人工）",
        BLOCKED: "fix（读报告修复 → 新指纹新运行）",
        DEGRADED: "human_confirm（知情放行，携带降级明细）",
        REVIEW: "human_confirm（人工复核后放行）",
    }.get(verdict, "human_confirm")


def verdict_badge(verdict: str) -> str:
    return {PASS: "🟢", REVIEW: "🟡", DEGRADED: "🟠", BLOCKED: "🔴"}.get(verdict, "🟡")
