"""
scenario_api.py —— 场景包接口契约（整个"编译期插拔"架构的枢纽）

一个场景包就是 dev/scenarios/ 下的一个 Python 包，必须暴露：

    build_scenario() -> ScenarioDefinition

build.py 在编译时导入它，把返回的 ScenarioDefinition 和通用引擎一起
编译进 agent.exe。分发到用户手里的永远是"单场景的确定性产品"，
但我们的仓库是同一套底座派生出来的——「通用」发生在编译期，
不发生在运行期（详见 docs/explanation/architecture.md 和 方案对比.md 第 3 节）。

场景包必须遵守的边界（违反任何一条都会破坏护城河或可维护性）：

1. 只依赖本文件 + 标准库 + 白名单第三方库（见 build/requirements.txt：
   pyyaml，以及本档位新增的 openpyxl/python-docx/pdfplumber——只允许
   经 parse_input 间接使用，不要在场景包里直接 import）。
   不要 import fastapi/uvicorn——serve 层属于引擎，场景包在两条启动路径
   （run.bat / open_workbench.bat）下都以同样的步骤函数运行。
2. 判断阈值、评分权重、决策分支全部写死在场景包 code/ 里（会被编译）。
   不要为了调参方便把它们外置成运行期读取的配置——编译保护等于白做。
3. 排版结构、行业术语、输出格式模板放在 assets/pro/templates/（明文），
   代码里通过 runtime.read_template() 读取，不要硬编码绝对路径。
4. 跨周期状态（"上周做了什么"）只通过 runtime.state_get/state_set 存取，
   引擎会自动加场景名前缀，避免多场景状态互相污染。
5. 步骤函数只做两件事：往 ctx.outputs 写结果、往 ctx.issues 记问题。
   不要自己起线程/进程，不要联网，不要弹窗——那些是引擎的职责。
"""

from __future__ import annotations

import re
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# 引擎提供的共用工具（场景包可直接使用）
# ---------------------------------------------------------------------------

# read_text_auto / read_file_auto / file_kind 的实现与导出见文件末尾 parse_input re-export


# ---------------------------------------------------------------------------
# 输入契约：工作台表单和 CLI 参数共用同一份声明
# ---------------------------------------------------------------------------

class InputKind(str, Enum):
    """输入字段的形态，决定工作台渲染成什么控件、CLI 怎么解析。"""

    FILE = "file"            # 文件路径：工作台弹文件选择器，CLI 收路径参数
    TEXT = "text"            # 单行文本
    MULTILINE = "multiline"  # 多行文本（粘贴原始材料用）
    CHOICE = "choice"        # 枚举选择（固定选项，避免自由输入产生歧义）


@dataclass(frozen=True)
class InputField:
    """任务的一个输入参数。

    工作台据此自动渲染表单，CLI 据此解析 `--字段 值` 参数，
    两套入口永远吃同一份声明，不会出现"按钮能跑、命令行跑不了"的分叉。
    """

    field_id: str                      # kebab-case，如 "source-file"
    label: str                         # 展示给用户的名字，如 "本周记录文件"
    kind: InputKind = InputKind.TEXT
    required: bool = True
    default: str | None = None
    choices: tuple[str, ...] = ()      # kind=CHOICE 时必填
    help_text: str = ""                # 字段旁的一句解释，可为空


# ---------------------------------------------------------------------------
# 问题与复核：场景控制"什么情况需要人工看一眼"的唯一手段
# ---------------------------------------------------------------------------

class IssueLevel(str, Enum):
    INFO = "info"      # 仅记录，不影响终态
    WARN = "warn"      # 记录并提示，不拦截
    REVIEW = "review"  # 任一步产生 => 本次运行终态为 REVIEW_REQUIRED
    ERROR = "error"    # 当前步骤失败，后续步骤不再执行，终态 FAILED


@dataclass(frozen=True)
class Issue:
    """步骤中发现的问题。

    注意 REVIEW 才是"待人工复核"——把"提示性信息"也标成 REVIEW
    会让用户每次都被迫确认，等于狼来了，宁缺毋滥。
    """

    level: IssueLevel
    message: str                          # 给用户看的一句话
    detail: dict[str, Any] = field(default_factory=dict)  # 结构化补充，进 last_result.json


# ---------------------------------------------------------------------------
# V2.1: 统一错误模型 —— Runtime 按分类决定恢复策略
# ---------------------------------------------------------------------------

class ErrorCode(str, Enum):
    """执行错误的统一分类。

    Runtime 拿到 ExecutionError 后按 code 查 RecoveryPolicy 决定
    retry / rollback / review / fail。场景包抛异常时尽量选最精确的 code；
    引擎兜底把未识别的 Exception 归为 UNKNOWN_ERROR。
    """

    VALIDATION_ERROR = "validation_error"      # 输入契约不满足（不可重试）
    INPUT_ERROR = "input_error"                # 输入数据格式/内容异常（不可重试）
    TRANSIENT_ERROR = "transient_error"        # 临时性故障（可重试：网络抖动、文件锁）
    TIMEOUT_ERROR = "timeout_error"            # 步骤超时（可重试，看 policy）
    RESOURCE_ERROR = "resource_error"          # 资源不足（磁盘满、内存不足）
    STEP_ERROR = "step_error"                  # 步骤业务逻辑失败（看 policy）
    DEPENDENCY_ERROR = "dependency_error"      # 外部依赖不可用（模板文件缺失等）
    OUTPUT_ERROR = "output_error"              # 产物写出失败（目录不可写等）
    INTEGRITY_ERROR = "integrity_error"        # 数据完整性破坏（溯源违规等）
    POLICY_ERROR = "policy_error"              # 策略违规（授权过期、档位不匹配）
    UNKNOWN_ERROR = "unknown_error"            # 未分类（兜底）


@dataclass(eq=False)
class ExecutionError(Exception):
    """Runtime 的统一错误表示。

    场景包可以主动抛 ExecutionError（精确分类），也可以抛普通 Exception
    （引擎兜底包装为 UNKNOWN_ERROR）。Runtime 根据此对象决定恢复路径：
    retry / rollback / review / fail。
    """

    code: ErrorCode
    message: str
    severity: str = "error"                    # error / critical
    retryable: bool = False
    recoverable: bool = False
    requires_review: bool = False
    cause: Optional[BaseException] = None      # 原始异常（保留 traceback 链）
    step_index: int = -1                       # 发生错误的步骤序号（0-based）

    @classmethod
    def from_exception(cls, exc: BaseException, step_index: int = -1) -> ExecutionError:
        """把任意 Exception 包装为 ExecutionError（引擎兜底路径）。"""
        if isinstance(exc, ExecutionError):
            exc.step_index = step_index
            return exc
        return cls(
            code=ErrorCode.UNKNOWN_ERROR,
            message=str(exc),
            retryable=False,
            recoverable=False,
            cause=exc,
            step_index=step_index,
        )

    @classmethod
    def transient(cls, message: str, cause: BaseException | None = None) -> ExecutionError:
        return cls(code=ErrorCode.TRANSIENT_ERROR, message=message,
                   retryable=True, recoverable=True, cause=cause)

    @classmethod
    def timeout(cls, message: str) -> ExecutionError:
        return cls(code=ErrorCode.TIMEOUT_ERROR, message=message,
                   retryable=True, recoverable=True)

    @classmethod
    def validation(cls, message: str) -> ExecutionError:
        return cls(code=ErrorCode.VALIDATION_ERROR, message=message,
                   retryable=False, recoverable=False)

    @classmethod
    def resource(cls, message: str, cause: BaseException | None = None) -> ExecutionError:
        """磁盘满/内存不足/句柄耗尽等资源枯竭类错误——不可重试（重试大概率原地再炸一次）。"""
        return cls(code=ErrorCode.RESOURCE_ERROR, message=message,
                   retryable=False, recoverable=False, cause=cause)


# ---------------------------------------------------------------------------
# V2.1: 步骤执行策略 —— 异常处理成为 Workflow Definition 的一部分
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StepPolicy:
    """单个步骤的执行策略。

    场景包在定义 Step 时附带 policy，Runtime 据此决定：
    - 超时后怎么办（timeout_seconds）
    - 失败后是否重试、重试几次、退避策略（retry_*）
    - 最终失败后是否需要人工复核（requires_review）

    所有字段有合理默认值（不重试、不超时），现有场景包无需改动。
    """

    timeout_seconds: float = 0               # 0 = 不限时
    max_attempts: int = 1                    # 1 = 不重试，>1 = 失败后最多重试 N-1 次
    retry_delay: float = 1.0                 # 首次重试等待秒数
    backoff: float = 2.0                     # 退避倍数（指数退避：delay * backoff^attempt）
    max_delay: float = 60.0                  # 单次等待上限
    jitter: float = 0.1                      # 随机抖动比例（防惊群）
    requires_review: bool = False            # 最终失败后标记 REVIEW 而非 FAILED


# ---------------------------------------------------------------------------
# V2.1: Step —— 带策略的步骤定义（向后兼容 StepFn）
# ---------------------------------------------------------------------------


@dataclass
class Step:
    """带执行策略和降级路径的步骤定义。

    场景包可以用 Step 替代裸 StepFn 来获得 Runtime 的异常治理能力：

        Step(
            execute=step_load,
            policy=StepPolicy(timeout_seconds=30, max_attempts=3),
            fallback=step_load_simple,  # primary 失败时的降级路径
        )

    裸函数（StepFn）仍然被支持——Runtime 自动用默认 StepPolicy 包装。
    """

    execute: StepFn                           # 主执行函数
    policy: StepPolicy = field(default_factory=StepPolicy)
    fallback: Optional[StepFn] = None         # V2.3: 降级执行函数（可选）
    name: str = ""                            # 步骤名（日志/审计用，空则用序号）

    def __call__(self, ctx: RunContext) -> None:
        """让 Step 本身可被当作 StepFn 调用（向后兼容路径）。"""
        self.execute(ctx)


# ---------------------------------------------------------------------------
# V2.3: 执行模式 —— 标记结果质量
# ---------------------------------------------------------------------------

class ExecutionMode(str, Enum):
    """执行模式：标记最终结果的产生路径。

    NORMAL: 所有步骤按 primary path 成功完成
    DEGRADED: 至少一个步骤使用了 fallback 降级路径
    PARTIAL: 部分步骤被跳过（前置失败导致）
    """

    NORMAL = "normal"
    DEGRADED = "degraded"
    PARTIAL = "partial"


# ---------------------------------------------------------------------------
# P1: 结果可信度 —— 完整审计链类型
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StepDataFlow:
    """单个步骤的数据流记录。

    记录每个步骤写了哪些 outputs 键，用于构建步骤间数据流图。
    通过步骤执行前后的 outputs 快照 diff 得到。
    """

    step_index: int
    step_name: str
    keys_written: tuple[str, ...] = ()      # 该步骤新增或修改的 outputs 键
    keys_count: int = 0                     # 写入的键数量


@dataclass(frozen=True)
class EnvironmentSnapshot:
    """执行时的环境快照。

    记录执行时的引擎版本、场景版本、模板指纹等，
    用于事后验证"产物确实是在这个环境下产生的"。
    """

    engine_version: str = ""                # 引擎版本
    scenario_name: str = ""                 # 场景名
    scenario_version: str = ""              # 场景版本
    template_fingerprints: dict = field(default_factory=dict)  # 模板文件指纹
    python_version: str = ""                # Python 版本


# ---------------------------------------------------------------------------
# V2.1: 恢复阶段 —— 与 Execution State 正交的维度
# ---------------------------------------------------------------------------

class RecoveryPhase(str, Enum):
    """恢复阶段：描述 Runtime 当前处于哪种恢复路径。

    与 RunStatus（PENDING/RUNNING/DONE/REVIEW_REQUIRED/FAILED）正交：
    - state = RUNNING + recovery = FALLBACK → 正在走降级路径
    - state = DONE + recovery = DEGRADED → 降级成功完成
    """

    NONE = "none"
    FALLBACK = "fallback"
    DEGRADED = "degraded"


# ---------------------------------------------------------------------------
# V2.1: 恢复策略 —— Workflow 级别的异常治理总纲
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RecoveryPolicy:
    """Workflow 级别的恢复策略。

    场景包通过 TaskDefinition.recovery_policy 声明。Runtime 在步骤失败时
    按此策略决定恢复路径。未声明时使用 DEFAULT（不重试、不回滚、不降级）。
    """

    retry_enabled: bool = False
    fallback_enabled: bool = False
    review_on: tuple[str, ...] = ()          # 触发 REVIEW 的 ErrorCode 列表

    @staticmethod
    def DEFAULT() -> RecoveryPolicy:
        return RecoveryPolicy()


# ---------------------------------------------------------------------------
# V2.1: 执行日志 —— 步骤级审计记录
# ---------------------------------------------------------------------------

class StepStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"                      # 前置步骤失败，未执行
    RETRYING = "RETRYING"                    # 正在重试
    TIMED_OUT = "TIMED_OUT"                  # 超时


@dataclass
class ExecutionLogEntry:
    """单个步骤的执行记录。"""

    step_index: int
    step_name: str
    status: StepStatus = StepStatus.PENDING
    attempt: int = 0
    started_at: str = ""
    finished_at: str = ""
    duration_ms: float = 0
    error_code: str = ""
    error_message: str = ""


@dataclass
class ExecutionLog:
    """一次 Workflow 执行的完整日志。"""

    entries: list[ExecutionLogEntry] = field(default_factory=list)
    total_steps: int = 0
    recovery_phase: RecoveryPhase = RecoveryPhase.NONE

    def start_step(self, index: int, name: str) -> ExecutionLogEntry:
        import time
        entry = ExecutionLogEntry(
            step_index=index,
            step_name=name or f"step_{index}",
            status=StepStatus.RUNNING,
            attempt=1,
            started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.entries.append(entry)
        return entry

    def finish_step(self, entry: ExecutionLogEntry, status: StepStatus) -> None:
        import time
        entry.status = status
        entry.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
        if entry.started_at:
            from datetime import datetime
            try:
                t0 = datetime.strptime(entry.started_at, "%Y-%m-%d %H:%M:%S")
                t1 = datetime.strptime(entry.finished_at, "%Y-%m-%d %H:%M:%S")
                entry.duration_ms = (t1 - t0).total_seconds() * 1000
            except ValueError as exc:
                print(f"[degraded] 执行日志时间戳不可解析,时长置空: {exc!r}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 引擎服务门面：场景包触达引擎能力的唯一通道
# ---------------------------------------------------------------------------

class EngineRuntime(ABC):
    """引擎在运行步骤前注入到 RunContext 的服务对象。

    场景包拿到的 dist_root 只用于读模板/写产物，不允许枚举
    BROWSABLE_DIRS 之外的目录——工作台"文件"标签的白名单校验
    在 file_browser.py 里，场景包不要绕过它自己拼路径。
    """

    @property
    @abstractmethod
    def dist_root(self) -> Path:
        """运行时根目录（含 templates/ output/ storage/ 的那层）。"""

    @abstractmethod
    def read_template(self, rel_name: str) -> str:
        """读场景包 assets/pro/templates/ 的明文资产，如 "rules.yaml"。"""

    @abstractmethod
    def output_dir(self) -> Path:
        """产物目录（运行时根的 output/）。报告/Excel/Word 一律写这里，不写 storage/。"""

    @abstractmethod
    def state_get(self, key: str, default: Any = None) -> Any:
        """读跨周期状态（引擎自动加场景名前缀）。"""

    @abstractmethod
    def state_set(self, key: str, value: Any) -> None:
        """写跨周期状态。只放小体量 JSON 可序列化数据，别把整份输入塞进去。"""

    @abstractmethod
    def state_keys(self, prefix: str = "") -> list[str]:
        """枚举本场景的状态键（字典序）。prefix 是字面前缀不是通配符。

        给"记录会累积"的场景用（记忆检索、挂账历史）；键已含场景前缀
        归一，场景包只看到自己的键。
        """

    @abstractmethod
    def data_dir(self) -> Path:
        """本场景的私有数据目录（storage/data/<场景名>/，已确保存在）。

        给持续增长、用户不看原始形态的数据（记忆库、长期台账）——
        别塞 state KV（纪律：小体量），也别伪装成产物污染 output/。
        文件格式由场景包自定，引擎不猜。
        """

    @abstractmethod
    def resolve_input_file(self, user_path: str) -> Path:
        """校验输入文件存在并规范化路径。场景包不要用 open(user_path) 直开。"""


# ---------------------------------------------------------------------------
# 运行上下文与步骤
# ---------------------------------------------------------------------------

@dataclass
class RunContext:
    """一次任务执行的上下文，步骤函数的唯一入参。

    inputs 由引擎按 InputField 校验后填入（键为 field_id，FILE 类
    已经是 resolve_input_file 之后的绝对路径）；outputs/issues 由
    步骤函数写入；runtime 是引擎注入的服务门面。
    """

    task_id: str
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    runtime: EngineRuntime | None = None   # 引擎在执行前注入，步骤内不应为 None


StepFn = Callable[[RunContext], None]
"""步骤函数：拿 ctx、写 outputs/issues、不返回值。

顺序执行，前一步的 outputs 后一步可见——需要"分阶段给用户看中间结果"
时，把中间结果也写进 outputs（带阶段前缀的键），而不是打印到控制台。
"""


# ---------------------------------------------------------------------------
# 任务与场景定义
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TaskDefinition:
    """一个可执行的任务 = 工作台的一个功能按钮 = CLI 的一个 --task 值。"""

    task_id: str                          # kebab-case，如 "generate-report"
    label: str                            # 按钮文字，如 "生成周报"
    description: str = ""                 # 按钮下的一句说明/确认文案
    input_fields: tuple[InputField, ...] = ()
    steps: tuple = ()                     # tuple[StepFn | Step, ...]
    recovery_policy: RecoveryPolicy = field(default_factory=RecoveryPolicy.DEFAULT)


@dataclass(frozen=True)
class ScenarioDefinition:
    """场景包的完整自描述，build_scenario() 的返回值。

    它同时是三处的单一事实来源：
    - 运行期：引擎据此分发任务、渲染工作台表单；
    - 物料期：materialize_skill.py 据此渲染 SKILL.md 的 name/description 占位符；
    - 测试期：dev/tests/scenarios/ 据此枚举所有任务逐个跑确定性断言。
    """

    name: str                    # kebab-case，如 "weekly-report"，用作分发命名与状态前缀
    display_name: str            # 用户可见名，如 "周报生成器"
    version: str                 # 语义化版本，打进 build ID
    description: str = ""        # 一句话：做什么、何时该被触发（SKILL.md 同款语义）
    tasks: tuple[TaskDefinition, ...] = ()
    trial_limit: int | None = None   # None = 用引擎默认试用次数

    # -- 查询 ---------------------------------------------------------------

    def task(self, task_id: str) -> TaskDefinition | None:
        for t in self.tasks:
            if t.task_id == task_id:
                return t
        return None

    # -- 契约校验 -----------------------------------------------------------

    _KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

    def validate(self) -> None:
        """契约校验：不完整直接抛 ValueError，fast-fail。

        宁可在构建期报错，也不要让一个漏填 choices 的任务
        在运行时才炸。
        """
        for attr, value in (("name", self.name), ("version", self.version)):
            if not value:
                raise ValueError(f"ScenarioDefinition.{attr} 不能为空")
        if not self._KEBAB.match(self.name):
            raise ValueError(f"场景名必须是 kebab-case：{self.name!r}")
        if not self.tasks:
            raise ValueError("至少要定义一个任务")

        seen_tasks: set[str] = set()
        for t in self.tasks:
            if not self._KEBAB.match(t.task_id):
                raise ValueError(f"任务 ID 必须是 kebab-case：{t.task_id!r}")
            if t.task_id in seen_tasks:
                raise ValueError(f"任务 ID 重复：{t.task_id!r}")
            seen_tasks.add(t.task_id)
            if not t.steps:
                raise ValueError(f"任务 {t.task_id!r} 没有步骤")

            seen_fields: set[str] = set()
            for f in t.input_fields:
                if not self._KEBAB.match(f.field_id):
                    raise ValueError(
                        f"任务 {t.task_id!r} 的字段 ID 必须是 kebab-case：{f.field_id!r}"
                    )
                if f.field_id in seen_fields:
                    raise ValueError(
                        f"任务 {t.task_id!r} 的字段 ID 重复：{f.field_id!r}"
                    )
                seen_fields.add(f.field_id)
                if f.kind is InputKind.CHOICE and not f.choices:
                    raise ValueError(
                        f"CHOICE 字段 {f.field_id!r} 必须提供 choices"
                    )


# Pro 档位：通用办公格式（xlsx/docx/pdf -> 纯文本）适配器与
# read_text_auto 都实现在 parse_input.py（编码容错也搬了过去，避免
# 循环导入），这里 re-export 维持「场景包只从 scenario_api 拿工具」的边界
from parse_input import (  # noqa: E402,F401
    file_kind,
    read_excel_rows,
    read_file_auto,
    read_text_auto,
    write_excel_rows,
)

__all__ = [
    "file_kind",
    "read_file_auto",
    "read_excel_rows",
    "read_text_auto",
    "write_excel_rows",
    "InputKind",
    "InputField",
    "IssueLevel",
    "Issue",
    "ErrorCode",
    "ExecutionError",
    "StepPolicy",
    "Step",
    "StepFn",
    "ExecutionMode",
    "StepDataFlow",
    "EnvironmentSnapshot",
    "RecoveryPhase",
    "RecoveryPolicy",
    "StepStatus",
    "ExecutionLogEntry",
    "ExecutionLog",
    "EngineRuntime",
    "RunContext",
    "TaskDefinition",
    "ScenarioDefinition",
    "read_text_auto",
]
