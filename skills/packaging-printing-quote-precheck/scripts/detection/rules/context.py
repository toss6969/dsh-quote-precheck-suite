"""
context.py —— 规则的输入契约

RuleContext 是规则能看到的**全部**东西。它把"读磁盘/建索引"这类
昂贵且共享的准备工作收在扫描器一侧做一次，规则只管判定：

- 规则不自己 open 文件、不自己遍历仓库——那样 N 条规则就要重复 N 遍；
- 规则是纯函数，测试只需造一个 RuleContext，不用起引擎、不用真仓库；
- 将来加"执行层产物""OpenAPI 快照"这类新事实源，是往 RuleContext
  加字段，规则签名不变。
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..findings import Finding


@dataclass(frozen=True)
class RuleContext:
    """单个被审文件 + 全仓共享事实。"""

    # -- 当前文件 --
    rel_path: str          # 仓内相对路径（/ 分隔）
    tree: ast.AST          # 已解析的 AST
    file_sha: str          # 文件内容 sha256，进 EvidenceRef

    # -- 全仓共享事实（扫描器一次性备好）--
    repo_root: Path
    module_index: dict[str, list[str]] = field(default_factory=dict)
    declared_deps: set[str] = field(default_factory=set)

    # -- 判断标准的出处（写进 finding，便于回查手册）--
    manual_refs: dict[str, str] = field(default_factory=dict)

    def manual_ref(self, rule_id: str) -> str:
        return self.manual_refs.get(rule_id, "")


@dataclass(frozen=True)
class RuleSpec:
    """一条规则的自描述 + 它的判定函数。"""

    rule_id: str
    title: str
    severity: str                 # 该规则**可能**产出的最高级别（文档用途）
    detect: Callable[[RuleContext], list[Finding]]
    description: str = ""
