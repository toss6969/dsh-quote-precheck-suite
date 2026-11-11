"""
engine_runtime.py —— scenario_api.EngineRuntime 的引擎侧实现

场景包在步骤函数里拿到的 runtime 就是这个类的实例。
它存在的意义：把"场景包允许触达哪些引擎能力"收敛到一个显式的
白名单类里——场景包能读模板、写产物、存取跨周期状态、解析输入文件，
仅此而已。想加能力，先在这里加方法并想清楚边界，不要让场景包
直接摸 StateStore 或 dist_root 下的任意路径。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scenario_api import EngineRuntime
from state_store import StateStore


class EngineRuntimeImpl(EngineRuntime):
    def __init__(
        self,
        scenario_name: str,
        dist_root: Path,
        store: StateStore,
        output_subdir: str = "",
    ):
        self._scenario_name = scenario_name
        self._dist_root = dist_root
        self._store = store
        # 批量模式按源文件隔离产物（output/batch/01-<文件名>/）：
        # 场景产物名普遍含日期，N 个文件同日批量会互相覆盖——引擎兜底
        self._output_subdir = output_subdir

    @property
    def dist_root(self) -> Path:
        return self._dist_root

    def template_root_paths(self) -> list[Path]:
        """模板根目录候选：场景源码树的 assets/pro/templates/（本地直读）。"""
        snake = self._scenario_name.replace("-", "_")
        assets = (Path(__file__).resolve().parent.parent
                  / "scenarios" / snake / "assets" / "pro" / "templates")
        return [assets] if assets.is_dir() else []

    def read_template(self, rel_name: str) -> str:
        """读模板明文资产，返回原始文本。

        优先场景 assets/pro/templates/（本地直跑），包内无场景源码时
        回退 dist_root/templates/。YAML/JSON 的解析由场景包自己决定——
        引擎只负责给到内容，不替场景包猜测格式。
        """
        for root in self.template_root_paths():
            path = root / rel_name
            if path.is_file():
                return path.read_text(encoding="utf-8")
        raise FileNotFoundError(
            f"模板文件不存在：{rel_name}"
            "（检查场景包 assets/pro/templates/ 是否存在）"
        )

    def output_dir(self) -> Path:
        out = self._dist_root / "output"
        if self._output_subdir:
            out = out / self._output_subdir
        out.mkdir(parents=True, exist_ok=True)
        return out

    def state_get(self, key: str, default: Any = None) -> Any:
        return self._store.state_get(self._prefixed(key), default)

    def state_set(self, key: str, value: Any) -> None:
        self._store.state_set(self._prefixed(key), value)

    def state_keys(self, prefix: str = "") -> list[str]:
        # 前缀归一后枚举，再剥掉场景前缀——场景包只见自己的键
        full_prefix = self._prefixed(prefix)
        cut = len(self._scenario_name) + 1  # len("<name>:")
        return [k[cut:] for k in self._store.state_keys(full_prefix)]

    def data_dir(self) -> Path:
        data = self._dist_root / "storage" / "data" / self._scenario_name
        data.mkdir(parents=True, exist_ok=True)
        return data

    def resolve_input_file(self, user_path: str) -> Path:
        """校验用户提供的输入文件存在，返回规范化后的绝对路径。"""
        path = Path(user_path).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        else:
            path = path.resolve()
        if not path.is_file():
            raise FileNotFoundError(f"输入文件不存在：{path}")
        return path

    def _prefixed(self, key: str) -> str:
        # 场景名前缀：同一台机器装多个场景产品时，state.db 各自独立，
        # 这层前缀是给"未来可能共享状态文件"留的保险，成本一行。
        return f"{self._scenario_name}:{key}"
