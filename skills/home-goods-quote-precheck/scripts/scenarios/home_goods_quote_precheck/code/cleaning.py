"""
cleaning —— 报价清洗与标准化引擎(G-16 归一的前置层)

真实供应商报价格式千差万别:多行表头/合并单元格/中英混排/列名方言/
币种与单位混在数值里/GBK 编码。本模块把"脏表"变"净行":

  表头识别(别名归一后精确匹配,不做包含匹配防误命中)
  + 数值清洗(每步留 CellFix 修复痕迹)
  + 行级事件台账(丢弃行不再静默,全部进 RowEvent)

80-20 核心:核心数据 = 表头识别 + 表头对应数据完整性。表头识别失败由
场景层转文件级 UNCERTAIN;完整度汇总事实(FileFieldFacts)交 Q15 判定。
清洗不了的**不猜**:面议/区间价 → None + 行事件(fail-closed)。

本层只做确定性的解析与变换,不做业务判定——判据在 detection/rules/。
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass, field

from detection.quote_records import QuoteRecord

# 规范列与关键列(80-20 的"20"):表头必须命中关键列,否则文件级 UNCERTAIN
CANONICAL_COLS = ("supplier", "item", "unit_price", "qty", "moq", "package_unit")
MANDATORY_COLS = ("item", "unit_price")

# 通用别名库(30 行业并集;归一前原文,匹配时经 normalize_header_cell)。
# 收录原则:无行业歧义的整格精确匹配;有歧义的(如"单位")配正反例测试。
# 刻意**不收**:货号/价钱/描述类列名(歧义大,留负例验证表头识别失败通道)。
ALIAS_TABLE: dict[str, tuple[str, ...]] = {
    "supplier": (
        "供应商", "供应商名称", "供货商", "供货单位", "厂商", "公司",
        "公司名称", "报价单位", "报价公司", "报价方", "贵司",
        "supplier", "vendor", "suppliername",
    ),
    "item": (
        "物料", "物料名称", "物料编码", "料号", "品名", "品号", "产品名称",
        "产品", "货品名称", "货品", "商品名称", "商品", "材料名称", "型号",
        "规格型号", "产品型号", "零件号", "零件名称", "模具编号", "模具名称",
        "sku", "item", "itemname", "material", "product", "productname",
        "part", "partno", "partnumber",
    ),
    "unit_price": (
        "单价", "含税单价", "未税单价", "不含税单价", "裸价", "价格", "报价",
        "销售单价", "采购单价", "unit_price", "unitprice", "price", "unitcost",
    ),
    "qty": (
        "数量", "采购数量", "需求数量", "订购数量", "订购量", "用量", "件数",
        "qty", "quantity",
    ),
    "moq": (
        "起订量", "最小起订量", "最小订购量", "最小订购数量", "起订",
        "moq", "minorderqty", "minorderquantity",
    ),
    "package_unit": (
        "包装", "包装单位", "包装规格", "包装方式", "计量单位", "单位",
        "package_unit", "package", "packageunit", "unit",
    ),
}

# 币种标记:符号(NFKC 后只剩半角 ¥/$/€)与词(前缀或后缀,大小写不敏感)
_CURRENCY_SYMBOLS = {"¥": "CNY", "$": "USD", "€": "EUR"}
_CURRENCY_TOKENS = {
    "人民币": "CNY", "rmb": "CNY", "cny": "CNY", "元": "CNY",
    "美元": "USD", "usd": "USD", "us$": "USD",
    "欧元": "EUR", "eur": "EUR",
    "港币": "HKD", "hkd": "HKD",
    "日元": "JPY", "jpy": "JPY",
}

# 数值单位后缀(匹配时按长度降序尝试,防 "kg" 被 "g" 抢先;㎡ 经 NFKC → m2)
_UNIT_SUFFIXES = (
    "平方米", "千克", "公斤", "pcs", "pc", "个", "只", "支", "张", "片",
    "卷", "箱", "件", "桶", "袋", "包", "吨", "kg", "克", "g", "米", "m2",
    "cm", "mm", "m", "码", "双", "套", "台", "把", "条", "令", "刀", "打",
    "瓶", "罐", "盒", "听", "托", "板", "扎", "副", "对", "组",
)

# 行首/整行分隔线(———— / ==== 等),不当数据行
_DASH_LIKE = re.compile(r"^[-—–=_~\s]+$")

# 汇总/备注行前缀(报价单惯例:合计/小计/备注等行不是报价数据)
_SUMMARY_PREFIX = re.compile(
    r"^(合\s*计|总\s*计|小\s*计|价税合计|备\s*注|说\s*明|大\s*写|税\s*额"
    r"|total\b|subtotal\b)", re.IGNORECASE)

# 括号后缀组(中文括号经 NFKC → 半角;【】与[] NFKC 不变,一并处理)
_BRACKET_SUFFIX = re.compile(r"[（(【\[][^（）()【】\[\]]*[）)】\]]*$")


@dataclass(frozen=True)
class CellFix:
    """单格修复记录(清洗台账的最小单元)。"""

    field: str        # 规范列名
    original: str     # 原始值
    cleaned: str      # 清洗后值(数值清洗为终态字符串)
    fix: str          # 修复种类:nfkc/strip_ws/currency_symbol/thousands_sep/unit_suffix/wan_multiplier


@dataclass(frozen=True)
class RowEvent:
    """行级事件——丢弃/跳过必须留痕,消灭"静默丢弃"。"""

    row_no: int       # 1 基行号
    kind: str         # dropped_missing_item/dropped_missing_price/dropped_bad_numeric/skipped_blank/skipped_before_header
    detail: str


@dataclass(frozen=True)
class HeaderScan:
    """一张二维表的表头识别结果。"""

    found: bool
    row_no: int                       # 表头行号(1 基;未识别=0)
    col_map: dict[int, str] = field(default_factory=dict)      # 列序号 → 规范列
    raw_by_col: dict[int, str] = field(default_factory=dict)   # 列序号 → 原始表头文本
    merged_rows: tuple[int, ...] = ()  # 多行表头参与拼接的行号(含首行)
    scanned: int = 0                  # 扫描窗口内非空行数


@dataclass(frozen=True)
class TableResult:
    """一张二维表的完整清洗结果(记录 + 台账 + 完整度计数)。"""

    header: HeaderScan
    records: list = field(default_factory=list)     # list[QuoteRecord]
    row_events: tuple = ()                          # tuple[RowEvent, ...]
    cell_fixes: tuple = ()                          # tuple[CellFix, ...]
    data_rows: int = 0
    cells_expected: int = 0
    cells_filled: int = 0
    dropped_rows: int = 0


def normalize_header_cell(raw: object) -> str:
    """表头单元格归一:NFKC → 去空白/换行 → 去尾部括号组 → 小写。

    "单价（含税/元）"→"单价";"Unit Price (RMB)"→"unitprice";
    "供应商\\n名称"→"供应商名称"。纯函数,不查别名。
    """
    s = unicodedata.normalize("NFKC", "" if raw is None else str(raw))
    s = "".join(s.split())                      # 去所有空白(含换行/全角空格)
    while True:                                 # 尾部括号组可叠加,循环剥
        stripped = _BRACKET_SUFFIX.sub("", s)
        if stripped == s:
            break
        s = stripped
    return s.lower()


def build_alias_map(extra: dict | None = None) -> dict[str, str]:
    """别名库摊平成 {归一化别名: 规范列};外置扩展只增不减。

    extra: {"canonical": ["别名", ...]}(aliases-config JSON)。先摊内置库再
    应用外置(setdefault):外置只允许补充现场方言,不允许重定义规范列;
    canonical 必须是已知规范列,未知键显式报错(不静默忽略)。
    """
    flat: dict[str, str] = {}
    for canon, aliases in ALIAS_TABLE.items():
        for a in aliases:
            flat.setdefault(normalize_header_cell(a), canon)
    if extra:
        for canon, aliases in extra.items():
            canon = str(canon).strip().lower()
            if canon not in CANONICAL_COLS:
                raise ValueError(
                    f"别名扩展的 canonical 必须是规范列 {CANONICAL_COLS},"
                    f"收到未知键:{canon!r}")
            for a in aliases:
                flat.setdefault(normalize_header_cell(a), canon)
    return flat


def canonical_of(raw: object, alias_map: dict[str, str]) -> str | None:
    """单格表头 → 规范列;不命中返回 None(刻意不做包含/模糊匹配)。"""
    return alias_map.get(normalize_header_cell(raw))


def scan_header(rows: list, *, alias_map: dict[str, str],
                window: int = 25, min_hits: int = 2) -> HeaderScan:
    """在前 window 个非空行内找表头:命中 ≥min_hits 且覆盖关键列之一。

    随后看至多 2 个后继行:别名命中 ≥2 的行视为表头续行,拼进表头
    (典型:中文行+英文行**对齐**双行表头,或第二行补列);首行命中不足 2 的
    后继行即数据行,停止拼接。XLSX 合并单元格读出 None→"" 天然不命中。
    """
    indexed: list[tuple[int, list[str]]] = []
    for i, row in enumerate(rows, 1):
        cells = [("" if c is None else str(c)).strip() for c in row]
        if any(cells):
            indexed.append((i, cells))

    for idx, (row_no, cells) in enumerate(indexed[:window]):
        col_map: dict[int, str] = {}
        raw_by_col: dict[int, str] = {}
        for col, c in enumerate(cells):
            if not c:
                continue
            canon = canonical_of(c, alias_map)
            if canon:
                col_map[col] = canon
                raw_by_col[col] = c
        if len(col_map) < min_hits:
            continue
        if not (set(MANDATORY_COLS) & set(col_map.values())):
            continue  # 命中再多但不认识 item/单价,不认表头
        merged = [row_no]
        for r2_no, cells2 in indexed[idx + 1: idx + 3]:
            hit_cols = {}
            for col, c in enumerate(cells2):
                if not c:
                    continue
                canon = canonical_of(c, alias_map)
                if canon:
                    hit_cols[col] = canon
            if len(hit_cols) < 2:
                break  # 数据行:停止拼接
            for col, canon in hit_cols.items():
                col_map.setdefault(col, canon)
                raw_by_col.setdefault(col, cells2[col])
            merged.append(r2_no)
        return HeaderScan(found=True, row_no=row_no, col_map=col_map,
                          raw_by_col=raw_by_col, merged_rows=tuple(merged),
                          scanned=min(len(indexed), window))
    return HeaderScan(found=False, row_no=0, scanned=min(len(indexed), window))


def clean_numeric(raw: object, *, field: str = "") -> tuple:
    """脏数值 → (float|None, 币种, [CellFix])。解析不了返回 None,不猜。

    清洗链(每步留痕):NFKC/全角数字 → 去空白 → 剥币种(前缀符号/词或
    后缀词) → 去千分位 → 万倍率 → 剥单位后缀 → float 终验。
    仅当剥离后剩余是合法数字才应用变换("面议"/"10-12" → None)。
    """
    s = "" if raw is None else str(raw).strip()
    if not s:
        return None, "", []
    fixes: list[CellFix] = []
    t = unicodedata.normalize("NFKC", s)
    if t != s:
        fixes.append(CellFix(field, s, t, "nfkc"))
        s = t
    t = "".join(s.split())
    if t != s:
        fixes.append(CellFix(field, s, t, "strip_ws"))
        work = t
    else:
        work = s
    currency = ""

    low = work.lower()
    for tok, cur in sorted(_CURRENCY_TOKENS.items(), key=lambda kv: -len(kv[0])):
        if low.startswith(tok):
            currency = cur
            stripped = work[len(tok):].strip()
            fixes.append(CellFix(field, work, stripped, "currency_symbol"))
            work = stripped
            break
    if not currency and work[:1] in _CURRENCY_SYMBOLS:
        currency = _CURRENCY_SYMBOLS[work[0]]
        stripped = work[1:].strip()
        fixes.append(CellFix(field, work, stripped, "currency_symbol"))
        work = stripped
    low = work.lower()
    for tok, cur in sorted(_CURRENCY_TOKENS.items(), key=lambda kv: -len(kv[0])):
        if not tok or not low.endswith(tok):
            continue
        head = work[:-len(tok)].strip()
        if not re.fullmatch(r"-?\d+(\.\d+)?", head):
            continue  # 后缀词后面不是纯数字 → 不是币种标记(防误剥)
        currency = currency or cur
        fixes.append(CellFix(field, work, head, "currency_symbol"))
        work = head
        break

    if re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", work):
        stripped = work.replace(",", "")
        fixes.append(CellFix(field, work, stripped, "thousands_sep"))
        work = stripped

    mult = 1.0
    if re.fullmatch(r"-?\d+(\.\d+)?[万萬]", work):
        mult = 10000.0
        stripped = work[:-1]
        fixes.append(CellFix(field, work, stripped, "wan_multiplier"))
        work = stripped

    for u in sorted(_UNIT_SUFFIXES, key=len, reverse=True):
        if not work.lower().endswith(u):
            continue
        head = work[:-len(u)].strip()
        if not re.fullmatch(r"-?\d+(\.\d+)?", head):
            continue  # 剥后不是数字 → 不是单位后缀(防物料名误伤)
        fixes.append(CellFix(field, work, head, "unit_suffix"))
        work = head
        break

    try:
        return float(work) * mult, currency, fixes
    except ValueError:
        return None, currency, fixes


def decode_bytes(data: bytes,
                 encodings: tuple[str, ...] = ("utf-8-sig", "gb18030", "utf-16")
                 ) -> tuple:
    """依序 strict 解码,首个成功者胜;全败 (None, None)。

    UTF-16 BOM(FF FE/FE FF)显式优先——gb18030 对任意字节几乎全能"成功",
    乱序尝试会把 UTF-16 文件吞成乱码。gb18030 向下兼容 GBK/GB2312。
    """
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        encodings = ("utf-16",) + tuple(e for e in encodings if e != "utf-16")
    for enc in encodings:
        try:
            return data.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return None, None


def parse_csv_rows(text: str) -> list:
    """CSV 文本 → 二维字符串表;Sniffer 嗅探 ,/;/	 分隔符,失败回退逗号。"""
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
    except csv.Error:
        dialect = "excel"
    with io.StringIO(text, newline="") as fh:
        return list(csv.reader(fh, dialect))


def build_row(col_map: dict[int, str], raw_by_col: dict[int, str],
              cells: list[str], row_no: int, src: str) -> tuple:
    """一行数据 → (QuoteRecord 构造 kwargs|None, [RowEvent], [CellFix], 实填数)。

    缺 item 或 unit_price(空/不可解析) → None + 行事件;数值列不可解析
    同样整行丢弃(与旧管线口径一致,但现在留痕)。同规范列多列并存时
    (如 含税单价+未税单价)取最左列,其余列不覆盖。
    """
    events: list[RowEvent] = []
    fixes: list[CellFix] = []
    texts = {"supplier": "", "item": "", "package_unit": ""}
    numerics: dict[str, float] = {}
    currency = ""
    for col in sorted(col_map):
        canon = col_map[col]
        raw = cells[col].strip() if col < len(cells) else ""
        if canon in texts:
            if raw:
                texts[canon] = raw
        elif not raw:
            continue  # 数值格空:留给 Q14 行级缺证通道,不是本行的错
        elif canon in numerics:
            continue  # 同规范列已取最左
        else:
            value, cur, fx = clean_numeric(raw, field=canon)
            fixes += fx
            if value is None:
                events.append(RowEvent(
                    row_no, "dropped_bad_numeric",
                    f"{canon}={raw!r} 不可解析为数值"))
                continue
            numerics[canon] = value
            if canon == "unit_price" and cur:
                currency = cur

    tax_basis = ""
    for col in sorted(col_map):
        if col_map[col] == "unit_price":
            header_txt = raw_by_col.get(col, "")
            if "含税" in header_txt:
                tax_basis = "含税"
            elif "未税" in header_txt or "不含税" in header_txt or "裸价" in header_txt:
                tax_basis = "未税"
            break

    filled = sum(1 for v in texts.values() if v) + len(numerics)
    if not texts["item"]:
        events.append(RowEvent(row_no, "dropped_missing_item", "item 为空"))
        return None, events, fixes, filled
    if "unit_price" not in numerics:
        events.append(RowEvent(row_no, "dropped_missing_price",
                               "unit_price 为空或不可解析"))
        return None, events, fixes, filled
    kwargs = dict(
        supplier=texts["supplier"] or "(未署名)",
        item=texts["item"],
        unit_price=numerics["unit_price"],
        qty=numerics.get("qty", 0.0),
        moq=numerics.get("moq", 0.0),
        package_unit=texts["package_unit"],
        source_file=src, row_no=row_no,
        currency=currency, tax_basis=tax_basis,
    )
    return kwargs, events, fixes, filled


def process_table(rows: list, src: str, alias_map: dict[str, str]) -> TableResult:
    """一张二维表(CSV/XLSX sheet/PDF 表格)→ 清洗结果 + 完整度计数。

    完整度口径(80-20):应填格 = 数据行 × 表头声明的规范列数(去重);
    实填格 = 文本列非空 + 数值列清洗成功。声明列全空的行(脚注/备注)
    与分隔线不当数据行;表头识别失败/关键列缺失时计数全零(由场景层
    决定 UNCERTAIN 分流)。
    """
    scan = scan_header(rows, alias_map=alias_map)
    if not scan.found:
        return TableResult(header=scan)
    if not set(MANDATORY_COLS) <= set(scan.col_map.values()):
        return TableResult(header=scan)  # 关键列缺失:场景层转 UNCERTAIN

    header_end = max(scan.merged_rows) if scan.merged_rows else scan.row_no
    records: list[QuoteRecord] = []
    events: list[RowEvent] = []
    fixes: list[CellFix] = []
    data_rows = cells_filled = dropped = 0
    declared = len(set(scan.col_map.values()))

    for i, row in enumerate(rows, 1):
        if i <= header_end:
            if i < scan.row_no and any(str(c).strip() for c in row if c is not None):
                events.append(RowEvent(i, "skipped_before_header", "表头前非空行(抬头/日期/说明)"))
            continue
        cells = [("" if c is None else str(c)).strip() for c in row]
        if not any(cells):
            events.append(RowEvent(i, "skipped_blank", "空行"))
            continue
        if all(_DASH_LIKE.match(c) for c in cells if c):
            events.append(RowEvent(i, "skipped_blank", "分隔线行"))
            continue
        first_text = next((c for c in cells if c), "")
        if _SUMMARY_PREFIX.match(first_text):
            events.append(RowEvent(i, "skipped_blank", "汇总/备注行(合计/小计/备注等)"))
            continue
        mapped = {col: (cells[col] if col < len(cells) else "")
                  for col in scan.col_map}
        if not any(mapped.values()):
            events.append(RowEvent(i, "skipped_blank", "声明列全空(疑似脚注/备注行)"))
            continue
        core_cols = [col for col, canon in scan.col_map.items()
                     if canon in MANDATORY_COLS]
        if not any(mapped.get(col) for col in core_cols):
            # 关键列(item/单价)全空:不是报价数据行(小计/合计/脚注常落在首列)
            events.append(RowEvent(i, "skipped_blank", "关键列(item/单价)全空(疑似合计/脚注行)"))
            continue
        data_rows += 1
        kwargs, evs, fxs, filled = build_row(scan.col_map, scan.raw_by_col,
                                             cells, i, src)
        events += evs
        fixes += fxs
        cells_filled += filled
        if kwargs is None:
            dropped += 1
        else:
            records.append(QuoteRecord(**kwargs))

    return TableResult(
        header=scan, records=records, row_events=tuple(events),
        cell_fixes=tuple(fixes), data_rows=data_rows,
        cells_expected=data_rows * declared,
        cells_filled=cells_filled, dropped_rows=dropped,
    )


def completeness_ratio(cells_filled: int, cells_expected: int) -> float:
    """完整度 = 实填/应填;应填为 0(无数据行)按 1.0 记(不惩罚)。"""
    if cells_expected <= 0:
        return 1.0
    return cells_filled / cells_expected
