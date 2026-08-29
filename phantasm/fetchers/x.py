"""X / Twitter 推文抓取器。

数据源：官方 Twitter API v2（REST API，需要 App/User 的 Bearer Token）。

  - 解析 screen_name → user_id：
        GET https://api.twitter.com/2/users/by/username/<username>
  - 读取用户推文：
        GET https://api.twitter.com/2/users/<id>/tweets
            ?tweet.fields=created_at,public_metrics,referenced_tweets,author_id,entities
            &expansions=attachments.media_keys,author_id
            &media.fields=url,preview_image_url,type,width,height
            &user.fields=name,username,profile_image_url,verified
            &max_results=<limit>

鉴权：``Authorization: Bearer <bearer_token>``（App-Only token 即可读公开推文）。

限流：官方 API 有严格的按项目/按用户配额（例如 read 项目 500 req/15min 量级）。
请按 ``poll_interval_seconds`` 保守配置，避免触发 429。

``account_id`` 支持两种写法：
  - 数字 user_id（如 "44196397"），直接使用；
  - screen_name（如 "jack"），先做一次解析并缓存映射。
"""
from __future__ import annotations

import re
from typing import Optional

from ..config import ConfigManager
from ..http import HttpClient, FetchError, RateLimitedError
from ..models import Account, FetchResult, Post
from .base import BaseFetcher, format_display_time, parse_metrics, parse_time_to_ts, sanitize_text

API_BASE = "https://api.twitter.com/2"
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T")


class XFetcher(BaseFetcher):
    platform = "x"
    display_label = "X/Twitter"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._screen_to_id: dict[str, str] = {}

    async def fetch_recent(self, limit: int = 20) -> FetchResult:
        creds = self.config.x_credentials() or {}
        mode = self.config.get("credentials", {}).get("x", {}).get("mode", "api")
        if mode == "scrape":
            return self.err_result("X 配置 mode=scrape 暂不支持。官方禁止匿名网页抓取，请使用 mode=api 并配置 Bearer Token")
        token = (creds.get("bearer_token") or "").strip()
        if not token:
            return self.err_result("未配置 X Bearer Token（credentials.x.bearer_token）")

        user_id = await self._resolve_user_id()
        if not user_id:
            return self.err_result("无法解析 X 用户的 user_id，请确认 account_id 为数字 id 或正确的 screen_name")

        url = f"{API_BASE}/users/{user_id}/tweets"
        params = {
            "tweet.fields": "created_at,public_metrics,referenced_tweets,author_id,entities,lang",
            "expansions": "attachments.media_keys,author_id",
            "media.fields": "url,preview_image_url,type,width,height,alt_text",
            "user.fields": "name,username,profile_image_url,verified,verified_type",
            "max_results": max(5, min(limit, 100)),
        }
        headers = self._headers(token)
        try:
            data = await self.http.get_json(url, headers=headers, params=params)
        except FetchError as e:
            return self.err_result(f"X 抓取失败：{e}")

        if data.get("errors"):
            err = data["errors"][0]
            return self.err_result(f"X 返回错误：{err.get('message')} (code {err.get('code')})")

        includes = data.get("includes") or {}
        users_map = {u.get("id"): u for u in (includes.get("users") or []) if u.get("id")}
        media_map = {m.get("media_key"): m for m in (includes.get("media") or []) if m.get("media_key")}

        posts: list[Post] = []
        for tweet in data.get("data") or []:
            posts.append(self._parse_tweet(tweet, users_map, media_map, account_id=self.account.account_id, screen=self.account.account_id))
        posts = [p for p in posts if p]
        posts.sort(key=lambda p: p.created_ts, reverse=True)
        return FetchResult(posts=posts[:limit], total=len(posts))

    async def fetch_raw(self, limit: int = 5) -> list:
        """返回最近推文的原始 tweet 字典列表（调试用，不解析）。"""
        creds = self.config.x_credentials() or {}
        token = (creds.get("bearer_token") or "").strip()
        if not token:
            return []
        user_id = await self._resolve_user_id()
        if not user_id:
            return []
        params = {
            "tweet.fields": "created_at,public_metrics,referenced_tweets,author_id,entities,lang",
            "expansions": "attachments.media_keys,author_id",
            "media.fields": "url,preview_image_url,type,width,height,alt_text",
            "user.fields": "name,username,profile_image_url,verified,verified_type",
            "max_results": max(1, min(limit, 100)),
        }
        try:
            data = await self.http.get_json(
                f"{API_BASE}/users/{user_id}/tweets", headers=self._headers(token), params=params)
        except Exception:  # noqa: BLE001
            return []
        return (data.get("data") or [])[:limit]

    async def _resolve_user_id(self) -> str:
        """account_id 若是数字，直接用；否则用 screen_name 解析（带缓存）。"""
        acc_id = self.account.account_id.strip()
        if acc_id.isdigit():
            return acc_id
        if acc_id in self._screen_to_id:
            return self._screen_to_id[acc_id]
        creds = self.config.x_credentials() or {}
        token = (creds.get("bearer_token") or "").strip()
        try:
            data = await self.http.get_json(
                f"{API_BASE}/users/by/username/{acc_id}",
                headers=self._headers(token),
                params={"user.fields": "id,name,username"},
            )
            user = data.get("data") or {}
            uid = user.get("id") or ""
            if uid:
                self._screen_to_id[acc_id] = uid
                return uid
        except FetchError as e:
            self.logger.warning(f"解析 X screen_name 失败：{e}")
        return ""

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def _parse_tweet(self, tweet: dict, users_map: dict, media_map: dict,
                     account_id: str, screen: str) -> Optional[Post]:
        try:
            tid = str(tweet.get("id") or "")
            if not tid:
                return None
            author_id = str(tweet.get("author_id") or account_id)
            user = users_map.get(author_id) or {}
            username = user.get("username") or (screen if screen.isdigit() else screen)
            name = user.get("name") or username
            avatar = user.get("profile_image_url") or ""

            text = sanitize_text(
                tweet.get("text") or "",
                emoji_mode=self.config.render.get("emoji_mode", "strip"),
            )

            media_urls: list[str] = []
            media_types: list[str] = []
            att = tweet.get("attachments") or {}
            for mk in att.get("media_keys") or []:
                m = media_map.get(mk)
                if not m:
                    continue
                mtype = m.get("type") or "photo"
                media_types.append(mtype)
                if mtype == "photo":
                    u = m.get("url") or m.get("preview_image_url") or ""
                else:
                    u = m.get("preview_image_url") or m.get("url") or ""
                if u:
                    media_urls.append(u)

            metrics = tweet.get("public_metrics") or {}
            stats = {
                "like": int(metrics.get("like_count") or 0),
                "reply": int(metrics.get("reply_count") or 0),
                "repost": int(metrics.get("retweet_count") or 0),
                "quote": int(metrics.get("quote_count") or 0),
            }

            created_at = tweet.get("created_at") or ""
            ts = parse_time_to_ts(created_at)
            display_time = format_display_time(created_at)

            # 判断是否为转发（referenced_tweets 的 type=retweeted）
            referenced = tweet.get("referenced_tweets") or []
            is_retweet = any(r.get("type") == "retweeted" for r in referenced)

            permalink = f"https://x.com/{username}/status/{tid}"
            return Post(
                platform="x",
                account_id=account_id,
                account_name=name,
                post_id=tid,
                author_name=name,
                author_handle=f"@{username}",
                author_avatar_url=avatar,
                content=text,
                created_at=display_time,
                created_ts=ts,
                media_urls=media_urls,
                stats=stats,
                url=permalink,
                source_type="retweet" if is_retweet else "tweet",
                extra={
                    "verified": bool(user.get("verified")),
                    "verified_type": user.get("verified_type") or "",
                    "media_types": media_types,
                    "lang": tweet.get("lang") or "",
                },
            )
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"解析 X 推文失败（id={tweet.get('id')}）：{e}")
            return None
