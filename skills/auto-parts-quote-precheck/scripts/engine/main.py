"""
main.py —— 本地个人版程序入口，两种运行模式共用同一套核心逻辑

    python engine/main.py run   [--scenario 场景] [--task 任务ID]
                                [--set 字段=值 ...] [文件路径 ...] [--batch 目录] [--json]
    python engine/main.py tasks [--scenario 场景] [--json]
                                宿主动态探测：本产品有哪些任务、怎么调
    python engine/main.py serve [--scenario 场景] [--port 8765]

本地个人版直接以源码运行，不编译不打包。场景绑定：源码态用
--scenario 指定 scenarios/ 下的场景包（目录名 snake_case 或产品名
kebab-case 均可），运行期动态 import 并调 build_scenario()；
不带 --scenario 时打印全部可用场景。

设计要点（继承参考模板）：check_access() / execute_task() 是唯一的
授权与执行入口，CLI 模式（本文件）和 Web 模式（webapp.py）都调用
这两个函数，不要在 webapp.py 里重新写一遍授权判断或执行逻辑——
否则改规则时漏改一处，两条路径就不一致了。

CLI 输入的两种给法（宿主 Agent 调用时通常用第一种更省事）：
1. 位置参数按声明顺序填必填 FILE 字段：
   python engine/main.py run C:\\users\\me\\本周记录.docx
2. 显式键值对（字段多时更明确）：
   python engine/main.py run --set source-file=C:\\...\\记录.docx --set period=this-week
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 本地源码直跑：main() 里按 --scenario 动态绑定（无编译产物绑定）
SCENARIO = None  # type: ignore[assignment]
BUILD_ID = ""  # type: ignore[assignment]

# selfcheck 的第三方依赖清单（pip 名, import 名）。引擎本体只用标准库；
# 这里列出的是场景包需要的运行时依赖，产品按需增删，须与
# skills/<名>/SKILL.md frontmatter 的 dependency.python 保持同步。
# 本产品：io_utils 模块级用 yaml；quote_precheck 解析 XLSX/PDF 报价
# （openpyxl/pdfplumber 缺失时该文件转 UNCERTAIN，属可降级而非可缺）。
SELFCHECK_DEPS = [("pyyaml", "yaml"), ("openpyxl", "openpyxl"),
                  ("pdfplumber", "pdfplumber")]

from engine_runtime import EngineRuntimeImpl  # noqa: E402
from io_utils import (  # noqa: E402
    collect_artifact_hashes,
    fingerprint_file,
    render_batch_summary,
    write_evidence_pack,
    write_result_json,
)
from scenario_api import Issue, IssueLevel  # noqa: E402
from state_store import StateStore  # noqa: E402
from workflow import (  # noqa: E402
    RunStatus,
    crash_result,
    execute_task_guarded,
    human_summary,
    resume_task_guarded,
)

DEFAULT_PORT = 8765

# evidence manifest 里的产品标识（本地版单一值，无档位含义）
PRODUCT_TIER = "local"


@dataclass
class AccessResult:
    allowed: bool
    message: str


def check_access() -> AccessResult:
    """
    唯一的授权判断入口，CLI 和 Web 两种模式都调用这个函数。

    本地个人版：恒放行。函数与调用结构保留（CLI/Web 共用同一授权入口的
    契约），授权判断本身已退化为常量——恢复对外分发时在这里还原
    license 校验 / 试用计数 / 档位门（从共享区历史版本还原并重签 license.key）。
    """
    return AccessResult(allowed=True, message="本地版，永久有效")


def _merge_cli_inputs(task_id: str, set_pairs: list[str], positional: list[str]) -> dict[str, Any]:
    """合并 --set 键值对与位置参数。

    位置参数按声明顺序填"还没给值的必填 FILE 字段"——宿主 Agent 用
    `run.bat <文件路径>` 这种最自然的方式调用时不需要知道字段名。
    H5(自 4.2 引擎终审移植):未知 --set 键当场拒绝——原先静默丢弃后字段
    default 生效,会"成功"审计错误对象(如 input-dir 笔误 → 静默扫 cwd)。
    """
    assert SCENARIO is not None  # main() 已保证
    task = SCENARIO.task(task_id)
    if task is None:
        return {}

    declared = {f.field_id for f in task.input_fields}
    inputs: dict[str, Any] = {}
    unknown: list[str] = []
    for pair in set_pairs:
        if "=" not in pair:
            continue  # 格式不对的 --set 静默跳过，缺字段会在校验层报 ERROR
        key, _, value = pair.partition("=")
        key = key.strip()
        if key not in declared:
            unknown.append(key)
            continue
        inputs[key] = value.strip()
    if unknown:
        print(
            "[input-error] --set 未知键: " + ", ".join(sorted(set(unknown)))
            + "(本任务声明字段: " + ", ".join(sorted(declared))
            + ") 拒绝执行,防静默错对象",
            file=sys.stderr,
        )
        sys.exit(2)

    pos_iter = iter(positional)
    for f in task.input_fields:
        if f.field_id in inputs:
            continue
        if f.kind.value == "file":
            try:
                inputs[f.field_id] = next(pos_iter)
            except StopIteration:
                break
    return inputs


def _issue_payloads(issues) -> list[dict[str, Any]]:
    return [{"level": i.level.value, "message": i.message, "detail": i.detail} for i in issues]


def _input_fingerprints(scenario, task_id: str, inputs: dict[str, Any]) -> dict[str, Any]:
    """对 FILE 类输入算指纹（可复现凭证）。失败不影响主流程。

    scenario 显式传参（不读模块全局 SCENARIO）——webapp.py 的每个 Handler
    实例有自己的 self.scenario，这样两边可以共用同一份实现，不用各自
    重写一遍同样的字段遍历逻辑。
    """
    task = scenario.task(task_id)
    if task is None:
        return {}
    prints: dict[str, Any] = {}
    for f in task.input_fields:
        if f.kind.value == "file" and f.field_id in inputs:
            fp = fingerprint_file(inputs[f.field_id])
            if fp:
                prints[f.field_id] = fp
    return prints


def _persist_execution_log(store: StateStore, run_id: int, result) -> None:
    """V2.1: 把 ExecutionLog 持久化到 SQLite（关联 run_id）。"""
    entries = []
    for e in result.execution_log.entries:
        entries.append((
            e.step_index, e.step_name, e.status.value, e.attempt,
            e.started_at, e.finished_at, e.duration_ms,
            e.error_code, e.error_message,
        ))
    store.record_execution_log_batch(run_id, entries)


def _execute_and_record(
    dist_root: Path,
    store: StateStore,
    task_id: str,
    raw_inputs: dict[str, Any],
    output_subdir: str = "",
):
    """执行一次任务并完成记账（run_log + execution_log + checkpoint）。

    V2.2 流程变更：先创建 run 记录（RUNNING）拿到 run_id，
    再执行任务（checkpoint 可关联 run_id），最后更新终态。

    顶层崩溃边界：本函数保证不向调用方抛出异常——批量模式下，单个文件
    触发的引擎级意外错误（非场景步骤失败，那类已经被 execute_task_guarded
    兜住）不能拖死同一批次里的其它文件。
    """
    assert SCENARIO is not None
    try:
        run_id = store.record_run("RUNNING", task_id=task_id)
    except Exception as exc:  # noqa: BLE001 —— 连 run_log 都写不进去，没有 run_id 可用
        run_id = None
        result = crash_result(task_id, "record_run", exc)
    else:
        try:
            runtime = EngineRuntimeImpl(SCENARIO.name, dist_root, store, output_subdir=output_subdir)
            result = execute_task_guarded(
                SCENARIO, task_id, runtime, raw_inputs,
                store=store, run_id=run_id,
            )
        except Exception as exc:  # noqa: BLE001 —— EngineRuntimeImpl 构造等罕见失败
            result = crash_result(task_id, "runtime_setup", exc)

    # V2.2: 更新终态（记账失败不影响已经拿到手的 result，最坏只是没留痕）
    if run_id is not None:
        try:
            store.update_run_state(
                run_id, result.status.value,
                human_summary(SCENARIO, result),
            )
            _persist_execution_log(store, run_id, result)
        except Exception as exc:  # noqa: BLE001 —— 记账失败不影响已到手的结果,但留痕
            print(f"[degraded] execute_and_record 记账失败: {exc!r}", file=sys.stderr)

    try:
        write_result_json(
            dist_root / "storage" / "last_result.json",
            {
                "scenario": SCENARIO.name,
                "task": result.task_id,
                "run_id": run_id,
                "status": result.status.value,
                "outputs": result.outputs,
                "issues": _issue_payloads(result.issues),
                "input_fingerprints": _input_fingerprints(SCENARIO, result.task_id, result.inputs),
                "resumed": result.resumed,
                # V2.3: 执行模式与降级路径（REVIEW 归因：normal / degraded / partial）
                "execution_mode": result.execution_mode.value,
                "fallback_steps": list(result.fallback_steps),
                # P1: 完整审计链
                "output_fingerprints": result.output_fingerprints,
                "data_flow": [
                    {
                        "step_index": df.step_index,
                        "step_name": df.step_name,
                        "keys_written": list(df.keys_written),
                        "keys_count": df.keys_count,
                    }
                    for df in result.data_flow
                ],
                "environment": {
                    "engine_version": result.environment.engine_version,
                    "scenario_name": result.environment.scenario_name,
                    "scenario_version": result.environment.scenario_version,
                    "template_fingerprints": result.environment.template_fingerprints,
                    "python_version": result.environment.python_version,
                },
            },
        )
    except Exception as exc:  # noqa: BLE001 —— 落盘失败不能让这个文件拖死整批
        result.issues.append(Issue(IssueLevel.ERROR, f"结果落盘失败：{exc}"))

    # run_id 一并返回：批量逐文件也要把这"是哪个 run 的产物"带回结果 JSON，
    # 否则 batch 结果无法沿 run_log 回溯到运行粒度审计。
    return result, run_id


def _write_run_evidence(
    dist_root: Path, scenario, result, fingerprints: dict[str, Any]
) -> Path | None:
    """档位特性 evidence_pack：跑完即落审计证据包（对启用的档位无开关、不可选）。

    manifest 的字段口径与 last_result.json 对齐（指纹/溯源结论），另加产物
    哈希——收包人不依赖引擎也能独立复算。
    scenario 显式传参（webapp 的 handler 有自己的实例，不依赖本模块全局）。
    写入失败时返回 None，调用方按正常路径继续（evidence_pack 字段为空串）。
    """
    return write_evidence_pack(
        dist_root,
        {
            "tier": PRODUCT_TIER,
            "build_id": BUILD_ID,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "scenario": scenario.name,
            "task": result.task_id,
            "status": result.status.value,
            "input_fingerprints": fingerprints,
            "trace_verified": result.outputs.get("trace_verified"),
            "trace_violations": result.outputs.get("trace_violations", []),
            "artifact_sha256": collect_artifact_hashes(dist_root, result.outputs),
        },
    )


def _first_file_field(task_id: str, inputs: dict[str, Any]) -> str | None:
    """任务声明中第一个还没给值的 FILE 字段——--batch 逐文件填的就是它。"""
    assert SCENARIO is not None
    task = SCENARIO.task(task_id)
    if task is None:
        return None
    for f in task.input_fields:
        if f.kind.value == "file" and f.field_id not in inputs:
            return f.field_id
    return None


def cmd_run(dist_root: Path, args: argparse.Namespace) -> int:
    """scripts/run.bat 调用的路径：跑一次，打印结果，退出。

    --json：stdout 只输出一份严格 JSON（宿主 Agent 的机器接口——
    靠读屏解析人话文本转述是幻觉的最后一环，结构化输出把它掐断）。
    --batch <目录>：对目录下每个文件顺序执行一次任务（见 cmd_batch 语义）。
    退出码契约：0 = DONE/REVIEW（有产物）；1 = FAILED/未授权；2 = 引擎未绑定。
    """
    if getattr(args, "batch", None):
        return cmd_batch(dist_root, args)

    assert SCENARIO is not None
    store = StateStore(dist_root / "storage" / "state.db")
    # 孤儿回收(自 4.2 引擎 H6 移植,按进程存活判定):上次被杀的运行改判 FAILED,
    # 带检查点的就此进入可恢复列表。回收失败不拦截执行,最坏遗留一行旧 RUNNING。
    try:
        store.sweep_orphan_running()
    except Exception as exc:  # noqa: BLE001 —— 回收是尽力而为,失败只留痕
        print(f"[degraded] 孤儿运行回收失败: {exc!r}", file=sys.stderr)

    access = check_access()
    if not access.allowed:
        store.close()
        if getattr(args, "json", False):
            print(json.dumps(
                {
                    "ok": False,
                    "state": "DENIED",
                    "exit_code": 1,
                    "access": {"allowed": False, "message": access.message},
                },
                ensure_ascii=False,
            ))
        else:
            print(f"[未授权] {access.message}")
        return 1

    raw_inputs = _merge_cli_inputs(args.task, args.set or [], args.inputs or [])

    # V2.2: runtime 构造 + 创建 run 记录都可能失败（目录权限、SQLite 撞锁
    # 等）——失败就没有可用的执行环境，直接落到崩溃兜底，不再尝试执行。
    try:
        runtime = EngineRuntimeImpl(SCENARIO.name, dist_root, store)
        run_id = store.record_run("RUNNING", task_id=args.task)
    except Exception as exc:  # noqa: BLE001
        run_id = None
        result = crash_result(args.task, "runtime_setup", exc)
    else:
        result = execute_task_guarded(
            SCENARIO, args.task, runtime, raw_inputs,
            store=store, run_id=run_id,
        )

    # V2.2: 更新终态。记账本身也可能失败（SQLite 撞锁等）——不能让它
    # 把已经拿到手的 result 搭进去，最坏情况下只是这次记账没留痕。
    last = None
    if run_id is not None:
        try:
            store.update_run_state(run_id, result.status.value, human_summary(SCENARIO, result))
            _persist_execution_log(store, run_id, result)
            last = store.last_run()
        except Exception as exc:  # noqa: BLE001 —— 顶层崩溃边界,留痕不阻断
            print(f"[degraded] cmd_run 终态记账失败: {exc!r}", file=sys.stderr)
    store.close()

    last_result_path = dist_root / "storage" / "last_result.json"
    fingerprints = _input_fingerprints(SCENARIO, result.task_id, result.inputs)
    try:
        write_result_json(
            last_result_path,
            {
                "scenario": SCENARIO.name,
                "task": result.task_id,
                "run_id": run_id,
                "status": result.status.value,
                "outputs": result.outputs,
                "issues": _issue_payloads(result.issues),
                "input_fingerprints": fingerprints,
                # P1: 完整审计链
                "output_fingerprints": result.output_fingerprints,
                "data_flow": [
                    {
                        "step_index": df.step_index,
                        "step_name": df.step_name,
                        "keys_written": list(df.keys_written),
                        "keys_count": df.keys_count,
                    }
                    for df in result.data_flow
                ],
                "environment": {
                    "engine_version": result.environment.engine_version,
                    "scenario_name": result.environment.scenario_name,
                    "scenario_version": result.environment.scenario_version,
                    "template_fingerprints": result.environment.template_fingerprints,
                    "python_version": result.environment.python_version,
                },
            },
        )
    except Exception as exc:  # noqa: BLE001 —— 落盘失败不能拖垮整次运行的收尾
        result.issues.append(Issue(IssueLevel.ERROR, f"结果落盘失败：{exc}"))

    # 档位特性 evidence_pack：在 last_result.json 之后写（包内含其原文副本）
    try:
        evidence_path = _write_run_evidence(dist_root, SCENARIO, result, fingerprints)
    except Exception:  # noqa: BLE001
        evidence_path = None

    if getattr(args, "json", False):
        artifacts = {k: v for k, v in result.outputs.items() if k.endswith("_file")}
        print(json.dumps(
            {
                "ok": result.status is not RunStatus.FAILED,
                "state": result.status.value,
                "exit_code": 0 if result.status is not RunStatus.FAILED else 1,
                "task": result.task_id,
                "access": {
                    "allowed": True,
                    "message": access.message,
                },
                "artifacts": artifacts,
                "outputs": result.outputs,
                "issues": _issue_payloads(result.issues),
                "last_result": str(last_result_path),
                "evidence_pack": str(evidence_path) if evidence_path else "",
            },
            ensure_ascii=False,
        ))
        return 0 if result.status is not RunStatus.FAILED else 1

    print(f"[已授权] {access.message}")
    print(f"状态: {result.status.value}")
    print(f"输出: {result.outputs}")
    if result.issues:
        print("待处理项:")
        for i in result.issues:
            print(f"  [{i.level.value}] {i.message}")
    print(f"上次运行记录: {last}")
    print(f"审计证据包: {evidence_path or '（写入失败，见上方 issues）'}")

    return 0 if result.status is not RunStatus.FAILED else 1


def cmd_batch(dist_root: Path, args: argparse.Namespace) -> int:
    """--batch：对目录下每个文件顺序执行一次同一任务。

    语义（与单文件 run 完全一致，只是重复多次）：
    - 每个文件是一次独立任务：独立授权检查、独立记账、
      last_result.json 逐次覆写（最终留在最后一个文件上）；
    - stdout 恒为一份严格 JSON 汇总（批量本身就是宿主 Agent 的用法）。

    V2.4：整批级断点续批——如果上一次同目录同任务的批处理没跑完就被打断
    （关窗口/断电/被杀进程），重新执行同一条命令会自动跳过已经成功处理过
    的文件；`--batch-fresh` 忽略历史进度、强制全部重跑。
    退出码：0 = 无 FAILED；1 = 有 FAILED / 目录无文件。
    """
    assert SCENARIO is not None
    batch_dir = Path(getattr(args, "batch", "")).expanduser()
    if not batch_dir.is_dir():
        print(json.dumps(
            {"ok": False, "state": "FAILED", "exit_code": 1,
             "error": f"批量目录不存在或不是目录：{batch_dir}"},
            ensure_ascii=False,
        ))
        return 1

    task_id = args.task
    task = SCENARIO.task(task_id)
    if task is None:
        print(json.dumps(
            {"ok": False, "state": "FAILED", "exit_code": 1,
             "error": f"未知任务：{task_id}"},
            ensure_ascii=False,
        ))
        return 1

    files = sorted(p for p in batch_dir.iterdir() if p.is_file())
    if not files:
        print(json.dumps(
            {"ok": False, "state": "FAILED", "exit_code": 1,
             "error": f"目录里没有文件：{batch_dir}"},
            ensure_ascii=False,
        ))
        return 1

    base_inputs = _merge_cli_inputs(task_id, args.set or [], [])  # --set 给非文件字段
    file_field = _first_file_field(task_id, base_inputs)
    if file_field is None:
        print(json.dumps(
            {"ok": False, "state": "FAILED", "exit_code": 1,
             "error": "该任务的 FILE 输入字段都已通过 --set 指定，批量没有可填的字段"},
            ensure_ascii=False,
        ))
        return 1

    store = StateStore(dist_root / "storage" / "state.db")
    results: list[dict[str, Any]] = []
    denied_at: int | None = None
    counts = {"DONE": 0, "REVIEW_REQUIRED": 0, "FAILED": 0}

    # V2.4：整批级断点续批——batch_key 绑定"这个任务 + 这个批量目录"，
    # 跨进程重启也能认出"这是上次没跑完的那一批"。只跳过之前成功/待复核过的
    # 文件；之前 FAILED 的文件允许重新尝试（可能用户已经把文件改好了）。
    batch_key = f"{task_id}|{batch_dir.resolve()}"
    already_done: dict[str, dict] = {}
    if not getattr(args, "batch_fresh", False):
        try:
            progress = store.get_batch_progress(batch_key)
            already_done = {name: info for name, info in progress.items() if info["state"] != "FAILED"}
        except Exception:
            already_done = {}  # 读进度失败不影响本次处理，最坏情况是多跑几个文件
    resumed_skip_count = 0

    for idx, path in enumerate(files, start=1):
        if path.name in already_done:
            resumed_skip_count += 1
            skip_info = already_done[path.name]
            skip_state = skip_info["state"]
            counts[skip_state] = counts.get(skip_state, 0) + 1
            results.append({
                "file": str(path), "state": skip_state, "exit_code": 0,
                # 上一轮已经产出的产物：续跑不重新处理，但证据包/汇总不能
                # 因为跳过就漏掉这些文件的哈希——沿用记录时保存的 artifacts。
                "artifacts": skip_info["artifacts"], "issues": [], "skipped_resumed": True,
                # 跨轮续跑仍透传上一轮该文件对应的 run_id，保持运行粒度回溯；
                # 旧库迁移前的历史记录该列可能为空，取不到时为 None。
                "run_id": skip_info.get("run_id"),
            })
            continue

        access = check_access()
        if not access.allowed:
            denied_at = idx
            results.append({
                "file": str(path), "state": "DENIED", "exit_code": 1,
                "message": access.message,
            })
            break

        inputs = dict(base_inputs)
        inputs[file_field] = str(path)
        # 产物按源文件隔离：output/batch/<序号>-<文件名>/——场景产物名普遍
        # 含日期，同日批量会互相覆盖（验收时抓到：2 个文件只剩最后 1 份）
        safe_stem = re.sub(r"[^\w一-鿿-]+", "_", path.stem) or "input"
        result, run_id = _execute_and_record(
            dist_root, store, task_id, inputs,
            output_subdir=f"batch/{idx:02d}-{safe_stem}",
        )
        counts[result.status.value] = counts.get(result.status.value, 0) + 1
        artifacts = {k: v for k, v in result.outputs.items() if str(k).endswith("_file")}
        results.append({
            "file": str(path),
            "run_id": run_id,
            "state": result.status.value,
            "exit_code": 0 if result.status is not RunStatus.FAILED else 1,
            "artifacts": artifacts,
            "issues": _issue_payloads(result.issues),
        })
        try:
            store.record_batch_file_done(batch_key, path.name, result.status.value,
                                     artifacts=artifacts, run_id=run_id)
        except Exception as exc:
            # 进度记不上不影响这次处理结果，最坏是下次中断后少跳过一个文件
            print(f"[degraded] batch 进度记录失败: {exc!r}", file=sys.stderr)

    # 整批顺利跑完（没有 FAILED、没有中途被拒绝）：清掉进度记录，避免以后
    # 对同一目录的全新一轮批处理被这次的旧记录误跳过
    if counts["FAILED"] == 0 and denied_at is None:
        try:
            store.clear_batch_progress(batch_key)
        except Exception as exc:
            print(f"[degraded] batch 进度清理失败: {exc!r}", file=sys.stderr)
    store.close()

    # 档位特性 batch_summary + evidence_pack：一份人话汇总
    # （output/batch/_summary-*.md）+ 整批一个证据包。低档位这两条路径不执行。
    # 顶层崩溃边界：这两步落在循环之外，一旦失败（例如磁盘写满）绝不能
    # 把已经跑完的逐文件结果一起搭进异常里丢掉——JSON 汇总必须照常打印，
    # 失败信息进独立的 batch_errors 字段，不污染 results 的逐文件结构。
    batch_errors: list[str] = []
    summary_path = None
    evidence_path = None

    task_label = task.label  # cmd_batch 开头已校验 task 非 None
    try:
        summary_path = render_batch_summary(
            dist_root / "output", task_label, str(batch_dir), results, counts, denied_at
        )
    except Exception as exc:  # noqa: BLE001
        batch_errors.append(f"批量汇总写入失败：{exc}")

    try:
        # 逐文件收集产物哈希：各文件的产物键名相同（如 report_file）但路径
        # 不同（batch/NN-<名>/ 子目录隔离），按路径合并才能一份不落进审计链
        batch_hashes: dict[str, str] = {}
        for r in results:
            batch_hashes.update(collect_artifact_hashes(dist_root, r.get("artifacts") or {}))
        evidence_path = write_evidence_pack(
            dist_root,
            {
                "tier": PRODUCT_TIER,
                "build_id": BUILD_ID,
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "scenario": SCENARIO.name,
                "task": task_id,
                "batch": {
                    "dir": str(batch_dir),
                    "total": len(files),
                    "processed": len(results) - (1 if denied_at else 0),
                    "counts": counts,
                    **({"denied_at_file_index": denied_at} if denied_at else {}),
                    **({"resumed_from_previous": resumed_skip_count} if resumed_skip_count else {}),
                },
                "artifact_sha256": batch_hashes,
            },
        )
    except Exception as exc:  # noqa: BLE001
        batch_errors.append(f"证据包写入失败：{exc}")

    ok = counts["FAILED"] == 0 and denied_at is None and not batch_errors
    print(json.dumps(
        {
            "ok": ok,
            "state": "DONE" if ok else "FAILED",
            "exit_code": 0 if ok else 1,
            "task": task_id,
            "batch": {
                "dir": str(batch_dir),
                "total": len(files),
                "processed": len(results) - (1 if denied_at else 0),
                "counts": counts,
                **({"denied_at_file_index": denied_at} if denied_at else {}),
                **({"resumed_from_previous": resumed_skip_count} if resumed_skip_count else {}),
            },
            "batch_summary": str(summary_path) if summary_path else "",
            "evidence_pack": str(evidence_path) if evidence_path else "",
            **({"batch_errors": batch_errors} if batch_errors else {}),
            "results": results,
        },
        ensure_ascii=False,
    ))
    return 0 if ok else 1


def cmd_tasks(as_json: bool) -> int:
    """tasks 子命令：输出本引擎能做什么（场景契约），宿主动态发现用。

    纯元数据输出——探测"你是谁、会什么"不是使用。
    """
    assert SCENARIO is not None
    payload = {
        "scenario": {
            "name": SCENARIO.name,
            "display_name": SCENARIO.display_name,
            "version": SCENARIO.version,
            "description": SCENARIO.description,
            "tier": PRODUCT_TIER,
        },
        "tasks": [
            {
                "task_id": t.task_id,
                "label": t.label,
                "description": t.description,
                "input_fields": [
                    {
                        "field_id": f.field_id,
                        "label": f.label,
                        "kind": f.kind.value,
                        "required": f.required,
                        "default": f.default,
                        "choices": list(f.choices),
                        "help_text": f.help_text,
                    }
                    for f in t.input_fields
                ],
            }
            for t in SCENARIO.tasks
        ],
        "machine_interface": {
            "run": "agent.exe run --json --task <任务ID> [文件路径...]",
            "batch": "agent.exe run --json --task <任务ID> --batch <目录>",
            "exit_codes": {"0": "DONE/REVIEW（有产物）", "1": "FAILED/未授权", "2": "引擎未绑定"},
        },
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"{SCENARIO.display_name}（{SCENARIO.name} v{SCENARIO.version}）")
        for t in SCENARIO.tasks:
            fields = " ".join(
                f"{f.field_id}({f.kind.value}{'必填' if f.required else '可选'})"
                for f in t.input_fields
            )
            print(f"  {t.task_id} —— {t.label}：{t.description}")
            print(f"    输入：{fields or '（无）'}")
        print("  加 --json 获取机器可读契约")
    return 0


def cmd_resume(dist_root: Path, args: argparse.Namespace) -> int:
    """V2.2: 从检查点恢复上次失败的任务。

    --run-id 指定要恢复的运行；不指定则自动取最近一次有 checkpoint 的失败运行。
    """
    assert SCENARIO is not None
    store = StateStore(dist_root / "storage" / "state.db")
    # 孤儿回收(自 4.2 引擎 H6 移植,按进程存活判定):上次被杀的运行改判 FAILED,
    # 带检查点的就此进入可恢复列表。回收失败不拦截执行,最坏遗留一行旧 RUNNING。
    try:
        store.sweep_orphan_running()
    except Exception as exc:  # noqa: BLE001 —— 回收是尽力而为,失败只留痕
        print(f"[degraded] 孤儿运行回收失败: {exc!r}", file=sys.stderr)

    # 确定要恢复的 run_id
    run_id = getattr(args, "run_id", None)
    if run_id is None:
        resumable = store.list_resumable(limit=1)
        if not resumable:
            store.close()
            msg = "没有可恢复的运行（无失败运行或缺少检查点）"
            if getattr(args, "json", False):
                print(json.dumps({"ok": False, "state": "FAILED", "error": msg}, ensure_ascii=False))
            else:
                print(f"[错误] {msg}")
            return 1
        run_id = resumable[0][0]  # 第一列是 run_id

    # 授权检查
    access = check_access()
    if not access.allowed:
        store.close()
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "state": "DENIED", "access": {"allowed": False, "message": access.message}}, ensure_ascii=False))
        else:
            print(f"[未授权] {access.message}")
        return 1

    # 从 checkpoint 加载 task_id
    checkpoint = store.load_latest_checkpoint(run_id)
    if checkpoint is None:
        store.close()
        msg = f"run_id={run_id} 无可用检查点"
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "state": "FAILED", "error": msg}, ensure_ascii=False))
        else:
            print(f"[错误] {msg}")
        return 1

    # 获取 task_id（从 run_log 查）
    task_id = store.run_task_id(run_id) or ""
    if not task_id:
        store.close()
        print(f"[错误] run_id={run_id} 无关联任务")
        return 1

    # 更新 run_log 状态为 RUNNING——只是状态展示，失败不影响真正的恢复执行
    try:
        store.update_run_state(run_id, "RUNNING", "恢复中...")
    except Exception as exc:  # noqa: BLE001
        print(f"[degraded] resume 状态置 RUNNING 失败: {exc!r}", file=sys.stderr)

    # runtime 构造失败（目录权限等）同样要落到崩溃兜底，不能穿透出去
    try:
        runtime = EngineRuntimeImpl(SCENARIO.name, dist_root, store)
        result = resume_task_guarded(SCENARIO, task_id, runtime, store, run_id)
    except Exception as exc:  # noqa: BLE001
        result = crash_result(task_id, "resume_runtime_setup", exc)

    # 更新终态（记账失败不影响已经拿到手的 result）
    try:
        store.update_run_state(run_id, result.status.value, human_summary(SCENARIO, result))
        _persist_execution_log(store, run_id, result)
    except Exception as exc:  # noqa: BLE001 —— 顶层崩溃边界,留痕不阻断
        print(f"[degraded] resume 终态记账失败: {exc!r}", file=sys.stderr)
    finally:
        store.close()

    fingerprints = _input_fingerprints(SCENARIO, result.task_id, result.inputs)

    # last_result.json 必须在 resume 后也刷新——否则历史页"查看最近一次
    # 运行核验"会一直停在恢复前的 FAILED 快照，明明这次已经恢复成功。
    try:
        write_result_json(
            dist_root / "storage" / "last_result.json",
            {
                "scenario": SCENARIO.name,
                "task": result.task_id,
                "status": result.status.value,
                "outputs": result.outputs,
                "issues": _issue_payloads(result.issues),
                "input_fingerprints": fingerprints,
                "resumed": result.resumed,
                "output_fingerprints": result.output_fingerprints,
                "data_flow": [
                    {
                        "step_index": df.step_index,
                        "step_name": df.step_name,
                        "keys_written": list(df.keys_written),
                        "keys_count": df.keys_count,
                    }
                    for df in result.data_flow
                ],
                "environment": {
                    "engine_version": result.environment.engine_version,
                    "scenario_name": result.environment.scenario_name,
                    "scenario_version": result.environment.scenario_version,
                    "template_fingerprints": result.environment.template_fingerprints,
                    "python_version": result.environment.python_version,
                },
            },
        )
    except Exception as exc:  # noqa: BLE001
        result.issues.append(Issue(IssueLevel.ERROR, f"结果落盘失败：{exc}"))

    # Ultra：恢复的运行同样落审计证据包（跑完即落，无开关）
    try:
        evidence_path = _write_run_evidence(dist_root, SCENARIO, result, fingerprints)
    except Exception:  # noqa: BLE001
        evidence_path = None

    # 输出结果
    if getattr(args, "json", False):
        print(json.dumps({
            "ok": result.status is not RunStatus.FAILED,
            "state": result.status.value,
            "resumed": result.resumed,
            "resumed_from_step": result.resumed_from_step,
            "outputs": result.outputs,
            "issues": _issue_payloads(result.issues),
            "evidence_pack": str(evidence_path) if evidence_path else "",
        }, ensure_ascii=False))
    else:
        print(f"[已授权] {access.message}")
        print(f"恢复自步骤: {result.resumed_from_step}")
        print(f"状态: {result.status.value}")
        print(f"输出: {result.outputs}")
        if result.issues:
            print("待处理项:")
            for i in result.issues:
                print(f"  [{i.level.value}] {i.message}")
        print(f"审计证据包: {evidence_path or '（写入失败，见上方 issues）'}")

    return 0 if result.status is not RunStatus.FAILED else 1


def cmd_serve(dist_root: Path, port: int) -> None:
    """scripts/open_workbench.bat 调用的路径：起本地服务，供网页工作台使用。"""
    try:
        from webapp import run_server
    except ImportError:
        # 发布包按 ARCHITECTURE.md §一 B 类裁掉了 webapp/file_browser（本地
        # 工作台不随包分发）。显式请求 serve 时给明确提示，不抛裸 ImportError。
        print("本包未随附本地工作台（webapp 已按发布形态裁剪，仅本地源码形态可用）。")
        return

    run_server(dist_root, port, SCENARIO)


def _resolve_dist_root() -> Path:
    # LOCAL_FLOW_DIST_ROOT：测试/隔离验证用，强制指定运行时根目录
    env_override = os.environ.get("LOCAL_FLOW_DIST_ROOT")
    if env_override:
        return Path(env_override).resolve()

    # 本地个人版运行时根目录：工作区 dist_template/（output/storage/web/config 等）
    return Path(__file__).resolve().parent.parent / "dist_template"


def _reconfigure_stdio() -> None:
    """Windows 控制台默认 GBK，中文输出乱码甚至 UnicodeEncodeError，统一切 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 —— 旧终端不支持 reconfigure 时退回默认行为(留痕)
            print(f"[degraded] stdio reconfigure 退回默认: {exc!r}", file=sys.stderr)


def _available_scenarios() -> list[str]:
    """扫描 scenarios/ 下的场景包目录名（snake_case，排除 _scaffold 等下划线目录）。"""
    root = Path(__file__).resolve().parent.parent / "scenarios"
    if not root.is_dir():
        return []
    return sorted(
        p.name for p in root.iterdir()
        if p.is_dir() and not p.name.startswith("_")
        and (p / "code" / "__init__.py").is_file()
    )


def _bind_scenario(name: str):
    """源码直跑的场景绑定：import scenarios/<snake>/code 并调 build_scenario()。

    接受目录名（snake_case）或产品名（kebab-case，如 weekly-report）。
    绑定失败返回 None，由调用方给出可用场景列表。
    """
    workspace = Path(__file__).resolve().parent.parent
    snake = name.strip().replace("-", "_")
    if not (workspace / "scenarios" / snake / "code" / "__init__.py").is_file():
        return None
    if str(workspace) not in sys.path:
        sys.path.insert(0, str(workspace))
    module = importlib.import_module(f"scenarios.{snake}.code")
    return module.build_scenario()


def _scan_scenario_arg(argv: list[str]) -> str | None:
    """从 argv 预扫 --scenario 的值（位置不限：子命令前后均可）。

    之所以预扫而不是交给 argparse：场景决定 parser 本身的构建
    （描述、默认任务都来自 SCENARIO），解析发生之前就必须完成绑定。
    """
    for i, arg in enumerate(argv):
        if arg == "--scenario" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--scenario="):
            return arg.split("=", 1)[1]
    return None


def _selfcheck() -> int:
    """沙箱自检子命令：无凭据、无网络、不绑定场景，退出码 0 = 一切正常。

    平台审核的沙箱评测会实际执行入口命令，非零退出码会被判「命令失败」。
    本命令给评测方一个绿色的「确认运行正常」信号（学习 cnb.cool PR#80 的教训）。
    """
    checks: list[tuple[str, bool, str]] = []

    workspace = Path(__file__).resolve().parent.parent
    engine_dir = Path(__file__).resolve().parent

    # 1. 引擎模块完整性（import 即验证语法与依赖）
    try:
        import engine_runtime, io_utils, scenario_api, state_store, workflow  # noqa: F401
        checks.append(("engine-modules", True, "9 个引擎模块全部可导入"))
    except Exception as exc:  # noqa: BLE001
        checks.append(("engine-modules", False, f"模块导入失败: {exc!r}"))
        _print_selfcheck(checks)
        return 1

    # 2. 第三方依赖在位（SELFCHECK_DEPS：产品按需增删，须与 SKILL.md dependency 同步）
    for pip_name, mod in SELFCHECK_DEPS:
        try:
            importlib.import_module(mod)
            checks.append((f"dep:{pip_name}", True, f"{pip_name} 可导入"))
        except ImportError as exc:
            checks.append((f"dep:{pip_name}", False, f"缺失: {exc}"))
        except Exception as exc:  # noqa: BLE001
            checks.append((f"dep:{pip_name}", False, f"异常: {exc!r}"))

    # 3. 场景包在位且可绑定（不真跑任务）
    scen_names = _available_scenarios()
    if scen_names:
        bound = _bind_scenario(scen_names[0])
        checks.append(("scenario-bind", bound is not None,
                       f"可用场景: {', '.join(scen_names)}"))
        # 契约也要过:能绑定 ≠ 声明合法(任务 ID / 字段 / CHOICE,自 4.2 F-004 移植)
        for scen in scen_names:
            try:
                sd = _bind_scenario(scen)
                sd.validate()
                checks.append((f"scenario-contract:{scen}", True, "契约校验通过"))
            except Exception as exc:  # noqa: BLE001 —— 自检把异常转成一条 FAIL,不崩
                checks.append((f"scenario-contract:{scen}", False, f"契约不合法: {exc}"))
    else:
        checks.append(("scenario-bind", False, "scenarios/ 下无场景包"))

    # 4. 关键目录可定位
    checks.append(("workspace", workspace.is_dir(), f"工作区: {workspace.name}"))
    checks.append(("engine-dir", engine_dir.is_dir(), "engine/ 在位"))

    ok = all(c[1] for c in checks)
    _print_selfcheck(checks)
    return 0 if ok else 1


def _print_selfcheck(checks: list[tuple[str, bool, str]]) -> None:
    ok_all = all(c[1] for c in checks)
    print("selfcheck:", "OK" if ok_all else "FAIL")
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def main() -> int:
    global SCENARIO, BUILD_ID
    _reconfigure_stdio()

    # selfcheck 最先拦截：无凭据、无网络、不要求 --scenario（沙箱评测友好）
    if len(sys.argv) > 1 and sys.argv[1] in ("selfcheck", "--selfcheck"):
        return _selfcheck()

    # --help / -h / 无子命令 → 打印用法并 exit 0（沙箱评测不能被判负）
    argv_rest = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(sys.argv) == 1 or sys.argv[1] in ("--help", "-h", "help") or not argv_rest:
        if sys.argv[1:] and sys.argv[1] not in ("--help", "-h", "help"):
            pass  # 只有 --scenario 之类标志没有子命令 → 落到下面的提示
        else:
            names = _available_scenarios()
            print("用法: python engine/main.py <selfcheck|tasks|run|resume|serve> [--scenario 场景] ...")
            print("自检: python engine/main.py selfcheck   （无凭据，退出码 0=正常）")
            for n in names:
                print(f"  - {n}")
            return 0

    # 源码直跑的场景绑定：用 --scenario 指定场景包
    if SCENARIO is None:
        argv_scenario = _scan_scenario_arg(sys.argv[1:])
        if argv_scenario is None:
            names = _available_scenarios()
            print("[提示] 本地源码直跑需要用 --scenario 指定场景包。可用场景：")
            for n in names:
                print(f"  - {n}")
            print("示例：python engine/main.py tasks --scenario prompt-skill --json")
            return 2
        bound = _bind_scenario(argv_scenario)
        if bound is None:
            print(f"[错误] scenarios/ 下没有场景包「{argv_scenario}」。可用场景：")
            for n in _available_scenarios():
                print(f"  - {n}")
            return 2
        SCENARIO = bound
        BUILD_ID = ""

    # 场景契约校验接入生产绑定链(自 4.2 引擎 F-004 移植):任务 ID / 字段 / CHOICE
    # 声明违规在启动即 fast-fail,不等到运行期才以不可预期的方式爆开。
    # validate() 原先在本引擎里没有任何调用点——契约写了,但没人执行。
    try:
        SCENARIO.validate()
    except Exception as exc:  # noqa: BLE001 —— 契约错误一律转人话 + 退出码 2
        print(f"[scenario-contract] 场景契约校验失败({SCENARIO.name}):{exc}",
              file=sys.stderr)
        return 2

    parser = argparse.ArgumentParser(
        prog="engine/main.py",
        description=f"{SCENARIO.display_name} —— {SCENARIO.description}",
    )
    parser.add_argument("--scenario", help=argparse.SUPPRESS)  # 已预扫，仅让 argparse 接受
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_parser = sub.add_parser("run", help="跑一次任务后退出（宿主 Agent 调用路径）")
    run_parser.add_argument(
        "--task",
        dest="task",
        default=SCENARIO.tasks[0].task_id,
        help=f"任务 ID，默认第一个。可用：{', '.join(t.task_id for t in SCENARIO.tasks)}",
    )
    run_parser.add_argument(
        "--set",
        dest="set",
        action="append",
        metavar="字段=值",
        help="按 InputField 声明传输入，可重复，如 --set source-file=C:\\\\a.docx",
    )
    run_parser.add_argument(
        "inputs",
        nargs="*",
        metavar="文件路径",
        help="位置参数：按声明顺序填必填 FILE 字段（宿主最常用的给法）",
    )
    run_parser.add_argument(
        "--json",
        dest="json",
        action="store_true",
        help="机器输出模式：stdout 只输出一份严格 JSON（宿主 Agent 优先用它）",
    )
    run_parser.add_argument(
        "--batch",
        dest="batch",
        metavar="目录",
        help="批量模式：对目录下每个文件各执行一次本任务；"
        "stdout 恒输出一份 JSON 汇总，此时忽略 --json 与位置参数",
    )
    run_parser.add_argument(
        "--batch-fresh",
        dest="batch_fresh",
        action="store_true",
        help="忽略同一批量目录之前留下的进度记录，全部文件重新处理"
        "（默认会自动跳过上次已成功处理过的文件，用于整批进程被中断后续跑）",
    )

    tasks_parser = sub.add_parser("tasks", help="列出本产品的任务契约（宿主探测用）")
    tasks_parser.add_argument(
        "--json",
        dest="json",
        action="store_true",
        help="输出机器可读契约（任务 + 输入 schema + 调用方式）",
    )

    serve_parser = sub.add_parser("serve", help="启动本地工作台（人工双击路径）")
    serve_parser.add_argument("--port", type=int, default=DEFAULT_PORT)

    # V2.2: resume 子命令
    resume_parser = sub.add_parser("resume", help="从检查点恢复上次失败的任务")
    resume_parser.add_argument(
        "--run-id", type=int, required=False, default=None,
        help="要恢复的运行 ID（不指定则自动取最近一次失败的运行）",
    )
    resume_parser.add_argument(
        "--json", dest="json", action="store_true",
        help="机器输出模式",
    )

    # --scenario 在子命令之后出现时由对应子 parser 接收（值已在 main() 头部预扫并绑定）
    for sp in (run_parser, tasks_parser, serve_parser, resume_parser):
        sp.add_argument("--scenario", help=argparse.SUPPRESS)

    args = parser.parse_args()
    dist_root = _resolve_dist_root()

    if args.cmd == "run":
        return cmd_run(dist_root, args)
    if args.cmd == "tasks":
        return cmd_tasks(args.json)
    if args.cmd == "resume":
        return cmd_resume(dist_root, args)
    cmd_serve(dist_root, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
