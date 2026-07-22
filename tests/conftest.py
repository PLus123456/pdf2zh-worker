"""契约测试脚手架。

刻意不装 pdf2zh-next：这些用例验的是 HTTP 协议语义（状态码/幂等/队列/清扫），
真翻译由一个假 runner 顶替。真跑 pdf2zh 的冒烟验证见 README 的「上线前自检」。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pdf2zh_worker import manager as manager_module
from pdf2zh_worker.app import create_app
from pdf2zh_worker.config import WorkerConfig
from pdf2zh_worker.runner import TranslateOutcome, TranslationFailed

TOKEN = "t" * 40
PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n"


def auth(token: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


class FakeRunner:
    """可编排的假翻译器：能挂起、能失败、能记录被调了几次。"""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.release.set()  # 默认不挂起
        self.started = threading.Event()
        self.calls = 0
        self.fail_with: str | None = None
        self.emit_dual = True
        self.seen_params = []

    async def __call__(
        self,
        config,
        params,
        *,
        source: Path,
        output_dir: Path,
        glossary_file,
        mono_target: Path,
        dual_target: Path,
        on_progress,
    ) -> TranslateOutcome:
        import asyncio

        self.calls += 1
        self.seen_params.append(params)
        self.started.set()
        on_progress("parsing", 10)

        # 跨线程等测试放行（TestClient 的事件循环在另一个线程里）
        while not self.release.is_set():
            await asyncio.sleep(0.01)

        if self.fail_with:
            raise TranslationFailed(self.fail_with)

        on_progress("translating", 80)
        mono_target.write_bytes(PDF_BYTES + b"mono")
        if self.emit_dual:
            dual_target.write_bytes(PDF_BYTES + b"dual")
        return TranslateOutcome(
            mono_path=mono_target,
            dual_path=dual_target if self.emit_dual else None,
            total_seconds=1.5,
        )


def make_config(tmp_path: Path, **overrides) -> WorkerConfig:
    base = dict(
        token=TOKEN,
        host="127.0.0.1",
        port=8791,
        data_dir=tmp_path / "data",
        concurrency=1,
        queue_limit=2,
        max_bytes=1024 * 1024,
        retention_hours=24,
        sweep_minutes=30,
        log_level="WARNING",
        llm_timeout_seconds=300,
        auto_extract_glossary=False,
        report_interval=0.5,
        pool_max_workers=None,
        ignore_cache=False,
    )
    base.update(overrides)
    return WorkerConfig(**base)


@pytest.fixture
def fake_runner(monkeypatch) -> FakeRunner:
    runner = FakeRunner()
    monkeypatch.setattr(manager_module, "run_translation", runner)
    return runner


@pytest.fixture
def config(tmp_path) -> WorkerConfig:
    return make_config(tmp_path)


@pytest.fixture
def app(config):
    return create_app(config)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def start_params(**overrides) -> dict:
    payload = {
        "langIn": "en",
        "langOut": "zh",
        "qps": 4,
        "watermark": False,
        "glossary": None,
        "llm": {
            "baseUrl": "https://example.test/api/translate/llm-proxy/v1",
            "apiKey": "k" * 40,
            "model": "lecture-live-gateway",
        },
    }
    payload.update(overrides)
    return payload


def wait_for_status(client: TestClient, job_id: str, wanted, timeout: float = 5.0) -> dict:
    """轮询到目标状态；超时直接把最后一次状态打出来，省得看一个光秃秃的 assert。"""
    wanted = {wanted} if isinstance(wanted, str) else set(wanted)
    deadline = time.time() + timeout
    body: dict = {}
    while time.time() < deadline:
        response = client.get(f"/jobs/{job_id}", headers=auth())
        if response.status_code == 200:
            body = response.json()
            if body["status"] in wanted:
                return body
        time.sleep(0.02)
    raise AssertionError(f"等 {wanted} 超时，最后状态：{body}")
