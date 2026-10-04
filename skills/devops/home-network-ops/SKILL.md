---
name: home-network-ops
description: Use when 家庭网络出问题（mihomo 代理、IPv6、Tailscale 远程访问、局域网丢包/二层体检）。
metadata:
  hermes:
    tags: [network, proxy, mihomo, padavan, router, ipv6, tailscale, remote-access, fnos]
    category: devops
---

# 家庭网络运维：出网代理 / 路由器 / 入站远程访问

本机（飞牛 NAS + 旁路由）三类网络活：**出网代理**（mihomo 容器，所有联网活依赖它）、**路由器**、**入站远程访问**（主人异地访问 NAS 的 Web 服务）。先读拓扑事实，再动手。

🚨 **路由器已于 2026-10-01 从老毛子 Padavan 换成 ImmortalWrt（Newifi D2，`192.168.1.250`，2026-10-02 由 .2 迁入，原因：ARP 冲突）**。本文件第二节那套 Padavan Web API **已作废**，全部路由器步骤改为见 skill `immortalwrt-newifi`。查网络问题前先确认你面对的是哪台设备。

## 一、出网代理（mihomo 容器）

拓扑（重建会漂移，用前实测）：
- 容器 `mihomo`（镜像 `mihomo:local`，网络 `mihomo-net`，restart=unless-stopped）；宿主代理端口 `127.0.0.1:17890`（映射容器 7890）；API 宿主 `172.17.0.1:19090`（映射容器 9090，无 secret）
- 配置 `/etc/mihomo/config.yaml`（容器内，挂载路径以 `docker inspect` Mounts 为准）
- 依赖方：SearXNG、qB/AB、Hermes 自身的 web_search 与抓取

⚠️ **容器内 `127.0.0.1` 是容器自己的 loopback，不是宿主**：容器内测代理用 `172.17.0.1:17890`，宿主侧才用 `127.0.0.1:17890`。新加依赖方一律写**容器名** `http://mihomo:7890`（Docker DNS 永远解析到当前 IP）——写死容器 IP 是定时炸弹：重建后 IP 漂移、代理指回自己，全链路 ConnectError 且每次开机复现。

节点切换（mihomo API）：
- 节点组名就叫 `节点选择`；`GET /proxies/节点选择` 看 `now` 字段
- 切换必须 **PUT 到「节点选择」这个 Selector**：`PUT /proxies/节点选择` body `{"name":"目标节点"}` → 204；打到目标节点本身报 400 `Must be a Selector`
- 延迟测试 API（`/proxies/{name}/delay`）**不可靠**（节点全挂时 503/504，不代表不可用）→ 以实际转发为准：宿主 `curl -x http://127.0.0.1:17890 https://www.bing.com`（200 才算通）
- 节点名随订阅变，现测现选，别把名字记死

故障排查链（按序，先分层再下钻）：
1. **先分清症状层**：容器内服务报错 vs 代理本身不通——容器内 `curl -x http://172.17.0.1:17890 https://www.google.com`；宿主 `curl -x http://127.0.0.1:17890 ...`。**curl 返回 000 且耗时 <1ms = 没连上代理端口**（多半容器内错用 127.0.0.1）；耗时 5s+ 才是远端问题。
2. **看日志**：`docker logs mihomo --since 2m`，滤掉 `IPCIDR/127.0.0.0/8` 噪音。`dns resolve failed: couldn't find ip` = 机场节点域名 NXDOMAIN（→ 换订阅）；`connect error: EOF / context deadline exceeded` = 域名能解析但节点连不上（→ 先切节点）。
3. **验节点域名用国内 DoH**：`curl -s "https://dns.alidns.com/resolve?name=<域名>&type=A"`（Status 0 = 有解析，3 = NXDOMAIN）。**别用 dns.google**——它本身要梯子，代理挂了就循环依赖。
4. **看配置里的节点域名**：`docker exec mihomo sh -c "grep -E 'name:|server:' /etc/mihomo/config.yaml"`；一个机场常有多个域名，可能部分死部分活，切到活域名下的节点。
5. 全域名 NXDOMAIN、或活域名连上也被挡 → **机场侧问题**，等恢复或让主人查机场公告。

**分层判别（别一上来就换订阅）** —— 在宿主跑脱敏探针，输出只有计数/类别/md5，不打印任何地址：
```bash
python3 /opt/data/skills/devops/home-network-ops/scripts/probe_nodes_sanitized.py <宿主上 config.yaml 路径>
```
| 观测 | 结论 | 处置 |
|---|---|---|
| DNS 全 NXDOMAIN / 不解析 | 机场域名失效 | 换订阅 |
| DNS 正常，TCP 大面积 `refused`/`timeout`、0 个可建连 | **本地节点列表过期**（机场换了地址/端口，本地还是旧快照） | 重拉订阅 |
| TCP 能建连但过代理没流量 / 日志有认证类错误 | 账号或**同时在线的设备数**限制、协议不匹配 | 查机场后台，别换订阅 |
| 配置里「剩余流量：x GB」假节点显示到期/额度尽 | 账号到期 | 续费或换机场 |
| 宿主直连国内也慢/000 | 宿主网络层 | 查宿主网关，别动 mihomo |

**判据：「只有本机的老要修、主人自己的客户端一直稳」= 本地这份是旧快照**。客户端是账号登录每次现取节点，本地这份写死不会自动续——这条现象一出现就按「节点列表过期」直查，不用绕 DNS/账号。设备数限制的特征是「连得上但没流量/被踢」，**绝不会**是端口 `refused`。

- 机场失效/换订阅的标准化流程（订阅转换、策略组引用清理、验证清单）另见 skill `mihomo-airport-ops`；本机脚本 `/opt/data/scripts/mihomo_update_sub.py`、搜索链路体检 `/opt/data/scripts/search_repair.sh`。
- 机场节点故障时 **SearXNG 上游会全部 ConnectError**，表现像 SearXNG 坏了——先验代理，别冤枉服务本身。

## 二、路由器（~~老毛子 Padavan~~ 已被 ImmortalWrt 顶替）

> ⛔ **以下整节为历史存档，勿照做。** 现在的设备是 ImmortalWrt（Newifi D2，`192.168.1.250`），
> 走 **SSH**（不是 Web API）。接入方式、拓扑、安全基线、改配置 SOP 全在 skill `immortalwrt-newifi`。

旧设备（老毛子 Padavan）存档，仅供理解历史配置——管理地址 `http://192.168.123.1`，登录 admin/admin（HTTP Basic）。

⚠️ **SSH 连不上是常态，别死磕**：老毛子 dropbear 与现代 OpenSSH 握手直接卡死超时，加老 KEX 算法也常没用 → **一律走 Web API**。

Web API 路径（无 CSRF token 的老版本）：
1. `curl -s -u admin:admin "http://192.168.123.1/<页面>.asp" > /tmp/page.html` 读配置页，解析 select/radio 的 selected 判断现状
2. 构造**完整**表单 POST 到 `/start_apply.htm`（`action_mode=apply` + hidden 字段 current_page/sid_list + 全部业务字段；**漏字段会保存异常**）
3. 重新 GET 配置页，看 select 的 `selected` 变化确认生效

IPv6 关键字段（`Advanced_IPv6_Content.asp`）：`ip6_service=dhcp6`（Native DHCPv6，光猫拨号/DHCP WAN 选它；static/6in4/6to4/6rd 是隧道类型）、`ip6_wan_dhcp=2`、`ip6_lan_auto=1`、`ip6_lan_radv=1`、`ip6_lan_dhcp=2`、`ip6_dns_auto=1`；`wan_proto=dhcp`（宽带类型，**勿改**）。apply 后 60-90 秒 WAN 自动协商拿到地址，无需重启路由器。

其他：状态页是 JS 动态加载，curl 抓不到 WAN/IPv6 状态 → 从宿主侧验证；**物理 Reset 会破坏固件**，恢复出厂必须走 Web 后台；路由器同时跑 Clash/ss_tproxy（配置 `/opt/app/clash/config/config.yaml`，external-controller `127.0.0.1:9090`）；已装 Entware（`/opt/bin`）。

## 三、入站远程访问（主人异地访问 NAS）

现状：**公网 IPv4 无**（运营商内网 NAT）→ 端口转发/DDNS 不可行；IPv6 运营商有、路由器默认禁用 → 开启后是零成本直连机会。QQ/微信 gateway 已通，本条只管 **Web 服务**的远程访问。

决策树：
```
有公网 IPv4？ ──是──→ 路由器 WireGuard / DDNS+端口转发（最快）
   否
IPv6 可用？ ──是──→ Tailscale（IPv6 直连，免打洞，速度=上行带宽）
   否
            └──→ Tailscale（DERP 中继兜底）/ Cloudflare Tunnel / frp(VPS)
```
本机现状 = Tailscale 为主，且**必须靠直连**：实测两端 IPv6 打洞成功时 52–56ms（`tailscale ping` 显示 `via [240e:...]:41641`）。但**别默认「DERP 够用」**——DERP 全在境外（最近 LA/SEA 177–188ms），且本机国际下行极差（测东京：下行 1.63 Mbps / 上行 43.71 Mbps）→ **一旦回落中继，吞吐塌到个位数 Mbps，只够小流量**。大文件场景要自建 DERP 或 frp。

IPv6 是否已开（宿主侧两步，SSH 棉棉@172.17.0.1）：
```bash
ip -6 addr show | grep inet6 | grep -vE "fe80|::1"                     # 有 240e:/2408: 才是全局地址
curl -6 -s -m 8 -o /dev/null -w "%{http_code}" https://www.qq.com       # 000 = 外网不通
```
未开就去路由器上开（新设备是 ImmortalWrt，步骤见 skill `immortalwrt-newifi`），开完宿主重测。

Tailscale 要点：WireGuard + NAT 打洞（P2P 直连）+ DERP 中继兜底；**流量慢的根因**是手机在运营商严格 NAT 后打洞失败、全走 DERP（国内免费 DERP 节点少）→ 提速靠路由器开 UPnP、IPv6 直连、自建 DERP（需 VPS）。

测线路带宽 / 判直连还是中继 / 量 NAS↔手机的端到端吞吐：见 `references/throughput-testing.md`（含 Ookla CLI 在本机会踩的坑、IPv6 吞吐怎么量、systemd-run 起临时测速服务）。

🔒 **安全红线**：Hindsight 8888、面板 36080 这类**无鉴权**服务在公网暴露前必须先加鉴权（Tailscale 内网访问天然安全；反代必须加 Basic Auth）。绝不把无鉴权服务直接反代到公网。

## 四、局域网二层体检（从 NAS/容器这个独立观测点）

症状：「某台设备持续丢包 / 上了旁路由就全网不稳 / 是不是二层被污染」→ **先只读体检，再改任何配置**（不要先动路由器）。
完整判定表、受控试验步骤、RA 字段解码见 `references/lan-l2-diagnostics.md`；一键探针 `scripts/l2_probe.py`（AF_PACKET 被动抓包：bcast / ra / arp / macprof / arpdetail / dhcp，外加主动 arpprobe）。

铁律（每条都是拿时间换来的）：
- 容器内**没有 `CAP_NET_RAW`**（`ping` 直接 `Operation not permitted`，raw socket 全废）→ 一切主动探测 SSH 到宿主跑：`SSH_ASKPASS=/opt/data/scripts/askpass.sh SSH_ASKPASS_REQUIRE=force ssh 棉棉@172.17.0.1`，宿主侧再 `export SUDO_ASKPASS=/vol1/1000/<USER> sudo -A …`（`sudo -n` 会直接失败）。
- 宿主**没有 tcpdump/arp-scan/nmap/ethtool**：别去装、更别把「工具缺失」当结论 → 用 Python `AF_PACKET` 被动抓包替代（脚本见上）。`busybox` 自带 `arp/arping/ip/ifconfig`，但**必须写全 `busybox arping`**——裸 `arping` 不在 PATH，经 `sudo -A bash -c` 调用会**静默给出空结果**，很容易被读成「没有设备应答」。
- **网关对自己的 ICMP 有控制面限速，会伪装成「二层丢包」**：光猫约 10 pps 封顶（5pps→0% 丢、20pps→38%、100pps→82%），且**与包大小无关**（1200B@20pps 同样丢）。判据：以同速率 ping 一个**被转发**的目的（223.5.5.5 / 8.8.8.8）仍 0% 丢 → 是网关限速，不是 L2。
- **同一 IP 被两个 MAC 应答 = 真会全丢包，不是「延迟抖动」**：用「强制邻居缓存」受控试验定性（单播 ARP 请求可把缓存钉在指定 MAC）。**每一轮都必须回读 `ip neigh` 确认缓存真的指向目标 MAC**，否则同样配置能随机测出 0% 和 100% 两种结果，结论直接作废。
- **采样前先看端口计数器**：`ip -s link show <iface>` 间隔 ≥30s 取两次，errors/carrier=0 且 dropped 不增长 → 观测口自身健康，别再往链路层归因。
- 只读边界：允许被动抓包 + ICMP echo + ARP 请求 + TCP 连接尝试（banner grab）；**不允许发 DHCP DISCOVER**（会取到租约/可能改本机地址）、不允许配置写入与重启。报告里「实测」与「推断」分开写，未验证项单列一节。
- **已解决案例（2026-10-02）**：`192.168.1.2` 曾被一台**天邑康和 CPE**（MAC `90:52:bf:24:53:18`，疑为父亲那边的路由器）抢答 ARP，它只回 ARP、不承载流量 → 网关/DNS 指向 .2 的客户端成段全丢包（受控试验：缓存钉在旁路由 MAC 0% 丢，钉在天邑 MAC 100% 丢；广播解析命中黑洞 MAC 概率≈65%）。
  **处理结果**：旁路由已从 `.2` **迁到 `.250`**（主人明确指令；旁路由自己让位，不用碰光猫也不用碰对方设备）。迁后 `.2` 只剩天邑那台应答，冲突消失；`.250` 0% 丢包、出网正常。
  ⚠️ 若在设备上手动把网关/DNS 写成 `192.168.1.2`，要改成 `192.168.1.250`。判据：`.2` 只由一个 MAC（`90:52:bf`）应答 = 冲突已消失。

## Pitfalls（踩过的）

- 代理配置写死容器 IP → 重建后自环，全链路挂且每次开机复现（改容器名）
- `curl` 000 + <1ms = 没连上代理端口（容器内错用 127.0.0.1），不是远端问题
- 容器内服务报错先验代理再看服务（SearXNG ConnectError ≠ SearXNG 坏）
- 改路由器配置前先记录当前值；IPv6 开启不影响 IPv4 上网（独立协议，风险低）
- ~~老毛子 SSH 不可用（dropbear 握手卡死）→ 走 Web；物理 Reset 破坏固件~~（设备已换，见 skill `immortalwrt-newifi`）
- **设备换代后先改 skill 再排障**：照着已失效的旧设备步骤去操作新设备，比没有 skill 更危险
- **本地这份是静态快照、不会自己续**：配置里没有 `proxy-providers` 段（grep 一下就是 0 条）→ mihomo 从不重拉订阅；节点是内联写死的。症状「隔几周全灭、每次要用先修」不是随机故障，是缺自动续订。收尾时向主人提议挂零 token 的定时探测自愈（不通→重拉订阅+重启→仍不通才告警），判据一条就够：宿主 `curl -x 127.0.0.1:17890` 打 bing 的 http code。
- **节点是多行块，不是一行一个**：`- name:` / `  type:` / `  server:` / `  port:` 各占一行；按行 grep 配对 server+port 会得 0 个节点（白跑一轮），要按 `- ` 分块解析。
- **数节点别用 `/providers/proxies`**：静态内联配置下它的计数与真实节点数对不上（把策略组也算了），且所有 provider 的 `updatedAt` 恒为 `0001-01-01T00:00:00Z`（可当「无自动更新机制」的判据）。数节点用 `/proxies` 按 `type` 计数。
- **读原文会触发 provider 内容风控**：代理/订阅/节点清单是输入侧审核的最高敏类型，命中即 `HTTP 400 Content Exists Risk`（模型不跑、重试无效、会话永久废）→ 一律用脱敏探针（只 print 计数/md5/布尔），必须引用订阅 URL 时用 shell 变量传、不让它出现在输出里；子代理也会被同样打中，任务书里要求它把结论写进只含布尔/数值的结果文件，父级读文件核验。
