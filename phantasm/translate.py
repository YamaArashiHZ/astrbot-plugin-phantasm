"""对外语帖子正文做翻译（走 AstrBot 已配置的 LLM，无需另配 key）。

判定策略（v1.14.0 起）：
- **优先用平台语言标记**：X API 会带 ``lang``；命中目标语言则直接跳过（省一次调用）。
- 拿不到 ``lang``（如 RSSHub 的 RSS）时，**交给模型自己判定**：同一次调用里要求
  「若已是目标语言，只回复 ``__SKIP__``；否则只输出译文」。这样避免了「汉字占比」
  这类启发式在日语（汉字多）上的误判。
- **按账号覆盖**（``accounts[].translate_mode``）：
    - ``off``  ：该账号不翻译（优先级最高）
    - ``force``：该账号一律翻译，无视全局开关与判定
    - ``auto`` ：默认，按上面两条走

翻译结果由调用方写入卡片（主卡片显示译文，原文卡片随附图打包）。
"""
from __future__ import annotations

import logging
import re
from typing import Any

# 模型用于表达「这段文本已经是目标语言，不需要翻译」
SKIP_TOKEN = "__SKIP__"

_SYSTEM_PROMPT = "你是一个只输出译文的翻译引擎。"

# 只翻译（force 模式：不要求模型做判定）
PROMPT_PLAIN = (
    "把下面这条社交平台帖子的正文翻译成{lang}。要求：\n"
    "1) 只输出译文本身，不要任何解释、不要加引号、不要重复原文；\n"
    "2) 保留原有的换行分段；\n"
    "3) 话题标签(#)与 @用户名 保留原样不翻译。\n\n"
    "正文：\n{text}"
)

# 判定 + 翻译（auto 模式：由模型决定是否需要翻译）
PROMPT_DETECT = (
    "下面是一段社交平台帖子的正文。请按以下顺序处理：\n"
    "1) 如果它**已经是{lang}**（无需翻译），只回复 {skip}，不要回复任何其它内容；\n"
    "2) 否则把它翻译成{lang}，只输出译文本身：不要解释、不要加引号、不要重复原文；\n"
    "3) 保留原有换行分段；话题标签(#)与 @用户名 保留原样不翻译。\n\n"
    "正文：\n{text}"
)

# 加在自定义提示词前面的判定规则（用户自定义 prompt 且 auto 模式时）
_DETECT_PREFIX = (
    "先判断：如果正文**已经是{lang}**（无需翻译），只回复 {skip}，不要回复其它内容。\n"
    "否则按下面要求处理：\n"
)

_VALID_MODES = ("auto", "force", "off")
_ZH_FAMILY = {"zh", "cmn", "yue", "wuu", "nan", "hak"}


def cjk_ratio(text: str) -> float:
    """汉字占「字母」的比例（仅用于日志/诊断参考，不再作为判定依据）。"""
    if not text:
        return 0.0
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    han = sum(1 for c in letters if "\u4e00" <= c <= "\u9fff")
    return han / len(letters)


def _letter_count(text: str) -> int:
    return len(re.findall(r"[^\W\d_]", text or "", flags=re.UNICODE))


class TranslationSkipped(Exception):
    """模型判定「已是目标语言」，无需翻译。"""


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

    def target_lang(self) -> str:
        return str(self.cfg.get("target_lang", "zh") or "zh")

    # ----------------------------------------------------------------
    # 语言 / 账号模式
    # ----------------------------------------------------------------
    @staticmethod
    def lang_family(code: str) -> str:
        """把语言码归一到「语族」（zh-cn/zh-tw/zh → zh）。"""
        c = (code or "").strip().lower().replace("_", "-")
        if not c:
            return ""
        base = c.split("-")[0]
        return "zh" if base in _ZH_FAMILY else base

    def account_mode(self, post=None, account=None) -> str:
        """取该账号的翻译模式：auto / force / off（默认 auto）。"""
        raw = ""
        if account is not None:
            raw = str(getattr(account, "translate_mode", "") or "")
        if not raw and post is not None:
            try:
                for a in self.config.accounts():
                    if a.platform == getattr(post, "platform", "") and \
                       a.account_id == getattr(post, "account_id", ""):
                        raw = str(getattr(a, "translate_mode", "") or "")
                        break
            except Exception:  # noqa: BLE001
                raw = ""
        mode = raw.strip().lower()
        return mode if mode in _VALID_MODES else "auto"

    def decide_by_config(self, post=None, account=None) -> tuple[bool, str]:
        """纯配置层的决策：返回 (是否应当尝试翻译, 说明)。

        优先级：账号 off > 账号 force > 全局开关 > 账号 auto。
        """
        mode = self.account_mode(post, account)
        if mode == "off":
            return False, "该账号已关闭翻译（translate_mode=off）"
        if mode == "force":
            return True, "该账号强制翻译（translate_mode=force）"
        if not self.enabled():
            return False, "全局未启用翻译"
        return True, "自动判定"

    # ----------------------------------------------------------------
    # Provider 解析：AstrBot 里「配了模型但没设默认」时 get_using_provider() 会返回 None，
    # 所以必须逐级兜底，否则翻译会静默失效。
    # ----------------------------------------------------------------
    @staticmethod
    def _prov_candidates(context) -> list:
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
        want = str(self.cfg.get("provider_id") or "").strip()
        cands = self._prov_candidates(self.context)
        if want and cands:
            for p in cands:
                if self._prov_label(p) == want and hasattr(p, "text_chat"):
                    return p, f"配置指定({want})"
        fn = getattr(self.context, "get_using_provider", None)
        if callable(fn):
            try:
                p = fn()
                if p is not None and hasattr(p, "text_chat"):
                    return p, "AstrBot 当前模型"
            except Exception as e:  # noqa: BLE001
                self.logger.debug(f"get_using_provider 失败：{e}")
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
        for p in cands:
            try:
                if hasattr(p, "text_chat") and self._is_enabled(p):
                    return p, f"自动选用({self._prov_label(p) or 'unknown'})"
            except Exception:  # noqa: BLE001
                continue
        if want:
            return None, f"未找到 provider_id={want} 的模型，且没有其它可用模型"
        return None, "AstrBot 里没有可用的对话模型（服务提供商里需配置并启用一个 LLM）"

    # ----------------------------------------------------------------
    def _build_prompt(self, text: str, allow_skip: bool) -> str:
        lang = self.target_lang()
        custom = str(self.cfg.get("prompt") or "").strip()
        if custom:
            tpl = custom
            if allow_skip and "{skip}" not in tpl:
                tpl = _DETECT_PREFIX + tpl
        else:
            tpl = PROMPT_DETECT if allow_skip else PROMPT_PLAIN
        return (tpl.replace("{lang}", lang)
                   .replace("{skip}", SKIP_TOKEN)
                   .replace("{text}", text))

    @staticmethod
    def _extract_text(resp) -> str:
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
        return out.strip()

    async def translate(self, text: str, allow_skip: bool = True) -> str:
        """翻译。``allow_skip=True`` 时由模型判定是否需要翻译。

        - 模型判定「已是目标语言」→ 抛 :class:`TranslationSkipped`
        - 失败 → 抛异常（调用方决定是否回退原文）
        """
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

        body = self._build_prompt(text, allow_skip)
        resp = await prov.text_chat(prompt=body, system_prompt=_SYSTEM_PROMPT)
        out = self._extract_text(resp)

        if allow_skip and SKIP_TOKEN in out:
            raise TranslationSkipped()
        out = re.sub(r'^["“”\']+|["“”\']+$', "", out).strip()
        if not out:
            raise RuntimeError("模型返回了空译文")
        return out

    # ----------------------------------------------------------------
    async def maybe_translate(self, post, account=None) -> str:
        """按配置与模型判定翻译；不需要/失败时返回空串（调用方回退原文）。

        全过程留痕：``[翻译]`` 前缀记录「模式 → 是否调用 → 判定 → 结果」。
        """
        pid = getattr(post, "kebab_id", "?")
        should, why = self.decide_by_config(post, account)
        if not should:
            self.logger.debug(f"[{pid}] [翻译] 跳过：{why}")
            return ""

        content = (post.content or "").strip()
        if _letter_count(content) < 2:
            self.logger.info(f"[{pid}] [翻译] 跳过：正文过短或没有文字")
            return ""

        mode = self.account_mode(post, account)
        lang = str((getattr(post, "extra", None) or {}).get("lang") or "")
        target = self.target_lang()

        # 平台给了权威 lang 且已是目标语言 → 直接跳过，省一次模型调用
        if mode != "force" and lang and self.lang_family(lang) == self.lang_family(target):
            self.logger.info(f"[{pid}] [翻译] 跳过：平台标记 lang={lang} 已是目标语言({target})")
            return ""

        allow_skip = (mode != "force")
        self.logger.info(
            f"[{pid}] [翻译] 调用模型：模式={mode}"
            f"{'（由模型判定是否需要翻译）' if allow_skip else '（强制翻译，不做判定）'}"
            f" | lang={lang or '未知'} | 正文 {len(content)} 字 | 目标 {target}")
        try:
            out = await self.translate(content, allow_skip=allow_skip)
        except TranslationSkipped:
            self.logger.info(f"[{pid}] [翻译] 模型判定：已是目标语言({target})，不翻译")
            return ""
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"[{pid}] [翻译] 失败，回退原文：{e}")
            return ""
        preview = out if len(out) <= 100 else out[:100] + "…"
        preview = preview.replace("\n", " ⏎ ")
        self.logger.info(
            f"[{pid}] [翻译] 完成：{len(content)} 字 → {len(out)} 字"
            + (f"（模型：{self._last_provider}）" if self._last_provider else "")
            + f" | 译文：{preview}")
        return out

    # ----------------------------------------------------------------
    def judge(self, text: str, lang: str = "") -> tuple[bool, str]:
        """粗略启发式判定（**仅供参考**，用于诊断输出；正式判定走模型）。"""
        text = (text or "").strip()
        if not text:
            return False, "正文为空"
        lang = (lang or "").strip().lower()
        if lang and self.lang_family(lang) == self.lang_family(self.target_lang()):
            return False, f"平台标记 lang={lang}（已是目标语言）"
        r = cjk_ratio(text)
        return True, f"汉字占比 {r:.2f}（仅供参考，正式判定由模型给出）"
