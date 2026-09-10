"""卡片主题与色板。

色板从 ``config.render.theme`` 读取，默认提供 B 站与 X 两套。所有颜色均为
十六进制字符串（``#RRGGBB``），通过 :func:`hex_to_rgb` 转成 Pillow 用的
``(r, g, b)`` 元组。
"""
from __future__ import annotations

from typing import Any, Optional

DEFAULT_THEMES: dict[str, dict[str, str]] = {
    "bilibili": {
        "primary": "#FB7299",      # B 站粉
        "background": "#FFFFFF",
        "text": "#18191C",
        "subtext": "#9499A0",
        "accent": "#00AEEC",       # B 站蓝
        "media_bg": "#F1F2F3",
        "divider": "#E3E5E7",
        "badge_bg": "#3F3F46",
        "badge_fg": "#FFFFFF",
    },
    "x": {
        "primary": "#000000",      # X 黑
        "background": "#FFFFFF",
        "text": "#0F1419",
        "subtext": "#536471",
        "accent": "#1D9BF0",       # X 蓝
        "media_bg": "#EFF3F4",
        "divider": "#EFF3F4",
        "badge_bg": "#3F3F46",
        "badge_fg": "#FFFFFF",
    },
}

# 深色主题（黑底，仿 X 深色）
DEFAULT_DARK_THEMES: dict[str, dict[str, str]] = {
    "bilibili": {
        "primary": "#FB7299",
        "background": "#18191C",
        "text": "#E7E9EA",
        "subtext": "#9499A0",
        "accent": "#00AEEC",
        "media_bg": "#2A2C31",
        "divider": "#33363C",
        "badge_bg": "#3A3A3A",
        "badge_fg": "#E7E9EA",
    },
    "x": {
        "primary": "#1D9BF0",
        "background": "#000000",
        "text": "#E7E9EA",
        "subtext": "#71767B",
        "accent": "#1D9BF0",
        "media_bg": "#202327",
        "divider": "#2F3336",
        "badge_bg": "#3A3A3A",
        "badge_fg": "#E7E9EA",
    },
}

# 不同来源的通用主题别名 -> 默认平台主题
_PLATFORM_KEY = {"bilibili": "bilibili", "x": "x", "twitter": "x", "bili": "bilibili"}


def resolve_theme(platform: str, user_cfg: dict[str, Any], dark: bool = False,
                  dark_cfg: dict[str, Any] | None = None) -> dict[str, str]:
    """按平台返回最终色板（dark=True 用深色，用户配置覆盖默认）。"""
    key = _PLATFORM_KEY.get(platform.lower(), platform.lower())
    defaults = DEFAULT_DARK_THEMES if dark else DEFAULT_THEMES
    base = dict(defaults.get(key, defaults.get("x", {})))
    # 用户覆盖：深色优先 dark_cfg，否则 user_cfg(浅色)（深色且没配 dark_cfg 时也用浅色的用户覆盖）
    overlay = dark_cfg.get(key) if (dark and isinstance(dark_cfg, dict)) else None
    if overlay is None:
        overlay = (user_cfg or {}).get(key) or (user_cfg or {}).get(platform.lower()) or {}
    if isinstance(overlay, dict):
        base.update(overlay)
    # 兜底：缺字段用对应默认
    for k, v in defaults.get(key, {}).items():
        base.setdefault(k, v)
    return base


def hex_to_rgb(value: str | None, fallback: str = "#000000") -> tuple[int, int, int]:
    if not value:
        value = fallback
    v = str(value).lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    try:
        return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))
    except (ValueError, IndexError):
        return hex_to_rgb(fallback)


def theme_primary(theme: dict[str, str]) -> tuple[int, int, int]:
    return hex_to_rgb(theme.get("primary", "#000000"))
