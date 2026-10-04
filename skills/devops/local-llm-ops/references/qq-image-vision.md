# QQ 附件链路（图片 / 文档 / 语音）——网关自己会落地，先别急着手动下载

主人从 QQ 发来的东西，**网关会自动下载并缓存到本地，再把路径写进消息**——所以图能看、文件能读、语音有转写，不需要主人先去共享文件夹中转。

## 落地位置（适配器 `gateway/platforms/qqbot/adapter.py` 的 `_process_attachments`）

| 附件类型 | 落盘位置 | 我看到的形态 |
|---|---|---|
| 图片（`image/*`） | `/opt/data/cache/images/img_<uuid12>.jpg` | 消息里带图片引用；可直接 `vision_analyze` 该路径 |
| 文档（PDF/Word/Excel…） | `/opt/data/cache/documents/doc_<uuid12>_<原名>` | 正文末尾附 `[file: <原名> (<绝对路径>)]` |
| 视频 | 同上（label 为 `video`） | `[video: <原名> (<路径>)]` |
| 语音 | 转写文本（不走 STT 也能用 QQ 自带的 `asr_refer_text`） | 正文里附 `[Voice] <转写>`；失败则是 `[Voice] [语音识别失败]` |

**推论（省主人一步）**：主人说「给你发文件你也看不到、只能我自己传到共享文件夹」是**旧印象**——PDF/图片/语音直接发就行，我拿到的是本地绝对路径，能直接 `read_file` / `ocr_pdf.py`。共享文件夹留给大文件或一批文件。

**排障**：附件没出现时先看网关日志里的 `attachment[0]: content_type=... url=... filename=...` 行——没这行就是平台侧没把附件送过来（不是本地处理失败）。

## 图片的识别走哪条路（当前默认：本地）

QQ 图片落地后按 `auxiliary.vision` 的路由处理，当前**默认发给宿主 llama.cpp 的 minicpm-v4.6:1b（`http://172.17.0.1:8083/v1`）**，主模型只拿到文字描述。配置、验证方法（不重启网关也能验）和回滚步骤见 `hermes-image-routing.md`。

⚠️ 因为主模型看不到原始像素，**图里要引用的数字/表格/单号/时间戳不要引用描述值**——改走 `scanned-pdf-ocr` 技能（`ocr_pdf.py` 直接吃图片）。本地小模型会把 `123 GB` 读成 `132 GB`、把时间戳读成别的年份，而且读起来很像对的。

## 容器内访问本地服务的地址

本地后端一律走宿主 docker 网桥 `172.17.0.1`，容器内 `127.0.0.1` 不通：llama.cpp vision `172.17.0.1:8083`、SearXNG `:18888`、Hindsight `:8888`。

## 手动下载（仅当附件没自动落地、或要复现旧链路时）

QQ 多媒体 CDN 的下载**要带鉴权头**，适配器用的是 `Authorization: QQBot <access_token>`（缺了会返回非 200）；URL 里的 rkey 签名会过期，过期就重新从消息里取最新 URL。手动路径：把 URL 下载成文件 → 直接喂本地视觉实例的 `/v1/chat/completions`（`image_url` 用 data URL），或直接走 `ocr_pdf.py`。
