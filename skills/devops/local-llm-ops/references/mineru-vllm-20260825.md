# MinerU 本地部署 + vllm 显存互斥（2026-08-25 实测）

MinerU（上海 AI 实验室 OpenDataLab 开源 PDF→Markdown/JSON 解析引擎）在飞牛 NAS（RTX 3060 12G）的部署与按需使用记录。大学场景：精密 PDF 提取（公式/表格/版面）按需启用，日常用 Hermes 自带 read_file/OCR/minicpm 看图即可。

## 部署要点

- **官方预构建镜像拉不了**：docker hub `opendatalab/mineru` 仓库不存在；ghcr.io `opendatalab/mineru` 需认证（token API 拿不到 + manifest 403）——**只能用仓库里的 china/Dockerfile 自构建**
- 构建（国内源，实测约 30 分钟内）：
  ```bash
  git clone --depth 1 https://github.com/opendatalab/MinerU /tmp/MinerU   # 走代理（github.com 代理通）
  docker build -t mineru:latest -f docker/china/Dockerfile .
  ```
  - china/Dockerfile：基础镜像 `docker.m.daocloud.io/vllm/vllm-openai`（DaoCloud 国内镜像）+ 阿里云 PyPI + ModelScope 模型源——全程国内
  - 镜像大（vllm 框架 ~10GB + 全家桶模型 ~5GB ≈ 15-20GB）——**「隆重」的原因不是 1.2B 模型本身（bf16 ≈ 2.4GB），是整栈**
- 启动（8G 内存限制 + GPU）：
  ```bash
  docker run -d --name mineru-api --restart unless-stopped --memory 8g --gpus all \
    -p 8000:8000 --ipc host --ulimit memlock=-1 --ulimit stack=67108864 \
    -e MINERU_MODEL_SOURCE=local \
    mineru:latest mineru-api --host 0.0.0.0 --port 8000 --gpu-memory-utilization 0.4
  ```
- API：`POST /file_parse`（同步 multipart `files=@xxx.pdf`）返回 markdown；`/tasks` 异步；`/health`；`/docs` 浏览器调试

## ⚠️ vllm 与 Ollama 显存互斥（关键坑）

**症状**：`status: failed` + vllm 日志 `RuntimeError: Engine core initialization failed`，或任务直接报 `CUDA out of memory. Tried to allocate 20.00 MiB. GPU 0 has a total capacity of 11.63 GiB of which 8.19 MiB is free`——**本地推理栈占满显存时它无法初始化**。实测：三个 llama.cpp 容器（extract 5.5G / embed 3.9G / vision 1.8G）常驻时卡上只剩 8 MiB；**`backend=pipeline` 也照样走 GPU**（不是选对 backend 就能跑）。

**结论：这个 docker 版不要用，严格 OCR 走 CPU 那条线。** `docker start mineru-api` 只值得用来「确认它还活着」——`/health` 会返 200、`/file_parse` 与 `/tasks` 都注册着，但真提交文件必 OOM，**别把 200 或「接口在」当成可用证据**（要验证就真的提交一个文件看 `status` 和 error）。真正干活用 venv 里的 MinerU 3.4.5 pipeline（`/opt/data/ocr-eval/ocr_pdf.py`，纯 CPU、零显存、2.4–2.8G 内存，图片和 PDF 都吃），分工与实测数据见 `scanned-pdf-ocr` 技能。

已不适用的历史做法：曾靠「先卸载 Ollama 模型腾显存」来给它让位——现在 GPU 由 llama.cpp 三件套常驻，而它们背后是 Hindsight 抽取、embedding 与看图路由，**不要为了这个 docker 服务去停它们**。它自己带 `MINERU_DEVICE_MODE=cpu`（`mineru/utils/config_reader.py` 读），但 CPU 路线就是上面那条 venv，没必要为此重建一个 29.7GB 镜像的容器。

- **用法约定**：保持停用即可（`restart=no`，不会自启）；哪天为了排查启过，验证完 `docker stop mineru-api` 收尾，别让它白占 8G memory 限额

## 附带教训

- 容器内 curl 127.0.0.1:17890（宿主代理）不通——**容器访问宿主代理用 172.17.0.1:17890**（docker0 网关）；所有「走代理」测试要在宿主 SSH 里做
- 从容器（Hermes）访问其他容器 API（如 mihomo 9090）：不同 docker 网络不通——用宿主 SSH 或容器 IP（docker inspect 拿）
- docker stop hermes 会杀掉棉棉自己所在环境——重建类操作必须在宿主 nohup 后台脚本里跑（见 hermes-container-ops）
