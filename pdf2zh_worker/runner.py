"""驱动 pdf2zh-next 跑一篇 PDF。

只碰 pdf2zh 的公开 API（`pdf2zh_next.high_level.do_translate_async_stream` +
`SettingsModel`），不改它一行源码——AGPL 边界就靠这条：wrapper 是独立进程里的
调用方，pdf2zh 原样安装、原样运行。

pdf2zh 的导入放在函数里做（lazy import）：它的依赖树很重（onnxruntime / pymupdf /
scikit-image…），而契约测试只关心 HTTP 语义，不该被迫装这一整坨。
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .config import WorkerConfig
from .models import TranslateJobParams

logger = logging.getLogger(__name__)

#: 进度回调：(stage, progress 0-100) -> None
ProgressCallback = Callable[[Optional[str], int], None]


class TranslationFailed(RuntimeError):
    """翻译失败（pdf2zh 报错 / 产物缺失 / 卡死）。message 会原样回给主应用。"""


@dataclass
class TranslateOutcome:
    mono_path: Optional[Path]
    dual_path: Optional[Path]
    total_seconds: float


def write_glossary_csv(entries, path: Path) -> None:
    """术语表 → babeldoc 吃的 CSV。

    格式来自 `babeldoc.glossary.Glossary.from_csv`：表头必须有 source / target，
    可选 tgt_lng。我们不写 tgt_lng —— 留空表示对任何目标语种都生效，省得
    "zh" / "zh-CN" 这种写法差异让整张表被静默过滤掉。
    """
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["source", "target", "tgt_lng"], doublequote=True)
        writer.writeheader()
        for entry in entries:
            src = (entry.src or "").strip()
            dst = (entry.dst or "").strip()
            if not src or not dst:
                continue
            writer.writerow({"source": src, "target": dst, "tgt_lng": ""})


def build_settings(
    config: WorkerConfig,
    params: TranslateJobParams,
    *,
    output_dir: Path,
    glossary_file: Optional[Path],
):
    """把协议参数翻译成 pdf2zh 的 SettingsModel。"""
    from pdf2zh_next.config.model import BasicSettings, PDFSettings, SettingsModel, TranslationSettings
    from pdf2zh_next.config.translate_engine_model import OpenAISettings

    # 用 OpenAISettings 而不是 OpenAICompatibleSettings：后者的 validate_settings()
    # 里直接 `transform()` 成前者，字段一一对应，行为完全一致；而 OpenAISettings 在
    # `pdf2zh_next.__all__` 里，属于公开 API，升级时更不容易被挪走。
    engine = OpenAISettings(
        openai_model=params.llm.model,
        openai_base_url=params.llm.baseUrl,
        openai_api_key=params.llm.apiKey,
        openai_timeout=str(config.llm_timeout_seconds),
        # 温度/推理档位一律不发：主应用代理端会忽略请求里的这些字段，
        # 由 TRANSLATION 路由行统一决定，这里发了也是白发。
        openai_send_temprature=False,
        openai_send_reasoning_effort=False,
    )

    settings = SettingsModel(
        report_interval=config.report_interval,
        basic=BasicSettings(input_files=set(), debug=False, gui=False),
        translation=TranslationSettings(
            lang_in=params.langIn,
            lang_out=params.langOut,
            output=str(output_dir),
            qps=params.qps,
            # term_qps 对齐 qps，让 pdf2zh 复用同一个翻译器实例（见
            # high_level.create_babeldoc_config 里的 == 分支），少一套限流器
            term_qps=params.qps,
            pool_max_workers=config.pool_max_workers,
            ignore_cache=config.ignore_cache,
            glossaries=str(glossary_file) if glossary_file else None,
            no_auto_extract_glossary=not config.auto_extract_glossary,
        ),
        pdf=PDFSettings(
            # 协议：双语 + 单语产物都要
            no_dual=False,
            no_mono=False,
            watermark_output_mode="watermarked" if params.watermark else "no_watermark",
        ),
        translate_engine_settings=engine,
    )
    return settings


def _pick(primary: Optional[Path], fallback: Optional[Path]) -> Optional[Path]:
    for candidate in (primary, fallback):
        if candidate and Path(candidate).exists():
            return Path(candidate)
    return None


async def run_translation(
    config: WorkerConfig,
    params: TranslateJobParams,
    *,
    source: Path,
    output_dir: Path,
    glossary_file: Optional[Path],
    mono_target: Path,
    dual_target: Path,
    on_progress: ProgressCallback,
) -> TranslateOutcome:
    """跑完一篇，把产物归一化到 mono_target / dual_target。

    抛 TranslationFailed 表示业务失败；asyncio.CancelledError 会原样冒泡
    （DELETE 取消任务时靠它把 pdf2zh 子进程收掉）。
    """
    from pdf2zh_next.high_level import do_translate_async_stream

    output_dir.mkdir(parents=True, exist_ok=True)
    settings = build_settings(
        config, params, output_dir=output_dir, glossary_file=glossary_file
    )

    result = None
    stream = do_translate_async_stream(settings, source)
    try:
        # aclosing：任务被取消时确保异步生成器的 finally 跑到，
        # 由它去 terminate/kill pdf2zh 的翻译子进程，不留孤儿。
        async with contextlib.aclosing(stream):
            async for event in stream:
                kind = event.get("type")
                if kind in ("progress_start", "progress_update", "progress_end"):
                    overall = event.get("overall_progress") or 0
                    progress = max(0, min(100, int(overall)))
                    on_progress(event.get("stage"), progress)
                elif kind == "finish":
                    result = event.get("translate_result")
                    break
                elif kind == "error":
                    raise TranslationFailed(
                        _trim(event.get("error") or "pdf2zh 未给出错误详情")
                    )
    except asyncio.CancelledError:
        raise
    except TranslationFailed:
        raise
    except asyncio.TimeoutError as exc:
        # pdf2zh 内部对单个进度事件等 30 分钟；等爆了说明翻译进程卡死了
        raise TranslationFailed("翻译进程 30 分钟没有进度上报，判定卡死") from exc
    except Exception as exc:  # pdf2zh / babeldoc 抛出的一切
        raise TranslationFailed(_trim(f"{type(exc).__name__}: {exc}")) from exc

    if result is None:
        raise TranslationFailed("pdf2zh 结束但没有产出结果对象")

    want_watermarked = params.watermark
    mono_src = _pick(
        getattr(result, "mono_pdf_path", None) if want_watermarked else None,
        getattr(result, "no_watermark_mono_pdf_path", None)
        or getattr(result, "mono_pdf_path", None),
    )
    dual_src = _pick(
        getattr(result, "dual_pdf_path", None) if want_watermarked else None,
        getattr(result, "no_watermark_dual_pdf_path", None)
        or getattr(result, "dual_pdf_path", None),
    )

    if mono_src is None and dual_src is None:
        raise TranslationFailed("pdf2zh 结束但没有生成任何 PDF 产物")

    mono_final = _promote(mono_src, mono_target)
    dual_final = _promote(dual_src, dual_target)

    # 中间产物（分片 PDF、调试图…）留着白占盘；元信息都在 state.json 里
    shutil.rmtree(output_dir, ignore_errors=True)

    return TranslateOutcome(
        mono_path=mono_final,
        dual_path=dual_final,
        total_seconds=float(getattr(result, "total_seconds", 0.0) or 0.0),
    )


def _promote(src: Optional[Path], target: Path) -> Optional[Path]:
    """把 pdf2zh 输出目录里的产物搬到固定文件名，下载端点只认这个名字。"""
    if src is None:
        return None
    try:
        target.unlink(missing_ok=True)
        shutil.move(str(src), str(target))
        return target
    except Exception:
        logger.exception("产物归一化失败：%s -> %s", src, target)
        return None


def _trim(message: str, limit: int = 500) -> str:
    message = " ".join(str(message).split())
    return message if len(message) <= limit else message[: limit - 1] + "…"
