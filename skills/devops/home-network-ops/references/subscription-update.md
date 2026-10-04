# 机场订阅更新全流程（2026-08-27 实测成功）

场景：旧订阅节点域名全部 NXDOMAIN（机场换域名/下线），主人给新订阅链接。
完整替换 mihomo 节点，保留策略组和规则。

## 步骤

### 1. 下载订阅
```bash
curl -sL -o /tmp/new_sub.yaml "<订阅URL>"
```
订阅返回体是 **base64 编码的 trojan:// 链接列表**（每行一个节点），解码：
```bash
base64 -d /tmp/new_sub.yaml > /tmp/new_sub_decoded.txt
wc -l /tmp/new_sub_decoded.txt   # 节点数
grep -oE "@[a-zA-Z0-9.-]+" /tmp/new_sub_decoded.txt | sort | uniq -c  # 域名分布
```
- 旧域名全 NXDOMAIN、新域名出现 = 机场换域名实锤
- 节点名和旧配置一般一致（机场保持命名），但「剩余流量」数字会变（134.15 GB → 93.02 GB）

### 2. 转换（宿主跑，mihomo 容器内没有 python3）
```bash
docker cp mihomo:/etc/mihomo/config.yaml /tmp/mihomo_old_config.yaml
python3 /vol1/1000/<USER>
```
- 脚本只替换 `proxies:` 段，proxy-groups/rules 原样保留
- 校验：`python3 -c "import yaml; d=yaml.safe_load(open('/tmp/mihomo_new_config.yaml')); print(len(d['proxies']), len(d['proxy-groups']), len(d['rules']))"`

### 3. 清理组引用（必做！）
新订阅节点名可能和旧配置不一致 → mihomo 启动报
`Parse config error: proxy group[0]: ... not found` 并无限 Restarting：
```bash
python3 /vol1/1000/<USER>
```
- 按 proxies 名集合过滤 proxy-groups 的 proxies 引用，移除不存在的节点名
- 实际移除例：`香港家宽-IEPL`（新订阅拆成 01/02）、`韩国家宽-IEPL 03-05`、`菲律宾-IEPL 03 5倍消耗` 等

### 4. 部署
```bash
docker cp /tmp/mihomo_new_config.yaml mihomo:/etc/mihomo/config.yaml
docker restart mihomo
docker ps | grep mihomo        # 不能是 Restarting
docker logs mihomo --tail 5    # 无 fatal
```

### 5. 切活节点 + 验证
- 新订阅可能只有少数节点活着：用 `GET /proxies/{name}/delay?url=...&timeout=5000` 逐个测
- 503/504 的跳过，有真实延迟（如 313ms）的切换：
  ```python
  # PUT /proxies/节点选择  body {"name": "🇭🇰|香港-直连"} → 204
  ```
- 验证：宿主 `curl -x http://127.0.0.1:17890 https://www.bing.com` → 200
- 下游复测：SearXNG 搜索、qB tracker、宿主机代理

## 关键事实（2026-08-27 环境实测）

- mihomo 容器：`mihomo:local`，网络 `mihomo-net`，配置 `/etc/mihomo/config.yaml`（无挂载，docker cp 进出）
- **mihomo 容器内没有 python3**——转换脚本必须在宿主跑（宿主有 python3 3.x）
- API：宿主 `172.17.0.1:19090`（无 secret），PUT 目标必须是「节点选择」Selector
- 配置备份：`/tmp/mihomo_old_config.yaml.bak-20260827-sub`、`config.yaml.bak-20260825`（容器内）
- 新订阅活节点例：`🇭🇰|香港-直连`（313ms）——切换后 bing/google/github 全通
- 宿主 SSH：`棉棉@172.17.0.1`，sudo askpass 用 `/vol1/1000/<USER>`（cp 自 askpass.sh，密码相同）

## 曾踩的坑

1. `PUT /proxies/目标节点名` → 400 "Must be a Selector"（要 PUT 到「节点选择」组）
2. 忘记替换「剩余流量」数字引用 → mihomo fatal 找不到节点
3. 宿主路径 `/opt/data/scripts/...` 是容器内路径，宿主实际是 `/vol1/1000/<USER>`
