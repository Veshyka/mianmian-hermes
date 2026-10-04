---
name: bilibili-video-ops
description: B 站视频（BV 号）元数据/分章/字幕/转录兜底。免登录 API 拿官方分章 + 转录专名校正。
---

# B 站视频处理（免登录 API + 转录兜底）

B 站视频总结/处理的底层操作手册。与 video-summary skill 配合：video-summary 管流程（AI 字幕优先），本 skill 管 API 细节和转录兜底。

## 免登录 API（curl 直调，不需要 cookie）

### 1. 视频元数据（标题/UP主/时长/简介/统计）
```bash
curl -s "https://api.bilibili.com/x/web-interface/view?bvid=BV1dDub6PE31" \
  -H "Referer: https://www.bilibili.com" -H "User-Agent: Mozilla/5.0"
```
- `data.title` / `data.owner.name` / `data.duration`(秒) / `data.desc` / `data.stat.{like,danmaku,reply}` / `data.pubdate`
- 拿 `data.cid`（分片 id，player API 需要）

### 2. 官方分章时间戳（view_points）★ 视频总结时间戳导航的免费来源
```bash
curl -s "https://api.bilibili.com/x/player/v2?bvid=BV1dDub6PE31&cid=<cid>" \
  -H "Referer: https://www.bilibili.com" -H "User-Agent: Mozilla/5.0"
```
- `data.view_points[]` = `{from, to, content}`（秒级起止 + 章节名）→ 直接生成「时间戳导航」表格
- 免登录可用，比 whisper 转录拿时间戳准得多

### ⚠️ Referer 会踩风控（2026-09-23 aiohttp 实测，curl 也同理）
`api.bilibili.com` 对 **`Referer: https://www.bilibili.com/`（裸域名）返回 412**（HTML 风控页，不是 JSON），
而「**不带 Referer**」或「带具体视频页 `https://www.bilibili.com/video/<bvid>`」都 200；
`search/type` 接口反过来——要带站内 Referer 才稳。稳妥写法：**两种顺序各试一次就收手**，别重试风暴；
搜索接口另外要先 GET 一次 `https://www.bilibili.com/` 拿 `buvid3` 访客 cookie（无 cookie 易被拦）。
可用实现见 `chat-layer/plugin/hermes_lookup/main.py` 的 `_bili_api()`。

### 3. 字幕可用性判断（决定走 AI 字幕还是转录）
同一 player/v2 响应：
- `data.need_login_subtitle: true` → AI 字幕需要登录态（无 cookie 拿不到）
- `data.subtitle.subtitles[]` 空 + need_login_subtitle=true → 无公开字幕，走转录
- 有 cookie 时 AI 字幕最优：`yt-dlp --skip-download --write-subs --sub-langs "ai-zh,zh-CN,zh-Hans" --cookies <cookie> -o /tmp/bili_subs <BV链接>` 秒级拿 srt

## 转录兜底（无字幕/无登录态）

### 环境搭建（本机已验证）
- faster-whisper-medium 已下载（`~/.cache/huggingface/hub/models--Systran--faster-whisper-medium`），但 **lazy-packages 不会自动注入 python path**——必须建 venv：
```bash
uv venv /tmp/fwenv --python 3.11
uv pip install --python /tmp/fwenv/bin/python faster-whisper
# 转录：CPU+int8（容器无 GPU），HF_HUB_DISABLE_XET=1 避免 hf-mirror 401
```
- 12-13 分钟视频 CPU int8 medium 约 10-15 分钟跑完；输出 srt + 带 `[MM:SS]` 前缀的 txt

### 转录文本专名校正（whisper 中文对英文品牌/拼音名识别差，必查）
实测误识别映射（2026-08-28 BV1dDub6PE31）：
- OpenCub/OpenClub → **OpenClaw**；Aurama → **Ollama**；千蚊 → **千问(Qwen)**；DeepSec → **DeepSeek**；飞牛S → **飞牛OS(fnOS)**
- Adint → Agent；宁克ME Pro → 零刻ME Pro(N305)；Acer → balenaEtcher(烧录工具)；E-Lite/Ban → Bun/Node.js
- 校正方法：先拿官方分章名（view_points）和元数据做锚，再用 web_search 核实品牌/型号

## 坑
1. 无 cookie 时 yt-dlp 报 "Subtitles are only available when logged in" → 拿 cookie（fygo-browser skill 的 CDP 提取）或转录兜底
2. CDP 端口 16003 可能因宿主 socat 丢失而不通（`curl http://172.17.0.1:16003/json/version` 验证），不通就恢复 socat 或直接转录
3. 大 JSON 响应 gzip 崩溃 → curl 加 `-H 'Accept-Encoding: identity'`
4. 音频下载：`yt-dlp -f "bestaudio/best" -x --audio-format mp3` 免登录可用
