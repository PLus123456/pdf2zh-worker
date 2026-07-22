"""pdf2zh-worker —— 文档翻译 worker（协议 v1）。

主应用全程主动（推文件 → start → 轮询 → 取回 → DELETE），本 worker 完全被动、
可随时重启；重启后所有未完成任务标 failed，由主应用状态机按同一 jobId 幂等重派。
"""

__version__ = "1.0.0"
# 实现的协议契约版本（TRANSLATION_WORKER_PROTOCOL.md）
PROTOCOL_VERSION = "1"

__all__ = ["__version__", "PROTOCOL_VERSION"]
