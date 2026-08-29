"""配置管理。

插件的唯一权威配置来源是 ``data/plugin_data/astrbot_plugin_phantasm/config.json``，
由插件设置页 / ``/phantasm`` 命令读写。这里负责：

- 默认配置（:data:`DEFAULT_CONFIG`），与用户保存配置做深合并；
- 账户列表、凭据、渲染/投递等分段配置的读访问；
- 凭据脱敏（日志 / 设置页不回显明文）；
- 落盘（UTF-8、缩进、仅写入我们认识的键，未知键忽略）。
"""
from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

from .models import Account, Target

# 键位默认段（与 config.example.json 对齐）
DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "poll_interval_seconds": 300,
    "poll_jitter_seconds": 10,
    "log_level": "INFO",
    "image": {
        "output_mode": "keep",           # keep: 保留卡片 / temp: 发送后清理
        "dir": "cards",                  # 相对于 data_dir
        "format": "png",
        "aspect_ratio": "1:1",
        "corner_radius": 24,
    },
    "render": {
        "backend": "html",               # html: Playwright/Chromium(彩色emoji) / pillow: Pillow
        "font_path": "",
        "font_size_scale": 1.0,
        "emoji_mode": "strip",           # strip: 移除 emoji（Pillow 无法渲染彩色 emoji）/ keep: 保留（需 HTML 后端）
        "theme_mode": {"bilibili": "light", "x": "light"},  # light / dark（黑底仿 X）
        "dark_theme": {                   # 深色主题，可覆盖
            "bilibili": {"primary": "#FB7299", "background": "#18191C", "text": "#E7E9EA",
                         "subtext": "#9499A0", "accent": "#00AEEC", "media_bg": "#2A2C31", "divider": "#33363C"},
            "x": {"primary": "#1D9BF0", "background": "#000000", "text": "#E7E9EA",
                  "subtext": "#71767B", "accent": "#1D9BF0", "media_bg": "#202327", "divider": "#2F3336"},
        },
        "name_color": "#000000",
        "handle_color": "#536471",
        "verified_badge": True,
        "theme": {
            "bilibili": {
                "primary": "#FB7299",
                "background": "#FFFFFF",
                "text": "#18191C",
                "subtext": "#9499A0",
                "accent": "#00AEEC",
            },
            "x": {
                "primary": "#000000",
                "background": "#FFFFFF",
                "text": "#0F1419",
                "subtext": "#536471",
                "accent": "#1D9BF0",
            },
        },
    },
    "send": {
        "caption_format": "{platform} · {author} · {time}",
        "send_caption": True,
        "link_to_post": True,          # 在文字说明末尾附上原贴链接（QQ 内可点击）
        "media_max": 4,
        "max_jobs_per_account": 10,
        "send_text_when_no_media": True,
        "cd_view_seconds": 60,         # /视奸 的全局冷却（秒），避免频繁请求
        "bot_self_id": "",             # 附图打包转发的 bot QQ号（uin）；空则 /视奸 用 event.get_self_id()
        "bot_nickname": "",            # 附图打包转发的显示名（空则用 bot_self_id）
    },
    "network": {
        "proxy_enabled": False,
        "proxy": "http://127.0.0.1:7897",
        "timeout_seconds": 15,
        "connect_timeout_seconds": 8,
        "max_retries": 3,
        "retry_backoff_base": 1.5,
        "max_concurrency": 3,
    },
    "credentials": {
        "bilibili": {
            "cookie": "",
            "enable_risk_control_retry": True,
        },
        "x": {
            "mode": "api",               # api: 官方 API v2 / rss: RSSHub 第三方(免费不占额度)
            "bearer_token": "",
            "rss_base": "https://rsshub.app",   # mode=rss 时的 RSSHub 实例地址
            "api_key": "",
            "api_secret": "",
            "access_token": "",
            "access_token_secret": "",
        },
    },
    "storage": {
        "history_limit": 5000,
        "prune_on_load": True,
    },
    "accounts": [],
}


def _deep_merge(base: dict, override: dict) -> dict:
    """把 override 深合并进 base（原地修改并返回 base），数组整体替换。"""
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def mask_secret(value: str, keep: int = 4) -> str:
    """脱敏：尾部保留 ``keep`` 个字符，其余换为 ``*``。"""
    if not value:
        return ""
    s = str(value)
    if len(s) <= keep:
        return "*" * len(s)
    return "*" * (len(s) - keep) + s[-keep:]


class ConfigManager:
    """加载 / 保存 / 访问 Phantasm 配置。"""

    def __init__(self, data_dir: Path, logger: logging.Logger):
        self.data_dir = Path(data_dir)
        self.logger = logger
        self.config_path = self.data_dir / "config.json"
        self.config: dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
        self.load()

    # ----------------------------------------------------------------
    # 加载 / 保存
    # ----------------------------------------------------------------
    def load(self) -> None:
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        if self.config_path.exists():
            try:
                saved = json.loads(self.config_path.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    cfg = _deep_merge(cfg, saved)
                else:
                    self.logger.warning("config.json 顶层不是对象，已使用默认配置")
            except Exception as e:  # noqa: BLE001
                self.logger.warning(f"配置读取失败，使用默认配置：{e}")
        self.config = cfg

    def save(self) -> None:
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(
                json.dumps(self.config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"配置保存失败：{e}")

    # ----------------------------------------------------------------
    # 分段访问
    # ----------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def set_enabled(self, value: bool) -> None:
        self.config["enabled"] = bool(value)
        self.save()

    @property
    def poll_interval(self) -> int:
        try:
            return max(5, int(self.config.get("poll_interval_seconds", 300)))
        except (TypeError, ValueError):
            return 300

    @property
    def image(self) -> dict[str, Any]:
        return self.config.get("image", {})

    @property
    def render(self) -> dict[str, Any]:
        return self.config.get("render", {})

    @property
    def send(self) -> dict[str, Any]:
        return self.config.get("send", {})

    @property
    def network(self) -> dict[str, Any]:
        return self.config.get("network", {})

    @property
    def credentials(self) -> dict[str, Any]:
        return self.config.get("credentials", {})

    @property
    def storage(self) -> dict[str, Any]:
        return self.config.get("storage", {})

    # ----------------------------------------------------------------
    # 账户
    # ----------------------------------------------------------------
    def accounts(self) -> list[Account]:
        """把 config 里的 accounts 原始字典解析成 :class:`Account` 列表。"""
        out: list[Account] = []
        for raw in self.config.get("accounts", []):
            if not isinstance(raw, dict):
                continue
            acc = Account(
                platform=str(raw.get("platform", "")).strip().lower(),
                account_id=str(raw.get("account_id", "")).strip(),
                display_name=str(raw.get("display_name", "")).strip(),
                enabled=bool(raw.get("enabled", True)),
                targets=_parse_targets(raw.get("targets", [])),
            )
            if acc.platform and acc.account_id:
                out.append(acc)
        return out

    def account_keys(self) -> list[str]:
        return [a.key for a in self.accounts()]

    def add_account(self, platform: str, account_id: str, display_name: str = "") -> bool:
        platform = platform.strip().lower()
        account_id = account_id.strip()
        if platform not in ("bilibili", "x") or not account_id:
            return False
        if any(a.platform == platform and a.account_id == account_id for a in self.accounts()):
            return False
        self.config.setdefault("accounts", []).append({
            "platform": platform,
            "account_id": account_id,
            "display_name": display_name,
            "enabled": True,
            "targets": [],
        })
        self.save()
        return True

    def remove_account(self, platform: str, account_id: str) -> bool:
        platform = platform.strip().lower()
        accounts = self.config.get("accounts", [])
        new = [a for a in accounts
               if not (str(a.get("platform", "")).strip().lower() == platform
                       and str(a.get("account_id", "")).strip() == str(account_id).strip())]
        if len(new) == len(accounts):
            return False
        self.config["accounts"] = new
        self.save()
        return True

    def add_target(self, platform: str, account_id: str, target_type: str, target_id: str) -> bool:
        platform = platform.strip().lower()
        for a in self.config.get("accounts", []):
            if (str(a.get("platform", "")).strip().lower() == platform
                    and str(a.get("account_id", "")).strip() == str(account_id).strip()):
                if target_type not in ("group", "private", "umo"):
                    return False
                if not target_id:
                    return False
                a.setdefault("targets", []).append({"type": target_type, "id": target_id})
                self.save()
                return True
        return False

    def clear_targets(self, platform: str, account_id: str) -> bool:
        platform = platform.strip().lower()
        for a in self.config.get("accounts", []):
            if (str(a.get("platform", "")).strip().lower() == platform
                    and str(a.get("account_id", "")).strip() == str(account_id).strip()):
                a["targets"] = []
                self.save()
                return True
        return False

    def set_account_enabled(self, platform: str, account_id: str, enabled: bool) -> bool:
        platform = platform.strip().lower()
        for a in self.config.get("accounts", []):
            if (str(a.get("platform", "")).strip().lower() == platform
                    and str(a.get("account_id", "")).strip() == str(account_id).strip()):
                a["enabled"] = bool(enabled)
                self.save()
                return True
        return False

    def set_account_display_name(self, platform: str, account_id: str, name: str) -> bool:
        """编辑某账号的代称（display_name），用于 /视奸 与 /phantasm list 展示。"""
        platform = platform.strip().lower()
        for a in self.config.get("accounts", []):
            if (str(a.get("platform", "")).strip().lower() == platform
                    and str(a.get("account_id", "")).strip() == str(account_id).strip()):
                a["display_name"] = str(name or "").strip()
                self.save()
                return True
        return False

    # ----------------------------------------------------------------
    # 凭据
    # ----------------------------------------------------------------
    def credential(self, platform: str) -> dict[str, Any]:
        return self.credentials.get(platform, {}) or {}

    def bilibili_cookie(self) -> str:
        return (self.credential("bilibili").get("cookie") or "").strip()

    def x_credentials(self) -> dict[str, Any]:
        return dict(self.credential("x") or {})

    def masked_credentials(self) -> dict[str, Any]:
        """返回脱敏后的凭据（用于设置页 / 展示）。"""
        c = copy.deepcopy(self.credentials)
        bil = c.setdefault("bilibili", {})
        if bil.get("cookie"):
            bil["cookie"] = mask_secret(bil["cookie"], 6)
        x = c.setdefault("x", {})
        for key in ("bearer_token", "api_key", "api_secret", "access_token", "access_token_secret"):
            if x.get(key):
                x[key] = mask_secret(x[key], 4)
        return c


def _parse_targets(raw: list) -> list[Target]:
    out: list[Target] = []
    for t in raw or []:
        if not isinstance(t, dict):
            continue
        ttype = str(t.get("type", "")).strip().lower()
        tid = str(t.get("id", "")).strip()
        if not tid:
            continue
        if ttype not in ("group", "private", "friend", "umo"):
            continue
        if ttype == "friend":
            ttype = "private"
        out.append(Target(type=ttype, id=tid))
    return out
