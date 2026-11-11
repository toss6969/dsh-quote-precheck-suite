"""
state_store.py —— 本地状态持久化，用 SQLite，不用 MySQL

单文件数据库，零安装、零配置，对单用户本地工具完全够用。

相对参考模板的改动：新增 scenario_state KV 表，支撑场景包的
跨周期状态（scenario_api.EngineRuntime.state_get/state_set），
键由 EngineRuntimeImpl 自动加场景名前缀，多场景互不污染。

run_log 相对参考模板增加了 task_id 列：一个场景包有多个任务，
历史记录必须能区分"这次跑的是哪个按钮"。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any


def _process_alive(pid: int) -> bool:
    """这个进程还在不在。判断不了时按"在"处理——宁可漏回收一行,不可误杀在跑的任务。

    ★Windows 上绝不能用 os.kill(pid, 0):那里 os.kill 对非 CTRL 信号的实现是
    TerminateProcess,"探活"会直接把对方进程结束掉。改用 OpenProcess 查退出码。
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            # ERROR_ACCESS_DENIED(5)= 进程在,只是不归我们管;其余 = 不存在
            return kernel32.GetLastError() == 5
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


class StateStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：StepPolicy.timeout_seconds 让 workflow 的
        # _run_with_timeout 把步骤放进一个子线程执行，同一个连接因此会从
        # 创建它的主线程之外被访问。访问永远是顺序的（主线程 join() 等子
        # 线程跑完才继续），真正并发写入只会在"步骤超时后子线程仍在后台
        # 残留"这种边界情况下发生——CPython 默认链接的 sqlite3 是
        # serialized 线程模式，能自行处理，顶多在撞车时报可重试的
        # OperationalError，不会破坏数据。
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS run_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ran_at TEXT NOT NULL,
                task_id TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL,
                summary TEXT
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        # V2.1: 步骤级执行日志（审计/排障/恢复分析）
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS execution_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                step_index INTEGER NOT NULL,
                step_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 1,
                started_at TEXT NOT NULL DEFAULT '',
                finished_at TEXT NOT NULL DEFAULT '',
                duration_ms REAL NOT NULL DEFAULT 0,
                error_code TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (run_id) REFERENCES run_log(id)
            )
            """
        )
        # V2.2: 执行检查点（支持断点恢复）
        # 每个步骤成功后写入一条 checkpoint，保存完整的上下文快照。
        # resume 时取最新一条 checkpoint 恢复上下文，从下一步继续执行。
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS execution_checkpoint (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                task_id TEXT NOT NULL DEFAULT '',
                step_index INTEGER NOT NULL,
                next_step INTEGER NOT NULL,
                inputs_json TEXT NOT NULL DEFAULT '{}',
                outputs_json TEXT NOT NULL DEFAULT '{}',
                issues_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES run_log(id)
            )
            """
        )
        # V2.4: 整批级进度（--batch 跨进程中断后可跳过已处理文件）。
        # 按（batch_key, file_name）去重——batch_key = "<task_id>|<批量目录绝对路径>"，
        # 用文件名而不是序号匹配：目录里增删文件不会打乱"哪些已经处理过"的判断。
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS batch_progress (
                batch_key TEXT NOT NULL,
                file_name TEXT NOT NULL,
                state TEXT NOT NULL,
                artifacts_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL,
                run_id INTEGER,
                PRIMARY KEY (batch_key, file_name)
            )
            """
        )
        # V2.5: 旧库（本地个人版早期）的 batch_progress 没有 run_id 列。
        # 不迁移的话跨轮续跑时被跳过文件的 run_id 会被读成不存在，批量结果
        # 一断点续跑就失掉运行粒度回溯。ALTER ADD 对缺失列安全幂等。
        has_run_id = any(
            row[1] == "run_id"
            for row in self._conn.execute("PRAGMA table_info(batch_progress)").fetchall()
        )
        if not has_run_id:
            self._conn.execute("ALTER TABLE batch_progress ADD COLUMN run_id INTEGER")
        # V2.5: 工作台人审复核台账（运行粒度审计）。一条复核记录通过 run_id
        # 外键绑定到具体一次 run_log，可精确回溯"哪次运行的结论是人审给出的"。
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS review_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER,
                task_id TEXT NOT NULL DEFAULT '',
                verdict TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES run_log(id)
            )
            """
        )
        self._conn.commit()

    # -- 跨周期状态（场景包专用）---------------------------------------------

    def state_get(self, key: str, default: Any = None) -> Any:
        cur = self._conn.execute(
            "SELECT value FROM scenario_state WHERE key = ?", (key,)
        )
        row = cur.fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            # 写入侧统一走 state_set 的 json 序列化，出现解析失败说明
            # 数据被外部改过——按"不存在"处理比崩溃对用户更友好
            return default

    def state_set(self, key: str, value: Any) -> None:
        self._conn.execute(
            """
            INSERT INTO scenario_state (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                           updated_at = excluded.updated_at
            """,
            (
                key,
                json.dumps(value, ensure_ascii=False),
                time.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        self._conn.commit()

    def state_keys(self, prefix: str = "") -> list[str]:
        """枚举跨周期状态键（按字典序，确定性）。

        支撑"记录会累积"的场景（记忆库检索、挂账历史）：此前引擎没有
        枚举能力，daily_plan 只能按日期规律盲扫最近 14 天——间隔超过
        14 天就丢上期记录。prefix 会被转义，不是通配符语义。
        """
        cur = self._conn.execute(
            "SELECT key FROM scenario_state WHERE key LIKE ? ESCAPE '\\' ORDER BY key",
            (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",),
        )
        return [row[0] for row in cur.fetchall()]

    # -- 运行历史 -----------------------------------------------------------

    def _ensure_run_pid_column(self) -> None:
        """run_log.pid:谁在跑这一行。旧库没有这一列,就地补上(不丢数据)。"""
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(run_log)")}
        if "pid" not in cols:
            self._conn.execute("ALTER TABLE run_log ADD COLUMN pid INTEGER")
            self._conn.commit()

    def record_run(self, state: str, task_id: str = "", summary: str = "") -> int:
        """记录一次运行，返回 run_id（用于关联 execution_log 和 checkpoint）。"""
        self._ensure_run_pid_column()
        cur = self._conn.execute(
            "INSERT INTO run_log (ran_at, task_id, state, summary, pid) VALUES (?, ?, ?, ?, ?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"), task_id, state, summary, os.getpid()),
        )
        self._conn.commit()
        return cur.lastrowid

    def update_run_state(self, run_id: int, state: str, summary: str = "") -> None:
        """V2.2: 更新运行记录的终态（先 record_run 创建，执行完再更新）。

        置回 RUNNING(断点恢复)时顺带改写 pid:恢复它的是本进程。
        """
        self._ensure_run_pid_column()
        if state == "RUNNING":
            self._conn.execute(
                "UPDATE run_log SET state = ?, summary = ?, pid = ? WHERE id = ?",
                (state, summary, os.getpid(), run_id),
            )
        else:
            self._conn.execute(
                "UPDATE run_log SET state = ?, summary = ? WHERE id = ?",
                (state, summary, run_id),
            )
        self._conn.commit()

    def sweep_orphan_running(self, is_alive=None) -> int:
        """启动回收:把"进程已经不在了"的 RUNNING 改判为 FAILED(自 4.2 引擎 H6 移植)。

        为什么要有:进程被杀(关窗口 / 断电)时终态来不及写,那次运行永远停在
        RUNNING。而 list_resumable 只列 FAILED / REVIEW_REQUIRED——检查点明明存着,
        断点恢复却列不出来,恰好救不了最需要它的情况。改判 FAILED 后它就进了可恢复列表。

        为什么不照搬 4.2 的"启动即全判死":本引擎有本地工作台,同一进程里用线程
        并发跑任务,CLI 一启动就会把工作台正在跑的任务误判成孤儿。所以只回收
        进程确实不在了的行:本进程的不碰,活着的进程的不碰;没有 pid 的旧行
        (加列之前写的)视为孤儿。summary 追加标记而不覆盖,保留原有线索。
        返回本次改写的行数。
        """
        self._ensure_run_pid_column()
        alive = is_alive or _process_alive
        me = os.getpid()
        orphans = [
            run_id
            for run_id, pid in self._conn.execute(
                "SELECT id, pid FROM run_log WHERE state = 'RUNNING'"
            ).fetchall()
            if pid is None or (pid != me and not alive(pid))
        ]
        for run_id in orphans:
            self._conn.execute(
                "UPDATE run_log SET state = 'FAILED',"
                " summary = TRIM(COALESCE(summary, '') || ' interrupted (orphan sweep)')"
                " WHERE id = ?",
                (run_id,),
            )
        self._conn.commit()
        return len(orphans)

    def record_execution_log(
        self,
        run_id: int,
        step_index: int,
        step_name: str,
        status: str,
        attempt: int = 1,
        started_at: str = "",
        finished_at: str = "",
        duration_ms: float = 0,
        error_code: str = "",
        error_message: str = "",
    ) -> None:
        """V2.1: 记录单个步骤的执行日志（关联到 run_id）。"""
        self._conn.execute(
            """INSERT INTO execution_log
               (run_id, step_index, step_name, status, attempt,
                started_at, finished_at, duration_ms, error_code, error_message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, step_index, step_name, status, attempt,
             started_at, finished_at, duration_ms, error_code, error_message),
        )
        self._conn.commit()

    def record_execution_log_batch(self, run_id: int, entries: list[tuple]) -> None:
        """V2.1: 批量写入执行日志（一次运行结束后统一写入）。

        entries: list of (step_index, step_name, status, attempt,
                          started_at, finished_at, duration_ms,
                          error_code, error_message)
        """
        if not entries:
            return
        self._conn.executemany(
            """INSERT INTO execution_log
               (run_id, step_index, step_name, status, attempt,
                started_at, finished_at, duration_ms, error_code, error_message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(run_id, *e) for e in entries],
        )
        self._conn.commit()

    def get_execution_log(self, run_id: int) -> list[tuple]:
        """V2.1: 查询某次运行的步骤级执行日志。"""
        cur = self._conn.execute(
            """SELECT step_index, step_name, status, attempt,
                      started_at, finished_at, duration_ms,
                      error_code, error_message
               FROM execution_log WHERE run_id = ? ORDER BY step_index""",
            (run_id,),
        )
        return cur.fetchall()

    def last_run(self) -> tuple | None:
        cur = self._conn.execute(
            "SELECT ran_at, task_id, state, summary FROM run_log ORDER BY id DESC LIMIT 1"
        )
        return cur.fetchone()

    def list_runs(self, limit: int = 20) -> list[tuple]:
        """给"历史记录"面板用，返回最近 N 条运行记录。"""
        cur = self._conn.execute(
            "SELECT ran_at, task_id, state, summary FROM run_log "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return cur.fetchall()

    # -- V2.2: Checkpoint（断点恢复）----------------------------------------

    def save_checkpoint(
        self,
        run_id: int,
        task_id: str,
        step_index: int,
        next_step: int,
        inputs: dict,
        outputs: dict,
        issues: list,
    ) -> None:
        """V2.2: 保存执行检查点（步骤成功后调用）。

        保存完整的上下文快照（inputs/outputs/issues），resume 时据此恢复。
        每次 checkpoint 是增量追加（不覆盖），取最新一条即可。
        """
        self._conn.execute(
            """INSERT INTO execution_checkpoint
               (run_id, task_id, step_index, next_step,
                inputs_json, outputs_json, issues_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id, task_id, step_index, next_step,
                json.dumps(inputs, ensure_ascii=False, default=str),
                json.dumps(outputs, ensure_ascii=False, default=str),
                json.dumps(issues, ensure_ascii=False, default=str),
                time.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        self._conn.commit()

    def load_latest_checkpoint(self, run_id: int) -> dict | None:
        """V2.2: 加载某次运行的最新检查点。返回 None 表示无可用检查点。"""
        cur = self._conn.execute(
            """SELECT step_index, next_step, inputs_json, outputs_json, issues_json
               FROM execution_checkpoint
               WHERE run_id = ?
               ORDER BY id DESC LIMIT 1""",
            (run_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "step_index": row[0],
            "next_step": row[1],
            "inputs": json.loads(row[2]),
            "outputs": json.loads(row[3]),
            "issues": json.loads(row[4]),
        }

    def list_resumable(self, limit: int = 20) -> list[tuple]:
        """V2.2: 列出可恢复的执行（最近失败的、有 checkpoint 的运行）。

        每个 run 只取它最新的一条 checkpoint（子查询按 run_id 取
        MAX(id)）——一个 run 可能有多条 checkpoint（每步成功一条），之前用
        SELECT DISTINCT 对整行去重不会按 run 聚合，一个多步骤的失败 run
        能把 LIMIT 全占满，把其它真正可恢复的 run 挤出结果之外。
        """
        cur = self._conn.execute(
            """SELECT r.id, r.ran_at, r.task_id, r.state, r.summary,
                      c.next_step, c.created_at
               FROM run_log r
               INNER JOIN execution_checkpoint c ON c.id = (
                   SELECT MAX(c2.id) FROM execution_checkpoint c2 WHERE c2.run_id = r.id
               )
               WHERE r.state IN ('FAILED', 'REVIEW_REQUIRED')
               ORDER BY c.id DESC
               LIMIT ?""",
            (limit,),
        )
        return cur.fetchall()

    # -- V2.5: 人审复核台账（运行粒度审计）------------------------------------

    def run_task_id(self, run_id: int) -> str | None:
        """按 run_id 反查 run_log 里的 task_id——前端只带 run_id 时不带 task
        也能确定绑定关系；查不到说明 run_id 不存在。"""
        cur = self._conn.execute(
            "SELECT task_id FROM run_log WHERE id = ?", (run_id,)
        )
        row = cur.fetchone()
        return row[0] if row else None

    def record_review(
        self, run_id: int | None, task_id: str, verdict: str, reason: str
    ) -> int:
        """记一条人审复核结论，绑定到 run_id（可空：无 run 上下文的历史/兜底路径）。
        返回 review_log 行 id。"""
        cur = self._conn.execute(
            """INSERT INTO review_log (run_id, task_id, verdict, reason, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (run_id, task_id, verdict, reason, time.strftime("%Y-%m-%d %H:%M:%S")),
        )
        self._conn.commit()
        return cur.lastrowid

    def list_reviews(
        self, task_id: str | None = None, run_id: int | None = None, limit: int = 50
    ) -> list[dict]:
        """查人审复核记录（最新在前）。按 task_id 和/或 run_id 过滤，二者都可选。"""
        sql = "SELECT id, run_id, task_id, verdict, reason, created_at FROM review_log"
        clauses, params = [], []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        cur = self._conn.execute(sql, params)
        return [
            {
                "id": row[0], "run_id": row[1], "task_id": row[2],
                "verdict": row[3], "reason": row[4], "created_at": row[5],
            }
            for row in cur.fetchall()
        ]

    # -- V2.4: 批量整批级进度（跨进程中断续批）----------------------------------

    def get_batch_progress(self, batch_key: str) -> dict[str, dict]:
        """返回这个批次里已经处理过的文件名 -> {"state":..., "artifacts":...}。

        artifacts 是该文件当时产出的 *_file 键值对——续跑时跳过重新处理，
        但证据包/汇总仍要能收进这些文件的哈希，不能因为跳过就把它们从
        审计链里漏掉。空字典表示没有历史进度。
        """
        cur = self._conn.execute(
            "SELECT file_name, state, artifacts_json, run_id FROM batch_progress WHERE batch_key = ?",
            (batch_key,),
        )
        return {
            row[0]: {
                "state": row[1],
                "artifacts": json.loads(row[2]),
                "run_id": row[3],
            }
            for row in cur.fetchall()
        }

    def record_batch_file_done(
        self, batch_key: str, file_name: str, state: str, artifacts: dict | None = None,
        run_id: int | None = None,
    ) -> None:
        """记一个文件在这个批次里的处理终态、产物与该次运行的 run_id，供下次
        中断后恢复时跳过（跳过时仍可用 run_id 做运行粒度回溯）。"""
        self._conn.execute(
            """INSERT INTO batch_progress (batch_key, file_name, state, artifacts_json, updated_at, run_id)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(batch_key, file_name) DO UPDATE SET
                   state = excluded.state, artifacts_json = excluded.artifacts_json,
                   updated_at = excluded.updated_at, run_id = excluded.run_id""",
            (
                batch_key, file_name, state,
                json.dumps(artifacts or {}, ensure_ascii=False),
                time.strftime("%Y-%m-%d %H:%M:%S"),
                run_id,
            ),
        )
        self._conn.commit()

    def clear_batch_progress(self, batch_key: str) -> None:
        """整批顺利跑完后清掉进度记录——避免以后对同一目录的全新一轮批处理
        被旧记录误跳过（进度只为"续跑未完成的这一次"服务，不是永久去重库）。
        """
        self._conn.execute("DELETE FROM batch_progress WHERE batch_key = ?", (batch_key,))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
