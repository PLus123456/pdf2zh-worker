"""环境变量 → 运行配置。启动时严格校验，配置不合法直接拒启（不带病上线）。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

#: 鉴权 token 最短长度（协议要求 ≥32）
MIN_TOKEN_LENGTH = 32


class ConfigError(RuntimeError):
    """配置非法；main 捕获后打印人话并以非 0 退出。"""


def _env_int(
    env: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: Optional[int] = None,
) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是整数，当前值：{raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} 不能小于 {minimum}，当前值：{value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} 不能大于 {maximum}，当前值：{value}")
    return value


def _env_float(env: Mapping[str, str], name: str, default: float, *, minimum: float) -> float:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是数字，当前值：{raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} 不能小于 {minimum}，当前值：{value}")
    return value


def _env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class WorkerConfig:
    token: str
    host: str
    port: int
    data_dir: Path
    concurrency: int
    queue_limit: int
    max_bytes: int
    retention_hours: int
    sweep_minutes: int
    log_level: str

    # —— pdf2zh 行为开关（协议之外的运维旋钮，默认值即推荐值）——
    #: 传给 OpenAI 兼容后端的单请求超时（秒）。主应用代理是非流式的，
    #: 长文一次调用也就几秒，300s 足够且能兜住网关抖动。
    llm_timeout_seconds: int
    #: pdf2zh 的「自动抽取术语表」：额外跑一轮 LLM 抽词，质量略好但 token 成本上去了。
    #: 默认关掉——主应用按页报价，这部分成本落在运营方头上。
    auto_extract_glossary: bool
    #: 进度事件上报间隔（秒）。pdf2zh 默认 0.1s，长跑服务下调到 0.5s 省 IPC。
    report_interval: float
    #: 翻译线程池大小；None = 跟随 qps（pdf2zh 默认行为）
    pool_max_workers: Optional[int]
    #: 跳过 pdf2zh 的本地翻译缓存。
    #: 缓存键是 (engine, engine_params, 原文)，而 engine_params 里的模型名对我们
    #: **永远是占位符**（真实路由由主应用代理服务端强制）——所以主应用换了底层模型之后，
    #: 这台机器还会拿旧模型的译文来顶。要么在换模型时开这个开关，要么清一次缓存
    #: （`pdf2zh-worker` 菜单里有「清理翻译缓存」）。默认关＝用缓存，省钱。
    ignore_cache: bool

    @property
    def max_mb(self) -> int:
        return self.max_bytes // (1024 * 1024)

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def tombstones_dir(self) -> Path:
        """已清扫任务的墓碑：把「产物已过期」(410) 和「任务从没存在过」(404) 分开。"""
        return self.data_dir / "tombstones"


def load_config(env: Optional[Mapping[str, str]] = None) -> WorkerConfig:
    """从环境变量装配配置。任何一项不合法都抛 ConfigError。"""
    env = os.environ if env is None else env

    token = (env.get("TRANSLATE_WORKER_TOKEN") or "").strip()
    if not token:
        raise ConfigError("缺少 TRANSLATE_WORKER_TOKEN。生成一个：openssl rand -hex 32")
    if len(token) < MIN_TOKEN_LENGTH:
        raise ConfigError(
            f"TRANSLATE_WORKER_TOKEN 至少 {MIN_TOKEN_LENGTH} 个字符（当前 {len(token)}）。"
            " 生成一个：openssl rand -hex 32"
        )

    data_dir = Path(env.get("TRANSLATE_WORKER_DATA") or "./data").expanduser().resolve()
    # pdf2zh 的 glossaries 配置是「逗号分隔的路径列表」，路径里带逗号会被切断，
    # 与其等术语表静默丢失，不如启动就拦下。
    if "," in str(data_dir):
        raise ConfigError(f"TRANSLATE_WORKER_DATA 路径不能包含逗号：{data_dir}")

    max_mb = _env_int(env, "TRANSLATE_WORKER_MAX_MB", 50, minimum=1, maximum=2048)

    return WorkerConfig(
        token=token,
        host=(env.get("TRANSLATE_WORKER_HOST") or "127.0.0.1").strip(),
        port=_env_int(env, "TRANSLATE_WORKER_PORT", 8791, minimum=1, maximum=65535),
        data_dir=data_dir,
        concurrency=_env_int(env, "TRANSLATE_WORKER_CONCURRENCY", 1, minimum=1, maximum=64),
        queue_limit=_env_int(env, "TRANSLATE_WORKER_QUEUE_LIMIT", 8, minimum=1, maximum=1024),
        max_bytes=max_mb * 1024 * 1024,
        retention_hours=_env_int(
            env, "TRANSLATE_WORKER_RETENTION_HOURS", 24, minimum=1, maximum=24 * 30
        ),
        sweep_minutes=_env_int(env, "TRANSLATE_WORKER_SWEEP_MINUTES", 30, minimum=1, maximum=1440),
        log_level=(env.get("TRANSLATE_WORKER_LOG_LEVEL") or "INFO").strip().upper(),
        llm_timeout_seconds=_env_int(
            env, "TRANSLATE_WORKER_LLM_TIMEOUT", 300, minimum=10, maximum=3600
        ),
        auto_extract_glossary=_env_bool(env, "TRANSLATE_WORKER_AUTO_GLOSSARY", False),
        report_interval=_env_float(env, "TRANSLATE_WORKER_REPORT_INTERVAL", 0.5, minimum=0.05),
        pool_max_workers=(
            _env_int(env, "TRANSLATE_WORKER_POOL_MAX_WORKERS", 0, minimum=0, maximum=256) or None
        ),
        ignore_cache=_env_bool(env, "TRANSLATE_WORKER_IGNORE_CACHE", False),
    )
