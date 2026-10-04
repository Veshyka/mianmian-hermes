---
name: voice-stack
description: Use when 运维本地语音栈（STT 8090 / TTS 8091）。
---

# 语音容器栈（STT + TTS）

操作手册（最新命令与数字）：`/opt/data/stack/voice/README.md`；计划/决策史：`/opt/data/plans/voice-stack-PLAN.md`；改动台账：`/opt/data/ops-changelog/变更-20261003-记忆标签与语音栈计划.md`

栈位置：`/opt/data/stack/voice/`（宿主 `/vol1/1000/<USER>`）

| 服务 | 容器 | 端口（只绑 172.17.0.1） | 干什么 | 内存上限 |
|---|---|---|---|---|
| voice | voice | 8090 | STT，faster-whisper，OpenAI 兼容 `/v1/audio/transcriptions` | 4g |
| piper | voice-piper | 8091 | TTS，`/v1/audio/speech`（中文/英文） | 1g |

镜像：`ghcr.nju.edu.cn/speaches-ai/speaches:latest-cpu`（Docker Hub 本机不通）；piper 服务以它为基础镜像自建（`piper/Dockerfile`，`uv pip install piper-tts numpy onnxruntime fastapi uvicorn`）。

## 宿主操作怎么走（容器内没有 docker daemon）

```bash
# sudo 需要宿主上的 askpass 文件；重启宿主后 /tmp 会被清，先重推
/opt/data/hspr_ssh.sh 'umask 077; cat > /tmp/mian_sudo.sh; chmod 700 /tmp/mian_sudo.sh' < /opt/data/scripts/mian_sudo.sh
/opt/data/hspr_ssh.sh 'export SUDO_ASKPASS=/tmp/mian_sudo.sh; cd /vol1/1000/<USER> && sudo -A docker compose up -d'
```

- 只读信息优先用官方通道 `python3 /opt/data/scripts/trim_cli.py --json docker container ls`。
- **改 compose 后必须 `--force-recreate`**：本机踩过「限额/参数写在文件里但容器没重建 → 没生效」。改完用 `docker inspect <c> --format '{{.HostConfig.Memory}}'` 复核。

## 铁律（都交过学费）

1. **中文 TTS 不要用 speaches 的 TTS**：它把音色语言码 `zh` 交给 espeak 后端 → Kokoro 直接 `RuntimeError: language "zh" is not supported by the espeak backend`，piper 中文音色则静默合成 **0 字符空音频**（wav 只有 44 字节头）。**中文走 8091 的 piper 服务**（piper 音色 `config.json` 里 espeak voice = `cmn`，正确）。
2. **镜像里跑 uv 要 `cd /tmp` + `UV_NO_PROJECT=1`**：镜像 cwd `/home/ubuntu/speaches` 有 `uv.lock`，否则 `uv pip install X` 会回 `Audited 1 package` 却什么都没装（假成功）。
3. **pip 源**：`pypi.org` 本机时通时断 → 用 `--index-url https://mirrors.aliyun.com/pypi/simple/`。
4. **HF 走镜像**：`huggingface.co` 不通、`hf-mirror.com` 通 → 容器 env `HF_ENDPOINT=https://hf-mirror.com`；piper 服务不需要（音色复用同一目录）。
5. **模型目录权限**：容器以 uid 1000 写 HF 缓存，目录默认 0700 → 别的用户/备份读不到。下完新模型补 `sudo chmod -R a+rX .../stack/voice/models`（已修过一次 2026-10-03）。
6. **HF 快照里的音色配置叫 `config.json`**，不是 piper 默认找的 `<model>.onnx.json` → `PiperVoice.load(model, config_path=cfg)` 显式传。
7. **容器里没有 `uvx`**：官方文档的 `uvx speaches-cli ...` 不可用；模型下载走 HTTP：`curl -X POST http://172.17.0.1:8090/v1/models/<registry-id>`，清单 `curl -s .../v1/registry`。
8. **构建镜像时的用户**：speaches 镜像默认用户是 ubuntu(uid 1000)，写不了 `/opt` → 自建 venv 放 `/home/ubuntu/` 下。

## 量尺（别靠感觉说快慢）

```bash
cd /opt/data/stack/voice/bench && python3 run_bench.py --tag cpu-small
```

TTS 合成 → 把音频回喂 STT → 报耗时/RTF/转写文本，追加写 `bench/results.json`（含 tag 与时间），样本落 `bench/samples/`。改后端/模型/CPU-GPU 后都跑一遍再说话。

基线（2026-10-03，CPU，i3-12100F）：STT RTF 0.355–1.13（中文长音频最快）、TTS RTF 0.044–0.35；占用 voice 1.66/4 GiB、voice-piper 640 MiB/1 GiB。中文长句转写有错字，换 medium 前先量（内存上限要跟着抬）。

## GPU 该不该上

当前**不上**：显存只剩 ~2713 MiB（llama-extract+embed 占 9196/12288），而 llama-extract（27B 三值模型）有反复 cgroup OOM 前科；CPU 已比实时快 2–3 倍。要试就按 README 的步骤（换 `latest-cuda` + `devices: nvidia.com/gpu=all` + `int8_float16`），**并复查** `docker ps -a` 里 llama-extract 是否 `Exited (137)`、`dmesg -T | grep 'Memory cgroup out of memory'`。

## 接入 Hermes（2026-10-03 已上，两个 profile 都改了）

官方支持的路就是内置 `openai` provider + 自定 `base_url`（**不需要写插件**）：

```yaml
stt:
  provider: openai
  language: ''                            # ⚠️ 原来是 en：写死语言会把中文音频按英文转
  openai:
    model: Systran/faster-whisper-small   # 用 registry id
    base_url: http://172.17.0.1:8090/v1   # SDK POST 到 {base_url}/audio/transcriptions
    language: ''                          # 空串才回落上一层；两处都空 = 真自动识别
tts:
  provider: openai                        # ⚠️ 不能写 piper：那是进程内本地 piper，指不到 HTTP
  openai:
    base_url: http://172.17.0.1:8091/v1
    api_key: \"<SECRET>\"                   # ⚠️ TTS 没有"本地免 key"分支，必须非空占位
    model: piper-zh_CN-huayan-medium
    voice: piper-zh_CN-huayan-medium
```

- STT 侧：`base_url` 是私有地址（172.x/127.x）时 api_key 会被自动注成 `not-needed`，不用配。
- **怎么改**：`/opt/data/config.yaml` 被 Hermes 保护，`patch`/`write_file` 会被拒 —— 用官方 CLI：
  `hermes config set stt.provider openai`；不被识别的路径（`stt.provider`、`*.openai.base_url`、`tts.openai.api_key`）要加 `--force`。
  聊天门那份是 profile 配置：`HERMES_HOME=/opt/data/profiles/chat hermes config set ...`（`HERMES_PROFILE=chat` **不生效**，仍指主 config）。
- 改前先 `cp -a config.yaml config.yaml.bak-before-voice-stack-<时间戳>`；改后用 `diff` 核对**只**动了这几行（`config set` 会重写文件，确认没丢别的键）。
- **不用重启网关**：`stt/tts` 配置是每次调用时从 config.yaml 现读的。
- 实测验证（两个方向都得测）：
  - TTS：调 `text_to_speech` → 回执 `provider: openai`、出 wav 时长非零；宿主 `docker logs --tail 5 voice-piper` 应看到 `POST /v1/audio/speech 200 OK`
  - STT：`cd /opt/hermes && PYTHONPATH=/opt/hermes /opt/hermes/.venv/bin/python -c "from tools.transcription_tools import transcribe_audio; print(transcribe_audio('/path/x.wav'))"` → 应回 `{'success': True, 'transcript': '...', 'provider': 'openai'}`；`voice` 容器日志应有 `/v1/audio/transcriptions 200`。⚠️ 返回字典的键是 **`transcript`**，不是 `text`/`model`（看错键会误判成"转写为空"）。

## 与 Hermes 自带 STT/TTS 的关系

Hermes 自己有 `stt.provider: local`（faster-whisper lazy-package）与 `tts.provider: piper` —— 容器栈的价值是「一处统一服务 + 独立限额 + 多调用方共用」（聊天门/脚本/以后的 MaiBot），不是补它的功能缺失。要把 Hermes 接到容器上需插件/扩展点（`stt.provider` 枚举里**没有自定义 base_url**），接入方式仍待主人拍板，见计划书 P5。
