"""
steps.py —— 报价拉平预审适配层：解析归一(G-16) → E 族判据 → 预审报告

本层不做判定:解析/清洗/IO 在这里(清洗引擎在 cleaning.py),判据纯函数在
detection/rules/(Q11/Q13/Q14/Q15)。引擎能力只经 ctx.runtime 触达;
报价源文件零写入(只读解析)。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from pathlib import Path

from scenario_api import Issue, IssueLevel, RunContext

from detection.findings import count_by_severity
from detection.quote_records import FileFieldFacts
from detection.rules import ALL_QUOTE_RULES
from detection.verdict import compute_verdict, verdict_action

from .cleaning import (
    CANONICAL_COLS,
    MANDATORY_COLS,
    build_alias_map,
    completeness_ratio,
    decode_bytes,
    parse_csv_rows,
    process_table,
)

# 样例集自描述文件(非报价):跳过,不当"不支持的格式"转人工
_NON_QUOTE_FILES = {"manifest.json"}


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _iter_quote_files(root: Path):
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.name.startswith("~$"):
            continue
        if p.name.lower() in _NON_QUOTE_FILES:
            continue
        yield p


def _ingest_one(p: Path, rel: str, sha: str, alias_map: dict,
                records: list, uncertain: list,
                cleaning_log: list, field_facts: list) -> None:
    """单文件:格式分派 → 逐表清洗 → records/台账/UNCERTAIN 三分流。"""
    ext = p.suffix.lower()
    try:
        tables: list[tuple[str, list]] = []
        if ext == ".csv":
            text, _enc = decode_bytes(p.read_bytes())
            if text is None:
                uncertain.append((rel, "编码识别失败(utf-8-sig/gb18030/utf-16)——转人工"))
                return
            tables.append(("csv", parse_csv_rows(text)))
        elif ext == ".xlsx":
            import openpyxl  # 可选依赖:缺失时该文件转 UNCERTAIN
            wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
            for ws in wb.worksheets:
                tables.append((ws.title or "sheet", list(ws.iter_rows(values_only=True))))
            wb.close()
        elif ext == ".pdf":
            import pdfplumber  # 可选依赖
            with pdfplumber.open(p) as pdf:
                text = "".join(pg.extract_text() or "" for pg in pdf.pages)
                if not text.strip():
                    uncertain.append((rel, "无文本层(疑似扫描件)——GATE 1b 限缩:转人工,不做 OCR"))
                    return
                for pi, pg in enumerate(pdf.pages, 1):
                    for tbl in (pg.extract_tables() or []):
                        tables.append((f"pdf:p{pi}", tbl))
        else:
            uncertain.append((rel, f"不支持的格式 {ext}——转人工"))
            return

        for label, rows in tables:
            tr = process_table(rows, rel, alias_map)
            found = set(tr.header.col_map.values())
            fact = FileFieldFacts(
                source_file=rel, sheet_or_page=label,
                header_found=tr.header.found,
                header_row_no=tr.header.row_no,
                columns_found=tuple(sorted(found)),
                columns_missing=tuple(c for c in CANONICAL_COLS if c not in found),
                data_rows=tr.data_rows,
                cells_expected=tr.cells_expected,
                cells_filled=tr.cells_filled,
                dropped_rows=tr.dropped_rows,
                completeness=round(completeness_ratio(tr.cells_filled, tr.cells_expected), 4),
                sha=sha,
            )
            cleaning_log.append({
                "file": rel, "sheet_or_page": label,
                "header_found": tr.header.found,
                "header_row_no": tr.header.row_no,
                "columns_found": list(fact.columns_found),
                "columns_missing": list(fact.columns_missing),
                "data_rows": tr.data_rows,
                "cells_filled": tr.cells_filled,
                "cells_expected": tr.cells_expected,
                "completeness": fact.completeness,
                "dropped_rows": tr.dropped_rows,
                "row_events": [asdict(e) for e in tr.row_events],
                "cell_fixes": [asdict(f) for f in tr.cell_fixes],
            })
            if not tr.header.found:
                uncertain.append((rel, f"表头识别失败(扫描 {tr.header.scanned} 个非空行未命中列头别名)——转人工"))
                continue
            missing_must = [c for c in MANDATORY_COLS if c not in found]
            if missing_must:
                uncertain.append((rel, f"关键列缺失({','.join(missing_must)})——转人工"))
                continue
            if tr.data_rows == 0:
                uncertain.append((rel, "表头已识别但无有效数据行——转人工"))
                continue
            records += tr.records
            field_facts.append(asdict(fact))
    except ImportError as e:
        uncertain.append((rel, f"解析依赖不可用({e.__class__.__name__}:解析库未安装)——转人工"))
    except Exception as e:  # 单文件失败不拖垮整批;如实登记
        uncertain.append((rel, f"解析失败:{e}——转人工"))


def step_p1_ingest(ctx: RunContext) -> None:
    """P1 归一摄入(G-16):目录 → 编码回退/清洗/表头识别 → records + 台账 + UNCERTAIN。

    表头识别失败/关键列缺失/编码不可解 → UNCERTAIN 转人工,不硬判(GATE 1b 限缩);
    行级丢弃与单格修复全部进清洗台账(消灭静默丢弃)。
    """
    root = Path((ctx.inputs.get("input-dir") or ".").strip()).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"报价文件目录不存在:{root}")

    alias_map = build_alias_map()
    extra_path = (ctx.inputs.get("aliases-config") or "").strip()
    if extra_path:  # 外置别名扩展(可选):{"canonical": ["别名",...]},只增不减
        ep = Path(extra_path)
        if not ep.is_file():
            raise FileNotFoundError(f"别名扩展配置不存在:{ep}")
        alias_map = build_alias_map(json.loads(ep.read_text(encoding="utf-8")))
    else:
        # 行业默认别名包(随包 industry_aliases.json;外置 aliases-config 优先级更高)
        industry = Path(__file__).resolve().parents[4] / "industry_aliases.json"
        if industry.is_file():
            alias_map = build_alias_map(json.loads(industry.read_text(encoding="utf-8")))

    records, uncertain = [], []
    sha_cache: dict[str, str] = {}
    cleaning_log: list[dict] = []
    field_facts: list[dict] = []

    for p in _iter_quote_files(root):
        rel = p.relative_to(root).as_posix()
        sha_cache[rel] = sha = _sha256_file(p)
        _ingest_one(p, rel, sha, alias_map, records, uncertain,
                    cleaning_log, field_facts)

    ctx.outputs["input_dir"] = str(root)
    # outputs 只存可序列化形态(引擎证据包要 json 化);对象在步间经 _records 传递
    ctx.outputs["records"] = [asdict(r) for r in records]
    ctx.outputs["record_count"] = len(records)
    ctx.outputs["uncertain"] = [{"file": f, "reason": r} for f, r in uncertain]
    ctx.outputs["cleaning_log"] = cleaning_log
    ctx.outputs["field_facts"] = field_facts
    ctx.outputs["_sha_cache"] = sha_cache
    fixed_cells = sum(len(c["cell_fixes"]) for c in cleaning_log)
    ctx.issues.append(Issue(
        IssueLevel.INFO,
        f"P1 归一:报价记录 {len(records)} 条,UNCERTAIN 转人工 {len(uncertain)} 项,"
        f"清洗修复 {fixed_cells} 格",
        {"files": len(sha_cache)},
    ))


def _default_config_path(name: str) -> Path:
    """缺省判据配置定位:CWD 优先,回落技能根(随包配置)。

    宿主按 SKILL.md cd 到技能根时 CWD 即技能根;在别处运行时沿场景包
    上溯(scenarios/<场景>/code → scripts → 技能根)找到随包的配置,
    不再因 CWD 变化而 FileNotFoundError。
    """
    cwd = Path(name)
    if cwd.is_file():
        return cwd
    # code → quote_precheck → scenarios → scripts → 技能根(随包配置所在地)
    skill_root = Path(__file__).resolve().parents[4]
    return skill_root / name


def step_p2_evaluate(ctx: RunContext) -> None:
    """P2 判据链:外置配置 → Q11/Q13/Q14/Q15 纯函数 → findings + unknown 台账。"""
    raw_cfg = (ctx.inputs.get("rules-config") or "quote_rules_config.json").strip()
    cfg_path = Path(raw_cfg) if Path(raw_cfg).is_absolute() else _default_config_path(raw_cfg)
    if not cfg_path.is_file():
        raise FileNotFoundError(f"判据配置不存在:{cfg_path}(threshold 外置,缺配置不运行)")
    config = json.loads(cfg_path.read_text(encoding="utf-8"))

    from detection.quote_records import QuoteRecord
    records = [QuoteRecord(**d) for d in ctx.outputs["records"]]
    facts = [FileFieldFacts(**d) for d in ctx.outputs.get("field_facts", [])]
    sha_cache = ctx.outputs["_sha_cache"]
    sha_of = lambda r: sha_cache.get(r.source_file, "")

    findings = []
    for rule in ALL_QUOTE_RULES:
        cfg = config.get(rule.rule_id.lower()) or config.get(rule.rule_id) or {}
        findings += rule.evaluate(records, cfg, sha_of, facts)

    known = [f for f in findings if getattr(f, "kind", "fact") != "unknown"]
    unknown = [f for f in findings if getattr(f, "kind", "") == "unknown"]
    counts = count_by_severity(known)
    verdict = compute_verdict(counts)

    ctx.outputs["findings"] = [f.as_dict() for f in known]
    ctx.outputs["unknown_ledger"] = [
        {"rule": f.rule, "description": f.description,
         "evidence_ref": f.evidence_ref.as_dict()} for f in unknown
    ]
    ctx.outputs["counts"] = counts
    ctx.outputs["verdict"] = verdict
    ctx.outputs["config_version"] = config.get("rules_config_version", "?")
    ctx.outputs["_config_path"] = str(cfg_path)


def step_p3_report(ctx: RunContext) -> None:
    """P3 预审报告:对照表 + 清洗台账 + 异常清单 + UNCERTAIN 清单 + VERDICT。"""
    from detection.quote_records import QuoteRecord
    records = [QuoteRecord(**d) for d in ctx.outputs.get("records", [])]
    known = ctx.outputs.get("findings", [])
    unknown = ctx.outputs.get("unknown_ledger", [])
    verdict = ctx.outputs.get("verdict", "PASS")
    counts = ctx.outputs.get("counts", {})
    log = ctx.outputs.get("cleaning_log", [])

    has_fx = any(r.currency or r.tax_basis for r in records)
    head = "| 供应商 | 物料 | 单价 | 数量 | MOQ | 包装 | 来源 |行|"
    if has_fx:
        head += " 币种 | 口径 |"
    lines = [
        "# 报价拉平预审报告",
        "",
        f"> 生成:{time.strftime('%Y-%m-%dT%H:%M:%S')} | 输入:{ctx.outputs.get('input_dir','')}",
        f"> 判据配置:{ctx.outputs.get('config_version','?')} | 记录 {len(records)} 条"
        f" | UNCERTAIN 转人工 {len(ctx.outputs.get('uncertain', []))} 件",
        "",
        "## 1. 横向拉平对照表(G-16)",
        "",
        head,
        "|---" * (9 if has_fx else 8) + "|",
    ]
    for r in sorted(records, key=lambda x: (x.key(), x.unit_price)):
        row = (f"| {r.supplier} | {r.item} | {r.unit_price:g} | {r.qty:g} | "
               f"{r.moq:g} | {r.package_unit or '-'} | {r.source_file} |{r.row_no}|")
        if has_fx:
            row += f" {r.currency or '-'} | {r.tax_basis or '-'} |"
        lines.append(row)

    lines += ["", "## 2. 清洗与标准化台账(80-20:表头识别+数据完整性)", ""]
    if log:
        lines += ["| 文件 | 表头行 | 命中列 | 缺失列 | 数据行 | 实填/应填 | 完整度 | 丢弃行 | 修复格 |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for c in log:
            lines.append(
                f"| {c['file']}({c['sheet_or_page']}) | {c['header_row_no'] or '-'} | "
                f"{len(c['columns_found'])}/{len(CANONICAL_COLS)} | "
                f"{','.join(c['columns_missing']) or '-'} | {c['data_rows']} | "
                f"{c['cells_filled']}/{c['cells_expected']} | {c['completeness']:.0%} | "
                f"{c['dropped_rows']} | {len(c['cell_fixes'])} |"
            )
    else:
        lines.append("(无清洗台账)")

    lines += ["", "## 3. 异常清单(E 族判据)", ""]
    if known:
        lines += ["| 级别 | 规则 | 说明 | 证据 |", "|---|---|---|---|"]
        for f in known:
            ev = f["evidence_ref"]
            lines.append(
                f"| {f['severity']} | {f['rule']} | {f['description']} | "
                f"{ev['file']}:{ev['line_start']}(sha:{ev['sha'][:19]}…)"
            )
    else:
        lines.append("(无 E 族异常 finding)")
    lines += ["", "## 4. UNCERTAIN 转人工清单", ""]
    for u in ctx.outputs.get("uncertain", []):
        lines.append(f"- 文件级:{u['file']}——{u['reason']}")
    for u in unknown:
        ev = u.get("evidence_ref", {})
        lines.append(f"- 判据级:{u['rule']}——{u['description']}(证据:{ev.get('file','')}:{ev.get('line_start','')})")
    if not ctx.outputs.get("uncertain") and not unknown:
        lines.append("(无)")
    lines += [
        "",
        f"## 5. 裁决",
        "",
        f"- VERDICT: **{verdict}**({verdict_action(verdict)})",
        f"- counts: {counts or '{}'}",
        "- 预审归机器,比价决策归人(不替人拍板)。",
    ]

    out_dir = ctx.runtime.output_dir()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report = out_dir / f"precheck_{stamp}.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    blob = out_dir / f"precheck_findings_{stamp}.json"
    blob.write_text(json.dumps({
        "verdict": verdict, "counts": counts,
        "records": [{k: getattr(r, k) for k in
                     ("supplier", "item", "unit_price", "qty", "moq",
                      "package_unit", "source_file", "row_no",
                      "currency", "tax_basis")} for r in records],
        "findings": known, "unknown_ledger": unknown,
        "uncertain_files": ctx.outputs.get("uncertain", []),
        "cleaning_log": log, "field_facts": ctx.outputs.get("field_facts", []),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    for p in (report, blob):
        if p.stat().st_size == 0:
            raise RuntimeError(f"产物落盘后为 0 字节:{p}")

    ctx.outputs["report_file"] = str(report)
    ctx.outputs["findings_file"] = str(blob)
    ctx.outputs["explain"] = (
        f"归一 {len(records)} 条,finding {len(known)} 条"
        f"({counts}),UNCERTAIN {len(unknown) + len(ctx.outputs.get('uncertain', []))} 项,裁决 {verdict}"
    )
    level = IssueLevel.REVIEW if verdict in ("BLOCKED", "REVIEW") else IssueLevel.INFO
    ctx.issues.append(Issue(level, f"裁决 {verdict}:{verdict_action(verdict)}", {
        "verdict": verdict, "counts": counts, "report": str(report),
    }))
