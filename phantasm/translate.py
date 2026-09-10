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
        self._logged_provider = False      # 首次成功解析模型时打一条 INFO
        self._last_provider = ""           # 最近一次使用的模型（写进完成日志）

    # ----------------------------------------------------------------
    @property
    def cfg(self) -> dict[str, Any]:
        try:
            return self.config.translate or {}
        except Exception:  # noqa: BLE001
            return {}

    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled"))

    def judge(self, text: str, lang: str = "") -> tuple[bool, str]:
        """判断是否需要翻译，并给出**可读的判断依据**（用于日志留痕）。

        优先用平台给出的语言标记（X 的 ``lang``，如 ``ja``/``zh``），
        拿不到时退回「中文字符占比」启发式。
        """
        text = (text or "").strip()
        if not text:
            return False, "正文为空"
        if not self.cfg.get("only_non_chinese", True):
            return True, "已关闭「仅非中文时翻译」，一律翻译"
        lang = (lang or "").strip().lower()
        if lang:
            if lang.startswith("zh"):
                return False, f"平台标记 lang={lang}（中文）"
            return True, f"平台标记 lang={lang}（非中文）"
        try:
            threshold = float(self.cfg.get("cjk_threshold", 0.30))
        except (TypeError, ValueError):
            threshold = 0.30
        r = cjk_ratio(text)
        cmp = "<" if r < threshold else "≥"
        return (r < threshold), f"中文字符占比 {r:.2f} {cmp} 阈值 {threshold:.2f}"

    def needs_translation(self, text: str, lang: str = "") -> bool:
        return self.judge(text, lang)[0]

    # ----------------------------------------------------------------
    # Provider 解析：AstrBot 里「配了模型但没设默认」时 get_using_provider() 会返回 None，
    # 所以必须逐级兜底，否则翻译会静默失效。
    # ----------------------------------------------------------------
    @staticmethod
    def _prov_candidates(context) -> list:
        """尽力列出 AstrBot 里可用的 provider（不同版本字段名不同，全部做防御）。"""
        out: list = []
        for attr in ("get_all_providers", "get_providers"):
            fn = getattr(context, attr, None)
            if callable(fn):
                try:
                    v = fn()
                    if isinstance(v, (list, tuple)):
                        out.extend(v)
                    elif isinstance(v, dict):
                        out.extend(v.values())
                except Exception:  # noqa: BLE001
                    pass
        if out:
            return out
        pm = getattr(context, "provider_manager", None)
        if pm is None:
            return []
        for attr in ("providers", "inst_map", "provider_map"):
            v = getattr(pm, attr, None)
            if isinstance(v, (list, tuple)):
                out.extend(v)
            elif isinstance(v, dict):
                out.extend(v.values())
        return out

    @staticmethod
    def _prov_label(prov) -> str:
        try:
            meta = prov.meta() if callable(getattr(prov, "meta", None)) else None
            return str(getattr(meta, "id", "") or getattr(meta, "model", "") or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _is_enabled(self, prov) -> bool:
        cfg = getattr(prov, "provider_config", None)
        if isinstance(cfg, dict) and cfg.get("enable") is False:
            return False
        return True

    async def resolve_provider(self):
        """返回 (provider, 来源说明)；找不到返回 (None, 原因)。"""
        # 1) 指定 id 优先
        want = str(self.cfg.get("provider_id") or "").strip()
        cands = self._prov_candidates(self.context)
        if want and cands:
            for p in cands:
                if self._prov_label(p) == want and hasattr(p, "text_chat"):
                    return p, f"配置指定({want})"
        # 2) 当前生效的 provider（同步）
        fn = getattr(self.context, "get_using_provider", None)
        if callable(fn):
            try:
                p = fn()
                if p is not None and hasattr(p, "text_chat"):
                    return p, "AstrBot 当前模型"
            except Exception as e:  # noqa: BLE001
                self.logger.debug(f"get_using_provider 失败：{e}")
        # 3) 当前生效的 provider（异步版本）
        fna = getattr(self.context, "get_using_provider_async", None)
        if callable(fna):
            try:
                p = fna()
                if hasattr(p, "__await__"):
                    p = await p
                if p is not None and hasattr(p, "text_chat"):
                    return p, "AstrBot 当前模型(async)"
            except Exception as e:  # noqa: BLE001
                self.logger.debug(f"get_using_provider_async 失败：{e}")
        # 4) 兜底：任意一个启用中的对话模型
        for p in cands:
            try:
                if hasattr(p, "text_chat") and self._is_enabled(p):
                    return p, f"自动选用({self._prov_label(p) or 'unknown'})"
            except Exception:  # noqa: BLE001
                continue
        if want:
            return None, f"未找到 provider_id={want} 的模型，且没有其它可用模型"
        return None, "AstrBot 里没有可用的对话模型（服务提供商里需配置并启用一个 LLM）"

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

        prov, why = await self.resolve_provider()
        if prov is None:
            raise RuntimeError(why)
        self._last_provider = why
        if not self._logged_provider:
            self.logger.info(f"[翻译] 首次解析到模型：{why}")
            self._logged_provider = True

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
            raise RuntimeError("模型返回了空译文")
        return out

    async def maybe_translate(self, post) -> str:
        """按配置判断并翻译；不需要/失败时返回空串（调用方回退为原文）。

        全过程留痕：[翻译] 前缀的日志会记录「是否启用 → 判断依据 → 用哪个模型 → 结果」。
        """
        pid = getattr(post, "kebab_id", "?")
        if not self.enabled():
            self.logger.debug(f"[{pid}] [翻译] 未启用，跳过")
            return ""
        content = (post.content or "").strip()
        lang = str((getattr(post, "extra", None) or {}).get("lang") or "")
        need, why = self.judge(content, lang)
        if not need:
            self.logger.info(f"[{pid}] [翻译] 跳过：{why}（正文 {len(content)} 字）")
            return ""
        self.logger.info(f"[{pid}] [翻译] 需要翻译：{why} | 正文 {len(content)} 字 → 调用模型")
        try:
            out = await self.translate(content)
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"[{pid}] [翻译] 失败，回退原文：{e}")
            return ""
        self.logger.info(
            f"[{pid}] [翻译] 完成：{len(content)} 字 → {len(out)} 字"
            + (f"（模型：{self._last_provider}）" if self._last_provider else ""))
        return out
