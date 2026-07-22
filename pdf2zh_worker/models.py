"""协议里 POST /jobs/:id/start 的请求体模型（对齐主应用 `TranslateJobParams`）。

字段名是 camelCase，与主应用 `src/lib/translate/workerClient.ts` 一一对应；
校验失败一律回 400（协议：参数非法 → 400）。
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class GlossaryEntry(BaseModel):
    """一条术语：src 原文 → dst 译文。落盘成 pdf2zh 吃的 CSV。"""

    model_config = ConfigDict(extra="ignore")

    src: str = Field(min_length=1, max_length=200)
    dst: str = Field(min_length=1, max_length=500)


class LlmParams(BaseModel):
    """LLM 后端：指向主应用的 OpenAI 兼容代理，厂商 key 永不出主应用。"""

    model_config = ConfigDict(extra="ignore")

    baseUrl: str = Field(min_length=1, max_length=2000)
    #: 任务级一次性凭据——只在内存里活着，绝不落盘（见 store.JobState.to_dict）
    apiKey: str = Field(min_length=1, max_length=4000)
    model: str = Field(min_length=1, max_length=200)

    @field_validator("baseUrl")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith(("http://", "https://")):
            raise ValueError("llm.baseUrl 必须是 http(s) 绝对地址")
        return value


class TranslateJobParams(BaseModel):
    """start 的完整参数。协议 v1。"""

    model_config = ConfigDict(extra="ignore")

    langIn: str = Field(default="en", min_length=1, max_length=32)
    langOut: str = Field(default="zh", min_length=1, max_length=32)
    qps: int = Field(default=4, ge=1, le=1000)
    watermark: bool = False
    #: null / 空数组 = 不用术语表
    glossary: Optional[List[GlossaryEntry]] = None
    llm: LlmParams

    @field_validator("langIn", "langOut")
    @classmethod
    def _clean_lang(cls, value: str) -> str:
        return value.strip()

    @field_validator("glossary")
    @classmethod
    def _cap_glossary(cls, value: Optional[List[GlossaryEntry]]) -> Optional[List[GlossaryEntry]]:
        # 术语表要编译成 hyperscan 正则库，条目失控会把内存吃穿
        if value is not None and len(value) > 5000:
            raise ValueError("glossary 条目不能超过 5000 条")
        return value

    def redacted(self) -> dict:
        """落盘/打日志用的脱敏快照：抹掉一次性凭据。"""
        data = self.model_dump()
        if isinstance(data.get("llm"), dict):
            data["llm"] = {**data["llm"], "apiKey": "***"}
        return data
