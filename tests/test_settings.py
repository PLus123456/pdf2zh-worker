"""build_settings 对 pdf2zh 真模型的校验。

只有装了 pdf2zh-next 才跑（也就是真 worker 机器上）。这层很值：协议参数到
SettingsModel 的映射是照着 pdf2zh 源码手写的，字段一旦改名，这里立刻炸，
而不是等第一单跑到一半才发现。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pdf2zh_next", reason="没装 pdf2zh-next，跳过真模型校验")

from pdf2zh_worker.models import TranslateJobParams  # noqa: E402
from pdf2zh_worker.runner import build_settings, write_glossary_csv  # noqa: E402

from .conftest import make_config  # noqa: E402


def params(**overrides) -> TranslateJobParams:
    payload = {
        "langIn": "en",
        "langOut": "zh",
        "qps": 4,
        "watermark": False,
        "glossary": None,
        "llm": {
            "baseUrl": "https://app.example/api/translate/llm-proxy/v1",
            "apiKey": "sk-task-oneshot",
            "model": "lecture-live-gateway",
        },
    }
    payload.update(overrides)
    return TranslateJobParams.model_validate(payload)


def build(tmp_path, job_params, glossary=None):
    settings = build_settings(
        make_config(tmp_path),
        job_params,
        output_dir=tmp_path / "out",
        glossary_file=glossary,
    )
    settings.validate_settings()  # pdf2zh 自己的校验，过了才算真能跑
    return settings


def test_settings_pass_pdf2zh_validation(tmp_path):
    settings = build(tmp_path, params())

    assert settings.translation.lang_in == "en"
    assert settings.translation.lang_out == "zh"
    assert settings.translation.qps == 4
    # term_qps 对齐 qps，pdf2zh 才会复用同一个翻译器实例，少一套限流器
    assert settings.translation.term_qps == 4
    assert settings.translation.output == str(tmp_path / "out")
    # 协议：单语 + 双语产物都要
    assert settings.pdf.no_dual is False
    assert settings.pdf.no_mono is False
    # 走 file 参数，不走 input_files（后者是 CLI 用的，传了 pdf2zh 会警告并忽略）
    assert settings.basic.input_files == set()


def test_llm_backend_points_at_the_main_app_proxy(tmp_path):
    settings = build(tmp_path, params())
    engine = settings.translate_engine_settings

    # validate_settings 里 OpenAICompatible 会 transform 成 OpenAI
    assert engine.translate_engine_type == "OpenAI"
    # base_url 必须原样保留：pdf2zh 的 _clean_url 只砍尾斜杠和 /chat/completions，
    # 砍掉 /v1 的话请求就打到主应用不存在的路径上了
    assert engine.openai_base_url == "https://app.example/api/translate/llm-proxy/v1"
    assert engine.openai_api_key == "sk-task-oneshot"
    assert engine.openai_model == "lecture-live-gateway"
    # 温度/推理档位由主应用代理端强制，这边发了也是白发
    assert engine.openai_send_temprature is False
    assert engine.openai_send_reasoning_effort is False


@pytest.mark.parametrize(
    "watermark,expected",
    [(False, "no_watermark"), (True, "watermarked")],
)
def test_watermark_flag_maps_to_pdf2zh_mode(tmp_path, watermark, expected):
    settings = build(tmp_path, params(watermark=watermark))
    assert settings.pdf.watermark_output_mode == expected


def test_glossary_csv_is_readable_by_babeldoc(tmp_path):
    from babeldoc.glossary import Glossary

    csv_path = tmp_path / "glossary.csv"
    write_glossary_csv(params(glossary=[{"src": "transformer", "dst": "变换器"}]).glossary, csv_path)

    settings = build(tmp_path, params(glossary=[{"src": "transformer", "dst": "变换器"}]), csv_path)
    assert settings.translation.glossaries == str(csv_path)

    # 真拿 babeldoc 解一遍——表头写错的话它会抛，而不是静默丢掉整张表
    glossary = Glossary.from_csv(csv_path, target_lang_out="zh")
    assert [(e.source, e.target) for e in glossary.entries] == [("transformer", "变换器")]


def test_auto_glossary_switch_is_wired(tmp_path):
    # 默认关：主应用按页收费，多跑一轮 LLM 抽词的钱落在运营方头上
    assert build(tmp_path, params()).translation.no_auto_extract_glossary is True

    settings = build_settings(
        make_config(tmp_path, auto_extract_glossary=True),
        params(),
        output_dir=tmp_path / "out",
        glossary_file=None,
    )
    assert settings.translation.no_auto_extract_glossary is False
