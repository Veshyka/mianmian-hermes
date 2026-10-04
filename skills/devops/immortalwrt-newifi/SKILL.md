---
name: immortalwrt-newifi
description: Use when 运维 Newifi D2 的 ImmortalWrt 旁路由。
metadata:
  hermes:
    tags: [router, immortalwrt, openwrt, newifi, mt7621, bypass-router, openclash, mihomo, extroot]
    category: devops
---

# ImmortalWrt 旁路由（Newifi 3 / D2）运维

本机是家庭网络里的**旁路由**，不是主路由。⚠️ 与 skill `home-network-ops` 里那台**老毛子 Padavan（192.168.123.1）无关**——那台已于 2026-10-01 被本机顶替，该技能的路由器章节已过期。

## 设备与接入（实测）

```
型号    D-Team Newifi D2     /etc/board.json model = d-team,newifi-d2
固件    ImmortalWrt 24.10.6 (r33869-cf234f8de6d5)
目标    ramips/mt7621  内核 6.6.133  mipsel_24kc  442MB RAM
地址    192.168.1.250 (br-lan)   光猫 192.168.1.1
        🚨 2026-10-02 由 192.168.1.2 迁到 .250（ARP 冲突，见"地址冲突"一节）
```

**SSH 从容器直连**（`192.168.1.250:22` 实测通，2026-10-02 复测）：

```bash
ssh -i /opt/data/keys/newifi_ed25519 \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o BatchMode=yes root@192.168.1.250 "<命令>"
```

- 私钥在**容器内** `/opt/data/keys/newifi_ed25519`（600）。来源：`/vol00/WDC WD5000AAKX-22ERMA0/win/down/dsh-ssh/id_ed25519_newifi`（宿主侧，容器看不到 `/vol00`；**与主人 Windows 上 E 盘那把是同一对**）。
- 🚨 **2026-10-02 起 SSH 只认密钥**：`dropbear.main.PasswordAuth='off'` + `RootPasswordAuth='off'`，`/etc/dropbear/authorized_keys` 里装的就是这把钥。以前能进去是因为 `root` 是**空密码**（日志原话 `Auth succeeded with blank password for 'root'`）—— 那等于**同网段任何设备都能 root 登入**，已关。回退办法：`/root/backups/dropbear.<时间戳>` 覆盖回 `/etc/config/dropbear` + `/etc/init.d/dropbear restart`。
- ⚠️ **宿主侧（NAS 的 `棉棉` 用户）现在进不来**（那边没有这把钥 → `Permission denied (publickey)`）。要动路由器一律从**容器**直连（上面的命令），别绕宿主。
- 🚨 **`root` 是空密码 → LuCI 网页后台任何密码都能进**（2026-10-02 实测）：`/etc/shadow` 里 root 的 hash 为空，而 LuCI 判定是 `crypt(输入, 空hash) == 空hash` → **恒真**。实测 `root` + 空密码、`root` + 随便编的错密码，两次都 `luci: accepted login on / for root`；只有不带 cookie 时才是 403。**同网段任何设备都能进管理后台**。修 = 给 root 设密码（LuCI：System → Administration），这属于改凭证 → **要主人拍板**，别自己造一个。
  - ⚠️ 同源：SSH 以前也是靠这个空密码（已改为只认密钥）。`health_all.py` 的 `rt-auth` 会盯这两件事：空密码+PasswordAuth=on → fail；仅空密码（LuCI 敞开）→ warn。
- **脚本用 stdin 管道传，别拼引号**：`ssh … "sh -s" < local.sh`。
- 🚨 **脚本注释只用 ASCII**：中文注释 + BOM 会让 ash 报语法错。
- `scp` 必须加 `-O`（dropbear 无 sftp-server）。

## 拓扑（主人 2026-10-01 亲述 + 实测）

```
光猫 A ──┬── 交换机 a─b ── 电脑 B
         └── 交换机 c ── 路由器 LAN 口 2
```

- 路由器 **WAN 口空置不用**，只用 LAN 口接交换机 → 与光猫/电脑/NAS **同一层 L2、同一网段 192.168.1.0/24**。
- 所以 `network.wan`(proto dhcp) 永远拿不到租约 —— **这是设计，不是故障**，别去"修"它。
- 谁想走旁路由，就把该设备的**网关和 DNS 手动指向 192.168.1.250**。
- 好处：路由器挂了不影响电脑/NAS 正常联网（直连光猫）。

## 地址冲突：为什么从 192.168.1.2 搬到 192.168.1.250（2026-10-02 实测）

**症状**：从同 L2 的 NAS 看，`192.168.1.2` 的 ARP 被**两个 MAC 同时应答**：
- `20:76:93:55:93:33`（本机 Lenovo/Newifi OUI）——真正的旁路由
- `90:52:bf:24:53:18`（**四川天邑康和** OUI，电信 CPE 厂商）——**另一台设备**（主人认定是父亲那边的路由器）

那台设备**只应答 ARP、不转发任何流量**（45s 纯被动 0 帧、不发免费 ARP；TCP 80/22 无响应；ping 100% 丢）。

**后果是实打实的**（受控实测）：把 ARP 缓存强制指向 `90:52:bf` → 去往 `.2` 的流量 **100% 黑洞**（5/5 轮）；指向 `20:76:93` → 0% 丢包（5/5 轮）。而广播解析命中黑洞 MAC 的概率约 **65%**（19:10）。

**修法**：旁路由让出 `.2`，迁到空闲地址（`.250`）。不用碰光猫、也不用碰对方设备。
迁移后实测：`.2` 只剩 `90:52:bf` 一个应答者（冲突消失）、`.250` 0% 丢包、本机出网 0% 丢包（223.5.5.5）、dnsmasq 自动重绑到 `.250:53`。

**注意**：迁完 60 秒内没有任何设备再询问 `.2` —— 说明没有遗留的客户端配置。**若有人在设备上手动把网关/DNS 写成 `.2`，要改成 `.250`。**

## extroot 回退路径（内置 overlay）：必须镜像 + 真演练（2026-10-02 实测）

本机有两层 overlay：

```
/dev/sda        -> /overlay   ext4   29.6G   ← U 盘 extroot（upperdir=/overlay/upper）
/dev/mtdblock6  未挂载        jffs2  22.4M   ← 内置 overlay(rootfs_data) = 回退层
```

U 盘坏掉/被拔 → block-mount 找不到 UUID → **自动回退到内置 overlay 启动**。所以内置那份配置必须**是能用的**：否则路由器能起来却残废（地址撞黑洞、网桥塞错、WiFi 变开放）。

**2026-10-02 修复前，内置那份是重置前的旧货，四个致命项**：

| 项 | 旧（内置） | 新（镜像自当前运行配置） |
|---|---|---|
| `network.lan.ipaddr` | `192.168.1.2`（撞那台黑洞设备） | `192.168.1.250` |
| `network.globals.ula_prefix` | `fd65:e30e:54f2::/48`（NAS 指向的 IPv6 DNS 会失配） | `fd80:d338:9d87::/48` |
| `br-lan` ports | `eth0 lan1..4 wan`（DSA 下会断网） | `lan1 lan2 lan3 lan4` |
| `wireless` | SSID `ImmortalWrt` + `encryption none`（**开放网络**） | `shy` + `psk2` |
| `system` NTP | 域名（无 RTC → 时钟↔DNS 死循环） | 纯 IP |

### 怎么改（可复用）

```sh
M=/mnt/jffscheck
mount -t jffs2 /dev/mtdblock6 $M            # 先 ro 看、再 -o remount,rw 改
ls $M/upper/etc/config/                     # 注意文件在 upper/ 下（overlay 布局）
cp -a $M/upper/etc/config/. /root/backups/int-overlay-$(date +%Y%m%d-%H%M%S)/   # 先备份
for f in network wireless system dhcp firewall dropbear; do cp /etc/config/$f $M/upper/etc/config/$f; done
mkdir -p $M/upper/etc/dropbear && cp /etc/dropbear/authorized_keys $M/upper/etc/dropbear/
cp -a /etc/hotplug.d/iface/. $M/upper/etc/hotplug.d/iface/     # DNS 自愈脚本
cp -a /usr/sbin/zram-swap.sh $M/upper/usr/sbin/; cp /etc/rc.local $M/upper/etc/rc.local
cmp -s /etc/config/network $M/upper/etc/config/network && echo identical
sync; umount $M
```

**内置 fstab 里的 extroot 条目要保持 `enabled='1'`** —— boot 时读的是**内置那份** fstab，关掉就等于禁用 extroot。

### 演练（做一次，别只嘴上说"有保险"）

内置 overlay 的 `fstab.overlay.enabled` 改 `0`（= 模拟 U 盘没了）→ `reboot` → 验 `df -h /overlay` 是不是 `/dev/mtdblock6 jffs2 22.4M`；再改回 `1` → `reboot` → 验是不是 `/dev/sda ext4 29.6G`。

**实测（2026-10-02）**：回退启动 **约 40 秒**起来，`.250` / ULA / 网桥 lan1-4 / WPA2 / 外网 0% 丢包 / dnsmasq 全正常；切回 extroot **约 60 秒**。

🚨 判防火墙**只看 `nft list ruleset | wc -l`**（正常 217 行）：`/etc/init.d/firewall status` 会报 `active with no instances`、`running` 直接返回非零 —— **假警报**，别被骗着去"修防火墙"。

📡 这台路由器的运行态已在 `scripts/health_all.py` 里（`rt-ssh`/`rt-ip`/`rt-ports`/`rt-dhcp`/`rt-nft`/`rt-net`/`rt-dns` 7 项 + 自检 6 例）：地址漂回 `.2`、网桥被塞 `eth0`、开了 DHCP、nft 空、外网断 都会被点名。

## 改 LAN IP 的安全作业法（本机已验证，可复用）

远程改自己的管理地址 = 最高风险操作。本机用过的配方：

1. 飞行前 grep：`grep -rn "192.168.1.<旧IP>" /etc/config /etc/hotplug.d /etc/rc.local` 确认没有别的引用。
2. 先在路由器上挂一个 **180 秒自动回滚**看守，再改 IP：
```sh
# 关键：本机 BusyBox 没有 nohup！用 setsid 开新会话，SSH 断链也杀不掉它
setsid sh -c 'sleep 180; [ -f /tmp/ipmove.done ] || { uci set network.lan.ipaddr="<旧IP>"; uci commit network; /etc/init.d/network reload; }' >/dev/null 2>&1 &
```
3. `uci set network.lan.ipaddr=...; uci commit network; /etc/init.d/network reload`（**reload 不用 restart，不重启系统**）。
4. 立刻用**新地址**复测 SSH/ARP/ping/出网；确认无误再 `touch /tmp/ipmove.done` **取消回滚**，并等到过了 180 秒截止点再复查一次 IP 是否还在新地址（证明取消真的生效，而不是被自动回滚）。
5. 脚本用 stdin 管道传（`ssh … "sh -s" < x.sh`）。

⚠️ 本机 BusyBox 另一坑：**`nohup` 不存在**（`setsid` / `start-stop-demon` 在）。

## 优化设置（2026-10-02 实测，都可复用）

### 2.4G 信道：按「强信号邻居数」选，不是按总数

```sh
iwinfo phy0-ap0 scan | awk '/Channel: /{c=$NF} /Signal:/{t[c]++; if ($2+0 > -75) s[c]++} END{for (k in t) printf "ch%-3s total=%-3d strong=%d\n", k, t[k], s[k]+0}'
```

本机实测：**ch1 = 18 个 AP / 10 个强信号**（原位置）、ch11 = 14/5、ch6 = 10/5、**ch13 = 3/2** → 切 ch13，客户端信号 **-59 → -56 dBm**。
`uci set wireless.radio0.channel='13'; uci commit wireless; wifi reload`（WiFi 断 ~5-10 秒，Ethernet/SSH 不受影响）。
**5G 别乱动**：本机 ch36 / VHT80 只 1-2 个邻居、866Mbps、Link Quality 68/70。

### DNS 上游：先搞清楚谁在写 `resolv.conf.auto`

`/etc/init.d/openclash`（随 boot 被调用）在 `wan_dns`/`wan6_dns` 为空时（本机 WAN 口空置 → **恒为空**）会**写死** `/tmp/resolv.conf.d/resolv.conf.auto` 为 `119.29.29.29 + 8.8.8.8`。所以那个文件里写什么，**不**代表你配的什么。

改法（让那个文件不再参与决策）：
```sh
uci set dhcp.@dnsmasq[0].noresolv='1'
uci -q delete dhcp.@dnsmasq[0].server
uci add_list dhcp.@dnsmasq[0].server='223.5.5.5'
uci add_list dhcp.@dnsmasq[0].server='119.29.29.29'
uci commit dhcp; /etc/init.d/dnsmasq restart
# 验证：grep -E "^(server|no-resolv)" /var/etc/dnsmasq.conf.*
```
🚨 **8.8.8.8 在这条线上不是被劫持**（查不存在的域名它老实回 NXDOMAIN），但会给国内站点返回**海外 CDN**（`www.baidu.com → www.wshifen.com`）—— 判断该不该留看的是地理答案，不是通不通。

### 垃圾 / 回收站 / 备份的规矩（本机，主人 2026-10-02 明确）

- 垃圾**不要 rm**：放进容器回收站 `/opt/data/.local/share/Trash/files/`，`scripts/backup_prune.py`（cron 周日 04:30）**7 天**后自动清。
- **备份只放备份文件夹** `/opt/data/backups/<用途>/`；别到处撒 `.bak`（pruner 按家族只留最新 5 个）。
- 路由器配置快照**别用 `sysupgrade -b`**（本机 29.8MB，90% 是 `/etc/openclash/core` 里的 clash 内核二进制）→ 用：
  ```sh
  tar -czf /tmp/router-cfg-lite.tar.gz -C / etc/config etc/dropbear etc/hotplug.d etc/rc.local usr/sbin/zram-swap.sh
  ```
  实测 **10.7KB**，落在 `/opt/data/backups/router/`。
- 误操作留下的 `/upper` 树**两个 overlay 各有一份**（U 盘 `/overlay/upper/upper`、内置 overlay 的 `upper/upper`）。搬走后合并视图里 `/upper` 仍会列出但**里面 0 项**（内核缓存的空 dentry，重启即消失）——别以为没删干净，也别再往里装钥匙。

### 插件可行性（先实测再说）

`adblock-fast` 的列表源实测：`adguardteam.github.io` **200/1.0MB** ✅、`raw.githubusercontent.com/StevenBlack/hosts` **200/793KB** ✅、`cdn.jsdelivr.net`(hagezi) **403** ❌
→ **没代理也拉得动列表**（先前「必然拉不动」的说法是错的）。但它会改所有拿这台当 DNS 的设备（含 NAS 的 IPv6 DNS 路径）→ 装之前先跟主人说一声。

## 广告过滤：adblock-fast（2026-10-02 装，含实测要命的坑）

装：`opkg update && opkg install adblock-fast luci-app-adblock-fast`（LuCI 页面在 服务→AdBlock-Fast）。

**只开一条列表就够**（主人要求「规则不要多」）：`uci show adblock-fast` 里 16 个 `config file_url`
段默认全关，只把一段置 1。包默认那条 jsDelivr 源从这条线 **403**；本机实测可用：
`https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts` → 41,588 条。
关键配置：`enabled=1`、`dns=dnsmasq.servers`（不用换 dnsmasq-full）、`compressed_cache=1`。

硬件代价实测：内存 48.7M→51.5M（**+2.9MB**，脚本跑完即退不常驻），overlay 增 ~2MB。

### ⚠️ 最大的坑：无缓存重启后过滤静默失效

`/etc/rc.d/S20adblock-fast` 启动太早（LAN/DNS 未通）→ `Failed to download ...`。
它自己的 `service_triggers()` 只在 `/dev/shm/adblock-fast` 非空时才挂 wan 触发，
而**这台没有在用的 WAN 口 → 永远不触发 → 没有任何重试**。`compressed_cache` 那份 .gz
被开机那次消费掉，指望不上第二次开机。
症状（一眼判定）：`wc -l /var/run/adblock-fast/dnsmasq.servers` 报 no such file，
`nslookup doubleclick.net 127.0.0.1` 不返回 NXDOMAIN。

修：加 `/etc/hotplug.d/iface/90-adblock-fast`（ifup 后补启动，**必须单飞**——
本机 2.4G/5G 各触发一次 ifup，不加锁会有 3 个实例互踩临时文件全失败，
日志症状是 `try 1` 出现两次）：

```sh
[ "$ACTION" = "ifup" ] || exit 0
mkdir /tmp/adblock-boot.lock 2>/dev/null || exit 0
( i=0; while [ $i -lt 5 ]; do sleep 20; i=$((i+1)); \
  [ -s /var/run/adblock-fast/dnsmasq.servers ] && exit 0; \
  pgrep -f "adblock-fast start" >/dev/null 2>&1 && continue; \
  ping -c1 -W2 223.5.5.5 >/dev/null 2>&1 || continue; \
  logger -t adblock-fast-boot "starting (try $i)"; \
  /etc/init.d/adblock-fast start >/dev/null 2>&1; done ) &
```

它建议装 `gawk grep sed coreutils-sort`，但**本机 ImmortalWrt 源里没这几个包**
（`Unknown package`），不装也能用，只是下载+排序要 ~50s。

### 覆盖面（别误判）

只有**把 DNS 交给这台路由器的客户端**被过滤 = 它的 WiFi 设备（`:53` 被
`redirect to :53` 强制、`:853` 被 reject 防 DoT 绕过）。走网线的设备（含 NAS）直接问光猫，
过滤不到。反过来这正是「旁路由挂了不影响上网」的原因。

验证三连：规则数 >1000、`nslookup doubleclick.net 127.0.0.1` 出 NXDOMAIN、
`nslookup www.baidu.com 127.0.0.1` 正常。健康检查里对应 `rt-adb` 项
（开着但规则为 0 或不拦 → warn）。

## 安全基线（每次动配置前后必跑）

```sh
echo "ip: $(uci get network.lan.ipaddr)  mask: $(uci get network.lan.netmask)"
echo "gw: $(uci get network.lan.gateway)  dns: $(uci get network.lan.dns)"
echo "dhcp-ignore: $(uci get dhcp.lan.ignore)"     # 必须 = 1
echo "wan: $(uci get network.wan.proto)"
ip -4 addr show br-lan | grep 'inet '
```

期望值：`192.168.1.250 / 255.255.255.0 / gw 192.168.1.1 / dns 192.168.1.1 / ignore=1`。

## 🚨 别信"把 eth0/wan 加回 br-lan"这类建议

本机是 **DSA**（无 `swconfig` 二进制，`/etc/board.json` 里 lan.ports = lan1-4、wan 独立）。
DSA 下 **`eth0` 是 CPU conduit**，塞进 bridge 会出事。

实测 `/root/backups/network.20260422-093633`（重置前原始配置）：`br-lan` ports **就是 `lan1 lan2 lan3 lan4`**，从来没有 eth0/wan。

👉 **`br-lan` = lan1-4 是正确状态，不要动。**

## 会反复踩的坑（都是实测换来的）

### opkg / 依赖
- **musl 下的假依赖**：`librt`、`libstdcpp6` 这类"找不到"是**纯元数据假象**——musl 把实时函数并进了 libc，`librt.so` 本就不该存在，而 opkg 不解析虚拟 Provides。用 `opkg install --force-depends <包>`。判据：`readelf -d <so> | grep NEEDED` 里没有 librt。
- **TLS 假故障**：同一个 URL 裸 `wget` 能下、`opkg` 报 `Failed to send request: Operation not permitted` → 是 opkg 内置 wget 的 TLS 校验缺陷，**不是网络问题**。加 `--no-check-certificate`。
- 源用 USTC 镜像（`/etc/opkg/distfeeds.conf`）。

### extroot（U 盘扩容）
- 🚨 **复制的是 `upper/` 的内容，不是 `/overlay` 本身**：
  - ✅ `cp -a /overlay/upper/. /mnt/usb/upper/`
  - ❌ `cp -a /overlay/. /mnt/usb/upper/` → 数据被套进 `upper/upper/`，overlayfs 读不到，表现为"配置全丢"
- 本机 **`/overlay/upper/upper` 这个残留目录还在（3.0M，2026-10-01 那次失误留下的）**。系统当前运行正常（`upperdir=/overlay/upper`），该目录是死数据。
- fstab 用 **UUID**，不用设备名。安全网：block-mount 找不到 UUID 会**自动回退内置 overlay** → 永远保证内置 overlay 里有正确的 `192.168.1.2` + SSH 公钥。

### zram
- 🚨 `comp_algorithm` 在 zram0 初始化后**无法修改**，所有算法都被拒（`zram: Can't change algorithm for initialized device`）→ **内核默认 `lzo-rle` 是唯一选择**。
- 🚨 用 `#!/bin/sh /etc/rc.common` 写 init 脚本时**可执行调用会静默失败**（`start()` 从不被调、无报错）→ 改用 `/etc/rc.local` 直接调用普通脚本。

### 时钟 / DNS 循环依赖（本机曾因此彻底下不了包）
- 设备**没有 RTC**（无 `/dev/rtc`），开机靠 NTP，而 **NTP 走 DNS**。DNS 一坏 → 时钟永久错 → 所有 HTTPS 证书"尚未生效"。
- 解法：`system.ntp.server` 用**纯 IP**（本机：203.107.6.88 / 120.25.115.20 /.21 / 114.118.7.161）断开循环。
- ⚠️ 时钟偏差大时 `ntpd` 日志的 offset 会是天文数字，先看 `date` 再信证书报错。

## dnsmasq 上游与那条 ICMP 告警（同一根因，2026-10-01 定位）

`dhcp.@dnsmasq[0].resolvfile = /tmp/resolv.conf.d/resolv.conf.auto`，**dnsmasq 的上游就是它**。

- 现状文件内容：`119.29.29.29` + `8.8.8.8`，**与 `uci get network.lan.dns`(192.168.1.1) 不一致**。
- 来源已定位：这两个地址正是 **OpenClash 配置里的默认 DNS**（`/etc/config/openclash` 第 77/96/150 行）。`/root/backups/resolv.conf.auto.pre-openclash`（15:33）里还是 `192.168.1.1 + 223.5.5.5` → 是 OpenClash 装机动作写的，**即使 `enable=0`**。
- **同一条根因**解释了内核告警 `icmp: detected local route for 192.168.1.2 during ICMP sending, src 8.8.8.8`（以及 `src 119.29.29.29`）——src 正是这两个上游，dnsmasq 在探测它们。
- 该告警**只在开机后约 93 秒内爆发 77 条，之后不再出现**（29 分钟后复查：`tail -1` 仍是 `[93.409864]`）。属**一次性启动噪声，不是持续故障**。
- 危害：8.8.8.8 在国内不可达 → dnsmasq 白等一次。想收拾就把上游改回 `192.168.1.1`（与 UCI 意图一致）。

## 机场订阅（2026-10-01 实测）

订阅 URL 是 **clash 格式机场**（`cat.cn-ping.com`）。

**关键规律：返回格式由 UA 决定，不是内容固定**

| UA | 大小 | 格式 |
|---|---|---|
| `clash.meta`（**OpenClash 默认 `sub_ua`**） | 78,537 B | **clash YAML** ✅ |
| `clash-verge/v1.0`、`ClashforWindows/0.20.39` | 78,537 B | clash YAML ✅ |
| `mihomo/1.19.0` | 28,240 B | base64 的 trojan 链接列表 |
| 浏览器 UA | 28,240 B | base64 |
| **空 UA（wget 默认）** | **0 B** | 无 |

👉 **空 UA / wget 裸请求会拿到 0 字节。** 但 OpenClash 默认发 `clash.meta`，能拿到原生 YAML，**不需要改 `sub_ua`**。

🚨 **别把“某次超时”归因到 UA 上。** 同一个 UA 会时而 200（2 秒）、时而 000（60 秒超时）——**请求是间歇性超时的**，这才是前任说的“下载不可靠”的真身。判据：多跑几次 + 拉开间隔（`sleep 5`）再下结论。

🚨 **绝不用 `sh <脚本>` 调 OpenClash 的脚本。** 它们全是 `#!/bin/bash`，用了 `${PIPESTATUS[0]}` 等 bash 专有语法。用 BusyBox ash 跑会**静默失败**，而且报错位置极具误导性——实测报的是 `basename FILE...  Usage` 用法错，真凶却是解释器（那句守卫失效 → `DOWNLOAD_PATH` 为空 → `basename ""` 报用法）。

正确调法（让 shebang 生效）：
```sh
/usr/share/openclash/openclash.sh <订阅名>     # ✅ 直接执行
sh /usr/share/openclash/openclash.sh <订阅名>  # ❌ 会静默失败
```

### 用 CLI 加订阅并下载（实测 2026-10-01 可复现）
```sh
uci add openclash config_subscribe > /tmp/newsec
SEC=$(cat /tmp/newsec)
uci set openclash.$SEC.name='dsh'          # 决定文件名 /etc/openclash/config/dsh.yaml
uci set openclash.$SEC.address="$URL"    # URL 从文件读，别上命令行
uci set openclash.$SEC.enabled='1'
uci set openclash.$SEC.sub_ua='clash.meta'
uci commit openclash

/usr/share/openclash/openclash.sh dsh    # 前台跑，等 15-60 秒
```
产出 `/etc/openclash/config/dsh.yaml`，**78,537 字节 / 102 个 trojan 节点**，日志出现 `Update Successful!` 即为成功。

### 验证产出的命令（都不碰内容）
```sh
wc -c < /etc/openclash/config/dsh.yaml          # 大小
grep -ac '^proxies:'      /etc/openclash/config/dsh.yaml
grep -ac 'type: trojan'   /etc/openclash/config/dsh.yaml   # 节点数
head -1 /etc/openclash/config/dsh.yaml          # 格式（应为 mixed-port: 7890）
```

### 本机 BusyBox 的坑
- `cat -n` **不支持** → 用 `cat`
- `timeout` **不存在** → 用 `&` + 轮询 `kill -0` 自己实现
- ruby **在**（`/usr/bin/ruby`）——OpenClash 用它校验/处理 YAML，别乱卸

## 用路由器当「活订阅」的备份源（2026-10-01 实测）

当 NAS/别的设备上那条订阅链接失效时，**路由器 OpenClash 里存的那条往往是活的**（不同 URL）。读法（URL 只写文件、绝不上屏）：

```sh
SEC=$(uci show openclash | sed -n "s/^openclash\.\([^.]*\)=config_subscribe$/\1/p" | head -1)
uci get openclash.$SEC.address      # 重定向到文件，不要打印
```

与 NAS 那份比对只用 md5（`md5sum | cut -c1-10`）。拉取要用对的 UA：`mihomo/1.19.0` → base64 节点列表；`clash.meta` → clash YAML。

## 用 API 切节点（不碰 Web UI）

运行配置 `/etc/openclash/dsh.yaml` 里有 `external-controller: 0.0.0.0:9090` 和 `secret:`；本机 **ruby 没有 cgi/json 模块**，但 JSON 是 YAML 子集 → `YAML.load(`curl ...`)` 能解析。要点：
- **策略组 type 是小写** `select` / `url-test`，不是 `Selector`；按 `Selector` 过滤会得出「没有组」的假结论。
- 组延迟测试 `/group/<urlencoded>/delay` 在本机内核上可能返回空 → 退化为逐节点 `/proxies/<n>/delay`（实测 8 个新加坡节点全活）。
- URL 编码自己写（`gsub(/[^A-Za-z0-9\-_.~]/){'%%%02X' % c.ord}`），别 require 'cgi'。
- 切完回读 `now` 确认；配置里有 `profile: store-selected` → **选择会持久化**，重启不丢。
- 验证：`curl -x "http://$AUTH@127.0.0.1:7890" https://www.google.com` 期望 302（AUTH 从配置 `authentication:` 段取）。

## 坑：本机 BusyBox 工具是坑王（已逐个验证）

**以下命令/写法在这台路由器上会给假结果或直接报错，别用：**

| 写法 | 真相 | 替代 |
|---|---|---|
| `/dev/tcp/host/port` | **ash 不支持**，永远报 nonexistent directory（曾被误读为“节点全部不可达”） | `curl -sS http://host:port` 看报错类型 |
| `nc -z host port` | BusyBox nc 不支持 `-z`，**永远返回“closed”** | 同上 |
| `cat -n file` | 不支持 `-n` | `cat file` 或 `sed -n` |
| `timeout N cmd` | **不存在** | `cmd &` + `kill -0` 轮询 |
| `nslookup` | 容器侧没有 | 手写 DNS 查询（Python/socket） |
| `python3` | 路由器上**没有** | ruby（但只有 yaml，**无 socket/json**） |
| ruby `require 'socket'/'json'` | LoadError | 用 curl/awk/sed 代替 |

🚨 **教训：报错信息可能离真因两站远。** 同一个“失败”可能是探测工具坏了，不是被测对象坏了。**换一种方法复测再下结论。** 本机已被假结果坑过 4 次。

## 🚨 旁路由最关键的一条：`router_self_proxy` 必须为 0

**症状**：显式代理端口能用，但透明代理（LAN 客户端路径）全超时，国内网站也解析失败。

**内核日志的特征证据**（同一个内核、同一个策略组，两边对照）：
```
[TCP] 127.0.0.1:51914(curl) --> www.google.com:443 ... using 节点选择[...]      ← 成功
[TCP] dial ... 192.168.1.2:39258(curl) --> www.google.com:443 error: i/o timeout  ← 失败
[TCP] dial DIRECT ... www.baidu.com:443 error: dns resolve failed: context deadline exceeded
```

**根因**：`router_self_proxy=1` 让路由器自身流量也走代理 → **clash 拨号节点的出站流量被自己的 tproxy 抓回来** → 转圈到超时。国内 DNS 失败同理（clash 发给 223.5.5.5 的查询被自己抓走）。

**修法**：`uci set openclash.config.router_self_proxy='0'` → 路由器自身直连，**转发链（`openclash` chain / PREROUTING）不受影响**，LAN 客户端照旧被代理。旁路由本来就该这样。

**验证**：修前 baidu `000`/10s 超时 → 修后 `200`/2.7s；修后路由器自己访问 google 返回 `000` 是**正确行为**（不走代理）。

## 坑：节点域名有多个 A 记录，其中一部分是死 IP

**症状**：代理忽然全挂，延迟测试 `all proxies timeout`；重启后立刻恢复。

**证据**：同一域名 `ddrr1.tube-cat.com` 在不同时刻解析出：
- `43.207.189.22` —— 容器侧验证 **OPEN**
- `54.250.174.250` —— 内核报 `i/o timeout`

**根因**：mihomo 缓存了节点域名的 A 记录，**缓存到死 IP 就整条链路全废**，直到缓存过期。不是配置错，不是节点死。

**处理**：`/etc/init.d/openclash restart` 清缓存即可恢复（实测重启后连续 4 次 `302`，1.7~2.7s）。

## 验证代理是否真的通（可复现）

运行时配置里有 `authentication`，**不带鉴权直连代理端口会返回 `407`**（这个 407 反而是“链路通”的好信号）。

```sh
AUTH=$(awk '/^authentication:/{f=1;next} f&&/^[a-z]/{f=0} f&&/:/{gsub(/^[[:space:]-]+/,"");print}' /etc/openclash/dsh.yaml | head -1)
curl -s -o /dev/null -w '%{http_code} %{time_total}s' -x "http://$AUTH@127.0.0.1:7890" https://www.google.com
# 期望 302
```

**端口**：内核日志报的 mixed 口是 7893，但**当 HTTP 代理用要走 7890**（实测 7893 不通）。

**客户端 DNS 验证**（从局域网任一机器，手写查询）：
```
问 192.168.1.2 → 被墙域名返回 198.18.x.x（fake-ip）
问 192.168.1.1 → 返回真实 IP
```
注意 fake-ip 模式下**国内域名也给假 IP**（如 baidu → 198.18.0.4），这是正常的，clash 在连接时再按规则决定直连还是代理。

## 没用的尝试（别再走一遍）

`bypass_gateway_compatible=1`（即官方“旁路网关兼容”）**对本症状无效**——那个只是排障兜底，不是治本。已改回 0。

### 诊断抓手
- `curl https://<域名>` 超时但 `curl https://223.5.5.5/` 正常 → **是 DNS/域名解析的事，不是 curl 坏**
- 看 `dhcp.@dnsmasq[0].resolvfile` 指向的文件内容是否含国内不可达的 `8.8.8.8`；该文件会被 netifd 重建（实测会自愈回 `192.168.1.1`）
- `/etc/init.d/dnsmasq restart` 会让 netifd 按 UCI 重建它（比手改文件正规）

### 验证订阅时的纪律（血泪）
🚨 **验证脚本里绝不能 `head`/`cat` 响应体。** 机场按 UA 返回的可能是**一整行 base64**，`head -1` 会把全部节点凭据（UUID/服务器/端口）打进日志。**只报 http code + size + 格式判类。**

## 改配置的作业纪律（强制，前任在这里翻过车）

```
① 备份  cp /etc/config/<file> /root/backups/<file>.<时间戳>
② 预演  先只读展示"将要改什么"，确认目标值
③ 单项  只改一项
④ 验证  该项生效 + 安全基线未变
⑤ 重启验证（如需要）：单独一轮，事前打印回退路径，事后用独立手段确认
⑥ 记录  留命令输出，区分"实测"与"推断"
```

🚨 **一次只改一项。重启类验证必须单独进行。** 前任把 zram 自启 + extroot 启用 + 重启验证打包在同一轮，导致掉线后**无法归因**。

🚨 远程管理无法物理接触的设备时，**重启是最高风险操作**，必须保证赌输也能拿回控制权。

### 绝对禁止
1. 改 LAN IP / 掩码 / 网关（**当前地址 `192.168.1.250`**；没有明确指令 + 自动回滚看守，一次也不许改）；**禁止开 LAN 的 DHCP**（`dhcp.lan.ignore` 必须为 `1`）
2. `firstboot` / `mtd erase` / `sysupgrade -n` 等清配置命令
3. 把两个及以上变更与重启放进同一轮
4. 未验证回退路径就重启
5. 遇到死结**立即停止并报告**，不要反复试

## 只读体检抓手（2026-10-02 实测，排查"全网丢包"用）

- **`dhcp.lan.ignore=1` 只关 DHCPv4，不关 IPv6 RA**：`strings /usr/sbin/odhcpd` 里**没有 `ignore`**，`dhcp.lan.ra='server'` 照旧生效。判据是 `/tmp/odhcpd-piofolder/br-lan`（RFC9096 PIO 文件）非空 + 日志每 5~9 分钟一条 `No default route present, setting ra_lifetime to 0!`。
- **但 `ra_lifetime=0` → 不抢默认网关**（本机无 v6 默认路由，odhcpd 自己降级）。跨机验证：宿主 `/proc/net/if_inet6` 里出现 `fd80d3389d87...` 的 ULA 地址 = RA 真被同 L2 主机接受；宿主的 v6 默认路由仍只指向光猫。
- **单口 = 无环**：`/sys/class/net/{lan1..4,wan}/carrier` 看谁插着线（本机常态**只有 lan3=1**）；再用 `brctl showmacs br-lan` 确认没有 MAC 同时挂在两个口。STP 关着也不怕，单口不可能成环。`ForwDatagrams=0`（`/proc/net/snmp`）= 本机不在任何人的数据路径上。
- **ICMP 不可作判据**：光猫 192.168.1.1 对 ICMP echo 丢 40~60%，同一时刻 TCP 12/12 (0.48ms)、UDP/53 20/20。**必须换 TCP/UDP 复测**再下"L2 丢包"结论。
- **无线占用用 `iw dev phy1-ap0 survey dump`**（读 `channel busy time`/`channel transmit time`，两者相除得"本机发射占信道比例"）。实测 2.4G ch1 busy 28.7% 但**本机只贡献 0.35%** → 拥挤来自别的 AP。`iw dev <if> station dump` 看客户端 `tx retries` 比例。
- `br-lan.rx_dropped` 常年约 **1 包/秒** 缓慢增长（机制未证实，本机无 tcpdump 无法归因）——**不是故障**，别被它带偏。

## 未做 / 待办
- OpenClash 已装（0.47.071 + `clash_meta` Mihomo Meta alpha-ge183c58 mipsle）但 `enable=0`，**缺机场订阅链接**。
- ⚠️ **2026-10-02 更正**：两个 WiFi **不是开放网络**——SSID 均为 `shy`、`encryption='psk2'`（WPA2-PSK，2.4G ch1 HT20 / 5G ch36 VHT80）。早期记录的 `encryption='none'` 已过时。
- `/overlay/upper/upper` 残留目录未清。
- 前任的 skill `openwrt-one-observe`（Go 板 OpenWrt One 的只读观察法）值得抄的是**读法**："不要报只有单一测量的结论"、只读不装守护进程、报温度必须同时报余量。硬件部分（mt7915）不适用于本机的 mt7621。
