"""
detection.rules —— 规则注册表

**一条规则一个文件**，签名统一为：

    detect(ctx: RuleContext) -> list[Finding]

加新规则 = 新增一个文件 + 在对应注册表里登记一行，不改任何已有代码。
规则之间互不 import：需要共享的事实（模块索引、包导出、依赖清单）
一律经 RuleContext 拿——由扫描器一次性备好，规则是纯函数。

**注册表分两张，这条线是发布边界**：
  - 本文件 = 业务判据（报价 q* 族），产品运行时闭包只需要它；
  - factory_registry = 工厂自检 linter（D/E 族，扫的是我们自己的 .py），
    只有本仓 tests 与 scan.py 用，**不得进入产品 import 链**。
曾经两张表挤在同一个 `__init__` 里急 import，导致 `from detection.rules
import ALL_QUOTE_RULES` 把 linter 一并拉进闭包——裁剪器按"可达即保留"
如实随包，工厂内部代码质量工具就此出现在对外发布物里。
"""

from __future__ import annotations

from .context import RuleContext, RuleSpec
from .q11_cross_quote import RULE as Q11_CROSS_QUOTE
from .q13_quote_consistency import RULE as Q13_CONSISTENCY
from .q14_moq_package import RULE as Q14_MOQ_PACKAGE
from .q15_field_completeness import RULE as Q15_FIELD_COMPLETENESS

# 报价预审业务判据(E 族+G-16 质量面,登记册快照锚点):
# 签名 evaluate(records, config, sha_of, field_facts=None)
# 与 AST 判据链(ALL_RULES/detect)分立——靶面是归一业务数据不是代码
ALL_QUOTE_RULES = (Q11_CROSS_QUOTE, Q13_CONSISTENCY, Q14_MOQ_PACKAGE,
                   Q15_FIELD_COMPLETENESS)

__all__ = ["ALL_QUOTE_RULES", "RuleContext", "RuleSpec"]
