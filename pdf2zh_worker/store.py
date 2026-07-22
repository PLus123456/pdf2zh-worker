"""任务目录与状态持久化。

目录布局（`<data>` = TRANSLATE_WORKER_DATA）：

    <data>/jobs/<jobId>/state.json     任务状态（真源，重启后仍要能读出终态）
    <data>/jobs/<jobId>/source.pdf     主应用推来的原件
    <data>/jobs/<jobId>/glossary.csv   术语表（可选）
    <data>/jobs/<jobId>/out/           pdf2zh 的输出目录（中间产物一堆）
    <data>/jobs/<jobId>/mono.pdf       归一化后的单语产物（下载端点直接读这个）
    <data>/jobs/<jobId>/dual.pdf       归一化后的双语产物
    <data>/tombstones/<jobId>          清扫墓碑，用来把 410 和 404 分开

状态写盘一律 tmp + os.replace 原子落地：进程被 kill 也不会留下半截 JSON，
否则重启时解析失败会把一个「已成功、产物还在」的任务误判成丢失。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Iterator, Optional

from .config import WorkerConfig

logger = logging.getLogger(__name__)

#: 协议：jobId = 主应用 JobQueue 行 id，字符集 [A-Za-z0-9_-]。
#: 这条正则同时也是路径穿越的唯一防线——不合规的 id 一律 400，不落地任何目录。
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

STATUS_CREATED = "created"
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

TERMINAL_STATUSES = frozenset({STATUS_SUCCEEDED, STATUS_FAILED})
#: 墓碑保留时长：比产物保留久得多，这样主应用晚来的下载能拿到明确的 410 而不是含糊的 404
TOMBSTONE_RETENTION_SECONDS = 7 * 24 * 3600


def is_valid_job_id(job_id: str) -> bool:
    return bool(JOB_ID_RE.match(job_id))


@dataclass
class JobState:
    job_id: str
    status: str = STATUS_CREATED
    stage: Optional[str] = None
    progress: int = 0
    error: Optional[str] = None
    mono_bytes: Optional[int] = None
    dual_bytes: Optional[int] = None
    total_seconds: Optional[float] = None
    input_bytes: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    #: 脱敏后的 start 参数快照（apiKey 已抹掉），仅供排错
    params: Optional[dict] = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "JobState":
        known = {f for f in cls.__dataclass_fields__}  # noqa: SLF001 - dataclass 公开元数据
        return cls(**{k: v for k, v in raw.items() if k in known})

    def to_api(self) -> dict:
        """GET /jobs/:id 的响应体（协议 v1 字段名）。"""
        output = None
        if self.status == STATUS_SUCCEEDED:
            output = {}
            if self.mono_bytes is not None:
                output["monoBytes"] = self.mono_bytes
            if self.dual_bytes is not None:
                output["dualBytes"] = self.dual_bytes
            if self.total_seconds is not None:
                output["totalSeconds"] = round(self.total_seconds, 2)
        return {
            "jobId": self.job_id,
            "status": self.status,
            "stage": self.stage,
            "progress": self.progress,
            "error": self.error,
            "output": output,
        }


class JobStore:
    """任务目录 + 状态的唯一入口。内存里缓存一份，写盘 write-through。"""

    def __init__(self, config: WorkerConfig) -> None:
        self.config = config
        self._states: Dict[str, JobState] = {}
        config.jobs_dir.mkdir(parents=True, exist_ok=True)
        config.tombstones_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 路径 ----------

    def job_dir(self, job_id: str) -> Path:
        return self.config.jobs_dir / job_id

    def source_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "source.pdf"

    def glossary_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "glossary.csv"

    def output_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "out"

    def artifact_path(self, job_id: str, variant: str) -> Path:
        """variant: mono | dual —— 归一化后的产物路径。"""
        return self.job_dir(job_id) / f"{variant}.pdf"

    def _state_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "state.json"

    # ---------- 状态读写 ----------

    def load_all(self) -> None:
        """启动时把磁盘上的任务读回内存。坏掉的状态文件跳过并留在盘上待人工看。"""
        if not self.config.jobs_dir.exists():
            return
        for entry in sorted(self.config.jobs_dir.iterdir()):
            if not entry.is_dir() or not is_valid_job_id(entry.name):
                continue
            state = self._read_state_file(entry.name)
            if state is not None:
                self._states[entry.name] = state

    def _read_state_file(self, job_id: str) -> Optional[JobState]:
        path = self._state_path(job_id)
        try:
            raw = json.loads(path.read_text("utf-8"))
        except FileNotFoundError:
            return None
        except Exception:
            logger.warning("任务 %s 的 state.json 解析失败，跳过", job_id, exc_info=True)
            return None
        try:
            return JobState.from_dict(raw)
        except Exception:
            logger.warning("任务 %s 的 state.json 字段不合法，跳过", job_id, exc_info=True)
            return None

    def get(self, job_id: str) -> Optional[JobState]:
        return self._states.get(job_id)

    def all_states(self) -> Iterator[JobState]:
        return iter(list(self._states.values()))

    def save(self, state: JobState) -> None:
        """写盘（原子）+ 更新内存缓存。"""
        self._states[state.job_id] = state
        job_dir = self.job_dir(state.job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        target = self._state_path(state.job_id)
        tmp = target.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(state.to_dict(), ensure_ascii=False), "utf-8")
            os.replace(tmp, target)
        except Exception:
            logger.exception("任务 %s 状态写盘失败", state.job_id)
            tmp.unlink(missing_ok=True)

    # ---------- 生命周期 ----------

    def reset_for_input(self, job_id: str) -> JobState:
        """覆盖式重建：清掉旧目录和墓碑，开一个全新的 created 任务。"""
        self.remove(job_id)
        self.clear_tombstone(job_id)
        self.job_dir(job_id).mkdir(parents=True, exist_ok=True)
        state = JobState(job_id=job_id)
        self.save(state)
        return state

    def remove(self, job_id: str) -> None:
        """删目录 + 清内存。幂等。"""
        self._states.pop(job_id, None)
        shutil.rmtree(self.job_dir(job_id), ignore_errors=True)

    # ---------- 墓碑 ----------

    def _tombstone_path(self, job_id: str) -> Path:
        return self.config.tombstones_dir / job_id

    def write_tombstone(self, job_id: str) -> None:
        try:
            self._tombstone_path(job_id).write_text(str(int(time.time())), "utf-8")
        except Exception:
            logger.warning("任务 %s 墓碑写入失败", job_id, exc_info=True)

    def has_tombstone(self, job_id: str) -> bool:
        return self._tombstone_path(job_id).exists()

    def clear_tombstone(self, job_id: str) -> None:
        self._tombstone_path(job_id).unlink(missing_ok=True)

    # ---------- 清扫 ----------

    def sweep(self, now: Optional[float] = None) -> int:
        """删掉超过保留期的任务目录，留下墓碑；顺带清理过期墓碑。返回删除的任务数。

        清扫对象：
          - 终态任务：按 finished_at 计龄
          - created 任务（推了文件却从没 start）：按 created_at 计龄，防弃件占盘
        运行中/排队中的任务永不清扫。
        """
        now = time.time() if now is None else now
        deadline = now - self.config.retention_hours * 3600
        removed = 0

        for state in list(self._states.values()):
            if state.status in (STATUS_QUEUED, STATUS_RUNNING):
                continue
            age_anchor = (
                state.finished_at
                if state.is_terminal and state.finished_at is not None
                else state.created_at
            )
            if age_anchor > deadline:
                continue
            logger.info("清扫任务 %s（status=%s）", state.job_id, state.status)
            self.remove(state.job_id)
            self.write_tombstone(state.job_id)
            removed += 1

        # 目录还在但内存里没有的（比如上一轮解析失败的），也按目录 mtime 收
        if self.config.jobs_dir.exists():
            for entry in self.config.jobs_dir.iterdir():
                if not entry.is_dir() or entry.name in self._states:
                    continue
                try:
                    if entry.stat().st_mtime <= deadline:
                        shutil.rmtree(entry, ignore_errors=True)
                        removed += 1
                except OSError:
                    continue

        tombstone_deadline = now - TOMBSTONE_RETENTION_SECONDS
        if self.config.tombstones_dir.exists():
            for entry in self.config.tombstones_dir.iterdir():
                try:
                    if entry.is_file() and entry.stat().st_mtime <= tombstone_deadline:
                        entry.unlink(missing_ok=True)
                except OSError:
                    continue

        return removed
