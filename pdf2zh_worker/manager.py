"""队列调度：入队、并发消费、取消、重启恢复、定时清扫。

worker 完全被动：不重试、不断点续跑、不回调主应用。任何异常都只是把任务写成
failed，等主应用下一次 tick 自己决定重派还是退款。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Dict, Optional, Tuple

from .config import WorkerConfig
from .models import TranslateJobParams
from .runner import TranslationFailed, run_translation
from .store import (
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    JobState,
    JobStore,
)

logger = logging.getLogger(__name__)

#: 取消一个跑着的任务后，最多等它收摊多久（pdf2zh 内部 join/terminate/kill 约 4s）
CANCEL_TIMEOUT_SECONDS = 30
RESTART_ERROR = "worker 重启导致任务中断"
CANCELLED_ERROR = "任务已被取消"


class QueueFull(RuntimeError):
    """排队数已达 TRANSLATE_WORKER_QUEUE_LIMIT —— 回 429，主应用留到下轮再派。"""


class JobManager:
    def __init__(self, config: WorkerConfig, store: JobStore) -> None:
        self.config = config
        self.store = store
        #: 排队中的任务，FIFO；value 是带明文 apiKey 的参数，只在内存里活着
        self._pending: "OrderedDict[str, TranslateJobParams]" = OrderedDict()
        #: 正在跑的任务 → 它的 asyncio.Task（用于 DELETE 取消）
        self._running: Dict[str, asyncio.Task] = {}
        #: 被 DELETE 主动取消的 jobId。用来把「只该死这一个任务」和
        #: 「消费者自己被 shutdown 取消」区分开——否则一次 DELETE 会连消费者一起收掉，
        #: 并发能力从此少一格。
        self._cancelled: set = set()
        #: 唤醒消费者用的信号队列。必须等到 start()（已经在事件循环里）才建：
        #: asyncio 的同步原语会绑定创建时所在的循环，在 __init__ 里建会绑错循环，
        #: 表现是任务永远停在 queued——消费者根本收不到唤醒。
        self._signal: Optional["asyncio.Queue[str]"] = None
        self._consumers: list = []
        self._sweeper: Optional[asyncio.Task] = None
        self._closing = False

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        self._signal = asyncio.Queue()
        self.store.load_all()
        self._recover_after_restart()
        swept = self.store.sweep()
        if swept:
            logger.info("启动清扫：删除 %d 个过期任务", swept)
        for index in range(self.config.concurrency):
            self._consumers.append(asyncio.create_task(self._consume(index), name=f"worker-{index}"))
        self._sweeper = asyncio.create_task(self._sweep_loop(), name="sweeper")
        logger.info(
            "调度已启动：并发 %d，排队上限 %d，保留 %dh",
            self.config.concurrency,
            self.config.queue_limit,
            self.config.retention_hours,
        )

    async def shutdown(self) -> None:
        self._closing = True
        tasks = [*self._consumers, *self._running.values()]
        if self._sweeper is not None:
            tasks.append(self._sweeper)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(set(tasks), timeout=CANCEL_TIMEOUT_SECONDS)
        self._consumers.clear()
        self._running.clear()
        self._pending.clear()

    def _recover_after_restart(self) -> None:
        """协议：重启后所有 queued/running 一律标 failed，由主应用按同 jobId 幂等重派。"""
        now = time.time()
        broken = 0
        for state in self.store.all_states():
            if state.status in (STATUS_QUEUED, STATUS_RUNNING):
                state.status = STATUS_FAILED
                state.error = RESTART_ERROR
                state.finished_at = now
                self.store.save(state)
                broken += 1
        if broken:
            logger.warning("重启：%d 个未完成任务已标记 failed，等主应用重派", broken)

    # ---------- 查询 ----------

    def queue_snapshot(self) -> dict:
        return {
            "running": len(self._running),
            "queued": len(self._pending),
            "capacity": self.config.concurrency,
            "queueLimit": self.config.queue_limit,
        }

    def is_active(self, job_id: str) -> bool:
        """排队中或运行中——PUT input 撞上它要回 409。"""
        return job_id in self._pending or job_id in self._running

    def position_of(self, job_id: str) -> int:
        for index, pending_id in enumerate(self._pending):
            if pending_id == job_id:
                return index
        return 0

    # ---------- 入队 / 取消 ----------

    def enqueue(self, state: JobState, params: TranslateJobParams) -> Tuple[str, int]:
        """把任务放进队列。返回 (status, position)。队列满抛 QueueFull。

        幂等：已排队/运行中/已成功的任务原样回当前状态，不重复入队。
        """
        if state.job_id in self._running:
            return STATUS_RUNNING, 0
        if state.job_id in self._pending:
            return STATUS_QUEUED, self.position_of(state.job_id)
        if state.status == STATUS_SUCCEEDED:
            return STATUS_SUCCEEDED, 0

        if self._signal is None:
            raise RuntimeError("调度还没启动")
        if len(self._pending) >= self.config.queue_limit:
            raise QueueFull(f"排队已满（{len(self._pending)}/{self.config.queue_limit}）")

        state.status = STATUS_QUEUED
        state.stage = None
        state.progress = 0
        state.error = None
        state.started_at = None
        state.finished_at = None
        state.mono_bytes = None
        state.dual_bytes = None
        state.total_seconds = None
        state.params = params.redacted()
        self.store.save(state)

        self._pending[state.job_id] = params
        self._signal.put_nowait(state.job_id)
        return STATUS_QUEUED, self.position_of(state.job_id)

    async def cancel(self, job_id: str) -> None:
        """取消任务并等它真收摊。DELETE 靠它保证「先杀进程再删目录」。"""
        self._pending.pop(job_id, None)
        task = self._running.get(job_id)
        if task is None:
            return
        self._cancelled.add(job_id)
        task.cancel()
        # asyncio.wait 不会把 task 的异常抛给这里，正是我们要的
        await asyncio.wait({task}, timeout=CANCEL_TIMEOUT_SECONDS)
        if not task.done():
            logger.error("任务 %s 取消超时，进程可能还在跑", job_id)

    # ---------- 消费 ----------

    async def _consume(self, index: int) -> None:
        assert self._signal is not None
        while not self._closing:
            try:
                job_id = await self._signal.get()
            except asyncio.CancelledError:
                return
            params = self._pending.pop(job_id, None)
            if params is None:
                # 排队期间被 DELETE 掉了
                continue
            state = self.store.get(job_id)
            if state is None:
                continue
            try:
                await self._execute(state, params)
            except asyncio.CancelledError:
                # 消费者本身被取消（shutdown）：状态留在 running，
                # 下次启动时 _recover_after_restart 会把它标 failed
                return
            except Exception as exc:
                logger.exception("消费任务 %s 时出现意外错误", job_id)
                # 兜底：_execute 里任何没接住的异常都不能让任务卡在非终态
                current = self.store.get(job_id)
                if current is not None and not current.is_terminal:
                    self._fail(current, f"worker 内部错误：{type(exc).__name__}: {exc}")

    async def _execute(self, state: JobState, params: TranslateJobParams) -> None:
        job_id = state.job_id
        state.status = STATUS_RUNNING
        state.stage = None
        state.progress = 0
        state.error = None
        state.started_at = time.time()
        state.finished_at = None
        self.store.save(state)

        def on_progress(stage: Optional[str], progress: int) -> None:
            # 只在真的变了才写盘：进度事件 0.5s 一发，没必要每次都落 JSON
            if stage == state.stage and progress == state.progress:
                return
            state.stage = stage
            state.progress = progress
            self.store.save(state)

        try:
            glossary_file = self._glossary_for(job_id, params)
        except Exception as exc:
            # 别让准备阶段的异常把任务永远晾在 running 上——主应用要等满 180 分钟
            # 超时才会去救它
            self._fail(state, f"术语表写入失败：{type(exc).__name__}: {exc}")
            logger.exception("任务 %s 准备术语表失败", job_id)
            return

        task = asyncio.create_task(
            run_translation(
                self.config,
                params,
                source=self.store.source_path(job_id),
                output_dir=self.store.output_dir(job_id),
                glossary_file=glossary_file,
                mono_target=self.store.artifact_path(job_id, "mono"),
                dual_target=self.store.artifact_path(job_id, "dual"),
                on_progress=on_progress,
            )
        )
        self._running[job_id] = task
        logger.info("任务 %s 开始翻译（%s → %s）", job_id, params.langIn, params.langOut)
        try:
            outcome = await task
        except asyncio.CancelledError:
            # 两种来源要分开：DELETE 只杀这一个任务，消费者得活着接着干；
            # shutdown 是把消费者本身取消了，必须让 CancelledError 继续往上冒。
            deliberate = job_id in self._cancelled
            self._cancelled.discard(job_id)
            state.status = STATUS_FAILED
            state.error = CANCELLED_ERROR if deliberate else RESTART_ERROR
            state.finished_at = time.time()
            self.store.save(state)
            logger.info("任务 %s 已中断（%s）", job_id, state.error)
            if deliberate:
                return
            raise
        except TranslationFailed as exc:
            state.status = STATUS_FAILED
            state.error = str(exc)
            state.finished_at = time.time()
            self.store.save(state)
            logger.warning("任务 %s 翻译失败：%s", job_id, exc)
            return
        except Exception as exc:  # noqa: BLE001 - 兜底，绝不让调度线程死掉
            state.status = STATUS_FAILED
            state.error = f"worker 内部错误：{type(exc).__name__}: {exc}"[:500]
            state.finished_at = time.time()
            self.store.save(state)
            logger.exception("任务 %s 出现未预期错误", job_id)
            return
        finally:
            self._running.pop(job_id, None)

        state.status = STATUS_SUCCEEDED
        state.stage = "done"
        state.progress = 100
        state.error = None
        state.finished_at = time.time()
        state.total_seconds = outcome.total_seconds
        state.mono_bytes = _size_of(outcome.mono_path)
        state.dual_bytes = _size_of(outcome.dual_path)
        self.store.save(state)
        logger.info(
            "任务 %s 完成，用时 %.1fs（mono=%s dual=%s）",
            job_id,
            outcome.total_seconds,
            state.mono_bytes,
            state.dual_bytes,
        )

    def _fail(self, state: JobState, error: str) -> None:
        state.status = STATUS_FAILED
        state.error = error[:500]
        state.finished_at = time.time()
        self.store.save(state)

    def _glossary_for(self, job_id: str, params: TranslateJobParams):
        from .runner import write_glossary_csv

        if not params.glossary:
            return None
        path = self.store.glossary_path(job_id)
        write_glossary_csv(params.glossary, path)
        return path

    # ---------- 清扫 ----------

    async def _sweep_loop(self) -> None:
        interval = self.config.sweep_minutes * 60
        while not self._closing:
            try:
                await asyncio.sleep(interval)
                removed = self.store.sweep()
                if removed:
                    logger.info("定时清扫：删除 %d 个过期任务", removed)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("清扫出错，下轮继续")


def _size_of(path) -> Optional[int]:
    if path is None:
        return None
    try:
        return path.stat().st_size
    except OSError:
        return None
