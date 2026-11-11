"""
parse_input.py —— 通用办公格式 -> 纯文本 适配器（Pro 及以上档位）

read_file_auto(path)：按扩展名分发——
    .txt / .md / .csv / 未知扩展 -> read_text_auto（与 Pro 完全同逻辑）
    .xlsx / .xlsm                -> openpyxl 活动工作表，逐行逗号拼行
    .docx                        -> python-docx 段落 + 表格（按文档序）
    .pdf                         -> pdfplumber 逐页文本
    .xls                         -> 明确报错（openpyxl 不读旧版二进制格式，
                                   引导另存为 .xlsx，不上 xlrd——弃维护且增体积）

架构定位（io_utils 裁定的补充）：把「通用文件格式转纯文本」视作
read_text_auto 的同级工具，放引擎层；场景特有的解析（哪列是单号、
哪种行算噪声）仍在场景包 code/ 里写死。输出统一为"一行一条记录"
的文本，场景包继续用既有的行解析逻辑，不感知输入格式。

注：第三方库 import 全部放在函数内（延迟），保持启动轻量。
"""

from __future__ import annotations

import datetime
from pathlib import Path


def file_kind(path: str | Path) -> str:
    """按扩展名判定输入类别：text / excel / word / pdf（未知归 text）。

    .xls 也归 excel——由 read_file_auto 明确报错引导另存 .xlsx，
    不能掉进 text 分支按二进制乱读（read_text_auto 会"成功"产出一堆
    乱码记录，比报错糟糕得多）。
    """
    suffix = Path(path).suffix.lower()
    if suffix in (".xlsx", ".xlsm", ".xls"):
        return "excel"
    if suffix == ".docx":
        return "word"
    if suffix == ".pdf":
        return "pdf"
    return "text"


def read_text_auto(path: str | Path) -> str:
    """容错的输入文件读取：utf-8-sig -> gbk -> utf-8(replace)。

    （自 scenario_api 迁入，实现不变：避免 parse_input 与 scenario_api
    相互导入。scenario_api 侧保留 re-export，旧导入路径不受影响。）
    Windows 记事本的历史默认编码是 GBK，现代编辑器常见带 BOM 的 UTF-8，
    两种都是中国用户的日常输入。解码失败绝不能让任务 FAILED——
    按 utf-8 强制替换比报"步骤执行异常"对用户友好得多。
    """
    data = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _cell_to_text(value) -> str:
    """单元格值转文本：整数浮点去掉 .0、日期统一 YYYY-MM-DD、None 跳过。

    与「单号,金额」行语法对齐：1200.0 -> 1200，2026-09-01 00:00 -> 2026-09-01，
    否则场景包的金额正则（拒绝科学计数/千分位）会误伤正常数字。
    """
    if value is None:
        return ""
    if isinstance(value, datetime.datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, datetime.date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return str(value).strip()


def _read_excel(path: Path, sheet: str | None) -> str:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[sheet] if sheet else wb.active
        lines: list[str] = []
        for row in ws.iter_rows(values_only=True):
            cells = [_cell_to_text(v) for v in row]
            # 尾部空单元格不参与拼接；整行空跳过——空行进了文本会变成
            # 场景包眼里的"格式错误记录"，宁缺毋滥
            while cells and cells[-1] == "":
                cells.pop()
            if not cells:
                continue
            lines.append(",".join(cells))
        return "\n".join(lines)
    finally:
        wb.close()


def _read_word(path: Path) -> str:
    import docx

    document = docx.Document(str(path))
    parts: list[str] = []
    # iter_inner_content 是 python-docx 1.x 的文档序迭代 API（段落与表格
    # 按出现顺序混排）；旧版本没有时退化为"先段落后表格"，顺序信息弱化
    # 但内容不丢
    if hasattr(document, "iter_inner_content"):
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        for block in document.iter_inner_content():
            if isinstance(block, Paragraph):
                text = block.text.strip()
                if text:
                    parts.append(text)
            elif isinstance(block, Table):
                parts.extend(_docx_table_lines(block))
    else:
        for para in document.paragraphs:
            text = para.text.strip()
            if text:
                parts.append(text)
        for table in document.tables:
            parts.extend(_docx_table_lines(table))
    return "\n".join(parts)


def _docx_table_lines(table) -> list[str]:
    lines = []
    for row in table.rows:
        cells = [c.text.strip() for c in row.cells]
        while cells and cells[-1] == "":
            cells.pop()
        if cells:
            lines.append(",".join(cells))
    return lines


def _read_pdf(path: Path) -> str:
    import pdfplumber

    pages = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if text.strip():
                pages.append(text.strip())
    return "\n".join(pages)


def read_file_auto(path: str | Path, sheet: str | None = None) -> str:
    """通用输入读取：任何支持的格式统一转成纯文本（一行一条记录）。

    场景包的 load 步骤用它替代 read_text_auto——文本输入行为完全一致，
    办公格式（xlsx/docx/pdf）透明直入。解析失败抛异常，由引擎的步骤
    兜底转成 ERROR issue（不静默给半坏数据，同 execute_task 契约）。
    """
    p = Path(path)
    kind = file_kind(p)

    if kind == "text":
        return read_text_auto(p)
    if kind == "excel":
        if p.suffix.lower() == ".xls":
            raise ValueError(
                f"旧版 .xls 格式不支持：{p.name}，请先在 Excel 里另存为 .xlsx 再导入"
            )
        return _read_excel(p, sheet)
    if kind == "word":
        return _read_word(p)
    return _read_pdf(p)


def read_excel_rows(path: str | Path, sheet: str | None = None) -> list[list[str]]:
    """按单元格读 Excel → 行×单元格表的文本二维结构，**保留格间边界**。

    与 read_file_auto 的差异：read_file_auto 把每行单元格用逗号拼成一行文本，
    当某个格值里含逗号（如千分位金额 "1,500.00"）时会破坏列对齐；本函数返回
    结构化行（list[list[str]]），格值原样保留，供模板类场景在引擎层安全读列。

    仅支持 xlsx/xlsm（xls 明确报错引导另存）。场景包经 scenario_api.read_excel_rows
    使用，遵守"只 import scenario_api"边界；解析失败抛异常由步骤兜底转 ERROR。
    """
    p = Path(path)
    if p.suffix.lower() == ".xls":
        raise ValueError(
            f"旧版 .xls 格式不支持：{p.name}，请先在 Excel 里另存为 .xlsx 再导入"
        )
    import openpyxl

    wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
    try:
        ws = wb[sheet] if sheet else wb.active
        for row in ws.iter_rows(values_only=True):
            yield [_cell_to_text(v) for v in row]
    finally:
        wb.close()


def write_excel_rows(path: str | Path, sheets: list[tuple[str, list[str], list[list]]]) -> None:
    """按 (表名, 表头, 数据行) 写出 xlsx（标准化产物回填模板用）。

    openpyxl 惰性导入封装在引擎层，场景包只经 scenario_api.write_excel_rows 使用，
    遵守"只 import scenario_api"边界。仅支持 xlsx 写出；覆盖已存在文件。
    """
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = openpyxl.Workbook()
    first = True
    header_fill = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
    header_font = Font(bold=True)
    for name, headers, rows in sheets:
        ws = wb.active if first else wb.create_sheet()
        first = False
        ws.title = name
        ws.append(list(headers))
        for cell in ws[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")
        for r in rows:
            ws.append(list(r))
    wb.save(path)
