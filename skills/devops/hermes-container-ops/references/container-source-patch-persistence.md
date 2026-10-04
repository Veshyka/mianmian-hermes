# 容器内源码补丁的持久化（重建即自愈）

适用于**源码在镜像层、只有 data 目录是挂载卷**的容器（AstrBot 是典型）。这类补丁活在容器的可写层：
`docker compose up -d --force-recreate`、换镜像、手搓 `docker run` 都会让它消失，而且**退化是静默的**
（功能没了、不报错），只有用户用到才发现。所以不能只写「重建后照这份文档重打」。

## 配方（三道防线）

1. **补丁载荷放挂载目录**（代码层，随映射目录持久化），目录里固定四个文件：
   - `patch_<目标>.py` —— **合并版**补丁：把历史上分批打的改动合到一个脚本，一次覆盖全部锚点；幂等（每处带「已是目标状态」判据 → 跳过）、锚点命中数断言、`py_compile`、自动备份到 data 卷 `_setup_backup_<ts>/`，退出码非 0 = 有处没改成功。
   - `verify_patch.py` —— **机器判定自检**（AST 静态分析 + 文本判据），exit 0 = 生效。给重建流程当自动验收，也给人事后核验用。
   - `<svc>-entrypoint.sh` —— 启动入口：先 `patch` + `verify`（结果追加写 data 卷里的 `patches-boot.log`），再 `exec "$@"` 承接**镜像原 CMD**（镜像 `ENTRYPOINT=null` 时 CMD 会作为参数传进来；无参则 `exec python main.py`）。打补丁失败**不阻断启动**（宁可少功能也别把链路弄 down），只在日志里留 `!!! 自检未通过 !!!`。
   - `apply-from-host.sh` —— 宿主一键兜底：`docker exec mkdir -p` → `docker cp` 载荷进容器 → 打补丁 + 自检 → 复核签名 →（可选）`docker restart`。
2. **compose 接线**：`entrypoint: ["/bin/bash", "<容器内载荷路径>/<svc>-entrypoint.sh"]` + 只读挂载 `./patches:<容器内载荷路径>:ro`。
   - 先 `docker inspect` 确认镜像原本的 `ENTRYPOINT` / `CMD`（本机 AstrBot 是 `null` + `["python","main.py"]`），覆盖 entrypoint 时**必须保留原 CMD 的语义**，并把这条理由写进 compose 注释——上游改启动方式时后人才能发现。
   - 改 compose 前 `cp -a` 出 `.bak-before-<改动>-<ts>` 并记 md5；改完 `docker compose config` 看解析结果里 entrypoint + 挂载都在。
3. **留痕**：启动日志写进**挂载卷**（重建不丢），排查时 `docker exec <c> tail -20 <log>` 一眼看出这次是「已改」还是「跳过」、自检过没过。

## 验收（必须**模拟重建**，不能只看线上现在是好的）

线上容器此刻是补丁态，证明不了「重建后会自愈」。用**同一个镜像起一个一次性容器**跑同一条 entrypoint 链：

```bash
# A. 自愈 + 自检（PATCH_ONLY=1：只打补丁不启动服务，避免抢端口）
docker run --rm -v <宿主载荷目录>:/opt/<patches>:ro -v /tmp/<svc>-sim-data:/AstrBot/data \
  -e PATCH_ONLY=1 --entrypoint /bin/bash <镜像> /opt/<patches>/<svc>-entrypoint.sh
# 期望：已写入并通过 py_compile … / 改动 N 处 跳过 0 处 / PATCH_OK / [VERIFY] 补丁已生效 ✓

# B. 幂等：在**同一个容器内**连打两次（不是再 docker run 一次！新容器每次都是干净镜像层，看的是「又打了一遍」不是幂等）
docker run --rm -v <载荷>:ro -v <scratch-data>:/AstrBot/data --entrypoint /bin/bash <镜像> -c \
  'PY=/usr/local/bin/python3; $PY /opt/<patches>/patch_<目标>.py; echo ---第二次---; $PY /opt/<patches>/patch_<目标>.py; $PY /opt/<patches>/verify_patch.py'
# 期望第二次：改动 0 处 / 跳过 N 处

# C. 真 import 判定（补丁改的是签名，光看文件文本不够）
docker run --rm ... --entrypoint /bin/bash <镜像> /opt/<patches>/<svc>-entrypoint.sh /usr/local/bin/python -W ignore -c '<import 目标模块 + inspect.signature 断言首参 kind=POSITIONAL_ONLY + inspect.signature().bind(..., context="工具参数")>'
```

- **`-v /tmp/<svc>-sim-data:/AstrBot/data` 不能省**：补丁脚本会往 data 目录写备份目录，data 不存在时 `makedirs` 抛错 → 补丁根本没打上，而模拟结果看起来像「补丁无效」。入口脚本也应把日志路径降级到 `/tmp`（data 目录缺失时）以免连带写炸。
- 真链路验收要挑**不产生副作用**的模式（AstrBot 侧只跑 `verify_tools.py local` / `file`，不跑 `retain`）；验证完按 `references/hindsight-memory-curation.md` 的「验证产物收尾」复核残留。

## 同一类问题不止源码补丁：容器可写层的「设置」也会丢

打包成镜像的补丁会丢，**容器里手改过的系统设置**同样丢，一样静默。本机已知实例：容器时区
（`ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime` + `/etc/timezone`）写在可写层 → 换镜像/重建回到 UTC。

**丢之前先算影响，别一律当故障报**：看这处设置背后有没有硬编码兑底。同一处时区丢失，本机是
「日志时间戳整体差 8 小时」（人看日志会懵 + 容器内 `date` 类脚本偏差），但**业务逻辑不受影响**
（时间判断走适配器里的 `onebot_time.py`：`ZoneInfo("Asia/Shanghai")` + 硬编码 +8 兑底）。汇报时
把「影响日志」与「影响业务」分开写；动之前先 grep 一眼有没有这类双保险。

## 别只写清单，做成自愈看门狗

「重建后记得重打补丁 / 重设时区」是靠不住的承诺（靠人记 = 迟早漏）。做成脚本：**每轮只查现状，
坏了才修，健康零输出**：

```sh
grep -q "<补丁锚点>" /opt/hermes/<目标文件> || 走宿主重放补丁脚本
[ "$(date +%Z)" = CST ]                  || 走宿主重设 /etc/localtime + /etc/timezone
```

- 两条修复都走宿主（容器里没有 docker daemon）：`/opt/data/hspr_ssh.sh "export SUDO_ASKPASS=<映射>/scripts/mian_sudo.sh; sudo -A docker exec hermes …"`；askpass 用**持久的那份**（映射目录里的 `scripts/mian_sudo.sh`），不要靠宿主 `/tmp`。
- 注册成与其它看门狗同款的 cron：`no_agent=True` + `script=<文件名>`（相对 `~/scripts/`）+ 每 30 分钟；**健康零输出**不打扰，真修了才打一行。
- 首次上线验两件：空跑无输出且 rc=0；**修复命令单独跑一次**证明它真能改回来（幂等，重设成同值是安全的）。
- 顺手清「等下次启动才会变脏的陈旧状态」：本机实测清了适配器状态文件里**已修复问题**留下的 `consecutive_failures`，否则重启后看门狗会拿一个早就修好的问题推一次告警给主人。
- 判据别写成枚举（具体 provider 名、具体版本号）：一升级就误报。用家族前缀/家族特征。

## 盘点「更新前到底有什么会丢」的扫法

只扫 `/opt/data` 会得出「今天没动过系统」的错结论——真正易失的改动恰恰在别处。四个根一起扫：
`/opt/data`（挂载，活得下来）、`/tmp`（易失）、`/opt/hermes`（镜像层源码）、`/etc`（可写层设置），
过滤掉 `cache/`、`.venv/`、`__pycache__`。报告分两块写：**会丢的**（+判据 +影响）与**不会丢的**（逐条路径，带 mtime 当凭据）。

## 坑

- **分批打的补丁脚本必须合并后再持久化**：历史上一次只修一部分的脚本（`patch_..._v2` / `_v3`）单独跑会把系统修成**半坏**状态（改了一侧没改另一侧，症状换一个而不是消失）。合并版是重建后唯一入口；历史脚本降级为 `legacy-*`，并在登记文档里写明「别再单独跑」。
- **AST 判 `async def` 要匹配 `ast.AsyncFunctionDef`**：只判 `ast.FunctionDef` 会「找不到函数」→ 自检脚本常年假失败。判据写成 `isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))`。
- **判方法签名判「参数在 posonlyargs 里」，不是「args[0] 是它」**：类方法首参天然是 `self`，按 `args[0]` 判会把正确的补丁判成失败。
- **文本判据要锚定到调用点整段**，别用裸子串：`context=run_context` 这种字符串在文件别处也可能出现（同名字段/其它调用），会误报「调用点没改」。
- **entrypoint 覆盖后，线上旧容器与新 compose 会暂时不一致**（旧容器没挂载、没有 entrypoint）：补丁此刻仍在它的可写层里，功能正常；下次 compose 重建自然接管。**不要为了「让现状与配置一致」就擅自重建线上容器**——重建容器属需主人直接确认的操作，用一次性容器做等价验证即可，并在回报里说明这一条未验证项。
- 容器不是 compose 建的（手搓 `docker run`、导入镜像、别的脚本）时，entrypoint 自愈不会生效 → 用 `apply-from-host.sh` 兜底，并在文档里写清「重建走 compose 才有自动自愈」。
