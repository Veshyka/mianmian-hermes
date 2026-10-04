# B 站音频兜底下载：playurl API 免登录直拿（2026-09-01 实测）

## 触发场景
- yt-dlp 无 cookie 下载音频报 `HTTP Error 412: Precondition Failed`（B 站风控，2026-09-01 实测）
- 加 `--extractor-args "bilibili:prefer_multi_flv=False"` 或走 mihomo 代理均无效
- 需要转录但拿不到 AI 字幕 / 无 cookie

## 可行路径：playurl API 免登录直拿音频流

```bash
# 1. 先拿 cid（view API）
curl -s "https://api.bilibili.com/x/web-interface/view?bvid=<BV>" \
  -H "Referer: https://www.bilibili.com" -H "User-Agent: Mozilla/5.0"
# data.cid

# 2. 拿 dash 音频流（fnval=16 启用 dash）
curl -s "https://api.bilibili.com/x/player/playurl?bvid=<BV>&cid=<cid>&fnval=16&qn=0" \
  -H "Referer: https://www.bilibili.com" -H "User-Agent: Mozilla/5.0"
# data.dash.audio[] → 取 bandwidth 最大的 baseUrl

# 3. 下载裸流（.m4s）
curl -o /tmp/audio.m4s "<baseUrl>" -H "Referer: https://www.bilibili.com" -H "User-Agent: Mozilla/5.0"

# 4. ffmpeg 转 wav 再喂 faster-whisper
ffmpeg -i /tmp/audio.m4s -ar 16000 -ac 1 /tmp/audio.wav
```

## 实测数据（BV1D3th6VEHQ，12:05 视频）
- 拿到 ~170kbps 音频，15.4MB
- faster-whisper-medium CPU+int8 转录 420 段，耗时约 11 分钟

## 配套
- 转录环境：uv venv /tmp/fwenv + faster-whisper，HF_HUB_DISABLE_XET=1
- 转录后专名校正：参考主 SKILL.md 的误识别映射表（空筐→框框、记点→绩点、中策→综测、推勉→推免、三支衣服→三支一扶 等）
