"""TRANSLATION_WORKER_PROTOCOL.md v1 的契约测试：状态码就是契约，逐条钉死。"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from pdf2zh_worker.app import create_app
from pdf2zh_worker.store import JobState, JobStore

from .conftest import (
    PDF_BYTES,
    TOKEN,
    auth,
    make_config,
    start_params,
    wait_for_status,
)


def upload(client: TestClient, job_id: str, data: bytes = PDF_BYTES):
    return client.put(
        f"/jobs/{job_id}/input",
        content=data,
        headers={**auth(), "Content-Type": "application/pdf"},
    )


# ---------------- healthz ----------------


def test_healthz_without_auth_is_200_and_hides_queue(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_healthz_with_wrong_token_still_200_but_no_queue(client):
    # 主应用靠「有没有 queue 字段」判断 token 对不对，所以这里绝不能回 401
    response = client.get("/healthz", headers=auth("wrong" * 10))
    assert response.status_code == 200
    assert "queue" not in response.json()


def test_healthz_with_auth_exposes_queue_and_engine(client):
    body = client.get("/healthz", headers=auth()).json()
    assert body["ok"] is True
    assert body["queue"] == {"running": 0, "queued": 0, "capacity": 1, "queueLimit": 2}
    assert "engine" in body and "version" in body


# ---------------- 鉴权 ----------------


def test_every_job_endpoint_requires_auth(client):
    assert client.put("/jobs/j1/input", content=PDF_BYTES).status_code == 401
    assert client.post("/jobs/j1/start", json=start_params()).status_code == 401
    assert client.get("/jobs/j1").status_code == 401
    assert client.get("/jobs/j1/output/mono").status_code == 401
    assert client.delete("/jobs/j1").status_code == 401


# ---------------- PUT input ----------------


def test_put_input_rejects_illegal_job_id(client):
    assert client.put(
        "/jobs/bad..id/input", content=PDF_BYTES, headers=auth()
    ).status_code == 400


def test_put_input_rejects_empty_body(client):
    assert upload(client, "j1", b"").status_code == 400


def test_put_input_rejects_non_pdf(client):
    assert upload(client, "j1", b"just some bytes, definitely not a pdf").status_code == 400


def test_put_input_rejects_oversize(tmp_path):
    config = make_config(tmp_path, max_bytes=1024)
    with TestClient(create_app(config)) as client:
        response = upload(client, "j1", PDF_BYTES + b"x" * 4096)
        assert response.status_code == 413


def test_put_input_ok_then_job_is_created(client):
    response = upload(client, "j1")
    assert response.status_code == 200
    assert response.json() == {"received": len(PDF_BYTES)}

    body = client.get("/jobs/j1", headers=auth()).json()
    assert body["status"] == "created"
    assert body["progress"] == 0
    assert body["output"] is None


def test_put_input_overwrites_terminal_job(client, fake_runner):
    upload(client, "j1")
    client.post("/jobs/j1/start", json=start_params(), headers=auth())
    wait_for_status(client, "j1", "succeeded")

    assert upload(client, "j1").status_code == 200
    # 覆盖式重建：旧产物和旧状态一起没
    assert client.get("/jobs/j1", headers=auth()).json()["status"] == "created"
    assert client.get("/jobs/j1/output/mono", headers=auth()).status_code == 409


# ---------------- POST start ----------------


def test_start_without_input_returns_409(client):
    assert client.post("/jobs/nope/start", json=start_params(), headers=auth()).status_code == 409


def test_start_rejects_bad_json(client):
    upload(client, "j1")
    response = client.post(
        "/jobs/j1/start",
        content=b"not json",
        headers={**auth(), "Content-Type": "application/json"},
    )
    assert response.status_code == 400


def test_start_rejects_missing_llm(client):
    upload(client, "j1")
    payload = start_params()
    del payload["llm"]
    assert client.post("/jobs/j1/start", json=payload, headers=auth()).status_code == 400


def test_start_rejects_non_http_base_url(client):
    upload(client, "j1")
    payload = start_params()
    payload["llm"]["baseUrl"] = "file:///etc/passwd"
    assert client.post("/jobs/j1/start", json=payload, headers=auth()).status_code == 400


def test_start_returns_202_queued(client, fake_runner):
    upload(client, "j1")
    response = client.post("/jobs/j1/start", json=start_params(), headers=auth())
    assert response.status_code == 202
    assert response.json() == {"status": "queued", "position": 0}


# ---------------- 完整生命周期 ----------------


def test_success_lifecycle(client, fake_runner):
    upload(client, "j1")
    client.post("/jobs/j1/start", json=start_params(), headers=auth())
    body = wait_for_status(client, "j1", "succeeded")

    assert body["progress"] == 100
    assert body["error"] is None
    assert body["output"]["monoBytes"] > 0
    assert body["output"]["dualBytes"] > 0
    assert body["output"]["totalSeconds"] == 1.5

    for variant in ("mono", "dual"):
        response = client.get(f"/jobs/j1/output/{variant}", headers=auth())
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.content.endswith(variant.encode())

    assert client.delete("/jobs/j1", headers=auth()).status_code == 204
    assert client.delete("/jobs/j1", headers=auth()).status_code == 204  # 幂等
    assert client.get("/jobs/j1", headers=auth()).status_code == 404


def test_failure_is_reported_with_reason(client, fake_runner):
    fake_runner.fail_with = "上游 LLM 502"
    upload(client, "j1")
    client.post("/jobs/j1/start", json=start_params(), headers=auth())
    body = wait_for_status(client, "j1", "failed")
    assert body["error"] == "上游 LLM 502"
    assert body["output"] is None


def test_failed_job_can_be_restarted_without_reupload(client, fake_runner):
    fake_runner.fail_with = "临时故障"
    upload(client, "j1")
    client.post("/jobs/j1/start", json=start_params(), headers=auth())
    wait_for_status(client, "j1", "failed")

    fake_runner.fail_with = None
    assert client.post("/jobs/j1/start", json=start_params(), headers=auth()).status_code == 202
    assert wait_for_status(client, "j1", "succeeded")["error"] is None


# ---------------- 运行中的语义 ----------------


def test_running_job_blocks_input_overwrite_and_start_is_idempotent(client, fake_runner):
    fake_runner.release.clear()
    upload(client, "j1")
    client.post("/jobs/j1/start", json=start_params(), headers=auth())
    assert fake_runner.started.wait(5)
    wait_for_status(client, "j1", "running")

    assert upload(client, "j1").status_code == 409

    repeat = client.post("/jobs/j1/start", json=start_params(), headers=auth())
    assert repeat.status_code == 202
    assert repeat.json()["status"] == "running"
    assert fake_runner.calls == 1  # 没有被重复派发

    fake_runner.release.set()
    wait_for_status(client, "j1", "succeeded")


def test_delete_cancels_running_job_and_consumer_survives(client, fake_runner):
    """回归：DELETE 只该杀掉这一个任务，不能把消费者一起收掉（否则并发永久少一格）。"""
    fake_runner.release.clear()
    upload(client, "j1")
    client.post("/jobs/j1/start", json=start_params(), headers=auth())
    assert fake_runner.started.wait(5)

    assert client.delete("/jobs/j1", headers=auth()).status_code == 204
    assert client.get("/jobs/j1", headers=auth()).status_code == 404

    # 消费者还活着：下一个任务照样能跑完
    fake_runner.release.set()
    fake_runner.started.clear()
    upload(client, "j2")
    client.post("/jobs/j2/start", json=start_params(), headers=auth())
    assert wait_for_status(client, "j2", "succeeded", timeout=8)["progress"] == 100


def test_setup_failure_does_not_strand_the_job_in_running(client, fake_runner, monkeypatch):
    """准备阶段炸了也必须落到终态：卡在 running 的话主应用要空等满 180 分钟超时。"""
    from pdf2zh_worker import manager as manager_module

    def boom(*_args, **_kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(manager_module.JobManager, "_glossary_for", boom)

    upload(client, "j1")
    client.post("/jobs/j1/start", json=start_params(), headers=auth())
    body = wait_for_status(client, "j1", "failed")
    assert "术语表写入失败" in body["error"]
    assert fake_runner.calls == 0  # 根本没派到 pdf2zh 上


def test_queue_limit_returns_429(client, fake_runner):
    fake_runner.release.clear()
    upload(client, "running")
    client.post("/jobs/running/start", json=start_params(), headers=auth())
    assert fake_runner.started.wait(5)

    for job_id in ("q1", "q2"):
        upload(client, job_id)
        assert client.post(f"/jobs/{job_id}/start", json=start_params(), headers=auth()).status_code == 202

    upload(client, "q3")
    assert client.post("/jobs/q3/start", json=start_params(), headers=auth()).status_code == 429

    health = client.get("/healthz", headers=auth()).json()
    assert health["queue"] == {"running": 1, "queued": 2, "capacity": 1, "queueLimit": 2}

    fake_runner.release.set()
    wait_for_status(client, "q2", "succeeded", timeout=10)


# ---------------- 产物端点 ----------------


def test_output_before_finish_returns_409(client, fake_runner):
    upload(client, "j1")
    assert client.get("/jobs/j1/output/mono", headers=auth()).status_code == 409


def test_output_for_unknown_job_returns_404(client):
    assert client.get("/jobs/ghost/output/mono", headers=auth()).status_code == 404


def test_output_missing_variant_returns_404(client, fake_runner):
    fake_runner.emit_dual = False
    upload(client, "j1")
    client.post("/jobs/j1/start", json=start_params(), headers=auth())
    wait_for_status(client, "j1", "succeeded")

    assert client.get("/jobs/j1/output/mono", headers=auth()).status_code == 200
    assert client.get("/jobs/j1/output/dual", headers=auth()).status_code == 404


def test_output_after_sweep_returns_410(client, app, fake_runner):
    upload(client, "j1")
    client.post("/jobs/j1/start", json=start_params(), headers=auth())
    wait_for_status(client, "j1", "succeeded")

    store: JobStore = app.state.store
    state = store.get("j1")
    state.finished_at = time.time() - 48 * 3600  # 倒推到保留期之外
    store.save(state)
    assert store.sweep() == 1

    # 已清扫和从没存在过必须分得开：410 让主应用别再重试下载
    assert client.get("/jobs/j1/output/mono", headers=auth()).status_code == 410
    assert client.get("/jobs/j1", headers=auth()).status_code == 404


def test_reupload_clears_tombstone(client, app, fake_runner):
    store: JobStore = app.state.store
    store.write_tombstone("j1")
    upload(client, "j1")
    assert not store.has_tombstone("j1")


# ---------------- 重启 ----------------


def test_restart_marks_unfinished_jobs_failed(tmp_path):
    config = make_config(tmp_path)
    store = JobStore(config)
    for job_id, status in (("a", "queued"), ("b", "running"), ("c", "succeeded")):
        store.save(JobState(job_id=job_id, status=status, finished_at=time.time()))

    # 新进程起来：queued/running 一律标 failed，等主应用按同 jobId 幂等重派
    with TestClient(create_app(config)) as client:
        for job_id in ("a", "b"):
            body = client.get(f"/jobs/{job_id}", headers=auth()).json()
            assert body["status"] == "failed"
            assert body["error"] == "worker 重启导致任务中断"
        assert client.get("/jobs/c", headers=auth()).json()["status"] == "succeeded"


# ---------------- 术语表 ----------------


def test_glossary_is_written_as_babeldoc_csv(client, app, fake_runner):
    fake_runner.release.clear()
    upload(client, "j1")
    client.post(
        "/jobs/j1/start",
        json=start_params(glossary=[{"src": "transformer", "dst": "变换器"}]),
        headers=auth(),
    )
    assert fake_runner.started.wait(5)

    csv_path = app.state.store.glossary_path("j1")
    content = csv_path.read_text("utf-8")
    # babeldoc.glossary.Glossary.from_csv 要求表头含 source/target
    assert content.splitlines()[0] == "source,target,tgt_lng"
    assert "transformer,变换器," in content

    fake_runner.release.set()
    wait_for_status(client, "j1", "succeeded")


def test_api_key_never_hits_disk(client, app, fake_runner):
    upload(client, "j1")
    client.post("/jobs/j1/start", json=start_params(), headers=auth())
    wait_for_status(client, "j1", "succeeded")

    state_json = (app.state.store.job_dir("j1") / "state.json").read_text("utf-8")
    assert "k" * 40 not in state_json
    assert '"apiKey": "***"' in state_json
