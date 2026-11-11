"""
workflow.py —— V2.2 通用任务执行骨架（Deterministic Workflow Execution & Recovery Runtime）

V2.1 能力：
- 统一错误模型（ExecutionError + ErrorCode 分类）
- 步骤级超时（timeout_seconds）
- 重试 + 指数退避（Runtime 级，不在 steps.py 里）
- 步骤状态追踪（ExecutionLog）
- 补偿/回滚栈（Rollback Stack，LIFO）
- RecoveryPolicy 驱动恢复决策

V2.2 新增：
- Checkpoint：每个步骤成功后保存上下文快照到 SQLite
- Resume：从最新 checkpoint 恢复执行，不重跑已完成步骤
- 步骤超时（threading + scratch 隔离）：不用 multiprocessing——Windows 下
  只有 spawn，要求整条调用链可 pickle，代价是重新设计场景包拿到 runtime
  服务的通道。线程无法强制终止（CPython 限制），但超时步骤在 ctx 的隔离
  副本上执行，超时后真身状态不被残留线程污染（详见 _run_with_timeout）

向后兼容：
- 裸 StepFn（普通函数）仍然可用
- 现有场景包零改动即可运行
- checkpoint 默认关闭，通过 store + run_id 参数启用
"""

from __future__ import annotations

import copy
import sys
import errno
import random
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from scenario_api import (
    EngineRuntime,
    EnvironmentSnapshot,
    ErrorCode,
    ExecutionError,
    ExecutionLog,
    ExecutionLogEntry,
    ExecutionMode,
    InputKind,
    Issue,
    IssueLevel,
    RecoveryPhase,
    RecoveryPolicy,
    RunContext,
    ScenarioDefinition,
    Step,
    StepDataFlow,
    StepPolicy,
    StepStatus,
    TaskDefinition,
)


class RunStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAILED = "FAILED"


@dataclass
class RunResult:
    """execute_task / resume_task 的返回值。"""

    task_id: str
    status: RunStatus = RunStatus.PENDING
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    recovery_phase: RecoveryPhase = RecoveryPhase.NONE
    execution_log: ExecutionLog = field(default_factory=ExecutionLog)
    # V2.2: 是否从 checkpoint 恢复的执行
    resumed: bool = False
    resumed_from_step: int = -1
    # V2.3: 执行模式（标记结果质量）
    execution_mode: ExecutionMode = ExecutionMode.NORMAL
    fallback_steps: list[str] = field(default_factory=list)
    # P1: 完整审计链
    data_flow: list[StepDataFlow] = field(default_factory=list)
    output_fingerprints: dict[str, Any] = field(default_factory=dict)
    environment: EnvironmentSnapshot = field(default_factory=EnvironmentSnapshot)


def _has(issues: list[Issue], level: IssueLevel) -> bool:
    return any(i.level is level for i in issues)


def _issue_to_dict(issue: Issue) -> dict:
    """Issue 序列化（checkpoint 用）。"""
    return {
        "level": issue.level.value,
        "message": issue.message,
        "detail": issue.detail,
    }


def _dict_to_issue(d: dict) -> Issue:
    """Issue 反序列化（checkpoint 恢复用）。"""
    return Issue(
        level=IssueLevel(d["level"]),
        message=d["message"],
        detail=d.get("detail", {}),
    )


def _validate_inputs(
    task: TaskDefinition, raw_inputs: dict[str, Any] | None, runtime: EngineRuntime
) -> tuple[dict[str, Any], list[Issue]]:
    """按 InputField 契约校验并归一化输入。"""
    issues: list[Issue] = []
    inputs: dict[str, Any] = {}
    raw = raw_inputs or {}

    for f in task.input_fields:
        value = raw.get(f.field_id, f.default)
        if value is None or (isinstance(value, str) and not value.strip()):
            if f.required:
                issues.append(
                    Issue(IssueLevel.ERROR, f"缺少必填输入：{f.label}（{f.field_id}）")
                )
            continue

        if isinstance(value, str):
            value = value.strip()

        if f.kind is InputKind.FILE:
            try:
                value = str(runtime.resolve_input_file(value))
            except FileNotFoundError as exc:
                issues.append(Issue(IssueLevel.ERROR, str(exc)))
                continue
        elif f.kind is InputKind.CHOICE:
            if value not in f.choices:
                issues.append(
                    Issue(
                        IssueLevel.ERROR,
                        f"{f.label} 的值必须是：{' / '.join(f.choices)}",
                    )
                )
                continue

        inputs[f.field_id] = value

    return inputs, issues


# ---------------------------------------------------------------- V2.1: 步骤规范化

def _normalize_step(step_def) -> tuple:
    """把 StepFn 或 Step 统一解包为 (execute_fn, policy, fallback_fn, name)。"""
    if isinstance(step_def, Step):
        return step_def.execute, step_def.policy, step_def.fallback, step_def.name
    return step_def, StepPolicy(), None, ""


# ---------------------------------------------------------------- V2.2: 超时执行

class _StepTimeout(Exception):
    """内部异常：步骤执行超时。"""


def _run_with_timeout(execute_fn, ctx: RunContext, timeout_seconds: float) -> None:
    """执行步骤函数，超时检测 + 隔离写。

    实现说明：使用 threading 而非 multiprocessing，因为步骤函数通常需要
    ctx.runtime（读模板、写产物、存取状态），而 runtime 包含 SQLite 连接
    等不可 pickle 的资源，无法传入子进程。

    隔离写（V2.4 加固）：步骤线程操作的是 ctx 的**深拷贝 scratch**——
    inputs/outputs/issues 逐字段深拷贝，runtime 共享（SQLite 已开
    check_same_thread=False，访问保持顺序）。线程跑完（成功或抛异常）才把
    scratch 的 outputs/issues 覆盖回真身；超时则真身保持调用前原样，后台
    残留线程最多写脏自己的 scratch。Python 线程杀不死，这层解决的是
    "共享状态被悄悄写脏"，不解决"资源没释放"——残留线程占用的资源在它
    自然跑完前不会释放，它在超时前已产生的外部副作用（state_set、已写
    的文件）也不会被撤销，这是"不能真杀"的固有代价。

    限制：超时后 daemon 线程仍在后台运行；对 CPU 密集型文本处理场景，
    超时是安全网和检测机制，而非精确的强制终止。
    """
    if timeout_seconds <= 0:
        execute_fn(ctx)
        return

    scratch = RunContext(
        task_id=ctx.task_id,
        inputs=copy.deepcopy(ctx.inputs),
        outputs=copy.deepcopy(ctx.outputs),
        issues=copy.deepcopy(ctx.issues),
        runtime=ctx.runtime,
    )

    result = {"error": None}

    def _target():
        try:
            execute_fn(scratch)
        except Exception as e:
            result["error"] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=timeout_seconds)

    if t.is_alive():
        raise _StepTimeout(
            f"步骤执行超时（{timeout_seconds}s）：线程无法强制终止，仍在后台运行，"
            f"但本次输出不会被采信（未合并进结果，真身状态保持超时前原样）"
        )

    # 线程已跑完（无论成功或抛异常）：把 scratch 的最终状态整体覆盖回真身，
    # 保持"未超时时行为与不加超时完全一致"的契约（覆盖而非 update，
    # 因为 scratch 从 ctx 深拷贝而来已含全部旧键，步骤若删键也能生效）。
    ctx.outputs.clear()
    ctx.outputs.update(scratch.outputs)
    ctx.issues[:] = scratch.issues

    if result["error"] is not None:
        raise result["error"]


# ---------------------------------------------------------------- V2.1: 退避计算

def _compute_delay(policy: StepPolicy, attempt: int) -> float:
    """指数退避 + 随机抖动。"""
    delay = min(
        policy.retry_delay * (policy.backoff ** (attempt - 1)),
        policy.max_delay,
    )
    jitter_range = delay * policy.jitter
    delay += random.uniform(-jitter_range, jitter_range)
    return max(0, delay)


# ---------------------------------------------------------------- V2.4: 资源错误分类

_RESOURCE_ERRNOS = frozenset({
    errno.ENOSPC,   # 磁盘空间不足
    errno.EMFILE,   # 进程打开文件数超限
    errno.ENFILE,   # 系统打开文件数超限
})
if hasattr(errno, "ENOMEM"):
    _RESOURCE_ERRNOS = _RESOURCE_ERRNOS | {errno.ENOMEM}


def _is_resource_error(exc: BaseException) -> bool:
    """资源级错误判定：磁盘满/句柄耗尽/内存不足——不是所有 OSError 都算，
    FileNotFoundError/PermissionError 这类输入/权限问题不该被误判成
    "资源不足"（运维排障时这是完全不同的两类问题，误判会带偏排查方向）。

    这类错误与业务/逻辑错误是两回事——重试前该先看机器，而不是看代码。
    """
    if isinstance(exc, MemoryError):
        return True
    if isinstance(exc, OSError):
        return exc.errno in _RESOURCE_ERRNOS
    return False


# ---------------------------------------------------------------- V2.1: 单步骤执行（含重试 + 超时 + 错误分类）

def _execute_single_step(
    execute_fn,
    policy: StepPolicy,
    ctx: RunContext,
    recovery: RecoveryPolicy,
    log_entry: ExecutionLogEntry,
) -> ExecutionError | None:
    """执行单个步骤，包含重试和超时。返回 ExecutionError 或 None（成功）。"""
    max_attempts = policy.max_attempts if recovery.retry_enabled else 1

    for attempt in range(1, max_attempts + 1):
        log_entry.attempt = attempt
        if attempt > 1:
            log_entry.status = StepStatus.RETRYING
            delay = _compute_delay(policy, attempt)
            time.sleep(delay)

        try:
            issues_before = len(ctx.issues)
            # V2.2: 优先用 multiprocessing（可强制终止），失败回退 threading
            if policy.timeout_seconds > 0:
                _run_with_timeout(execute_fn, ctx, policy.timeout_seconds)
            else:
                execute_fn(ctx)

            # 步骤执行完毕，检查**本轮**是否通过 Issue 报告了 ERROR
            this_round = ctx.issues[issues_before:]
            if _has(this_round, IssueLevel.ERROR):
                # 失败原因取**最后一条 ERROR** 而非最后一条 issue：步骤可能先发
                # ERROR 再补 WARN 澄清（实测 plan-responses 曾先报用例 JSON 损坏
                # 再报孤儿文件），取 ctx.issues[-1] 会让失败原因引用一条无害的 WARN
                last_error = next((i for i in reversed(this_round)
                                   if i.level == IssueLevel.ERROR), this_round[-1])
                # STEP_ERROR 语义 = "看 policy"(scenario_api §ErrorCode):是否重试
                # 由 recovery.retry_enabled 与 policy.max_attempts 决定。此前这里写死
                # retryable=False,开了重试也一次不重试——下面的 continue 永远到不了
                # (自 4.2 引擎移植修复,plugin-forge 决议 D-60)。默认不开重试时
                # max_attempts 按 1 算,行为与修复前一致。
                err = ExecutionError(
                    code=ErrorCode.STEP_ERROR,
                    message=last_error.message,
                    retryable=True,
                    recoverable=False,
                    step_index=log_entry.step_index,
                )
                if not err.retryable or attempt >= max_attempts:
                    log_entry.error_code = err.code.value
                    log_entry.error_message = err.message
                    log_entry.status = StepStatus.FAILED
                    return err
                # 重试前丢弃失败轮次追加的 issue,避免残留把成功轮误判成 FAILED
                del ctx.issues[issues_before:]
                continue

            # 成功
            log_entry.status = StepStatus.DONE
            return None

        except _StepTimeout as exc:
            err = ExecutionError.timeout(str(exc))
            err.step_index = log_entry.step_index
            log_entry.status = StepStatus.TIMED_OUT
            if attempt >= max_attempts:
                log_entry.error_code = err.code.value
                log_entry.error_message = err.message
                log_entry.status = StepStatus.FAILED
                return err

        except ExecutionError as exc:
            exc.step_index = log_entry.step_index
            if not exc.retryable or attempt >= max_attempts:
                log_entry.error_code = exc.code.value
                log_entry.error_message = exc.message
                log_entry.status = StepStatus.FAILED
                return exc
            # retryable: continue to next attempt

        except (OSError, MemoryError) as exc:
            # V2.4 加固：资源枯竭（磁盘满/句柄耗尽/内存不足）在通用兜底之前
            # 单独归类，不笼统吞成 UNKNOWN_ERROR——运维排障时这是完全不同的
            # 两类问题。其余 OSError（文件不存在、无权限等）仍走通用路径。
            if _is_resource_error(exc):
                err = ExecutionError.resource(f"系统资源不足：{exc}", cause=exc)
            else:
                err = ExecutionError.from_exception(exc, log_entry.step_index)
            err.step_index = log_entry.step_index
            if not err.retryable or attempt >= max_attempts:
                log_entry.error_code = err.code.value
                log_entry.error_message = err.message
                log_entry.status = StepStatus.FAILED
                return err

        except Exception as exc:
            err = ExecutionError.from_exception(exc, log_entry.step_index)
            if not err.retryable or attempt >= max_attempts:
                log_entry.error_code = err.code.value
                log_entry.error_message = err.message
                log_entry.status = StepStatus.FAILED
                return err

    return ExecutionError(
        code=ErrorCode.UNKNOWN_ERROR,
        message="重试循环异常退出",
        step_index=log_entry.step_index,
    )


# ---------------------------------------------------------------- V2.2: Checkpoint 保存

def _save_checkpoint(
    store,
    run_id: int,
    task_id: str,
    step_index: int,
    ctx: RunContext,
) -> None:
    """V2.2: 保存执行检查点（步骤成功后调用）。

    best-effort：checkpoint 是恢复能力的凭证，不是执行本身的门槛——写入失败
    （SQLite 撞锁等）绝不能让整次执行崩掉。这一步实际已经成功、已经产生的
    输出与副作用（比如场景步骤往 data_dir() 追加了一条记录）不能因为"记不下
    断点"就被当成没发生过。退化成"这一步之后没法断点续跑"，压一条 WARN
    让用户知道，而不是让异常穿透到 execute_task_guarded 变成一次整体崩溃。
    """
    if store is None or run_id is None:
        return
    try:
        store.save_checkpoint(
            run_id=run_id,
            task_id=task_id,
            step_index=step_index,
            next_step=step_index + 1,
            inputs=dict(ctx.inputs),
            outputs=dict(ctx.outputs),
            issues=[_issue_to_dict(i) for i in ctx.issues],
        )
    except Exception as exc:  # noqa: BLE001 —— best-effort，只降级恢复能力
        ctx.issues.append(
            Issue(
                IssueLevel.WARN,
                f"断点保存失败（{exc}），如果接下来中断将无法从这一步恢复，"
                "但本步骤的结果不受影响",
                detail={"step_index": step_index, "exception_type": type(exc).__name__},
            )
        )


# ---------------------------------------------------------------- P1: 审计链辅助

def _snapshot_outputs(ctx: RunContext) -> set[str]:
    """P1: 快照当前 outputs 的键集合。"""
    return set(ctx.outputs.keys())


def _diff_data_flow(
    step_index: int,
    step_name: str,
    before: set[str],
    ctx: RunContext,
) -> StepDataFlow:
    """P1: 通过 outputs 键 diff 计算步骤的数据流。"""
    after = set(ctx.outputs.keys())
    written = after - before  # 新增的键
    # 也包括值被修改的键
    for key in before & after:
        written.add(key)  # 保守策略：存在即视为可能修改
    return StepDataFlow(
        step_index=step_index,
        step_name=step_name,
        keys_written=tuple(sorted(written)),
        keys_count=len(written),
    )


def _compute_output_fingerprints(runtime: EngineRuntime) -> dict[str, Any]:
    """P1: 扫描 output_dir，对每个产物文件计算指纹。"""
    import hashlib
    fingerprints = {}
    try:
        output_dir = runtime.output_dir()
        if not output_dir.exists():
            return fingerprints
        for path in sorted(output_dir.rglob("*")):
            if path.is_file():
                rel = str(path.relative_to(output_dir))
                data = path.read_bytes()
                fingerprints[rel] = {
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data),
                    "lines": data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0),
                }
    except Exception as exc:
        # 指纹是凭证，不是门槛——失败留痕,不影响主流程
        print(f"[degraded] 产物指纹留取失败: {exc!r}", file=sys.stderr)
    return fingerprints


def _build_environment_snapshot(
    scenario: ScenarioDefinition,
    runtime: EngineRuntime,
) -> EnvironmentSnapshot:
    """P1: 构建执行时的环境快照。"""
    import hashlib
    import sys

    template_fps = {}
    try:
        # 模板根：直读场景 assets/pro/templates/（本地个人版形态）
        roots = getattr(runtime, "template_root_paths", None)
        roots = roots() if roots else [runtime.dist_root / "templates"]
        for templates_dir in roots:
            if templates_dir.exists():
                for path in sorted(templates_dir.rglob("*")):
                    if path.is_file():
                        rel = str(path.relative_to(templates_dir))
                        data = path.read_bytes()
                        template_fps[rel] = hashlib.sha256(data).hexdigest()[:16]
    except Exception as exc:
        print(f"[degraded] 模板指纹快照失败: {exc!r}", file=sys.stderr)

    return EnvironmentSnapshot(
        engine_version="2.3",
        scenario_name=scenario.name,
        scenario_version=scenario.version,
        template_fingerprints=template_fps,
        python_version=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    )


# ---------------------------------------------------------------- 主执行入口

def execute_task(
    scenario: ScenarioDefinition,
    task_id: str,
    runtime: EngineRuntime,
    raw_inputs: dict[str, Any] | None = None,
    *,
    store=None,
    run_id: Optional[int] = None,
    start_step: int = 0,
    initial_ctx: Optional[RunContext] = None,
) -> RunResult:
    """唯一的任务执行入口。

    V2.2 新增参数：
    - store: StateStore 实例（启用 checkpoint 时必传）
    - run_id: 运行 ID（启用 checkpoint 时必传）
    - start_step: 从第几步开始执行（resume 时 > 0）
    - initial_ctx: 恢复的上下文（resume 时传入）
    """
    result = RunResult(
        task_id=task_id,
        started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
    )

    task = scenario.task(task_id)
    if task is None:
        available = ", ".join(t.task_id for t in scenario.tasks)
        result.issues.append(
            Issue(IssueLevel.ERROR, f"未知任务：{task_id}（可用任务：{available}）")
        )
        result.status = RunStatus.FAILED
        result.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
        return result

    # V2.2: Resume 模式 —— 从 checkpoint 恢复上下文
    if initial_ctx is not None:
        ctx = initial_ctx
        ctx.runtime = runtime
        result.resumed = True
        result.resumed_from_step = start_step
    else:
        ctx = RunContext(task_id=task_id, runtime=runtime)
        ctx.inputs, input_issues = _validate_inputs(task, raw_inputs, runtime)
        ctx.issues.extend(input_issues)

        if _has(ctx.issues, IssueLevel.ERROR):
            result.status = RunStatus.FAILED
            result.inputs = dict(ctx.inputs)
            result.outputs = dict(ctx.outputs)
            result.issues = list(ctx.issues)
            result.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
            return result

    # 执行准备
    result.status = RunStatus.RUNNING
    exec_log = result.execution_log
    exec_log.total_steps = len(task.steps)
    recovery = task.recovery_policy

    # P1: 环境快照（执行开始时记录）
    result.environment = _build_environment_snapshot(scenario, runtime)

    for i, step_def in enumerate(task.steps):
        execute_fn, policy, fallback_fn, step_name = _normalize_step(step_def)
        step_name = step_name or f"step_{i}"

        # V2.2: 跳过已完成的步骤（resume 模式）
        if i < start_step:
            entry = ExecutionLogEntry(
                step_index=i, step_name=step_name, status=StepStatus.SKIPPED
            )
            entry.error_message = "resumed: skipped (completed in previous run)"
            exec_log.entries.append(entry)
            continue

        # 前置步骤失败：跳过后续步骤
        if _has(ctx.issues, IssueLevel.ERROR):
            entry = ExecutionLogEntry(
                step_index=i, step_name=step_name, status=StepStatus.SKIPPED
            )
            exec_log.entries.append(entry)
            # V2.3: 有步骤被跳过 → PARTIAL 模式
            result.execution_mode = ExecutionMode.PARTIAL
            continue

        log_entry = exec_log.start_step(i, step_name)

        # P1: 步骤执行前快照 outputs 键（用于数据流追踪）
        outputs_before = _snapshot_outputs(ctx)

        # 执行主路径
        error = _execute_single_step(execute_fn, policy, ctx, recovery, log_entry)

        # P1: 记录步骤数据流
        data_flow_entry = _diff_data_flow(i, step_name, outputs_before, ctx)
        result.data_flow.append(data_flow_entry)

        if error is not None:
            # V2.3: 尝试 fallback（降级路径）
            if fallback_fn is not None and recovery.fallback_enabled:
                exec_log.recovery_phase = RecoveryPhase.FALLBACK
                log_entry.status = StepStatus.RUNNING  # 重置状态
                log_entry.error_message = f"primary failed, trying fallback: {error.message}"

                try:
                    fallback_fn(ctx)
                    # fallback 成功
                    result.fallback_steps.append(step_name)
                    result.execution_mode = ExecutionMode.DEGRADED
                    exec_log.recovery_phase = RecoveryPhase.DEGRADED
                    exec_log.finish_step(log_entry, StepStatus.DONE)
                    log_entry.error_message += " → fallback succeeded"

                    # V2.3: Review Gate — 降级成功也标记 REVIEW
                    ctx.issues.append(
                        Issue(
                            IssueLevel.REVIEW,
                            f"步骤 {step_name} 使用降级路径完成，结果质量可能下降，请人工确认",
                            detail={
                                "step_index": i,
                                "fallback_used": True,
                                "original_error": error.message,
                            },
                        )
                    )

                    # fallback 成功，保存 checkpoint 并继续
                    _save_checkpoint(store, run_id, task_id, i, ctx)
                    continue

                except Exception as fb_exc:
                    # fallback 也失败了
                    log_entry.error_message += f" → fallback also failed: {fb_exc}"
                    exec_log.recovery_phase = RecoveryPhase.NONE

            # 无 fallback 或 fallback 也失败：直接记 ERROR 并终止
            ctx.issues.append(
                Issue(
                    IssueLevel.ERROR,
                    f"步骤 {step_name} 失败：{error.message}",
                    detail={
                        "error_code": error.code.value,
                        "retryable": error.retryable,
                        "step_index": i,
                        "fallback_available": fallback_fn is not None,
                    },
                )
            )
            exec_log.finish_step(log_entry, StepStatus.FAILED)
            break

        # 主路径成功：标记完成 + 保存 checkpoint
        exec_log.finish_step(log_entry, StepStatus.DONE)
        _save_checkpoint(store, run_id, task_id, i, ctx)

    # 溯源自检
    _verify_traceability(ctx)

    # 终态判定
    if _has(ctx.issues, IssueLevel.ERROR):
        result.status = RunStatus.FAILED
    elif _has(ctx.issues, IssueLevel.REVIEW):
        result.status = RunStatus.REVIEW_REQUIRED
    else:
        result.status = RunStatus.DONE

    result.inputs = dict(ctx.inputs)
    result.outputs = dict(ctx.outputs)
    result.issues = list(ctx.issues)

    # P1: 输出指纹（产物文件 SHA-256）
    result.output_fingerprints = _compute_output_fingerprints(runtime)

    result.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
    return result


# ---------------------------------------------------------------- V2.2: Resume

def resume_task(
    scenario: ScenarioDefinition,
    task_id: str,
    runtime: EngineRuntime,
    store,
    run_id: int,
) -> RunResult:
    """V2.2: 从 checkpoint 恢复执行。

    加载最新 checkpoint，恢复上下文（inputs/outputs/issues），
    从下一个未完成的步骤继续执行。
    """
    checkpoint = store.load_latest_checkpoint(run_id)
    if checkpoint is None:
        result = RunResult(task_id=task_id)
        result.issues.append(
            Issue(IssueLevel.ERROR, f"无可用的检查点（run_id={run_id}）")
        )
        result.status = RunStatus.FAILED
        result.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
        return result

    # 从 checkpoint 恢复上下文
    ctx = RunContext(
        task_id=task_id,
        inputs=checkpoint["inputs"],
        outputs=checkpoint["outputs"],
        issues=[_dict_to_issue(d) for d in checkpoint["issues"]],
        runtime=runtime,
    )

    next_step = checkpoint["next_step"]

    return execute_task(
        scenario=scenario,
        task_id=task_id,
        runtime=runtime,
        store=store,
        run_id=run_id,
        start_step=next_step,
        initial_ctx=ctx,
    )


# ---------------------------------------------------------------- 溯源自检

def _verify_traceability(ctx: RunContext) -> None:
    """反幻觉自检：产物中的关键条目必须能在输入原文中找到出处。"""
    trace = ctx.outputs.pop("trace_check", None)
    if not isinstance(trace, dict):
        return
    source = str(trace.get("source", ""))
    items = [str(i) for i in trace.get("items", [])]
    violations = [i for i in items if i and i not in source]
    ctx.outputs["trace_verified"] = not violations
    ctx.outputs["trace_violations"] = violations
    if violations:
        preview = "；".join(v[:20] for v in violations[:3])
        suffix = f" 等 {len(violations)} 条" if len(violations) > 3 else ""
        ctx.issues.append(
            Issue(
                IssueLevel.REVIEW,
                f"有 {len(violations)} 条产物条目在输入原文中找不到出处"
                f"（疑似生成逻辑错误）：{preview}{suffix}，请逐条人工核对",
                detail={"violations": violations},
            )
        )


# ---------------------------------------------------------------- 历史摘要

_STATUS_LABELS = {
    RunStatus.DONE: "完成",
    RunStatus.REVIEW_REQUIRED: "待复核",
    RunStatus.FAILED: "失败",
    RunStatus.RUNNING: "运行中",
    RunStatus.PENDING: "等待",
}


def human_summary(scenario: ScenarioDefinition, result: RunResult) -> str:
    """run_log.summary 的人类可读摘要。"""
    task = scenario.task(result.task_id)
    parts = [
        task.label if task else result.task_id,
        _STATUS_LABELS.get(result.status, result.status.value),
    ]
    if result.resumed:
        parts.append(f"恢复自步骤{result.resumed_from_step}")
    artifacts = [str(v) for k, v in result.outputs.items() if k.endswith("_file_name")]
    if artifacts:
        parts.append("、".join(artifacts))
    return " · ".join(parts)


# ---------------------------------------------------------------- V2.4: 顶层崩溃边界
#
# 兜的不是场景步骤的异常（那是 _execute_single_step 的职责），而是引擎
# 自身代码的意外异常（如 checkpoint 写入撞 SQLite 故障、审计链构造出错）。
# 契约：无论引擎内部发生什么，都返回一个 FAILED 的 RunResult，绝不向
# 调用方抛异常——CLI（main.py）与 Web（webapp.py）两条入口统一走这层，
# 行为不因入口不同而分叉（对照案例：Go_pro content_check 的 panic 曾把
# 整个 HTTP handler 拖垮，同一类问题）。


def crash_result(task_id: str, where: str, exc: BaseException) -> RunResult:
    """引擎内部意外异常的兜底结果。"""
    result = RunResult(
        task_id=task_id,
        status=RunStatus.FAILED,
        started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    # 没有专门的"崩溃"execution_mode 档位——PARTIAL（部分步骤被跳过）语义上
    # 最接近"整次执行没能正常跑完"，用它而不是默认的 NORMAL，避免消费者只
    # 看 execution_mode 就把一次彻底崩溃误判成正常执行。
    result.execution_mode = ExecutionMode.PARTIAL
    result.issues.append(
        Issue(
            IssueLevel.ERROR,
            f"引擎内部错误（{where}，非场景步骤失败，请反馈日志）："
            f"{type(exc).__name__}: {exc}",
            detail={"where": where, "exception_type": type(exc).__name__},
        )
    )
    return result


def execute_task_guarded(scenario, task_id, runtime, raw_inputs=None, **kwargs) -> RunResult:
    """execute_task 的崩溃边界版——入口层（CLI/Web）一律调用这个。"""
    try:
        return execute_task(scenario, task_id, runtime, raw_inputs, **kwargs)
    except Exception as exc:  # noqa: BLE001 —— 这一层就是要兜一切
        return crash_result(task_id, "execute_task", exc)


def resume_task_guarded(scenario, task_id, runtime, store, run_id) -> RunResult:
    """resume_task 的崩溃边界版——入口层（CLI/Web）一律调用这个。"""
    try:
        return resume_task(scenario, task_id, runtime, store, run_id)
    except Exception as exc:  # noqa: BLE001
        return crash_result(task_id, "resume_task", exc)
