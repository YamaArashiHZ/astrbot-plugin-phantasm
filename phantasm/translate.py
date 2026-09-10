"""对外语帖子正文做翻译（默认走 AstrBot 已配置的 LLM Provider）。

设计：
- 只翻译「需要翻译」的正文（可配置为仅在正文基本不是中文时才翻）；
- 使用 AstrBot 当前生效的文本生成模型 ``context.get_using_provider()``，
  无需另配 API key；
- 译文写入 ``post.extra['translated_content']``，渲染时主卡片显示译文，
  原文卡片随附图一起放进「打包聊天记录」。
"""
from __future__ import annotations

import logging
import re
from typing import Any

# 内置提示词：要求只输出译文，避免 LLM 加解释/引号/重复原文
DEFAULT_PROMPT = (
    "把下面这条社交平台帖子的正文翻译成{lang}。要求：\n"
    "1) 只输出译文本身，不要任何解释、不要加引号、不要重复原文；\n"
    "2) 保留原有的换行分段；\n"
    "3) 话题标签(#)与 @用户名 保留原样不翻译。\n\n"
    "正文：\n{text}"
)


def cjk_ratio(text: str) -> float:
    """正文里中文字符占「字母」的比例（用于判断是否已经是中文）。"""
    if not text:
        return 1.0
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 1.0
    cjk = sum(1 for c in letters if "\u4e00" <= c <= "\u9fff")
    return cjk / len(letters)


class Translator:
    def __init__(self, context, config, logger: logging.Logger):
        self.context = context
        self.config = config
        self.logger = logger

    # ----------------------------------------------------------------
    @property
    def cfg(self) -> dict[str, Any]:
        try:
            return self.config.translate or {}
        except Exception:  # noqa: BLE001
            return {}

    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled"))

    def needs_translation(self, text: str) -> bool:
        text = (text or "").strip()
        if not text:
            return False
        if not self.cfg.get("only_non_chinese", True):
            return True
        try:
            threshold = float(self.cfg.get("cjk_threshold", 0.30))
        except (TypeError, ValueError):
            threshold = 0.30
        return cjk_ratio(text) < threshold

    def _provider(self):
        for name in ("get_using_provider", "get_using_provider_async"):
            fn = getattr(self.context, name, None)
            if callable(fn):
                try:
                    return fn()
                except Exception as e:  # noqa: BLE001
                    self.logger.debug(f"{name} 调用失败：{e}")
        return None

    async def translate(self, text: str) -> str:
        """返回译文；失败抛异常（由调用方决定是否降级为原文）。"""
        text = (text or "").strip()
        if not text:
            return ""
        try:
            max_chars = int(self.cfg.get("max_chars", 1200) or 1200)
        except (TypeError, ValueError):
            max_chars = 1200
        if max_chars > 0 and len(text) > max_chars:
            text = text[:max_chars]

        prov = self._provider()
        if prov is None:
            raise RuntimeError("AstrBot 未配置可用的 LLM Provider（请先在 AstrBot 里配置模型）")

        lang = str(self.cfg.get("target_lang", "zh") or "zh")
        tpl = str(self.cfg.get("prompt") or "").strip() or DEFAULT_PROMPT
        body = tpl.replace("{lang}", lang).replace("{text}", text)

        resp = await prov.text_chat(prompt=body, system_prompt="你是一个只输出译文的翻译引擎。")
        out = ""
        try:
            out = str(getattr(resp, "completion_text", "") or "").strip()
        except Exception:  # noqa: BLE001
            out = ""
        if not out:
            try:
                rc = getattr(resp, "result_chain", None)
                out = str(rc).strip() if rc else ""
            except Exception:  # noqa: BLE001
                out = ""
        # 去掉 LLM 偶尔加的包裹引号
        out = re.sub(r'^["“”\']+|["“”\']+$', "", out).strip()
        if not out:
            raise RuntimeError("LLM 返回了空译文")
        return out

    async def maybe_translate(self, post) -> str:
        """按配置判断并翻译；不需要/失败时返回空串（调用方回退为原文）。"""
        if not self.enabled():
            return ""
        content = (post.content or "").strip()
        if not self.needs_translation(content):
            return ""
        try:
            out = await self.translate(content)
            self.logger.info(f"[{post.kebab_id}] 已翻译（{len(content)} -> {len(out)} 字）")
            return out
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"[{post.kebab_id}] 翻译失败，回退原文：{e}")
            return ""
