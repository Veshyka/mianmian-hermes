"""hermes_lookup —— 聊天门「单次查询」工具箱。

给棉棉（AstrBot 侧的聊天门）装三类**一次调用就出结果**的查询能力，
把「多轮 / 长活 / 登录态」的活挡在门外（那类走 hermes_delegate 派给干活门）。

四个工具：
  * web_search        —— 走本机自建 SearXNG（172.17.0.1:18888），一次请求返回若干条
  * fetch_page        —— 抓单个网页正文（直连优先、挂了走宿主代理；公众号自动换微信 UA）
  * bilibili_lookup   —— BV/AV/链接 → 标题/UP/时长/统计/官方分章/字幕可用性（免登录 API）
  * bilibili_search   —— 关键词搜 B 站视频（先用首页拿 buvid3 cookie，再打 search API）

设计红线（别改）：
  1. **单次**：每个工具最多一次（fetch_page 最多直连+代理两次）网络请求，硬超时；
     没有翻页循环、没有重试风暴 —— 一旦开干就停不下来，长活必须派出去。
  2. **无登录态**：只用公开/免登录通道。要登录才能看的页面 → 明确告知拿不到，让 LLM 去派活。
  3. **失败要说得清**：超时/被墙/验证码/404 都返回人话，不静默返回空。

配置见 _conf_schema.json（容器 data/config/hermes_lookup_config.json）。
"""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import re
import time
from urllib.parse import quote, urlparse

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

DEFAULTS = {
    "enable": True,
    "enforce_boundary": True,
    "owner_umo": "aiocqhttp:FriendMessage:<OWNER_QQ>",
    "searxng_url": "http://172.17.0.1:18888",
    "proxy": "http://172.17.0.1:17890",
    "timeout": 15,
    "max_results": 5,
    "max_results_cap": 8,
    "fetch_max_chars": 6000,
    "trace_tools": False,
}

UA_BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# 微信公众号反爬：必须模拟微信内置浏览器，普通 UA 会弹验证码/「请在微信客户端打开」
UA_WECHAT = (
    "Mozilla/5.0 (Linux; Android 13; MicroMessenger/8.0.49.2560) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Version/4.0 Chrome/107.0.0.0 Mobile Safari/537.36"
)
BILI_HOME = "https://www.bilibili.com/"

# 「这不是正文」的特征串：命中就说明被反爬/登录墙挡了
WALL_MARKERS = (
    "wappoc_appmsgcaptcha",
    "请在微信客户端打开",
    "百度安全验证",
    "环境异常，完成验证后即可继续访问",
    "Just a moment",
    "cf-browser-verification",
    "enable JavaScript and cookies to continue",
    "访问过于频繁",
)

BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
AV_RE = re.compile(r"av(\d+)", re.I)


def _now() -> float:
    return time.monotonic()


def _fmt_num(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if n >= 100000000:
        return f"{n / 100000000:.1f}亿"
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


def _fmt_dur(sec) -> str:
    """秒数 → m:ss / h:mm:ss；已经是 "12:34" 这种字符串就原样返回；空值返回空串。"""
    if sec is None or (isinstance(sec, str) and not sec.strip()):
        return ""
    if isinstance(sec, str):
        s = sec.strip()
        if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", s):
            return s
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return ""
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _strip_tags(s: str) -> str:
    return html_lib.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


class Main(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = {**DEFAULTS, **(config or {})}
        self._session: aiohttp.ClientSession | None = None
        self._bili_cookie_at: float = 0.0
        self._allow_cache: list[str] | None = None
        self._allow_cache_at: float = 0.0
        logger.info(
            "[hermes_lookup] 就绪 searxng=%s proxy=%s timeout=%ss 工具=web_search/fetch_page/"
            "bilibili_lookup/bilibili_search"
            % (self.config["searxng_url"], self.config["proxy"], self.config["timeout"])
        )

    async def terminate(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ---------------- 基础设施 ----------------

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._cfg_int("timeout", 15)),
                headers={"Accept-Encoding": "identity"},  # 大响应 gzip 崩的坑，统一关掉压缩
            )
        return self._session

    async def _http_get(self, url: str, headers: dict | None = None,
                        proxy: str | None = None, timeout: int | None = None):
        """单次 GET。返回 (status, text, final_url, err)。任何异常都吞成 err 字符串。"""
        session = await self._get_session()
        to = aiohttp.ClientTimeout(total=timeout or self._cfg_int("timeout", 15))
        try:
            async with session.get(
                url,
                headers=headers or {},
                proxy=proxy or None,
                timeout=to,
                allow_redirects=True,
            ) as resp:
                raw = await resp.read()
                text = self._decode(raw, resp.headers.get("Content-Type", ""))
                return resp.status, text, str(resp.url), None
        except asyncio.TimeoutError:
            return 0, "", url, f"超时（>{to.total}s）"
        except aiohttp.ClientError as exc:
            return 0, "", url, f"连接失败：{type(exc).__name__}"
        except Exception as exc:  # noqa: BLE001
            return 0, "", url, f"请求异常：{type(exc).__name__}: {exc}"

    @staticmethod
    def _decode(raw: bytes, content_type: str) -> str:
        charset = ""
        m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
        if m:
            charset = m.group(1)
        if not charset:
            m = re.search(rb'charset=["\']?([\w-]+)', raw[:4096], re.I)
            if m:
                charset = m.group(1).decode("ascii", "ignore")
        for enc in [charset, "utf-8", "gb18030"]:
            if not enc:
                continue
            try:
                return raw.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", "ignore")

    # ---------------- 网页正文提取 ----------------

    @staticmethod
    def _html_to_text(doc: str) -> str:
        """HTML → 纯文本。优先 BeautifulSoup（能处理未闭合/嵌套的 style、script），
        拿不到就退回正则版。公众号正文优先取 #js_content。"""
        try:
            from bs4 import BeautifulSoup  # noqa: PLC0415

            soup = BeautifulSoup(doc, "html.parser")
            for tag in soup(["script", "style", "noscript", "svg", "iframe", "template"]):
                tag.decompose()
            node = soup.find(id="js_content") or soup.body or soup
            text = node.get_text("\n")
        except Exception:  # noqa: BLE001
            body = doc
            m = re.search(r'<div[^>]*id="js_content"[^>]*>(.*)', doc, re.S)
            if m:
                body = m.group(1)
            for tag in ("script", "style", "noscript", "svg", "iframe"):
                body = re.sub(rf"<{tag}\b[^>]*>.*?(</{tag}\s*>|$)", " ", body,
                              flags=re.S | re.I)
            body = re.sub(r"<!--.*?-->", " ", body, flags=re.S)
            body = re.sub(r"<(br|/p|/div|/li|/h[1-6]|/tr)[^>]*>", "\n", body, flags=re.I)
            text = html_lib.unescape(re.sub(r"<[^>]+>", " ", body))

        text = html_lib.unescape(text)
        # 有些站点（实测：baidu）把 <style>/<script> 整段塞在属性或模板字符串里，
        # 转义后在正文里还原成真标签 → 再清一遍（含未闭合的）
        text = re.sub(r"<(style|script|template)\b[^>]*>.*?(</\1\s*>|$)", " ",
                      text, flags=re.S | re.I)
        text = text.replace("\u00a0", " ").replace("\u200b", "")
        lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
        out, blank = [], 0
        for ln in lines:
            if not ln:
                blank += 1
                if blank > 1:
                    continue
            else:
                blank = 0
            # 丢掉残留标签行 / CSS·JS 大块（长度长且 { } ; 密度高）
            if ln.startswith("<"):
                continue
            if len(ln) > 150:
                junk = sum(ln.count(c) for c in "{};")
                if junk / len(ln) > 0.02:
                    continue
            out.append(ln)
        return "\n".join(out).strip()

    @staticmethod
    def _page_title(doc: str) -> str:
        m = re.search(r"<title[^>]*>(.*?)</title>", doc, re.S | re.I)
        return _strip_tags(m.group(1)) if m else ""

    @staticmethod
    def _hit_wall(text: str) -> bool:
        head = text[:3000]
        return any(mk.lower() in head.lower() for mk in WALL_MARKERS)

    # ---------------- 工具 1：网页搜索 ----------------

    async def _do_web_search(self, query: str, max_results: int = 5) -> str:
        query = (query or "").strip()
        if not query:
            return "（搜索失败：query 为空）"
        cap = self._cfg_int("max_results_cap", 8)
        n = max(1, min(max_results or self._cfg_int("max_results", 5), cap))
        base = str(self.config["searxng_url"]).rstrip("/")
        url = f"{base}/search?q={quote(query)}&format=json&language=zh-CN&safesearch=0"
        status, text, _final, err = await self._http_get(url)
        if err or status != 200:
            return f"（搜索失败：SearXNG {status} {err or ''}。可以换个说法再试一次；还不行就派给干活门。）"
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return "（搜索失败：SearXNG 返回的不是 JSON，可能没开 format=json。）"
        results = data.get("results") or []
        if not results:
            dead = data.get("unresponsive_engines") or []
            tail = f"（掉线的引擎：{dead}）" if dead else ""
            return f"（没搜到结果：{query}{tail}。换个说法再试，或者派给干活门。）"
        lines = [f"搜索「{query}」→ {len(results)} 条（自建 SearXNG）："]
        for i, r in enumerate(results[:n], 1):
            title = _strip_tags(r.get("title") or "")[:120]
            link = r.get("url") or ""
            snippet = re.sub(r"\s+", " ", _strip_tags(r.get("content") or ""))[:220]
            lines.append(f"{i}. {title}\n   {link}\n   {snippet}")
        if len(results) > n:
            lines.append(f"（还有 {len(results) - n} 条没列）")
        return "\n".join(lines)

    # ---------------- 工具 2：抓网页正文 ----------------

    async def _do_fetch_page(self, url: str, max_chars: int = 6000) -> str:
        url = (url or "").strip()
        if not url:
            return "（抓取失败：url 为空）"
        if not re.match(r"^https?://", url, re.I):
            url = "https://" + url
        host = urlparse(url).netloc.lower()
        ua = UA_WECHAT if "mp.weixin.qq.com" in host else UA_BROWSER
        headers = {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        status, doc, final, err = await self._http_get(url, headers)
        tries = [f"直连 {status or '失败'} {err or ''}".strip()]
        # 直连拿不到（被墙/空壳/被拦）→ 换宿主代理再试一次，只试一次
        if err or status != 200 or len(doc) < 800:
            pstatus, pdoc, pfinal, perr = await self._http_get(url, headers,
                                                               proxy=self.config.get("proxy"))
            tries.append(f"代理 {pstatus or '失败'} {perr or ''}".strip())
            if not perr and pstatus == 200 and len(pdoc) >= len(doc):
                status, doc, final = pstatus, pdoc, pfinal
        if err and status == 0:
            return f"（抓取失败：{url} —— {'；'.join(tries)}）"
        if status != 200:
            return (f"（抓取失败：{url} 返回 HTTP {status}；{'；'.join(tries)}。"
                    "要登录/有验证码的页面我这边拿不到，这活该派给干活门。）")
        text = self._html_to_text(doc)
        if self._hit_wall(text) or self._hit_wall(doc):
            return (f"（抓取被拦：{final} 撞上反爬/验证页，没拿到正文。"
                    "这类（要登录、要验证码、要 JS 渲染的）页面派给干活门，别硬试。）")
        if len(text) < 120:
            return (f"（抓取到 {final} 但正文只有 {len(text)} 字，多半是 JS 渲染空壳或需要登录态。"
                    "换一个源，或者派给干活门。）")
        limit = max(500, min(max_chars or self._cfg_int("fetch_max_chars", 6000), 20000))
        body = text[:limit]
        note = "" if len(text) <= limit else f"\n…[已截断，正文共 {len(text)} 字，只给了前 {limit} 字]"
        title = self._page_title(doc)
        head = f"《{title}》\n{final}\n\n" if title else f"{final}\n\n"
        return head + body + note

    # ---------------- B 站 ----------------

    @staticmethod
    def _bili_target(target: str):
        """→ ("bvid", id) / ("aid", id) / (None, None)"""
        target = (target or "").strip()
        m = BV_RE.search(target)
        if m:
            return "bvid", m.group(1)
        m = AV_RE.search(target)
        if m:
            return "aid", m.group(1)
        if target.isdigit():
            return "aid", target
        return None, None

    async def _bili_api(self, path: str, params: dict, referer: str | None = None):
        """B 站 API GET，返回 (json, err)。

        坑（实测 2026-09-23）：`Referer: https://www.bilibili.com/`（裸域名）会被风控判 412，
        而「不带 Referer」或「带具体视频页 Referer」都 200 → 所以按顺序试两种，各一次，不再多试。
        """
        url = f"https://api.bilibili.com{path}"
        if params:
            url += "?" + "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
        headers_with = {"User-Agent": UA_BROWSER, "Referer": referer} if referer else None
        headers_plain = {"User-Agent": UA_BROWSER}
        attempts = [headers_with, headers_plain] if headers_with else [headers_plain, headers_with]
        last = ""
        for headers in attempts:
            if headers is None:
                continue
            status, text, _f, err = await self._http_get(url, headers)
            if status == 200:
                try:
                    return json.loads(text), None
                except json.JSONDecodeError:
                    last = "返回不是 JSON（可能被拦）"
                    continue
            last = f"HTTP {status} {err or ''}".strip()
        return None, last

    async def _do_bilibili_lookup(self, target: str) -> str:
        kind, bid = self._bili_target(target)
        if not kind:
            return (f"（没认出视频：{target!r}。给我 BV 号、av 号或 B 站视频链接都行。）")
        # 先拿访客 cookie（buvid3）：B 站风控对「无 cookie 的纯 API 请求」更容易 412
        warn = await self._ensure_bili_cookie()
        ref = f"https://www.bilibili.com/video/{bid}" if kind == "bvid" else None
        params = {"bvid": bid} if kind == "bvid" else {"aid": bid}
        data, err = await self._bili_api("/x/web-interface/view", params, referer=ref)
        if err:
            return f"（查 B 站失败：{err}{warn}）"
        if data.get("code") != 0:
            return f"（查 B 站失败：{data.get('message')}（code={data.get('code')}）{warn}）"
        d = data.get("data") or {}
        bvid = d.get("bvid") or bid
        stat = d.get("stat") or {}
        owner = (d.get("owner") or {}).get("name") or "?"
        pub = time.strftime("%Y-%m-%d", time.localtime(d.get("pubdate") or 0)) if d.get("pubdate") else "?"
        desc = re.sub(r"\s+", " ", (d.get("desc") or "").strip())[:300]
        out = [
            f"{bvid} 《{d.get('title') or '?'}》",
            f"UP：{owner} ｜ 时长 {_fmt_dur(d.get('duration')) or '?'} ｜ 发布 {pub} ｜ 分P {d.get('videos', 1)}",
            f"播放 {_fmt_num(stat.get('view'))} · 点赞 {_fmt_num(stat.get('like'))} · "
            f"投币 {_fmt_num(stat.get('coin'))} · 收藏 {_fmt_num(stat.get('favorite'))} · "
            f"弹幕 {_fmt_num(stat.get('danmaku'))} · 评论 {_fmt_num(stat.get('reply'))}",
        ]
        if desc:
            out.append(f"简介：{desc}")

        cid = d.get("cid")
        if cid:
            pdata, perr = await self._bili_api(
                "/x/player/v2", {"bvid": bvid, "cid": cid},
                referer=f"https://www.bilibili.com/video/{bvid}")
            if not perr and (pdata or {}).get("code") == 0:
                pd = pdata.get("data") or {}
                vps = pd.get("view_points") or []
                if vps:
                    out.append(f"官方分章（{len(vps)} 段）：")
                    for vp in vps:
                        out.append(f"  {_fmt_dur(vp.get('from'))} {vp.get('content') or ''}".rstrip())
                subs = ((pd.get("subtitle") or {}).get("subtitles") or [])
                if subs:
                    langs = "、".join(s.get("lan_doc") or s.get("lan") or "?" for s in subs)
                    out.append(f"字幕：有公开字幕（{langs}）")
                elif pd.get("need_login_subtitle"):
                    out.append("字幕：只有登录态 AI 字幕（我这没登录态，拿不到 → 要字幕就派给干活门）")
                else:
                    out.append("字幕：无公开字幕（要内容得转录 → 派给干活门）")
        out.append(f"链接 https://www.bilibili.com/video/{bvid}")
        return "\n".join(out)

    async def _ensure_bili_cookie(self) -> str:
        """访问一次 B 站首页拿到 buvid3（共享 session 的 cookie jar 里），1 小时有效。"""
        if _now() - self._bili_cookie_at < 3600:
            return ""
        status, _t, _f, err = await self._http_get(
            BILI_HOME, {"User-Agent": UA_BROWSER})
        if err or status != 200:
            return f"（拿 B 站访客 cookie 失败：HTTP {status} {err or ''}）"
        self._bili_cookie_at = _now()
        return ""

    async def _do_bilibili_search(self, keyword: str, max_results: int = 5) -> str:
        keyword = (keyword or "").strip()
        if not keyword:
            return "（搜索失败：keyword 为空）"
        warn = await self._ensure_bili_cookie()
        cap = self._cfg_int("max_results_cap", 8)
        n = max(1, min(max_results or self._cfg_int("max_results", 5), cap))
        data, err = await self._bili_api(
            "/x/web-interface/search/type",
            {"search_type": "video", "keyword": keyword},
            referer=BILI_HOME)  # 实测：搜索接口要带站内 Referer，裸域名对 view/player 才踩雷
        if err:
            return f"（搜 B 站失败：{err}{warn}）"
        if data.get("code") != 0:
            return f"（搜 B 站失败：{data.get('message')}（code={data.get('code')}）{warn}）"
        res = (data.get("data") or {}).get("result") or []
        if not res:
            return f"（B 站没搜到「{keyword}」）"
        lines = [f"B 站搜「{keyword}」→ {len(res)} 条："]
        for i, r in enumerate(res[:n], 1):
            title = _strip_tags(r.get("title") or "").replace("\n", " ")[:90]
            dur = _fmt_dur(r.get("duration"))
            pub = (time.strftime("%Y-%m-%d", time.localtime(r.get("pubdate")))
                   if r.get("pubdate") else "")
            bits = [r.get("bvid"), f"UP {r.get('author')}"]
            if dur:
                bits.append(f"时长 {dur}")
            bits.append(f"播放 {_fmt_num(r.get('play'))}")
            if pub:
                bits.append(pub)
            lines.append(
                f"{i}. 《{title}》\n   " + " ｜ ".join(str(b) for b in bits) +
                f"\n   https://www.bilibili.com/video/{r.get('bvid')}"
            )
        return "\n".join(lines)

    # ---------------- 给 LLM 的工具 ----------------
    # 注意：AstrBot 的 llm_tool 只认 docstring 里的 `Args:` 段（不读类型注解），
    # 参数一律写成 string（数字也传字符串，函数里自己转），改了描述要连 Args 一起改。

    @filter.llm_tool(name="web_search")
    async def web_search(self, event: AstrMessageEvent, query: str, max_results: str = "") -> str:
        """联网搜索（走本机自建 SearXNG），一次调用出结果。闲聊不用它；要查事实、找链接、找资料时才用。

        Args:
            query(string): 搜索关键词，一句话
            max_results(string): 要几条，默认 5，最多 8
        """
        if not self.config.get("enable", True):
            return "（查询工具已关闭）"
        try:
            n = int(max_results) if str(max_results).strip() else self._cfg_int("max_results", 5)
        except (TypeError, ValueError):
            n = self._cfg_int("max_results", 5)
        logger.info(f"[hermes_lookup] web_search q={query[:60]!r} n={n}")
        return await self._do_web_search(query, n)

    @filter.llm_tool(name="fetch_page")
    async def fetch_page(self, event: AstrMessageEvent, url: str, max_chars: str = "") -> str:
        """抓一个网页的正文（单页、单次）。主人发来链接要正文/摘要时用；公众号链接会自动换微信 UA。

        Args:
            url(string): 要抓的网址（http/https 开头）
            max_chars(string): 正文最多给多少字，默认 6000
        """
        if not self.config.get("enable", True):
            return "（查询工具已关闭）"
        try:
            n = int(max_chars) if str(max_chars).strip() else self._cfg_int("fetch_max_chars", 6000)
        except (TypeError, ValueError):
            n = self._cfg_int("fetch_max_chars", 6000)
        logger.info(f"[hermes_lookup] fetch_page url={url[:120]!r}")
        return await self._do_fetch_page(url, n)

    @filter.llm_tool(name="bilibili_lookup")
    async def bilibili_lookup(self, event: AstrMessageEvent, target: str) -> str:
        """查一个 B 站视频的信息：标题/UP/时长/播放点赞/简介/官方分章/字幕可用性。给 BV 号或视频链接。

        Args:
            target(string): BV 号、av 号或 B 站视频链接
        """
        if not self.config.get("enable", True):
            return "（查询工具已关闭）"
        logger.info(f"[hermes_lookup] bilibili_lookup target={target[:80]!r}")
        return await self._do_bilibili_lookup(target)

    @filter.llm_tool(name="bilibili_search")
    async def bilibili_search(self, event: AstrMessageEvent, keyword: str,
                              max_results: str = "") -> str:
        """在 B 站按关键词搜视频，返回标题/BV号/UP/时长/播放量。

        Args:
            keyword(string): 搜索词
            max_results(string): 要几条，默认 5，最多 8
        """
        if not self.config.get("enable", True):
            return "（查询工具已关闭）"
        try:
            n = int(max_results) if str(max_results).strip() else self._cfg_int("max_results", 5)
        except (TypeError, ValueError):
            n = self._cfg_int("max_results", 5)
        logger.info(f"[hermes_lookup] bilibili_search kw={keyword[:60]!r} n={n}")
        return await self._do_bilibili_search(keyword, n)

    # ---------------- 工具 5：最近活动（只读，替代她以前用 python 查库的做法） ----------------

    async def _do_recent_activity(self) -> str:
        """只读查「主人最近说过话没有」，给她的主动冒头用（以前要她跑 python，已按边界收回）。"""
        import sqlite3  # noqa: PLC0415

        umo = str(self.config.get("owner_umo") or "")
        qq = umo.rsplit(":", 1)[-1] if umo else ""
        if not qq:
            return "（没配 owner_umo，查不了）"

        def _ago(ts) -> str:
            if not ts:
                return "没记录"
            try:
                from datetime import datetime, timedelta, timezone  # noqa: PLC0415

                if isinstance(ts, (int, float)) or (isinstance(ts, str) and ts.isdigit()):
                    dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
                else:
                    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                cst = dt.astimezone(timezone(timedelta(hours=8)))
                mins = (datetime.now(timezone.utc) - dt).total_seconds() / 60
                return f"{cst:%Y-%m-%d %H:%M}（约 {mins:.0f} 分钟前）"
            except Exception:  # noqa: BLE001
                return str(ts)

        try:
            con = sqlite3.connect("file:/AstrBot/data/data_v4.db?mode=ro", uri=True)
            try:
                last_msg = con.execute(
                    "select max(created_at) from platform_message_history "
                    "where user_id=? and sender_id=?", (umo, qq)).fetchone()[0]
                last_conv = con.execute(
                    "select max(updated_at) from conversations where user_id=?",
                    (umo,)).fetchone()[0]
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001
            return f"（查最近活动失败：{type(exc).__name__}）"
        return (
            f"主人最后一条消息：{_ago(last_msg)}\n"
            f"私聊会话最后更新：{_ago(last_conv)}\n"
            "（两张表的时间戳是 UTC，插件已经换算成北京时间。半小时内聊过就别去打扰他。）"
        )

    @filter.llm_tool(name="recent_activity")
    async def recent_activity(self, event: AstrMessageEvent) -> str:
        """查主人最近有没有跟我说话、多久之前（只读）。主动找他之前用来判断该不该打扰。

        Args:
            dummy(string): 随便传个空字符串
        """
        logger.info("[hermes_lookup] recent_activity")
        return await self._do_recent_activity()

    # ---------------- 自检 ----------------

    @filter.on_astrbot_loaded()
    async def census_tools(self) -> None:
        """启动时打两行自检，30 秒后再打一次（MCP 工具是启动后才注册的，早打会漏）。"""
        await self._log_toolset()
        asyncio.create_task(self._late_census())

    async def _late_census(self) -> None:
        try:
            await asyncio.sleep(30)
            await self._log_toolset()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[hermes_lookup] 延迟自检失败: {exc}")

    async def _log_toolset(self) -> None:
        try:
            tmgr = self.context.get_llm_tool_manager()
            names = sorted(t.name for t in tmgr.func_list)
            logger.info(f"[hermes_lookup] 已注册工具面({len(names)}): {','.join(names)}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[hermes_lookup] 工具面自检失败: {exc}")
            return
        allow = self._persona_allowlist()
        if not allow:
            return
        missing = [n for n in names if n not in allow]
        logger.info(
            f"[hermes_lookup] 白名单稽核：人格白名单 {len(allow)} 项；"
            f"注册但未列入的 {len(missing)} 个（她够不着，新增插件工具要记得加）: "
            f"{','.join(missing) or '无'}"
        )

    def _persona_allowlist(self) -> list[str]:
        """从 AstrBot 库里读她的人格工具白名单（只读，缓存 60 秒；读不到就返回空 = 不摘工具）。"""
        now = time.time()
        if self._allow_cache is not None and now - self._allow_cache_at < 60:
            return self._allow_cache
        out: list[str] = []
        try:
            import sqlite3  # noqa: PLC0415

            con = sqlite3.connect("file:/AstrBot/data/data_v4.db?mode=ro", uri=True)
            try:
                row = con.execute(
                    "select tools from personas where persona_id='mianmian'").fetchone()
            finally:
                con.close()
            if row and row[0]:
                val = json.loads(row[0])
                if isinstance(val, list):
                    out = val
        except Exception:  # noqa: BLE001
            out = []
        self._allow_cache = out
        self._allow_cache_at = now
        return out

    @filter.on_llm_request()
    async def enforce_boundary(self, event: AstrMessageEvent, req) -> None:
        """边界执行 + 自检（trace_tools）。

        为什么必须在钩子里做：AstrBot 的 local runtime（`computer_use_runtime: local`，见
        astr_main_agent.py:1643 `_apply_local_env_tools`）**每轮都无条件**往请求里塞
        astrbot_execute_shell / astrbot_shell_session / astrbot_execute_python /
        astrbot_file_read|write|edit_tool / astrbot_grep_tool，还在 system_prompt 里加一段英文
        「你能跑 shell 和 Python」——人格的 tools 白名单挡不住它们。所以这里按白名单**逐一摘掉**，
        并按本轮真实工具面**逐句校正**那段英文（shell/python 句删掉，文件四件套的 workspace
        相对路径提示留下 —— 见 astr_main_agent.py:1706-1722 原文）。
        """
        ft = getattr(req, "func_tool", None)
        removed: list[str] = []
        sp_fixed: list[str] = []
        if self.config.get("enforce_boundary", True) and ft is not None:
            allow = set(self._persona_allowlist())
            if allow:
                for name in [t.name for t in list(ft.tools)]:
                    if name not in allow:
                        ft.remove_tool(name)
                        removed.append(name)
                sp = getattr(req, "system_prompt", None)
                if sp:
                    # 2026-09-23：文件四件套已经还给棉棉，shell/python 仍然不给。
                    # 那段英文提示是**整段无条件**注入的，所以必须按本轮真实工具面逐句校正，
                    # 不能整段删（删了她就用不了 workspace 相对路径），也不能留着
                    # （留着她会以为自己能跑 shell —— 提示词和工具面必须一致）。
                    no_shell = ("astrbot_execute_shell" not in allow
                                and "astrbot_execute_python" not in allow)
                    if no_shell:
                        sp = sp.replace(
                            "`astrbot_execute_shell` and `astrbot_execute_python` "
                            "use it as their working directory. ", "")
                        sp_fixed.append("shell/python 句")
                    if not {"astrbot_file_read_tool", "astrbot_grep_tool"} & allow:
                        sp = "\n".join(ln for ln in sp.split("\n")
                                       if "Current workspace:" not in ln)
                        sp_fixed.append("workspace 句")
                if sp:
                    kept = [
                        ln for ln in sp.split("\n")
                        if "host local environment and can execute shell commands" not in ln
                        and "astrbot_shell_session" not in ln
                        and "automatically return a managed session" not in ln
                        and "`astrbot_execute_shell`" not in ln
                        and "`astrbot_execute_python`" not in ln
                    ]
                    req.system_prompt = "\n".join(kept)
        if sp_fixed:
            logger.info(f"[hermes_lookup] 边界执行：提示词校正 {'/'.join(sp_fixed)}")
        if removed:
            logger.info(
                f"[hermes_lookup] 边界执行：本轮摘掉 {len(removed)} 个白名单外工具: "
                f"{','.join(sorted(removed))}"
            )
        if self.config.get("trace_tools", False) and ft is not None:
            names = sorted(t.name for t in ft.tools)
            logger.info(f"[hermes_lookup] 本轮工具面({len(names)}): {','.join(names)}")
