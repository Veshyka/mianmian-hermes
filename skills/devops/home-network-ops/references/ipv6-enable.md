# 路由器 IPv6 开启完整实操（2026-08-25）

老毛子 Padavan（192.168.123.1，admin/admin，HTTP Basic）开启 IPv6 的完整记录。

## 结论

- 宽带本身有 IPv6（重庆电信 240e 段），只是路由器默认禁用
- 开启 Native DHCPv6 后 60-90 秒 WAN 自动协商，宿主拿到全局地址，无需重启路由器
- 目的：Tailscale 双端 IPv6 直连（免打洞免 DERP）

## 表单字段全集（Advanced_IPv6_Content.asp → start_apply.htm）

```
current_page=Advanced_IPv6_Content.asp
next_page=&next_host=&group_id=
sid_list=IP6Connection;General;
action_mode=apply&action_script=
wan_proto=dhcp                    # 宽带类型：dhcp（光猫拨号）勿改
hw_nat_mode=4
ip6_service=dhcp6                 # ★ 关键：禁用→Native DHCPv6
ip6_ppe_on=0
ip6_wan_if=0
ip6_wan_dhcp=2                    # 2=从两端（DHCP WAN 最通用）
ip6_6rd_dhcp=0
ip6_wan_priv=0
ip6_lan_auto=1                    # LAN 自动
ip6_lan_dhcp=2
ip6_lan_radv=1
ip6_dns_auto=1
ip6_cast_enable=1
napt66_enable=1
ip6_lan_sfps=4096&ip6_lan_sfpe=4352
ip6_6in4_remote=&ip6_6to4_relay=192.88.99.1&ip6_6rd_relay=
ip6_6rd_size=0&ip6_sit_mtu=1280&ip6_sit_ttl=64
ip6_wan_addr=&ip6_wan_size=64&ip6_wan_gate=
ip6_dns1=&ip6_dns2=&ip6_dns3=
ip6_lan_addr=&ip6_lan_size=64&ip6_lan_sflt=1800
```

提交：`curl -s -u admin:admin -X POST -d @post.txt "http://192.168.123.1/start_apply.htm"`（HTTP 200）

## 验证链

1. 重 GET 配置页：`ip6_service` select 的 `selected` 落在 `Native DHCPv6`（★）
2. 宿主侧：`ip -6 addr show | grep inet6 | grep -vE "fe80|::1"` → `240e:b30:1ff:1c02:...`（全局）
3. 外网：`curl -6 -s -m 8 -o /dev/null -w "%{http_code}" https://www.qq.com` → 非 000（501 也是网络层通的证明）

## 注意

- 配置页是 JS 动态渲染状态，curl 静态 HTML 抓不到 WAN 状态——状态验证走宿主侧
- 改配置前先备份当前值（本文件即备份：原 ip6_service=禁用）
- 老毛子 SSH（dropbear）与现代 OpenSSH 握手卡死——路由器操作一律 Web API
