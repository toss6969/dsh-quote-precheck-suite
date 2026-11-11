"""
findings.py —— 检测结果的数据形状

Finding 是**事实**，不是意见：每条都带 EvidenceRef 四元组
（file / symbol / line / sha），收报告的人不依赖本工具也能独立复算。

刻意只是数据类，不带行为：谁产生它、往哪写、怎么渲染，都不是它的事。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

# severity 全集。critical 的语义是"运行时确定出错"，不是"很严重"——
# 这条纪律决定了裁决能不能被信任，新规则要产 critical 必须先过这个门槛。
SEVERITIES = ("critical", "high", "medium", "low", "info")


@dataclass(frozen=True)
class EvidenceRef:
    """可穿透坐标：拿着它能回到被审快照里复算，证明这条 finding 不是编的。"""

    file: str
    symbol: str
    line_start: int
    line_end: int
    sha: str  # 被引用文件的 sha256

    def as_dict(self) -> dict:
        return {
            "file": self.file,
            "symbol": self.symbol,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "sha": self.sha,
        }


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    description: str
    evidence_ref: EvidenceRef
    manual_ref: str = ""      # 判断标准在手册里的锚点
    kind: str = "fact"

    def as_dict(self) -> dict:
        payload = {
            "kind": self.kind,
            "rule": self.rule,
            "severity": self.severity,
            "description": self.description,
            "manual_ref": self.manual_ref,
            "evidence_ref": self.evidence_ref.as_dict(),
        }
        # content_sha256：条目内容的规范化哈希，用作重放入账的去重键
        payload["content_sha256"] = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return payload


def count_by_severity(findings: list[Finding]) -> dict[str, int]:
    counts = dict.fromkeys(SEVERITIES, 0)
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return counts
