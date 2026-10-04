# hermes_lookup —— 聊天门「单次查询」工具箱

给棉棉（AstrBot 侧聊天门）装上**一次调用就出结果**的查询能力，并把「多轮 / 长活 / 登录态」
的活挡在她的手之外（那类走 `hermes_delegate` 派给干活门）。这是把聊天门从「聊天员」升成
「轻量助理」的那一半；另一半是人格提示词里的《工具与边界（硬规矩）》。

## 五个工具

| 工具 | 干什么 | 走的什么通道 |
|---|---|---|
| `web_search(query, max_results)` | 联网搜 | 本机自建 SearXNG `172.17.0.1:18888`（`format=json`），单次请求 |
| `fetch_page(url, max_chars)` | 抓单个网页正文 | 直连优先，拿不到（被墙/空壳）走宿主代理 `172.17.0.1:17890` 再试一次；`mp.weixin.qq.com` 自动换微信内置浏览器 UA |
| `bilibili_lookup(target)` | BV/AV/链接 → 标题·UP·时长·统计·简介·**官方分章**·字幕可用性 | `api.bilibili.com/x/web-interface/view` + `/x/player/v2`（免登录） |
| `bilibili_search(keyword, max_results)` | 关键词搜 B 站视频 | `/x/web-interface/search/type?search_type=video`（先取首页 buvid3 访客 cookie） |
| `recent_activity()` | 只读查「主人最近一次说话是多久前」 | 只读打开 `data_v4.db`（`platform_message_history` / `conversations`），UTC→CST 换算 |

前四个是「查外面」，第五个是「查自己这边」——它顶替了原来让她跑 `astrbot_execute_python`
查库的做法（那条路已按边界收回）。

## 硬边界是**两层**（缺一层就漏）

1. **人格白名单**：`personas.tools`（persona_id=`mianmian`）显式列出她能用的工具名。
   不为 `NULL`（`NULL` = 全给），也不为 `[]`（`[]` = 一个都不给）。
   写库脚本：`chat-layer/apply-persona-boundary.py`（幂等，先备份）。
2. **每轮剪枝（本插件 `enforce_boundary`）**：**这层少不了**。
   AstrBot 的 local runtime（`computer_use_runtime: local`）在
   `astr_main_agent.py:1643 _apply_local_env_tools()` **每轮无条件**往请求里塞
   `astrbot_execute_shell / astrbot_shell_session / astrbot_execute_python /
   astrbot_file_read_tool / astrbot_file_write_tool / astrbot_file_edit_tool / astrbot_grep_tool`，
   还往 system_prompt 里加一段英文「你能跑 shell 和 Python」——**人格白名单挡不住它们**。
   所以本插件在 `@filter.on_llm_request()` 里按白名单逐个 `remove_tool()`，
   并把那段英文提示从 `req.system_prompt` 删掉（否则自相矛盾）。

   实测（2026-09-23 14:07，主人私聊实轮）：

   ```
   [hermes_lookup] 边界执行：本轮摘掉 6 个白名单外工具: astrbot_execute_python,astrbot_execute_shell,
     astrbot_file_edit_tool,astrbot_file_write_tool,astrbot_grep_tool,astrbot_shell_session
   [hermes_lookup] 本轮工具面(19): astrbot_file_read_tool,bilibili_lookup,bilibili_search,
     delegate_to_hermes,fetch_page,future_task,list_memories,llm_publish_feed,llm_view_feed,
     memo_add,memo_remove,memo_replace,memo_view,recall,recent_activity,reflect,retain,
     send_message_to_user,web_search
   ```

## ⚠️ 维护坑：新增插件工具必须加进白名单

白名单是**闭集**：新插件注册的 LLM 工具如果没写进 `personas.tools`，她**够不着**
（每轮会被剪掉，只留一行日志）。所以每次装新插件工具后：

1. 加进 `chat-layer/apply-persona-boundary.py` 的 `ALLOW` 列表并重跑；
2. 看启动日志里的两行自检（下面「自检」一节），`注册但未列入` 那行如果是新工具，就是漏了。

## 自检（都写日志，不用翻库）

启动时（`on_astrbot_loaded`）打两行，30 秒后再打一次（MCP 工具是启动后才注册的，早打会漏）：

```
[hermes_lookup] 已注册工具面(41): <AstrBot 已注册的全部工具名>
[hermes_lookup] 白名单稽核：人格白名单 19 项；注册但未列入的 25 个（她够不着，新增插件工具要记得加）: ...
```

每轮（`on_llm_request`）：`边界执行：本轮摘掉 N 个…`；`trace_tools=true` 时再打一行最终工具面。

## 配置

容器 `data/config/hermes_lookup_config.json`（schema 见 `_conf_schema.json`）：
`enable`、`enforce_boundary`（默认 true）、`owner_umo`、`searxng_url`、`proxy`、`timeout`、
`max_results`、`max_results_cap`、`fetch_max_chars`、`trace_tools`（默认 false，核对工具面时临时开）。

## 部署 / 改代码

源码在 `chat-layer/plugin/hermes_lookup/`（本目录），**compose 没有挂载它** —— 它跟
`hermes_memory`/`hermes_memo` 一样住在数据卷里，所以**容器 `docker restart` 与重建都不会丢**。
同步（宿主上跑）：

```bash
sudo docker cp /vol1/1000/<USER> \
     astrbot:/AstrBot/data/plugins/hermes_lookup/
sudo docker restart astrbot        # ASTRBOT_RELOAD 在容器里没生效，改代码一律靠 restart
```

容器内自检（真网络、只读）：

```bash
sudo docker exec astrbot python3 /AstrBot/data/plugins/hermes_lookup/tests/verify_lookup.py
```

## 实测踩过的坑（别改回去）

1. **B 站 412**：`api.bilibili.com` 对 `Referer: https://www.bilibili.com/`（裸域名）会给 **412**，
   而「不带 Referer」或「带具体视频页 Referer `…/video/<bvid>`」都 200；搜索接口反过来要裸域名
   Referer。→ `_bili_api()` 按两种顺序各试一次后放弃，不再风暴重试。
2. **搜索接口要访客 cookie**：先 GET 一次 `https://www.bilibili.com/` 拿 `buvid3`（共享 session 的
   cookie jar，1 小时缓存），否则 search/type 会被拦。
3. **网页正文**：用 BeautifulSoup（`html.parser`）去 script/style 最稳；baidu 这类站会把
   `<style>` 整段塞在属性/模板里，转义后还原成真标签 → 文本化后再清一遍（含未闭合），
   再丢掉「长且 `{};` 密度高」的行。
4. **gzip 崩**：所有请求统一 `Accept-Encoding: identity`。
5. **境内/境外分流**：容器**没有**直连外网（google/github 直连返回 000），但境内站
   （baidu / so.com / 微信 / B 站 API）直连正常；境外站靠 `fetch_page` 的代理兜底这一次。
