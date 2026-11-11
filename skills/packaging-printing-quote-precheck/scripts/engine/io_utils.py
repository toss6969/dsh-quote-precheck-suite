"""
io_utils.py —— 文件读写封装骨架

引擎侧只保留"所有场景都会用到"的基础能力：YAML 模板加载、JSON 写出。
通用办公格式转纯文本（xlsx/docx/pdf -> 文本行）是付费两线共有的引擎级工具，
见 parse_input.py——与编码容错（read_text_auto）同级；
场景特有的解析（哪列是单号、哪种行算噪声）仍属于场景包的 code/，
随场景包写死。不要为了"灵活"把解析规则做成外部脚本运行时动态加载
——那等于把可执行代码明文暴露，比配置外部化的问题更严重。
"""

from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path
from typing import Any

import yaml


def fingerprint_file(path: str | Path) -> dict[str, Any] | None:
    """输入文件的指纹（SHA-256 + 字节数 + 行数）——可复现性凭证。

    写进 last_result.json：事后可证明"这份产物确实来自这份输入"。
    文件不可读时返回 None，绝不影响主流程（指纹是凭证，不是门槛）。
    """
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "lines": data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0),
    }


def load_display_rules(rules_path: Path) -> dict[str, Any]:
    """
    读取 templates/ 下的 YAML——只应包含展示层/非核心参数。
    判断阈值属于场景包 code/（写死，不外部化），永远不进这份文件。
    """
    if not rules_path.exists():
        return {}
    with rules_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def write_result_json(output_path: Path, payload: dict[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    # 同名 .html 核验页：办公用户不看 JSON，凭证要能直接打开/截图
    write_result_html(output_path.with_suffix(".html"), payload)


_STATUS_STYLES = {
    "DONE": ("完成", "#1a7f37"),
    "REVIEW_REQUIRED": ("待复核", "#b26a00"),
    "FAILED": ("失败", "#c62828"),
}


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def render_result_html(payload: dict[str, Any]) -> str:
    """last_result.json 的人可读核验页（浅色、内联样式、零外部依赖）。

    与 write_result_json 同构同源：同一份 payload 渲染，两份文件永不说
    两套话。内容四块：终态徽章、输入指纹（可复现凭证）、溯源自检结论、
    issues 与判定依据（explain）。
    """
    scenario = _esc(payload.get("scenario", ""))
    task = _esc(payload.get("task", ""))
    status = str(payload.get("status", ""))
    label, color = _STATUS_STYLES.get(status, (status, "#555"))
    outputs = payload.get("outputs") or {}
    issues = payload.get("issues") or []
    fingerprints = payload.get("input_fingerprints") or {}

    artifacts = {k: v for k, v in outputs.items() if str(k).endswith("_file")}

    _CSS = (
        "<style>body{font-family:'Microsoft YaHei',sans-serif;background:#f6f7f9;"
        "color:#24292f;margin:0;padding:24px;}main{max-width:860px;margin:0 auto;}"
        "h1{font-size:20px;}h2{font-size:15px;border-left:4px solid #0969da;"
        "padding-left:8px;margin:24px 0 8px;}table{width:100%;border-collapse:collapse;"
        "background:#fff;font-size:13px;}th,td{border:1px solid #d0d7de;padding:6px 10px;"
        "text-align:left;vertical-align:top;word-break:break-all;}th{background:#f0f2f5;}"
        ".badge{display:inline-block;padding:2px 12px;border-radius:12px;color:#fff;"
        "font-size:14px;}pre{background:#fff;border:1px solid #d0d7de;"
        "padding:10px;font-size:12px;overflow-x:auto;white-space:pre-wrap;}"
        ".muted{color:#57606a;font-size:12px;}li{margin:4px 0;}</style>"
    )

    parts: list[str] = [
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        f"<title>运行核验 · {scenario}</title>",
        _CSS + "</head><body><main>",
        f"<h1>运行核验 · {scenario}</h1>",
        f"<p>任务：<b>{task}</b>　<span class=\"badge\" style=\"background:{color}\">"
        f"{label}</span>　"
        "<span class=\"muted\">本页由引擎自动生成，与 last_result.json 同源。</span></p>",
    ]

    parts.append("<h2>输入指纹（可复现凭证）</h2>")
    if fingerprints:
        parts.append(
            "<table><tr><th>输入</th><th>SHA-256</th><th>字节</th><th>行数</th></tr>"
        )
        for field, fp in fingerprints.items():
            parts.append(
                f"<tr><td>{_esc(field)}</td><td>{_esc(fp.get('sha256', ''))}</td>"
                f"<td>{_esc(fp.get('bytes', ''))}</td><td>{_esc(fp.get('lines', ''))}</td></tr>"
            )
        parts.append("</table>")
    else:
        parts.append("<p class=\"muted\">本次运行没有文件输入。</p>")

    parts.append("<h2>溯源自检</h2>")
    if "trace_verified" in outputs:
        verdict = (
            "<p>✅ 全部关键条目都能在输入原文中找到出处。</p>"
            if outputs.get("trace_verified")
            else "<p>⚠ 有条目在输入原文中找不到出处，请逐条人工核对：</p><ul>"
            + "".join(f"<li>{_esc(v)}</li>" for v in outputs.get("trace_violations") or [])
            + "</ul>"
        )
        parts.append(verdict)
    else:
        parts.append("<p class=\"muted\">本任务未声明溯源自检。</p>")

    if artifacts:
        parts.append("<h2>产物文件</h2><ul>")
        parts.extend(f"<li>{_esc(p)}</li>" for p in artifacts.values())
        parts.append("</ul>")

    parts.append("<h2>待处理项（issues）</h2>")
    if issues:
        parts.append("<table><tr><th>级别</th><th>说明</th></tr>")
        for issue in issues:
            parts.append(
                f"<tr><td>{_esc(issue.get('level', ''))}</td>"
                f"<td>{_esc(issue.get('message', ''))}</td></tr>"
            )
        parts.append("</table>")
    else:
        parts.append("<p class=\"muted\">无。</p>")

    explain = outputs.get("explain")
    parts.append("<h2>判定依据（explain）</h2>")
    parts.append(
        f"<pre>{_esc(json.dumps(explain, ensure_ascii=False, indent=2))}</pre>"
        if explain is not None
        else "<p class=\"muted\">本任务未输出判定依据。</p>"
    )

    parts.append(
        "<h2>完整输出（outputs）</h2>"
        f"<pre>{_esc(json.dumps(outputs, ensure_ascii=False, indent=2))}</pre>"
        "</main></body></html>"
    )
    return "".join(parts)


def write_result_html(output_path: Path, payload: dict[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_result_html(payload), encoding="utf-8")


# ---------------------------------------------------------------- 审计特性
# 批量汇总报告 + 审计证据包：本地版恒启用，无档位开关。

import time  # noqa: E402 —— 档位特性段落专用，避免与上文加载顺序耦合
import zipfile  # noqa: E402


def _sha256_of(path: Path) -> str:
    """文件级 SHA-256（证据包用；fingerprint_file 面向输入，这里面向产物）。"""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def render_batch_summary(
    output_root: Path,
    task_label: str,
    batch_dir: str,
    results: list[dict],
    counts: dict,
    denied_at: int | None,
) -> Path:
    """把 --batch 的逐文件结果聚合成一份人话汇总（output/batch/_summary-*.md）。

    results 是 cmd_batch 已收集的逐文件 dict（file/state/artifacts/issues），
    这里只做展示层聚合——判定已经发生，本函数绝不重新计算任何结论。
    """
    target_dir = output_root / "batch"
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"_summary-{time.strftime('%Y-%m-%d')}.md"

    lines = [
        f"# 批量汇总 · {task_label}",
        "",
        f"- 批量目录：{batch_dir}",
        f"- 文件总数：{len(results)}",
        f"- 完成：{counts.get('DONE', 0)} | 待复核：{counts.get('REVIEW_REQUIRED', 0)}"
        f" | 失败：{counts.get('FAILED', 0)}",
    ]
    if denied_at:
        lines.append(f"- **授权在第 {denied_at} 个文件处中断**（之前的文件已正常处理）")
    lines += ["", "| # | 文件 | 状态 | 产物 | 待处理项 |", "|---|---|---|---|---|"]

    for idx, r in enumerate(results, start=1):
        state = r.get("state", "?")
        artifacts = "、".join(str(v) for v in (r.get("artifacts") or {}).values()) or "—"
        issues = "；".join(
            f"[{i.get('level', '?')}] {i.get('message', '')}" for i in (r.get("issues") or [])
        ) or "—"
        name = Path(str(r.get("file", ""))).name
        # 竖线是表格分隔符，出现在文件名/消息里会撕裂表格
        for bad in ("|", "\n"):
            name = name.replace(bad, "_")
            issues = issues.replace(bad, " ")
        lines.append(f"| {idx} | {name} | {state} | {artifacts} | {issues} |")

    lines += ["", "— 汇总由引擎自动生成，逐文件明细见各文件产物目录与 last_result.json。", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_evidence_pack(dist_root: Path, manifest: dict[str, Any]) -> Path:
    """审计证据包：manifest + 产物哈希 + last_result.json 原文，打进一个 zip。

    定位是「事后可独立核验」：拿走 zip 的人不依赖引擎也能复算——
    输入指纹/产物哈希/溯源结论都在 manifest 里，_last_result.json 是兜底信源
    原文副本。同秒冲突用 -N 后缀递增，不覆盖（审计链上覆盖即销毁证据）。
    """
    target_dir = dist_root / "output" / "evidence"
    target_dir.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = target_dir / f"evidence-{stamp}.zip"
    n = 1
    while path.exists():
        path = target_dir / f"evidence-{stamp}-{n}.zip"
        n += 1

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        last = dist_root / "storage" / "last_result.json"
        if last.is_file():
            zf.write(last, "last_result.json")
        for rel, digest in (manifest.get("artifact_sha256") or {}).items():
            artifact = dist_root / rel
            if artifact.is_file() and _sha256_of(artifact) == digest:
                # 哈希对得上才收进包：落盘后再被改动过的产物不进审计链
                zf.write(artifact, rel)
    return path


def collect_artifact_hashes(dist_root: Path, outputs: dict[str, Any]) -> dict[str, str]:
    """对 outputs 里的产物文件（*_file）算相对路径 -> sha256，供 manifest 用。"""
    hashes: dict[str, str] = {}
    for key, value in outputs.items():
        if not str(key).endswith("_file"):
            continue
        p = Path(str(value))
        if not p.is_file():
            continue
        try:
            rel = p.resolve().relative_to(dist_root.resolve()).as_posix()
        except ValueError:
            continue  # 产物不在 dist 内（异常情况）：不进审计包，也不报错
        hashes[rel] = _sha256_of(p)
    return hashes
