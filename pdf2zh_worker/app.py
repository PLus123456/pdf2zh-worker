"""HTTP 端点。严格照 TRANSLATION_WORKER_PROTOCOL.md v1 实现，状态码就是契约。

    GET    /healthz                无鉴权可访问；带对的 token 才多回 queue/engine
    PUT    /jobs/:id/input         推源 PDF
    POST   /jobs/:id/start         入队翻译
    GET    /jobs/:id               状态/进度
    GET    /jobs/:id/output/mono   单语产物
    GET    /jobs/:id/output/dual   双语产物
    DELETE /jobs/:id               幂等清理
"""

from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .config import WorkerConfig, load_config
from .manager import JobManager, QueueFull
from .models import TranslateJobParams
from .store import STATUS_SUCCEEDED, JobStore, is_valid_job_id

logger = logging.getLogger(__name__)

#: 上传时先攒这么多字节用来验 PDF 魔数
_MAGIC_WINDOW = 1024


def _engine_version() -> Optional[str]:
    """装了 pdf2zh-next 就报它的版本；没装（比如跑契约测试）就 None，不炸。"""
    try:
        from importlib.metadata import version

        return version("pdf2zh-next")
    except Exception:
        return None


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def create_app(config: Optional[WorkerConfig] = None) -> FastAPI:
    config = config or load_config()
    store = JobStore(config)
    manager = JobManager(config, store)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await manager.start()
        try:
            yield
        finally:
            await manager.shutdown()

    app = FastAPI(
        title="pdf2zh-worker",
        version=__version__,
        lifespan=lifespan,
        # 这台机器只服务主应用，不需要给人看的文档页，少一个攻击面
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.config = config
    app.state.store = store
    app.state.manager = manager

    def _authorized(request: Request) -> bool:
        header = request.headers.get("authorization") or ""
        if not header.startswith("Bearer "):
            return False
        # 常量时间比较：别让 token 被逐字节试出来
        return hmac.compare_digest(header[7:].strip(), config.token)

    async def require_auth(request: Request) -> None:
        if not _authorized(request):
            # 抛而不是 return：让 FastAPI 直接短路，端点函数根本不会被调到
            raise HTTPException(status_code=401, detail="鉴权失败")

    @app.exception_handler(StarletteHTTPException)
    async def _on_http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # 统一错误体形状：{"error": "..."}，路由自带的 404/405 也走这里
        return _error(exc.status_code, str(exc.detail))

    # ---------------- healthz ----------------

    @app.get("/healthz")
    async def healthz(request: Request):
        """恒 200。主应用靠「有没有 queue 字段」判断 token 对不对，所以
        token 错了也不能回 401，否则 admin「测试连接」只会看到一个含糊的报错。"""
        if not _authorized(request):
            return {"ok": True}
        return {
            "ok": True,
            "version": __version__,
            "queue": manager.queue_snapshot(),
            "engine": {"pdf2zh": _engine_version()},
        }

    # ---------------- 推源文件 ----------------

    @app.put("/jobs/{job_id}/input", dependencies=[Depends(require_auth)])
    async def put_input(job_id: str, request: Request):
        if not is_valid_job_id(job_id):
            return _error(400, "jobId 不合法（只允许 [A-Za-z0-9_-]，最长 128）")
        if manager.is_active(job_id):
            return _error(409, "任务正在排队或运行中，不能覆盖输入")

        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > config.max_bytes:
            return _error(413, f"文件超过上限 {config.max_mb}MB")

        # 覆盖式重建：旧任务（含终态）直接删目录重来
        state = store.reset_for_input(job_id)
        target = store.source_path(job_id)
        tmp = target.with_suffix(".pdf.part")
        total = 0
        head = b""
        try:
            with tmp.open("wb") as fh:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > config.max_bytes:
                        fh.close()
                        tmp.unlink(missing_ok=True)
                        return _error(413, f"文件超过上限 {config.max_mb}MB")
                    if len(head) < _MAGIC_WINDOW:
                        head += chunk[: _MAGIC_WINDOW - len(head)]
                    fh.write(chunk)
        except Exception:
            tmp.unlink(missing_ok=True)
            logger.exception("任务 %s 写入源文件失败", job_id)
            return _error(500, "写入源文件失败")

        if total == 0:
            tmp.unlink(missing_ok=True)
            return _error(400, "请求体为空")
        if b"%PDF" not in head:
            # 前 1KB 里连魔数都没有，基本可以断定不是 PDF；让它现在就失败，
            # 比排到队里跑十分钟再报一个 babeldoc 的天书堆栈强
            tmp.unlink(missing_ok=True)
            return _error(400, "不是 PDF 文件（前 1KB 内没有 %PDF 魔数）")

        os.replace(tmp, target)
        state.input_bytes = total
        store.save(state)
        logger.info("任务 %s 收到源文件 %d 字节", job_id, total)
        return {"received": total}

    # ---------------- 开始翻译 ----------------

    @app.post("/jobs/{job_id}/start", dependencies=[Depends(require_auth)])
    async def start_job(job_id: str, request: Request):
        if not is_valid_job_id(job_id):
            return _error(400, "jobId 不合法")

        state = store.get(job_id)
        if state is None or not store.source_path(job_id).exists():
            return _error(409, "还没有收到该任务的源文件")

        try:
            payload = await request.json()
        except Exception:
            return _error(400, "请求体不是合法 JSON")
        try:
            params = TranslateJobParams.model_validate(payload)
        except ValidationError as exc:
            return _error(400, f"参数非法：{_first_error(exc)}")

        try:
            status, position = manager.enqueue(state, params)
        except QueueFull as exc:
            return _error(429, str(exc))

        return JSONResponse({"status": status, "position": position}, status_code=202)

    # ---------------- 查询 ----------------

    @app.get("/jobs/{job_id}", dependencies=[Depends(require_auth)])
    async def get_job(job_id: str):
        if not is_valid_job_id(job_id):
            return _error(400, "jobId 不合法")
        state = store.get(job_id)
        if state is None:
            # 协议：404 = 任务丢失，主应用会重派
            return _error(404, "任务不存在")
        return state.to_api()

    # ---------------- 取产物 ----------------

    @app.get("/jobs/{job_id}/output/{variant}", dependencies=[Depends(require_auth)])
    async def get_output(job_id: str, variant: str):
        if not is_valid_job_id(job_id):
            return _error(400, "jobId 不合法")
        if variant not in ("mono", "dual"):
            return _error(404, "产物类型只能是 mono 或 dual")

        state = store.get(job_id)
        if state is None:
            # 有墓碑 = 曾经存在、已被清扫 → 410（主应用别再重试下载）
            if store.has_tombstone(job_id):
                return _error(410, "产物已过期清扫")
            return _error(404, "任务不存在")
        if state.status != STATUS_SUCCEEDED:
            return _error(409, f"任务尚未完成（当前 {state.status}）")

        path = store.artifact_path(job_id, variant)
        if not path.exists():
            return _error(404, f"没有生成 {variant} 产物")
        return FileResponse(
            path,
            media_type="application/pdf",
            filename=f"{job_id}-{variant}.pdf",
        )

    # ---------------- 清理 ----------------

    @app.delete("/jobs/{job_id}", dependencies=[Depends(require_auth)])
    async def delete_job(job_id: str):
        # 协议：恒 204。id 不合法也当作「没有这个任务」，直接回 204。
        if is_valid_job_id(job_id):
            await manager.cancel(job_id)  # 先杀进程
            store.remove(job_id)  # 再删目录
            logger.info("任务 %s 已清理", job_id)
        return Response(status_code=204)

    return app


def _first_error(exc: ValidationError) -> str:
    try:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first.get("loc", ()))
        return f"{location or '(root)'} {first.get('msg', '')}".strip()
    except Exception:
        return "字段校验失败"
