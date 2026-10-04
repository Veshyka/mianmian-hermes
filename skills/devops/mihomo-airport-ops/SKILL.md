---
name: mihomo-airport-ops
description: Use when 机场节点失效/换订阅/mihomo 代理不通（搜索失败、连不上外网）。诊断→换订阅→切节点标准化流程。
---

# mihomo 机场运维（标准化流程）

NAS 上 mihomo 容器（镜像 mihomo:local，容器名 mihomo，网络 mihomo-net）是全家出网代理：
- 宿主监听 `127.0.0.1:17890`（容器 7890）；API 映射宿主 `19090`（容器 9090），无 secret
- 依赖方：SearXNG（走 `http://mihomo:7890`）、qB/AB（走 `172.17.0.1:17890`）、棉棉翻墙
- 配置在容器内 `/etc/mihomo/config.yaml`（**无挂载**，改配置 = 宿主生成 → docker cp → restart）

## 症状识别（先诊断再动手）

| 症状 | 说明 |
|---|---|
| SearXNG 搜索报连接错误 | 上游引擎 ConnectError → 多半代理挂了 |
| `curl -x 127.0.0.1:17890 https://www.bing.com` → 000 | 宿主代理转发失败 |
| 代理日志 `dns resolve failed: couldn't find ip` | **机场节点域名失效（NXDOMAIN）→ 需换订阅** |
| 代理日志 `connect error: EOF / context deadline exceeded` | 节点活着但连不上（部分节点抽风）→ 先试切节点 |
| 隧道建立但 TLS 被掐（SSL_ERROR_SYSCALL） | 节点被墙/机场问题 → 切节点或换订阅 |

**注意**：在 Hermes 容器内 `127.0.0.1:17890` 是容器自己的 loopback，不是宿主的！容器内测代理要用 `172.17.0.1:17890`，宿主上才用 `127.0.0.1:17890`。

## 流程一：先切节点试试（5 分钟）

```bash
# API 查当前节点
curl -s "http://172.17.0.1:19090/proxies/%E8%8A%82%E7%82%B9%E9%80%89%E6%8B%A9" | python3 -m json.tool | grep now
# 延迟测试候选节点（香港-直连常活）
curl -s "http://172.17.0.1:19090/proxies/$(python3 -c 'import urllib.parse;print(urllib.parse.quote("🇭🇰|香港-直连"))')/delay?url=https://www.gstatic.com/generate_204&timeout=5000"
# 切换（PUT 到「节点选择」Selector，不是目标节点！）
curl -s -X PUT "http://172.17.0.1:19090/proxies/%E8%8A%82%E7%82%B9%E9%80%89%E6%8B%A9" -d '{"name":"🇭🇰|香港-直连"}' -H 'Content-Type: application/json'
# 验证
curl -s -o /dev/null -w "%{http_code}\n" --max-time 15 -x http://127.0.0.1:17890 https://www.bing.com
```

常见可用节点（2026-08-27 实测）：`🇭🇰|香港-直连`（313ms）。节点名会随订阅变，用延迟测试现测现选。

## 流程二：换订阅（机场域名失效时，约 10 分钟）

**前提**：拿到主人给的新订阅 URL（如 `<SUBSCRIPTION_URL>

1. **下载 + 解码**（订阅是 base64 编码的 trojan:// 链接列表）：
```bash
curl -sL --max-time 20 -o /tmp/new_sub.b64 "新订阅URL"
base64 -d /tmp/new_sub.b64 > /tmp/new_sub_decoded.txt   # 幂等：base64 -d 对纯文本也会通过
wc -l /tmp/new_sub_decoded.txt   # 应 100+ 行
```

2. **转换 + 部署 + 重启**（一键脚本，内含备份/校验/策略组清理）：
```bash
# 脚本在 /opt/data/scripts/mihomo_update_sub.py（宿主可访问：/vol1/1000/<USER>
# 参数1=订阅解码文件路径，参数2=旧配置导出路径（可选，默认容器内导出）
python3 /vol1/1000/<USER> /tmp/new_sub_decoded.txt
# 脚本自动完成：备份旧配置 → 转 trojan:// → 替换 proxies 段 → 清理失效组引用 → yaml 校验 → docker cp → docker restart
```

3. **等容器起来 + 切活节点 + 验证**：
```bash
sleep 12; docker ps | grep mihomo   # 应 Up 非 Restarting
# 用流程一的延迟测试选活节点并切换
# 验证：bing/google/github 走代理 + SearXNG 搜索
curl -s --max-time 25 "http://172.17.0.1:18888/search?q=test&format=json" | python3 -c "import sys,json;print(len(json.load(sys.stdin).get('results',[])),'条')"
```

## 看门狗：让代理自己修（2026-10-01 上线）

**问题**：本机节点列表是**写死的**（`/etc/mihomo/config.yaml` 里内联 100+ 个 trojan 节点，**没有 proxy-providers 段** → mihomo 永远不会自己去拉订阅）。机场一换节点地址/端口，本地整份列表就集体失效，表现是「每次要用都得先修」。

**看门狗**：`/opt/data/scripts/mihomo_watchdog.sh`（宿主同路径 `/vol1/1000/<USER>`），由 Hermes cron `*/30 * * * *` 以 `no_agent` 方式跑（job 名「mihomo 看门狗（代理自愈）」）。行为：

1. 探活：容器内经宿主代理 `172.17.0.1:17890` 拉 `gstatic/generate_204`，失败再试 bing
2. 不通 → **第 1 级自修**：API `/group/节点选择/delay` 测全组延迟，切到延迟最低的活节点（不重启）
3. 还不行 → **第 2 级自修**：读 `cache/mihomo/sub_url` → 拉订阅 → `base64 -d` → `mihomo_update_sub.py` 重写 proxies 段 → `docker restart mihomo`
4. 两级都失败 → **只在此时输出**一行告警（cron 直投主人）。
   **告警节奏（2026-10-03 主人嫌响太勤后改）**：连续不通 ≥3 轮（约 1.5h）才发 + **每日硬上限 1 条**
   （状态 `cache/mihomo/last_alert_day`，恢复时**不删**、次日自然重置）+ 保留 6h 冷却。
   连续计数 `cache/mihomo/down_streak`（恢复即清零，也用于「连续不通才报」）。
   单测：`/opt/data/tmp/test_mihomo_alert.sh`（把真脚本的 `emit_alert` 抠出来测 9 种情形，不碰网络）。
   ⚠️ 旧行为是「恢复时删掉 last_alert」→ 一次故障里反复坏/好就会反复响，这才是主人「怎么老看到这个」的根因。

**输出纪律**：脚本全程不打印节点名/订阅链接/密码/IP（只出 md5 前 6 位与数值），日志 `/opt/data/logs/mihomo_watchdog.log`。

**订阅链接会被机场轮换**：`cat.cn-ping.com/sabusuku` 这类路径失效时返回 **307 跳机场首页**（不是 404），此时能拉到的节点数是 0。

**链接失效时的权威来源 = 路由器**（2026-10-01 实测打通）：旁路由 OpenClash 里存着一条**独立且活的**订阅地址（与 NAS 上那条不是同一条）。取法（URL 只写文件，绝不上屏）：
```bash
# 路由器：读回订阅地址（host=192.168.1.250，key 见 skill immortalwrt-newifi）
ssh -i /opt/data/keys/newifi_ed25519 -o StrictHostKeyChecking=no root@192.168.1.250 \
  'SEC=$(uci show openclash | sed -n "s/^openclash\.\([^.]*\)=config_subscribe$/\1/p" | head -1); uci get openclash.$SEC.address'
# → 写入 /opt/data/cache/mihomo/sub_url（chmod 600），看门狗下轮就会用它重拉
```
⚠️ **2026-10-03 复测更正**：路由器那条与 NAS 上那条**是同一个 URL**（`md5sum` 前 6 位都是 `e2a7b3`），
不再互为「独立备份」。但两边拿到的**端点集不同**——因为 OpenClash 用 `clash.meta` UA（拿活端点），
NAS 用 `mihomo/*` UA（拿到退役的那套）。所以「路由器能通、NAS 不能」在本机其实证明的是 **UA 差异**，不是订阅/账号差异。
路由器那份 `/etc/openclash/config/*.yaml`（clash.meta 格式）可以直接当**活端点集**的旁证。

**拉订阅必须带对 UA（2026-10-03 重大修正，旧结论已失效）**：机场除了按 UA 分流**格式**，还会返回**不同的端点集**！

| UA | 返回 | 端点实测 |
|---|---|---|
| `sing-box/1.9` | base64 的 trojan 列表 | **活**（同分钟裸 TCP 99/102 可连、TLS 68/102 握手成功）← **用这个** |
| `mihomo/1.19.0` | base64 的 trojan 列表（长得一模一样） | **全死**（0/102：26 refused + 76 timeout） |
| `clash.meta` | 原生 clash YAML | 活，但不是 `mihomo_update_sub.py` 吃的格式 |

血泪教训（2026-10-03 实遇）：旧笔记写「`mihomo/1.19.0` → base64 列表」，看门狗一直用它重拉订阅，
于是**每次自愈都把那一整套死端点重装回去**，两级自修全部失败、代理反复坏三天，主人反复收到告警。
**判据不能只看「拉到了几条节点」**——必须对端点做**裸 TCP 采样**（能连上的才算）。
看门狗已改成按序试 UA（`sing-box/1.9` 优先）+ 端点裸 TCP 校验，选能连上的那套。

### 🚨 UA 不只是换格式，还会换**端点集**（2026-10-03 实测，很坑）

同一个订阅 URL，不同 UA 返回**不同的 server 域名**，节点数都是 102：

| UA | 格式 | 端点集 |
|---|---|---|
| `mihomo/1.19.0`、`Mihomo/*`、`mihomo/v1.19.0`、浏览器 UA | base64 | `Sclass-IEPL.catcat321.com` / `drr.tube-cat.com` / `dd12d1.catcat321.com` —— **全丢** |
| `sing-box/1.9` | base64 ✅ | `Sclass-A1` / `Sclass-00` / `ddrr1` / `ddrr2` / `36d` —— **活** |
| `clash.meta`、`clash-verge`、`ClashforWindows`、`clash`、`ClashX`、`Stash` | clash YAML | 同 sing-box 的活端点集 |

**症状**：订阅能拉到 102 节点、mihomo 延迟测试全 0、过代理全 000；但换 `sing-box` UA 秒好。
**判据**：对两组端点做**裸 TCP 采样**——死那套 0/102 能连（26 refused + 76 timeout），活那套 99/102 能连。
**结论**：这是**机场把退役服务器分给了老客户端 UA 的 profile**，不是本机网络/账号问题。
**修**：拉订阅用 `-A "sing-box/1.9"`（base64 + 活端点，正好喂 `mihomo_update_sub.py`）。
**别只信 UA=格式**：一定要在部署前对解出的端点做裸 TCP 校验（`/opt/data/scripts/wd_ep_check.py <decoded.txt>` 输出可达数；0 = 这套是死的，换下个 UA）。看门狗已内置这个循环。

**依赖与路径（2026-10-01 修的两个坑）**：
- 容器系统 python3 **没有 pyyaml**（PEP 668，装不进）→ 转换脚本用专用 venv：`/opt/data/venvs/mihomo-ops/bin/python`（已装 pyyaml）。用 `python3` 跑会 `校验失败: No module named 'yaml'`。
- `mihomo_update_sub.py` 原先把新配置写在**容器的** `/tmp`，却让宿主去 `docker cp` 那个路径 → `lstat: no such file or directory`。已改为**经 ssh stdin 把文件推到宿主**再 cp（`push_to_host()`）；从容器里跑也通了。
看门狗会告警让主人去机场后台复制新链接，存成一行文本到 `/opt/data/cache/mihomo/sub_url`（= 宿主 `.../mianmian/hermes/cache/mihomo/sub_url`，权限 600），它下轮自动重拉——主人不需要手改配置。

## 诊断：怎么判断是「节点失效」还是「污染/账号」

别只看 mihomo 自己的延迟测试（它只说「delay 全 0」，不区分原因）。**在宿主上对配置里的节点端点做裸 TCP 采样**才是硬证据（脚本样例：`/opt/data/cache/node_probe.py`，只输出计数）：

| 观测 | 含义 |
|---|---|
| DNS 解析全 OK，TCP 大量 **connection refused** | 域名/IP 都在，但端口没人监听 → **机场换了节点，本地列表过期**（最常见） |
| TCP 全 timeout、DNS 正常或解析到内网 IP | 疑似污染/封域名 → 换 DNS 或换订阅域名 |
| TCP 能建连但过代理没流量 | 账号/设备数限制或协议问题（设备超限会表现为被踢/无流量，**不会**是 refused） |
| 宿主直连境外 IP 不通、直连国内正常 | 是本机网络层被封，与账号无关 |

注意：这台机器上 mihomo 跑在 Docker 里（容器名 mihomo），**配置在容器内、无挂载**，所以宿主机 overlay 目录里那份 config.yaml 就是「当前生效的那份」；宿主 `/etc/mihomo` 不存在属正常。

## 已踩的坑（必读）

1. **节点名会变**：新订阅「剩余流量：134.15 GB」可能变「93.02 GB」；个别节点名（香港家宽-IEPL → 香港家宽-IEPL 01）会改名——策略组引用旧名会启动失败（`Parse config error: proxy group[0]: ... not found`）。`mihomo_update_sub.py` 会自动清理组里不存在的引用。
2. **mihomo 容器内没有 python3/curl**——转换必须在宿主做，或用 docker cp 传文件。
3. **PUT 切换必须打到「节点选择」Selector 上**，打目标节点会 400 `Must be a Selector`。
4. **写死 IP 的代理配置是定时炸弹**：容器重建后 IP 漂移，代理指向自己 → 全链路挂。SearXNG 已改 `http://mihomo:7890` 容器名；新加依赖方一律用容器名。
5. 重启 mihomo 后如果 `Restarting (1)`：立刻 `docker logs mihomo --tail 5` 看 Parse config error，多半是策略组引用失效节点。
6. 旧配置备份：`/etc/mihomo/config.yaml.bak-20260825`（8/25 节点切换前）、`config.yaml.bak-20260827-sub`（本次换订阅前）。回滚 = 拷回备份 + restart。

## 搜索坏了：先跑脚本，别再人肉试

```bash
bash /opt/data/scripts/search_repair.sh          # 逐环体检：Hermes 配置 → SearXNG 实例 → 引擎池 → mihomo 代理
bash /opt/data/scripts/search_repair.sh --check  # 只看不改
```
日志 `/opt/data/logs/search_repair.log`。

**根因（2026-09-19 实测，三条叠加）**：
1. **引擎池只剩单引擎**——google cse 独活，brave(429)/duckduckgo(access denied)/startpage(CAPTCHA)/google html(被 Google 废弃) 全挂 → 单点。
2. **熔断时间默认极长**——`suspended_times` 默认 AccessDenied/CAPTCHA=86400s(24h)、Google reCAPTCHA=604800s(7天)：引擎一挂停一天到一周，**不会自愈**。"偶尔坏"其实是"长期坏"。
3. **国内引擎走了境外代理**——`outgoing.proxies` 是全局的，国内引擎以境外 IP 抓取 → 必然 CAPTCHA。

**已完成修复（2026-09-19，走官方配置，无外挂）**：
- 配置真身：`/vol2/@apphome/trim.openclaw/data/workspace/searxng-docker/config/settings.yml`
- ⚠️ **`/etc/searxng/settings.yml` 是 bind mount，`docker cp` 覆盖报 `device or resource busy`** → 必须写挂载源（`docker inspect` 查 Destination=/etc/searxng/settings.yml 的 Source），改完恢复属主 `trim.openclaw:trim.openclaw 644`
- 改动：启用实测可用引擎 + 禁用死引擎 + `suspended_times` 压到 600s/300s + `request_timeout` 20→10s + 新增命名网络 `direct` 给国内引擎直连

**引擎现状（实测）**：✅ google cse（主力 ~20 条/查询，走代理）、bing（~10，代理）、sogou + 360search（`direct` 直连）、presearch、marginalia；❌ 已禁用 google html / brave / duckduckgo / startpage / **baidu / quark**（baidu、quark 直连和代理都会被 CAPTCHA，属反爬，别白费劲）。

**官方机制（关键）**：命名网络用 `outgoing.networks.<名>.proxies: {}` 定义（**`proxies` 置空 = 直连**），引擎用 `network: <名>` 引用；生效后用 `NETWORKS[<名>].proxies` 实测验证（容器内 `python -c` 导入 `searx.network.network`）。

**工具**（`/opt/data/hindsight-eval/searxng_fix/` = 宿主 `/vol1/1000/<USER>`）：
- `apply_settings.sh` —— 备份 → YAML 校验 → 写挂载源 → 重启 → 健康检查，**失败自动回滚**（宿主执行）
- `searxng_engine_probe.py` —— 逐引擎实测可用性（只读，容器内即可跑）
- `settings.yml.new` / `settings.yml.bak-*` —— 待生效配置 / 历史备份

**Hermes 侧保持 `web.search_backend = searxng`**（自建优先、不外挂）；`keyless_rescue: true` 是 Hermes 内置的失败兜底，保留即可，不要把它当主路。

## 验证清单（换完必测）

- [ ] `docker ps` mihomo Up（非 Restarting）
- [ ] `docker logs mihomo --tail 3` 无 fatal
- [ ] 宿主 `curl -x 127.0.0.1:17890 https://www.bing.com` → 200
- [ ] `curl -x 127.0.0.1:17890 https://www.google.com` → 302
- [ ] SearXNG 搜索 `curl "http://172.17.0.1:18888/search?q=test&format=json"` → 有结果
- [ ] 记忆更新：当前可用节点/新订阅域名（hindsight_retain）
