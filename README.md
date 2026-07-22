# pdf2zh-worker

给 lecture-live 主应用用的**文档翻译 worker**：一层薄薄的 HTTP wrapper，把
[pdf2zh-next](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next) 包成
`TRANSLATION_WORKER_PROTOCOL.md v1` 那套端点。装在独立机器上，主应用推 PDF 过来、
轮询进度、取回译文。

```
主应用 (lecture-live)                        这台机器
  TranslationTask 状态机
        │  PUT  /jobs/:id/input   ── 推源 PDF ──▶  pdf2zh-next
        │  POST /jobs/:id/start   ── 派单 ─────▶    │
        │  GET  /jobs/:id         ── 轮询进度 ◀──   │ 翻译中
        │  GET  /jobs/:id/output/{mono,dual}  ◀──  产物
        │  DELETE /jobs/:id       ── 清理 ─────▶
        ▲
        └── LLM 请求回流：pdf2zh 的 OpenAI 兼容后端指回主应用的
            /api/translate/llm-proxy/v1，token 计量在主应用侧，
            厂商 key 永远不出主应用
```

三条设计红线：

- **worker 完全被动**：不重试、不断点续跑、不回调。任何异常只是把任务写成
  `failed`，重派与否由主应用状态机决定。
- **可随时重启**：重启后所有 `queued`/`running` 一律标 `failed`，主应用按同一
  `jobId` 幂等重派。
- **不改 pdf2zh 一行源码**：只调它的公开 Python API。AGPL 边界靠这个划清。

---

## 一键安装

在**目标机器**上（Linux + systemd）：

```bash
git clone <本仓库> pdf2zh-worker && cd pdf2zh-worker
sudo ./install.sh            # 打开菜单，选 1 安装
# 或者无人值守：
sudo ASSUME_YES=1 ./install.sh install
```

装的时候它会做这些事：

1. 找一个 3.10–3.13 的 Python（pdf2zh-next 只吃这个区间）；系统上没有就问你要不要用
   `uv` 装一个 3.12
2. 建系统用户 `pdf2zh`、venv、数据目录 `/var/lib/pdf2zh-worker`
3. 装 `pdf2zh-next` 和本 wrapper
4. 生成 32 字节随机 token 写进 `/etc/pdf2zh-worker/worker.env`（0640，root:pdf2zh）
5. 写 systemd 单元并启动
6. 预热 babeldoc 的模型/字体资源（**首次要下几百 MB**，慢是正常的）
7. 打健康检查，把 Base URL / Token 打出来给你去主应用填

装完之后，任何时候敲一下就能再调出菜单：

```bash
sudo pdf2zh-worker
```

```
════ pdf2zh 文档翻译 worker ════   ● 运行中
  /opt/pdf2zh-worker   端口 8791   并发 1

   1) 安装 / 重装
   2) 升级（wrapper + pdf2zh-next）
   3) 查看状态与健康检查
   4) 查看实时日志
   5) 改配置（端口/并发/上限…）
   6) 查看 / 轮换鉴权 token
   7) 启动     8) 停止     9) 重启
  10) 预热模型资源
  11) 清理翻译缓存（换了底层模型之后用）
  12) 体检（doctor）
  13) 卸载
   0) 退出
```

菜单里的每一项都有等价的子命令，写脚本时直接用：

```bash
sudo pdf2zh-worker upgrade            # 升级 wrapper + pdf2zh-next 并重启
sudo pdf2zh-worker status             # 版本 / 端口 / 并发 / 磁盘 / healthz
sudo pdf2zh-worker doctor             # 全面体检
sudo pdf2zh-worker logs               # journalctl -f
sudo pdf2zh-worker config             # 交互改端口/并发/上限
sudo pdf2zh-worker token              # 看或轮换 token
sudo pdf2zh-worker restart
sudo pdf2zh-worker uninstall          # 会问要不要连数据一起删
sudo pdf2zh-worker uninstall --purge  # 数据、配置、用户一起清掉
```

---

## 接到主应用上

装完脚本会把这两行打出来，去主应用 **admin → 设置 → 翻译服务 → worker 集群** 新增一台：

| 字段 | 填什么 |
| --- | --- |
| Base URL | `https://worker.example.com`（nginx 443，**不带**尾斜杠、不带路径） |
| Token | 脚本打出来的那串，或 `sudo pdf2zh-worker token` 再看 |
| 并发 | 与这台的 `TRANSLATE_WORKER_CONCURRENCY` 一致，否则要么闲置要么排队 |
| QPS | 这台对主应用 LLM 代理的每秒请求上限，按网关余量给 |

填完点「测试连接」——它打的就是 `GET /healthz`。

> 主应用判断 token 对不对，看的是 healthz 有没有回 `queue` 字段（token 错了照样
> 200，只是不带详情）。所以「连得上但一直显示异常」＝ token 错了，不是网络问题。

### nginx

worker 只监听 `127.0.0.1`，公网唯一入口是 nginx 443。
配置见 [`deploy/nginx.conf.example`](deploy/nginx.conf.example)，两个容易踩的点：

- `client_max_body_size` 要 **≥** `TRANSLATE_WORKER_MAX_MB`，否则大论文被 nginx 先拦，
  主应用只看到一个来路不明的 413
- `proxy_read_timeout` / `proxy_send_timeout` 给到 600s，主应用的传输类调用留了 10 分钟

**千万别**把 pdf2zh 自带的 Gradio 7860 端口暴露到公网——那是个无鉴权的 WebUI。
本 wrapper 根本不启动它。

---

## 环境变量

都在 `/etc/pdf2zh-worker/worker.env`，改完 `sudo pdf2zh-worker restart`。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `TRANSLATE_WORKER_TOKEN` | **必填，≥32 字符** | Bearer 鉴权。太短直接拒绝启动 |
| `TRANSLATE_WORKER_HOST` | `127.0.0.1` | 监听地址。设成非回环会打警告 |
| `TRANSLATE_WORKER_PORT` | `8791` | 监听端口 |
| `TRANSLATE_WORKER_DATA` | `./data` | 任务目录根 |
| `TRANSLATE_WORKER_CONCURRENCY` | `1` | 同时翻译几篇。要和主应用那台 worker 行的并发对齐 |
| `TRANSLATE_WORKER_QUEUE_LIMIT` | `8` | 排队上限，满了 `start` 回 429 |
| `TRANSLATE_WORKER_MAX_MB` | `50` | 单文件上限，超了回 413 |
| `TRANSLATE_WORKER_RETENTION_HOURS` | `24` | 终态任务保留多久 |
| `TRANSLATE_WORKER_SWEEP_MINUTES` | `30` | 多久扫一次过期任务 |
| `TRANSLATE_WORKER_LOG_LEVEL` | `INFO` | `DEBUG` 能看到 pdf2zh 的阶段细节 |

协议之外的调优旋钮（默认值就是推荐值，一般别动）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `TRANSLATE_WORKER_LLM_TIMEOUT` | `300` | 单次 LLM 请求超时（秒） |
| `TRANSLATE_WORKER_AUTO_GLOSSARY` | `false` | 开了就让 pdf2zh 额外跑一轮 LLM 自动抽术语：译文一致性更好，**但 token 成本明显上去**，而主应用是按页收费的，这笔钱落在运营方头上 |
| `TRANSLATE_WORKER_REPORT_INTERVAL` | `0.5` | 进度上报间隔（秒） |
| `TRANSLATE_WORKER_POOL_MAX_WORKERS` | 跟随 qps | 翻译线程池大小 |
| `TRANSLATE_WORKER_IGNORE_CACHE` | `false` | 跳过 pdf2zh 的本地翻译缓存，见下 |

### ⚠️ 关于翻译缓存

pdf2zh 会把「原文 → 译文」存在本地 SQLite 里（`<data>/.cache/babeldoc/cache.v1.db`），
重复的段落不再花 token。缓存键是 `(引擎, 引擎参数, 原文)`，而**引擎参数里的模型名对我们
永远是那个占位符**（`llm.model`，真实路由由主应用代理服务端强制）——也就是说
worker 根本不知道背后到底跑的哪个模型。

后果：**主应用 admin 把 TRANSLATION 路由换成另一个模型之后，这台机器还会拿旧模型的
译文来顶**，直到缓存过期（保留最近 5 万条）。换模型时二选一：

```bash
sudo pdf2zh-worker            # 菜单 → 11) 清理翻译缓存（模型/字体资源不动）
sudo pdf2zh-worker clear-cache
```

或者干脆把 `TRANSLATE_WORKER_IGNORE_CACHE=true` 常开——代价是重复内容每次都重新花钱。

`HOME` / `XDG_CACHE_HOME` / `XDG_CONFIG_HOME` 由安装脚本指到数据目录里——
pdf2zh 在 **import 的时候**就会往 `~/.config/pdf2zh` 写东西，HOME 不可写会直接起不来。

---

## 端点速查

除 `healthz` 外全部要求 `Authorization: Bearer <token>`。

| 端点 | 成功 | 失败 |
| --- | --- | --- |
| `GET /healthz` | 恒 `200`；token 对才带 `queue`/`engine` | —— |
| `PUT /jobs/:id/input` | `200 {"received":n}` | `400` 空体/非 PDF/id 非法，`409` 任务在跑，`413` 超限 |
| `POST /jobs/:id/start` | `202 {"status","position"}` | `400` 参数非法，`409` 没有源文件，`429` 排队满 |
| `GET /jobs/:id` | `200` 状态快照 | `404` 任务不存在（主应用视为丢失并重派） |
| `GET /jobs/:id/output/mono\|dual` | `200 application/pdf` | `409` 没跑完，`404` 没这个产物，`410` 已清扫 |
| `DELETE /jobs/:id` | 恒 `204`（幂等，先杀进程再删目录） | —— |

`404` 和 `410` 是刻意分开的：清扫掉的任务会留一个墓碑，让主应用知道是「过期了」
而不是「任务丢了」，免得白白重派一轮。

任务目录长这样：

```
<data>/jobs/<jobId>/state.json     状态真源（原子写，重启后终态还在）
                   /source.pdf     主应用推来的原件
                   /glossary.csv   术语表（可选）
                   /mono.pdf       单语产物
                   /dual.pdf       双语对照产物
<data>/tombstones/<jobId>          清扫墓碑
```

---

## 本地跑一份（开发/调试）

不装服务、不用 root：

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e .

export TRANSLATE_WORKER_TOKEN="$(openssl rand -hex 32)"
export TRANSLATE_WORKER_DATA=./data
python -m pdf2zh_worker --check     # 体检：配置、目录可写、pdf2zh 能导入
python -m pdf2zh_worker --warmup    # 预下载模型资源
python -m pdf2zh_worker             # 起服务
```

冒烟验一把（LLM 那侧要有个能用的 OpenAI 兼容端点）：

```bash
JOB=smoke-$(date +%s)
H="Authorization: Bearer $TRANSLATE_WORKER_TOKEN"

curl -sf -H "$H" http://127.0.0.1:8791/healthz | jq
curl -sf -X PUT -H "$H" -H 'Content-Type: application/pdf' \
     --data-binary @paper.pdf "http://127.0.0.1:8791/jobs/$JOB/input"
curl -sf -X POST -H "$H" -H 'Content-Type: application/json' -d '{
  "langIn":"en","langOut":"zh","qps":4,"watermark":false,"glossary":null,
  "llm":{"baseUrl":"https://主应用域名/api/translate/llm-proxy/v1",
         "apiKey":"<任务级凭据>","model":"lecture-live-gateway"}
}' "http://127.0.0.1:8791/jobs/$JOB/start"

watch -n2 "curl -sf -H '$H' http://127.0.0.1:8791/jobs/$JOB | jq '{status,stage,progress}'"
curl -sf -H "$H" "http://127.0.0.1:8791/jobs/$JOB/output/dual" -o dual.pdf
curl -sf -X DELETE -H "$H" "http://127.0.0.1:8791/jobs/$JOB"
```

### 测试

```bash
pip install -e '.[test]'
python -m pytest              # 装了 pdf2zh 是 36 条，没装是 30 条 + 6 skip
bash tests/test_install_sh.sh # install.sh 的 13 项回归
```

- `tests/test_protocol.py`（30 条）用假 runner 顶掉真翻译，验 HTTP 语义：状态码、
  幂等、队列上限、取消后消费者还活着、清扫后 410、apiKey 不落盘。不需要装 pdf2zh。
- `tests/test_settings.py`（6 条）只在装了 pdf2zh-next 的机器上跑，拿它自己的
  `validate_settings()` 校验参数映射——字段一旦被上游改名，这里立刻炸，而不是等
  第一单跑到一半才发现。
- 真 PDF 端到端见上面的冒烟流程。

---

## 排错

| 症状 | 多半是 |
| --- | --- |
| 服务起不来，日志说 `TRANSLATE_WORKER_TOKEN 至少 32 个字符` | token 太短，`sudo pdf2zh-worker token` 重新生成 |
| 主应用「测试连接」说异常，但 curl healthz 是通的 | 两边 token 不一致——healthz 回的 JSON 里没有 `queue` 就是它 |
| 第一单卡在 0% 很久 | 在下 babeldoc 模型。`sudo pdf2zh-worker warmup` 先下完 |
| 任务全 failed，错误是 `worker 重启导致任务中断` | 服务刚重启过；主应用会自己重派，看 `journalctl` 查为什么重启 |
| 任务失败提示上游 LLM 报错 | 主应用的 `/api/translate/llm-proxy` 那侧的问题：凭据过期（任务终态即吊销）或网关挂了 |
| 磁盘涨得快 | `TRANSLATE_WORKER_RETENTION_HOURS` 调小，或 `sudo pdf2zh-worker status` 看占用 |
| 大文件传到一半断 | nginx 的 `client_max_body_size` / `proxy_read_timeout` 没跟上 |

日志：`sudo pdf2zh-worker logs`，或 `journalctl -u pdf2zh-worker -n 200`。
想看 pdf2zh 每个阶段的细节就把 `TRANSLATE_WORKER_LOG_LEVEL` 改成 `DEBUG`。

---

## 容器部署

不想用 systemd 的话看 [`deploy/Dockerfile`](deploy/Dockerfile)。要点：容器里
`TRANSLATE_WORKER_HOST` 设 `0.0.0.0`，端口映射时再绑回宿主机 `127.0.0.1`，
`/data` 做成持久卷（模型缓存在里面，不然每次重建都要重下）。

---

## 许可

本 wrapper 以 **AGPL-3.0-or-later** 发布（见 [LICENSE](LICENSE)），与它驱动的
pdf2zh-next 保持一致。

wrapper 通过 pdf2zh-next 的公开 Python API 调用它，**不修改其源码**；
部署在独立机器上，与主应用之间只有 HTTP 协议边界。
