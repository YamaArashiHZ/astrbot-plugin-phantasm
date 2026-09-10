"""Bilibili 动态抓取器。

数据源：B站 Web 端「用户动态」接口（REST API）。

  GET https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space
      ?host_mid=<uid>&timezone_offset=-480

鉴权：需要 Cookie。最低要求 ``buvid3`` / ``buvid4``（本抓取器会自动通过
fingerprint 接口获取并合并）；强烈建议在配置里提供登录后的完整 Cookie
（至少含 ``SESSDATA``），否则容易触发 B 站风控（HTTP -352 / -412）。

限流：B 站对同一 IP 的匿名动态请求有明显的风控。配置
``credentials.bilibili.enable_risk_control_retry`` 开启后，遇到 -352/-412 会
给予退避重试并给出「提供 SESSDATA / 增大轮询间隔」的提示。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

from ..config import ConfigManager
from ..http import HttpClient, FetchError, RateLimitedError
from ..models import Account, FetchResult, Post
from .base import BaseFetcher, describe_platform, parse_metrics, parse_time_to_ts, sanitize_text

SPACE_FEED = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
FINGERPRINT = "https://api.bilibili.com/x/frontend/finger/spi"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# B 站风控/参数错误码
_RISK_CODES = {-412, -352, -509}

# 完整请求头里常见的 header 名前缀（用于识别「粘贴了 Cookie Editor 导出的整段 header」）
_HEADER_LINE_RE = re.compile(
    r"(?i)^(:authority|:method|:path|:scheme|accept|cookie|user-agent|"
    r"content-type|origin|referer|sec-|priority|accept-language|"
    r"accept-encoding|connection|content-length)\b")


def _extract_cookie_value(raw: str) -> str:
    """从 Cookie 配置值中提取真正的 ``Cookie:`` 内容。

    兼容两种输入：
      - 纯 Cookie 值：``SESSDATA=…; bili_jct=…; buvid3=…``
      - Cookie Editor 导出的完整请求头（多行 ``key: value``），自动取 ``cookie:`` 行

    其它无法识别的情况返回空串，交由上层提示。
    """
    if not raw:
        return ""
    s = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not s:
        return ""
    first_lower = s.split("\n", 1)[0].strip().lower()

    # 多行 header 或首行即以 header 名开头 → 视为完整请求头，找 cookie: 行
    if "\n" in s or _HEADER_LINE_RE.match(s):
        for line in s.split("\n"):
            line = line.strip()
            if line.lower().startswith("cookie:"):
                return line.split(":", 1)[1].strip()
        # 是 header 块但没 cookie: 行
        if _HEADER_LINE_RE.match(first_lower) and not first_lower.startswith("cookie"):
            return ""
    # 单行：去掉可能的 cookie: 前缀
    if first_lower.startswith("cookie:"):
        return s.split(":", 1)[1].strip()
    return s


class BilibiliFetcher(BaseFetcher):
    platform = "bilibili"
    display_label = "Bilibili"

    async def fetch_recent(self, limit: int = 20) -> FetchResult:
        account_id = self.account.account_id
        cookie = await self._build_cookie()
        if not cookie:
            return self.err_result("未配置 bilibili Cookie（可仅配置 buvid3/buvid4，或干脆提供完整 Cookie）")

        headers = self._headers(cookie)
        params = {"host_mid": account_id, "timezone_offset": "-480"}
        try:
            data = await self.http.get_json(SPACE_FEED, headers=headers, params=params)
        except RateLimitedError as e:
            return self.err_result(f"B站限流：{e}")
        except FetchError as e:
            return self.err_result(f"B站抓取失败：{e}")

        code = data.get("code")
        if code != 0:
            return self._handle_error(code, data)
        items = (data.get("data") or {}).get("items") or []
        posts = [p for p in (self._parse_item(it, account_id) for it in items) if p is not None]
        posts.sort(key=lambda p: p.created_ts, reverse=True)
        # B 站 feed 对部分图文（OPUS）动态不返回 desc（正文为空但有图），此时从
        # www.bilibili.com/opus/{id} 页面的 __INITIAL_STATE__ 补标题与正文。
        # 最多尝试前 3 条，避免批量请求触发风控。
        attempted = 0
        for p in posts:
            if not p.content and p.media_urls and attempted < 3:
                attempted += 1
                try:
                    title, body = await self._fetch_opus_content(p.post_id)
                    if title or body:
                        p.content = body or title
                        if title:
                            p.extra["title"] = title
                        p.extra["content_from_opus"] = True
                except Exception:  # noqa: BLE001
                    pass
        posts = self.apply_account_filters(posts)     # 按账号配置过滤转发
        return FetchResult(posts=posts[:limit], total=len(posts))

    async def _fetch_opus_content(self, post_id: str) -> tuple[str, str]:
        """从 OPUS 页面（www.bilibili.com/opus/{id}）的 __INITIAL_STATE__ 取标题与正文。

        B 站的 feed/detail JSON 接口对图文（OPUS）动态返回 desc:null，但 OPUS 网页会把
        完整内容内嵌在 HTML 的 ``window.__INITIAL_STATE__`` 里，这里回退抓取解析。
        """
        cookie = await self._build_cookie()
        url = f"https://www.bilibili.com/opus/{post_id}"
        headers = {"User-Agent": UA, "Referer": url}
        if cookie:
            headers["Cookie"] = cookie
        try:
            html = await self.http.get_bytes(url, headers=headers)
        except Exception:  # noqa: BLE001
            return "", ""
        text = html.decode("utf-8", errors="ignore")
        m = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;", text, re.S)
        if not m:
            return "", ""
        try:
            state = json.loads(m.group(1))
        except Exception:  # noqa: BLE001
            return "", ""
        detail = state.get("detail") or {}
        title = ""
        body_parts: list[str] = []
        for mod in detail.get("modules") or []:
            if not isinstance(mod, dict):
                continue
            mt = mod.get("module_type")
            if mt == "MODULE_TYPE_TITLE":
                title = (mod.get("module_title") or {}).get("text") or ""
            elif mt == "MODULE_TYPE_CONTENT":
                mc = mod.get("module_content") or {}
                for para in mc.get("paragraphs") or []:
                    if not isinstance(para, dict):
                        continue
                    if para.get("para_type") in (1, 3):  # 文本段落
                        t = self._extract_opus_paragraph(para)
                        if t:
                            body_parts.append(t)
        body = "\n".join(x for x in body_parts if x)
        return title, body

    @staticmethod
    def _extract_opus_paragraph(para: dict) -> str:
        nodes = ((para.get("text") or {}).get("nodes")) or []
        parts = []
        for node in nodes:
            if isinstance(node, dict):
                w = node.get("word") or {}
                parts.append(w.get("words") or "")
        return "".join(parts).strip()

    async def _fetch_detail_raw(self, post_id: str) -> dict:
        """返回动态详情接口的原始 data 对象（调试 / 正文补齐用）。"""
        cookie = await self._build_cookie()
        if not cookie:
            return {}
        url = f"https://api.bilibili.com/x/polymer/web-dynamic/v1/detail?id={post_id}&timezone_offset=-480"
        try:
            data = await self.http.get_json(url, headers=self._headers(cookie))
        except Exception:  # noqa: BLE001
            return {}
        if data.get("code") != 0:
            return {}
        return data.get("data") or {}

    async def fetch_detail(self, post_id: str) -> dict:
        """供调试命令使用：返回某动态的完整原始 data（含 item）。"""
        return await self._fetch_detail_raw(post_id)

    async def fetch_raw(self, limit: int = 5) -> list:
        """返回最近动态的原始 item 字典列表（调试用，不解析）。"""
        cookie = await self._build_cookie()
        if not cookie:
            return []
        params = {"host_mid": self.account.account_id, "timezone_offset": "-480"}
        try:
            data = await self.http.get_json(SPACE_FEED, headers=self._headers(cookie), params=params)
        except Exception:  # noqa: BLE001
            return []
        if data.get("code") != 0:
            return []
        return ((data.get("data") or {}).get("items") or [])[:limit]

    # ----------------------------------------------------------------
    def _headers(self, cookie: str) -> dict[str, str]:
        h = {
            "User-Agent": UA,
            "Referer": f"https://space.bilibili.com/{self.account.account_id}/dynamic",
            "Accept": "application/json, text/plain, */*",
        }
        if cookie:
            h["Cookie"] = cookie
        return h

    async def _build_cookie(self) -> str:
        """合并用户 Cookie 与自动获取的 buvid3/buvid4。

        ``config.credentials.bilibili.cookie`` 既可以是纯 Cookie 值
        （``SESSDATA=…; buvid3=…``），也可以是 Cookie Editor 导出的**完整请求头**：
        此时会从中提取 ``cookie:`` 行的内容，忽略其它 header。
        """
        user_cookie = _extract_cookie_value(self.config.bilibili_cookie())
        # 若用户已自带 buvid，则不覆盖
        merged = {part: val.strip() for part, val in (kv.split("=", 1) for kv in user_cookie.split(";") if "=" in kv)}

        def add(key: str, val: str) -> None:
            if val and key not in merged:
                merged[key] = val

        if "buvid3" not in merged or "buvid4" not in merged:
            try:
                fp = await self.http.get_json(FINGERPRINT, headers={"User-Agent": UA})
                d = (fp or {}).get("data") or {}
                add("buvid3", d.get("b_3") or "")
                add("buvid4", d.get("b_4") or "")
            except Exception as e:  # noqa: BLE001
                self.logger.debug(f"获取 bilibili fingerprint 失败：{e}")
        add("b_nut", str(int(time.time())))
        add("CURRENT_FNVAL", "4048")
        return "; ".join(f"{k}={v}" for k, v in merged.items())

    def _handle_error(self, code: int, data: dict) -> FetchResult:
        msg = (data.get("message") or f"code={code}")
        if code in _RISK_CODES or code in (-400,):
            return self.err_result(
                f"B站触发风控（code {code}）。请为 credentials.bilibili.cookie 填入登录后的 SESSDATA，"
                f"并适当增大 poll_interval_seconds：{msg}"
            )
        return self.err_result(f"B站接口返回错误（code {code}）：{msg}")

    # ----------------------------------------------------------------
    def _parse_item(self, item: dict, account_id: str) -> Optional[Post]:
        try:
            modules = item.get("modules") or {}
            author_mod = modules.get("module_author") or {}
            dyn_mod = modules.get("module_dynamic") or {}
            stat_mod = modules.get("module_stat") or {}

            post_id = str(item.get("id_str") or "")
            if not post_id:
                return None
            dtype = str(item.get("type") or "")

            desc = dyn_mod.get("desc") or {}
            content_text = self._extract_content(dyn_mod)

            media, title, source, card_stats, duration_text = self._extract_media(dtype, dyn_mod)

            author_name = author_mod.get("name") or self.account.account_id
            avatar = self._find_url(author_mod.get("avatar"))
            pub_text = author_mod.get("pub_time_text") or author_mod.get("pub_time") or ""
            # 优先用 item 顶层的 pub_ts（unix 秒）；否则解析 pub_time 相对文本
            pub_ts = item.get("pub_ts") or ""
            try:
                ts = float(pub_ts) if str(pub_ts).isdigit() else 0.0
            except (TypeError, ValueError):
                ts = 0.0
            if not ts:
                ts = parse_time_to_ts(str(author_mod.get("pub_time") or pub_text or ""))

            stats = self._extract_stats(stat_mod)
            stats.update({k: v for k, v in card_stats.items() if v})
            url = self._post_url(dtype, dyn_mod, post_id)

            # 转发：把原动态摘要放进 extra，渲染时展示“引用自”
            orig = item.get("orig")
            extra: dict[str, Any] = {"source": source, "dtype": dtype, "bili_type": dtype,
                                     "is_retweet": "FORWARD" in dtype}
            if duration_text:
                extra["bili_duration"] = duration_text
            orig_author = ""
            orig_content = ""
            orig_media: list[str] = []
            if orig:
                om = orig.get("modules") or {}
                o_author = (om.get("module_author") or {}).get("name") or ""
                o_dyn = om.get("module_dynamic") or {}
                o_content = self._extract_content(o_dyn)
                o_media, o_title, _o_src, _o_cs, _o_dur = self._extract_media(str(orig.get("type") or ""), o_dyn)
                orig_author, orig_content, orig_media = o_author, o_content, list(o_media)
                extra["orig_author"] = orig_author
                extra["orig_content"] = orig_content
                extra["orig_media"] = orig_media
                if title and not content_text:
                    content_text = sanitize_text(title, emoji_mode="strip")
                if o_title and not orig_content:
                    orig_content = o_title

            # 视频卡：正文往往为空，正文用“标题”，并把视频标题/时长作为主卡片信息
            if not content_text and title:
                content_text = sanitize_text(title, emoji_mode="strip")

            account_name = self.account.name
            if author_name and author_name != self.account.account_id:
                account_name = author_name

            # 有图但 feed 无正文：交由 fetch_recent 的 OPUS 回退补齐，此处仅留简短调试信息
            if not content_text and media:
                self.logger.debug(f"[bilibili:{post_id}] feed 无正文，将用 OPUS 网页补齐 type={dtype}")

            return Post(
                platform="bilibili",
                account_id=account_id,
                account_name=account_name,
                post_id=post_id,
                author_name=author_name,
                author_handle=f"UID {account_id}",
                author_avatar_url=avatar,
                content=content_text,
                created_at=pub_text,
                created_ts=ts,
                media_urls=media,
                stats=stats,
                url=url,
                source_type="forward" if "FORWARD" in dtype else source,
                extra=extra,
            )
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"解析 B 站动态失败（id={item.get('id_str')}）：{e}")
            return None

    def _extract_desc_text(self, desc: dict) -> str:
        """提取动态正文。

        优先 ``desc.text``；为空时回退拼接 ``desc.rich_text_nodes`` 的文本
        （部分图文/纯文本动态的正文只存在于 rich_text_nodes 中）。
        """
        emoji_mode = self.config.render.get("emoji_mode", "strip")
        if not desc:
            return ""
        txt = (desc.get("text") or "").strip()
        if txt:
            return sanitize_text(txt, emoji_mode=emoji_mode)
        nodes = desc.get("rich_text_nodes") or []
        if nodes:
            joined = "".join(str(n.get("text") or "") for n in nodes if isinstance(n, dict))
            return sanitize_text(joined, emoji_mode=emoji_mode)
        return ""

    def _extract_content(self, dyn_mod: dict) -> str:
        """尽力从动态结构中提取正文，依次：desc → major 标题/摘要 → 递归文本字段。"""
        if not dyn_mod:
            return ""
        # 1) desc.text / rich_text_nodes
        t = self._extract_desc_text(dyn_mod.get("desc") or {})
        if t:
            return t
        # 2) major 里的标题/摘要（视频/文章/音乐等）
        major = dyn_mod.get("major") or {}
        emoji_mode = self.config.render.get("emoji_mode", "strip")
        if isinstance(major, dict):
            for key in ("archive", "pgc", "article", "music", "common", "opus", "courses"):
                obj = major.get(key)
                if isinstance(obj, dict):
                    for f in ("title", "desc", "summary", "subtitle", "introduction"):
                        v = obj.get(f)
                        if isinstance(v, str) and v.strip() and len(v.strip()) > 1:
                            return sanitize_text(v, emoji_mode=emoji_mode)
        # 3) 递归兜底：找一个较长的 text/title/summary/desc 字段
        return self._search_text(dyn_mod, emoji_mode)

    def _search_text(self, obj, emoji_mode: str, depth: int = 0) -> str:
        if depth > 5:
            return ""
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in ("text", "title", "summary", "content") and isinstance(v, str) \
                        and len(v.strip()) >= 2:
                    return sanitize_text(v, emoji_mode=emoji_mode)
            for v in obj.values():
                r = self._search_text(v, emoji_mode, depth + 1)
                if r:
                    return r
        elif isinstance(obj, list):
            for v in obj:
                r = self._search_text(v, emoji_mode, depth + 1)
                if r:
                    return r
        return ""

    def _extract_media(self, dtype: str, dyn_mod: dict) -> tuple[list[str], str, str, dict[str, int], str]:
        """返回 (media_urls, title, source_type, card_stats, duration_text)。"""
        media: list[str] = []
        title = ""
        source = "text"
        card_stats: dict[str, int] = {}
        duration_text = ""
        major = dyn_mod.get("major") or {}

        mtype = major.get("type") or ""
        if mtype == "MAJOR_TYPE_ARCHIVE":
            arch = major.get("archive") or {}
            title = arch.get("title") or ""
            cover = self._find_url(arch.get("cover"))
            if cover:
                media.append(cover)
            source = "video"
            duration_text = arch.get("duration_text") or ""
            stat = arch.get("stat") or {}
            for k, label in (("play", "play"), ("danmaku", "danmaku")):
                if k in stat and stat.get(k):
                    try:
                        card_stats[label] = int(stat[k])
                    except (TypeError, ValueError):
                        pass
        elif mtype == "MAJOR_TYPE_PGC":
            pgc = major.get("pgc") or {}
            title = pgc.get("title") or ""
            cover = self._find_url(pgc.get("cover"))
            if cover:
                media.append(cover)
            source = "video"
            duration_text = pgc.get("duration_text") or ""
        elif mtype == "MAJOR_TYPE_DRAW":
            items = (major.get("draw") or {}).get("items") or []
            for it in items:
                u = self._find_url(it)
                if u and u.startswith("http"):
                    media.append(u)
            source = "images"
        elif mtype == "MAJOR_TYPE_ARTICLE":
            art = major.get("article") or {}
            title = art.get("title") or ""
            cover = self._find_url(art.get("cover"))
            if cover:
                media.append(cover)
            source = "article"
        elif mtype == "MAJOR_TYPE_MUSIC":
            music = major.get("music") or {}
            title = music.get("title") or ""
            cover = self._find_url(music.get("cover"))
            if cover:
                media.append(cover)
            source = "music"
        elif mtype == "MAJOR_TYPE_COMMON":
            common = major.get("common") or {}
            title = common.get("title") or ""
            cover = self._find_url(common.get("cover"))
            if cover:
                media.append(cover)
            source = "common"
        # 无 major 或 MAJOR_TYPE_NONE：纯文本

        return media, title, source, card_stats, duration_text

    def _extract_stats(self, stat_mod: dict) -> dict[str, int]:
        stats: dict[str, int] = {}
        for key in ("like", "comment", "forward", "coin", "favorite"):
            c = (stat_mod.get(key) or {}).get("count")
            try:
                stats[key] = int(c or 0)
            except (TypeError, ValueError):
                stats[key] = 0
        return stats

    def _post_url(self, dtype: str, dyn_mod: dict, post_id: str) -> str:
        major = dyn_mod.get("major") or {}
        if (major.get("type") == "MAJOR_TYPE_ARCHIVE") and major.get("archive", {}).get("bvid"):
            return f"https://www.bilibili.com/video/{major['archive']['bvid']}"
        if (major.get("type") == "MAJOR_TYPE_ARTICLE") and major.get("article", {}).get("id"):
            return f"https://www.bilibili.com/read/cv{major['article']['id']}"
        return f"https://t.bilibili.com/{post_id}"

    @classmethod
    def _find_url(cls, obj: Any) -> str:
        """递归地在任意结构里找第一个 http(s) 图片/资源 URL。"""
        if obj is None:
            return ""
        if isinstance(obj, str):
            return obj if obj.startswith("http") else ""
        if isinstance(obj, dict):
            # 常见字段直接命中
            for key in ("url", "src", "image_url", "cover", "image"):
                v = obj.get(key)
                r = cls._find_url(v)
                if r:
                    return r
            if "remote" in obj and isinstance(obj["remote"], dict):
                r = cls._find_url(obj["remote"].get("url"))
                if r:
                    return r
            if "image_src" in obj:
                r = cls._find_url(obj["image_src"])
                if r:
                    return r
            for v in obj.values():
                r = cls._find_url(v)
                if r:
                    return r
        if isinstance(obj, list):
            for v in obj:
                r = cls._find_url(v)
                if r:
                    return r
        return ""
