# 容器化部署（install.sh 的替代路线，适合已经有编排的环境）
#
#   docker build -f deploy/Dockerfile -t pdf2zh-worker .
#   docker run -d --name pdf2zh-worker \
#     -e TRANSLATE_WORKER_TOKEN="$(openssl rand -hex 32)" \
#     -e TRANSLATE_WORKER_HOST=0.0.0.0 \
#     -p 127.0.0.1:8791:8791 -v pdf2zh-data:/data pdf2zh-worker
#
# 注意 HOST 要设成 0.0.0.0（容器内部），端口映射时再绑回宿主机的 127.0.0.1，
# 公网还是只走 nginx 443。
#
# 也可以拿官方镜像 awwaawwa/pdfmathtranslate-next 当 base（pdf2zh 已经装好），
# 那样构建快得多；下面这个自建版本胜在依赖关系一目了然。

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TRANSLATE_WORKER_DATA=/data \
    TRANSLATE_WORKER_HOST=0.0.0.0 \
    TRANSLATE_WORKER_PORT=8791 \
    HOME=/data \
    XDG_CACHE_HOME=/data/cache \
    XDG_CONFIG_HOME=/data/config

WORKDIR /app

COPY pyproject.toml README.md ./
COPY pdf2zh_worker ./pdf2zh_worker

RUN pip install --upgrade pip \
    && pip install . \
    && python -c "import pdf2zh_next; print('pdf2zh-next', pdf2zh_next.__version__)"

# 不用 root 跑
RUN useradd --system --uid 10001 --home-dir /data pdf2zh \
    && mkdir -p /data/cache /data/config \
    && chown -R pdf2zh:pdf2zh /data
USER pdf2zh

VOLUME ["/data"]
EXPOSE 8791

# 模型资源建议构建后先跑一次预热并把 /data 做成持久卷：
#   docker run --rm -v pdf2zh-data:/data pdf2zh-worker python -m pdf2zh_worker --warmup
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8791/healthz', timeout=4).status==200 else 1)"

CMD ["python", "-m", "pdf2zh_worker"]
