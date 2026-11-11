"""
quote_records —— 报价归一记录的数据模型（detection 层共享类型）

归一(G-16)由场景层 steps 完成解析与 IO；本模块只承载**判定用的归一事实**：
规则纯函数只消费 QuoteRecord 列表，不碰磁盘——与 detection 层
"零 IO、事实经上下文备好"的纪律一致。

字段来源(登记册快照锚点,0_selection/out/snapshot.json entries):
  G-16 多供应商报价统一格式/排序 | E-11 多供应商报价异常/横向比较
  E-13 报价单一致性/报价异常提示 | E-14 MOQ/包装数量异常检查
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QuoteRecord:
    """一条归一后的报价行(一个供应商对一个物料的报价)。"""

    supplier: str          # 供应商名(归一后)
    item: str              # 物料/SKU 标识(归一后,大小写与首尾空白已收敛)
    unit_price: float      # 单价
    qty: float             # 报价数量
    moq: float             # 最小起订量(缺失=0,由 E-14 规则显式处理)
    package_unit: str      # 包装单位(如 箱/PCS/ROLL;缺失="")
    source_file: str       # 来源报价文件(相对输入目录)
    row_no: int            # 行号(1 基,用于 EvidenceRef)
    currency: str = ""     # 币种标记(清洗时从 ¥/$/USD 等提取;未标注="")
    tax_basis: str = ""    # 含税口径(表头"含税/未税/不含税/裸价";未标注="")

    def key(self) -> str:
        """物料归一键:大小写/空白收敛,供跨表横向比较。"""
        return " ".join(self.item.upper().split())


@dataclass(frozen=True)
class FileFieldFacts:
    """单文件(或单表)的字段完整度事实——Q15 的判定输入。

    由场景层清洗引擎(cleaning)构造:它是"判定用的归一事实",与 QuoteRecord
    同性;完整度口径=只惩罚"表头声明了却没填"的格(cells_expected=数据行×
    声明列数),未声明的列不折损完整度(缺 qty/moq 列的后果由 Q14 行级
    UNKNOWN 承接,不双重惩罚)。
    """

    source_file: str        # 来源报价文件(相对输入目录)
    sheet_or_page: str      # 表定位("csv"/sheet 名/"pdf:p3")
    header_found: bool      # 是否识别到表头
    header_row_no: int      # 表头行号(1 基;未识别=0)
    columns_found: tuple    # 命中的规范列(tuple[str,...])
    columns_missing: tuple  # 未命中的规范列
    data_rows: int          # 数据行数(表头/合并表头/空行之后)
    cells_expected: int     # 应填格数 = 数据行 × 声明列数
    cells_filled: int       # 实填格数(数值清洗失败不算填)
    dropped_rows: int       # 丢弃行数(缺 item/单价或数值不可解析)
    completeness: float     # cells_filled/cells_expected(expected=0 → 1.0)
    sha: str                # 来源文件 sha256 前缀


from dataclasses import dataclass
from typing import Any, Callable, List


@dataclass(frozen=True)
class QuoteRuleSpec:
    """报价判据的自描述(与 AST 判据的 RuleSpec 分立,不冒用 detect 签名)。

    evaluate(records, config, sha_of, field_facts=None) -> list[Finding]:
    纯函数消费归一记录与文件级字段事实(Q15 用;不消费的规则忽略该参数,
    带默认值保证旧三参调用零破坏);阈值经 config 传入(规则不做 IO)。
    """

    rule_id: str
    title: str
    severity: str                  # 该规则可能产出的最高级别
    ledger_entry: str              # 登记册条目锚点(E-xx/G-xx)
    evaluate: Callable[..., List[Any]]
    description: str = ""
