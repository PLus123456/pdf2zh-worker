"""进程入口：`python -m pdf2zh_worker`（systemd 用的就是它）。

    --check   只校验配置和依赖，不监听端口（安装脚本用来做上线前体检）
    --warmup  预下载 babeldoc 的模型/字体资源，避免第一单卡在下载上
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import ConfigError, load_config


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # httpx/openai 每次翻译请求都要刷一行，长跑服务会被淹掉
    for noisy in ("httpx", "httpcore", "openai", "pdfminer", "peewee"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="pdf2zh-worker", description="pdf2zh 文档翻译 worker")
    parser.add_argument("--check", action="store_true", help="只校验配置与依赖后退出")
    parser.add_argument("--warmup", action="store_true", help="预下载模型/字体资源后退出")
    args = parser.parse_args(argv)

    # 预热只是拉模型资源，跟 token/端口这些无关——放在 load_config 之前，
    # 这样安装脚本预热时不必把 token 抖到命令行上（ps 里人人可见）
    if args.warmup:
        _setup_logging("INFO")
        return _warmup(logging.getLogger("pdf2zh_worker"))

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"[pdf2zh-worker] 配置错误：{exc}", file=sys.stderr)
        return 2

    _setup_logging(config.log_level)
    logger = logging.getLogger("pdf2zh_worker")

    if args.check:
        return _check(config, logger)

    if config.host not in ("127.0.0.1", "localhost", "::1"):
        logger.warning(
            "监听地址是 %s（非回环）。协议要求只监听 127.0.0.1，公网只暴露 nginx 443 反代；"
            "确认这是你要的",
            config.host,
        )

    import uvicorn

    from .app import create_app

    logger.info("worker 启动：%s:%d，数据目录 %s", config.host, config.port, config.data_dir)
    uvicorn.run(
        create_app(config),
        host=config.host,
        port=config.port,
        log_level=config.log_level.lower(),
        # 访问日志里全是主应用每 20s 一次的轮询，没有信息量
        access_log=False,
    )
    return 0


def _check(config, logger) -> int:
    logger.info("配置 OK：监听 %s:%d，数据目录 %s", config.host, config.port, config.data_dir)
    logger.info(
        "并发 %d / 排队上限 %d / 单文件上限 %dMB / 保留 %dh",
        config.concurrency,
        config.queue_limit,
        config.max_mb,
        config.retention_hours,
    )
    try:
        config.jobs_dir.mkdir(parents=True, exist_ok=True)
        probe = config.jobs_dir / ".write-probe"
        probe.write_text("ok", "utf-8")
        probe.unlink()
    except Exception as exc:
        print(f"[pdf2zh-worker] 数据目录不可写：{config.data_dir}（{exc}）", file=sys.stderr)
        return 3
    try:
        import pdf2zh_next

        logger.info("pdf2zh-next 版本 %s", getattr(pdf2zh_next, "__version__", "unknown"))
    except Exception as exc:
        print(f"[pdf2zh-worker] 没能导入 pdf2zh_next：{exc}", file=sys.stderr)
        return 4
    logger.info("体检通过")
    return 0


def _warmup(logger) -> int:
    """把 babeldoc 的离线资源拉齐。装完就跑一次，别让第一个真单去等下载。"""
    try:
        import babeldoc.assets.assets

        logger.info("开始预热 babeldoc 资源（首次可能要下几百 MB）…")
        babeldoc.assets.assets.warmup()
        logger.info("预热完成")
        return 0
    except Exception as exc:
        print(f"[pdf2zh-worker] 预热失败：{exc}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    sys.exit(main())
