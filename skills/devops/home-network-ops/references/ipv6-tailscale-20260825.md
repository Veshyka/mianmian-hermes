# IPv6 开启 + Tailscale 部署实战（2026-08-25）

## 路由器 IPv6 开启（Web API 自动方式，实测成功）

老毛子 Padavan 支持 IPv6（Native DHCPv6/静态/6in4/6to4/6rd），默认禁用。SSH 不可用（dropbear 与现代 OpenSSH 握手卡死），**走 Web HTTP Basic**。

### 表单提交（无 CSRF token）
```bash
# 抓页面拿全部字段（hidden+text 26 个 + radio/select 选中值）
curl -s -m 8 -u admin:admin "http://192.168.123.1/Advanced_IPv6_Content.asp" > /tmp/router_ipv6.html
# 关键字段（urlencode 后 POST 到 start_apply.htm）：
#   ip6_service=dhcp6  ip6_wan_dhcp=2  ip6_lan_auto=1  ip6_lan_dhcp=2
#   ip6_lan_radv=1  ip6_dns_auto=1  ip6_cast_enable=1  napt66_enable=1
#   其余字段保持原值（wan_proto=dhcp、ip6_6to4_relay=192.88.99.1、ip6_sit_mtu=1280 等）
curl -s -m 15 -u admin:admin -X POST -d @/tmp/ipv6_post.txt "http://192.168.123.1/start_apply.htm"
```
- 验证：重抓页面解析 ip6_service select 的 selected 属性（★ 在 Native DHCPv6 = 保存成功）
- 生效：DHCPv6 自动协商 60-90 秒，**无需重启路由器**；成功 = 宿主拿 `240e:` 全局地址 + `curl -6` 通（重庆电信）

## Tailscale 部署（飞牛应用中心套件）

- 套件路径 `/vol2/@appcenter/tailscale`；查状态：`sudo tailscale --socket=/vol2/@appdata/tailscale/tailscaled.sock status`
- **Logged out / NeedsLogin** = 未授权：打开 `login.tailscale.com/a/<code>` 或面板登录按钮，浏览器用 Tailscale 账号授权
- 授权后设备上线（hostname=shy-2，IP 100.73.132.15）
- 子路由：`sudo tailscale --socket=<sock> set --advertise-routes=192.168.123.0/24` → **管理后台 https://login.tailscale.com/admin/machines 点设备 → Edit route settings → 批准**（不批准不生效）
- iPhone 客户端需外区 Apple ID（国区 App Store 无 Tailscale）

## 客户端注意

- **iOS 单 VPN 隧道限制**：Tailscale 和梯子（Shadowrocket/Quantumult）同时开会互相踢——场景分离：在家路由器透明代理；在外二选一手动切换
- DERP 延迟数字（190-215ms 国外节点）只是中继 ping，IPv6 直连后不走，判断用 `tailscale ping`（direct/relay）
- Tailscale 账号建议开两步验证（账号被盗 = 内网可入）
