"""上下文增强（P0）：媒体降级、成员标注、历史预算折叠。

全部 best-effort：任何异常不打断 LLM 请求（调用方 try/except）。

- 媒体降级（media_mode）：共享会话历史中的图片部件按策略处理，
  防止跨平台失效 URL 拖垮请求。不发起网络验证，仅按配置处理。
- 成员标注（aliases）：把组内成员昵称映射注入 system_prompt
  （单次注入，不进历史、无累积）。
- 历史预算（history_budget + summary_enabled）：共享历史超长时
  「最近 N 条全量 + 头部摘要 + 中段折叠」，摘要失败退化纯截断。
"""

import datetime
import logging

from astrbot.core.provider.entities import ProviderRequest

logger = logging.getLogger(__name__)


class ContextEnhancer:
    def __init__(self, star) -> None:
        self.star = star

    # ---------- 判定 ----------

    def is_shared_request(self, req: ProviderRequest) -> bool:
        """当前请求是否属于某共享组会话。"""
        cid = req.conversation.cid if req.conversation else None
        if not cid:
            return False
        merged = self.star.config.get("merged", {})
        return cid in merged.values()

    def _cfg(self, key: str, default):
        return self.star.config.get(key, default)

    # ---------- 入口 ----------

    async def apply(self, event, req: ProviderRequest) -> None:
        """共享会话请求的上下文增强总入口（best-effort）。"""
        try:
            if not self.is_shared_request(req):
                return
            await self._apply_media_mode(req)
            self._apply_member_context(req)
            await self._apply_budget(req, event)
        except Exception as e:
            logger.error("[CircleMemory] 上下文增强失败（放行原请求）: %s", e)

    # ---------- 媒体降级 ----------

    def _text_part(self, text: str) -> dict:
        return {"type": "text", "text": text}

    async def _apply_media_mode(self, req: ProviderRequest) -> None:
        mode = self._cfg("media_mode", "placeholder")
        if mode not in ("ignore", "placeholder", "caption"):
            return
        for ctx in req.contexts:
            content = ctx.get("content")
            if not isinstance(content, list):
                continue
            new_parts = []
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "image_url":
                    new_parts.append(item)
                    continue
                if mode == "ignore":
                    continue
                if mode == "caption":
                    url = ""
                    img = item.get("image_url")
                    if isinstance(img, dict):
                        url = img.get("url") or ""
                    elif isinstance(img, str):
                        url = img
                    if url:
                        cap = await self._caption_image(url)
                        new_parts.append(
                            self._text_part(f"[图片: {cap}]") if cap else self._text_part("[图片]")
                        )
                        continue
                    new_parts.append(self._text_part("[图片]"))
                else:
                    new_parts.append(self._text_part("[图片]"))
            ctx["content"] = new_parts
        # 顶层图片列表同样处理
        if mode == "ignore":
            req.image_urls = []
        elif mode == "placeholder":
            if req.image_urls:
                req.image_urls = []

    async def _caption_image(self, url: str) -> str:
        """调用 LLM 转述图片（用当前会话 provider；失败返回空串）。"""
        try:
            provider = self.star.context.get_using_provider()
            if not provider:
                return ""
            resp = await provider.text_chat(
                prompt="请用中文简要描述这张图片的内容，不超过 50 字。",
                image_urls=[url],
                persist=False,
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
            return text[:100]
        except Exception as e:
            logger.debug("[CircleMemory] 图片转述失败: %s", e)
            return ""

    # ---------- 成员标注（单次注入 system_prompt） ----------

    def _apply_member_context(self, req: ProviderRequest) -> None:
        cid = req.conversation.cid if req.conversation else ""
        merged = self.star.config.get("merged", {})
        group_name = next((g for g, c in merged.items() if c == cid), None)
        if not group_name:
            return
        aliases = (self.star.config.get("aliases") or {}).get(group_name) or {}
        group = next(
            (g for g in self.star.config.get("user_groups", []) if g.get("name") == group_name),
            None,
        )
        if not group:
            return
        members = group.get("umos", [])
        if not members:
            return
        lines = ["共享会话成员身份（发言时用昵称称呼对方）："]
        for m in members:
            nick = aliases.get(m) or ""
            platform = m.split(":", 1)[0] if ":" in m else m
            who = f"{nick}（{platform}）" if nick else platform
            lines.append(f"- {who}")
        block = "\n".join(lines)
        # 单次注入：system_prompt 每轮重建，不进历史，无累积
        if block not in req.system_prompt:
            req.system_prompt = (req.system_prompt.rstrip() + "\n\n" + block).strip()

    # ---------- 历史预算与摘要 ----------

    def _content_text(self, content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))
            return "".join(parts)
        return str(content)

    async def _apply_budget(self, req: ProviderRequest, event) -> None:
        budget = int(self._cfg("history_budget", 0) or 0)
        if budget <= 0:
            return
        total = sum(len(self._content_text(c.get("content", ""))) for c in req.contexts)
        if total <= budget:
            return
        summary_on = bool(self._cfg("summary_enabled", False))
        summary_target = int(self._cfg("summary_target_chars", 800) or 800)
        keep_tail = max(1, len(req.contexts) // 4)  # 保留最近 1/4 条消息全量

        head_msgs = []
        tail_msgs = req.contexts[-keep_tail:]
        middle_msgs = req.contexts[:-keep_tail] if keep_tail else []
        for c in middle_msgs:
            if c.get("role") == "system":
                head_msgs.append(c)
        middle_msgs = [c for c in middle_msgs if c.get("role") != "system"]

        folded: list[dict] = list(head_msgs)
        # 头部摘要（best-effort；失败退化占位说明）
        if middle_msgs:
            summary = ""
            if summary_on:
                try:
                    provider = self.star.context.get_using_provider()
                    if provider:
                        mid_text = "\n".join(
                            self._content_text(c.get("content", ""))[:400]
                            for c in middle_msgs
                        )[:4000]
                        resp = await provider.text_chat(
                            prompt=(
                                "以下是多平台共享会话的一段历史消息，请用中文总结为"
                                f"不超过 {summary_target} 字的要点摘要，只输出摘要本身：\n\n{mid_text}"
                            ),
                            persist=False,
                        )
                        summary = (getattr(resp, "completion_text", "") or "").strip()[:summary_target]
                except Exception as e:
                    logger.debug("[CircleMemory] 历史摘要失败，退化截断: %s", e)
            if summary:
                folded.append({"role": "user", "content": f"[历史摘要] {summary}"})
            else:
                folded.append({
                    "role": "user",
                    "content": f"[中间 {len(middle_msgs)} 条历史消息已折叠省略]",
                })
        folded.extend(tail_msgs)
        req.contexts = folded
        logger.info(
            "[CircleMemory] 共享历史预算折叠: %d 条 → %d 条（预算 %d 字符）",
            len(middle_msgs) + len(tail_msgs) + len(head_msgs),
            len(folded),
            budget,
        )
