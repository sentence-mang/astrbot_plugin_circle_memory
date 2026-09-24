"""上下文增强（P0）：媒体降级、成员标注、历史预算折叠。

全部 best-effort：任何异常不打断 LLM 请求（调用方 try/except）。

- 媒体降级（media_mode）：共享会话历史中的图片部件按策略处理，
  防止跨平台失效 URL 拖垮请求。不发起网络验证，仅按配置处理。
- 成员标注（aliases）：把组内成员昵称映射注入 system_prompt
  （单次注入，不进历史、无累积）。
- 历史预算（history_budget + summary_enabled）：共享历史超长时
  「最近 N 条全量 + 头部摘要 + 中段折叠」，摘要失败退化纯截断。
"""

from astrbot.api import logger
from astrbot.core.provider.entities import ProviderRequest


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
            umo = getattr(event, "unified_msg_origin", None) if event else None
            await self._apply_media_mode(req, umo)
            self._apply_member_context(req)
            await self._apply_budget(req, umo)
        except Exception as e:
            logger.error("[CircleMemory] 上下文增强失败（放行原请求）: %s", e)

    # ---------- 媒体降级 ----------

    def _text_part(self, text: str) -> dict:
        return {"type": "text", "text": text}

    async def _apply_media_mode(self, req: ProviderRequest, umo: str | None = None) -> None:
        mode = self._cfg("media_mode", "placeholder")
        if mode not in ("ignore", "placeholder", "caption"):
            return
        # 图片轮数控制：image_window>0 时仅保留最近 N 条 user 消息中的图片，
        # 更早的图片一律转 [图片] 占位（省 token）
        image_window = int(self._cfg("image_window", 0) or 0)
        user_count = 0
        for ctx in req.contexts:
            if ctx.get("role") == "user":
                user_count += 1
        keep_from = user_count - image_window if image_window > 0 else -1
        user_seen = 0
        for ctx in req.contexts:
            content = ctx.get("content")
            if not isinstance(content, list):
                continue
            if ctx.get("role") == "user":
                user_seen += 1
            is_recent_img = user_seen > keep_from if image_window > 0 else True
            new_parts = []
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "image_url":
                    new_parts.append(item)
                    continue
                # 轮数窗口内的图片按 media_mode 处理；窗口外一律占位
                if not is_recent_img:
                    new_parts.append(self._text_part("[图片]"))
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
                        cap = await self._caption_image(url, umo)
                        new_parts.append(
                            self._text_part(f"[图片: {cap}]") if cap else self._text_part("[图片]")
                        )
                        continue
                    new_parts.append(self._text_part("[图片]"))
                else:
                    new_parts.append(self._text_part("[图片]"))
            ctx["content"] = new_parts
        # 顶层图片列表：窗口外（image_window 无法定位所属轮次）统一按 mode 处理
        if mode == "ignore":
            req.image_urls = []
        elif mode == "placeholder":
            if req.image_urls:
                req.image_urls = []

    async def _get_caption_provider(self, umo: str | None = None):
        """图片转述用的 provider：优先 caption_provider_id 指定的 provider；
        未配置或找不到时退回触发者当前会话 provider（umo 会话偏好，若启用了
        provider 会话隔离）。对齐 AstrBot 核心自身图片转述兜底的同一模式。"""
        provider_id = (self._cfg("caption_provider_id", "") or "").strip()
        if provider_id:
            provider = self.star.context.get_provider_by_id(provider_id)
            if provider:
                return provider
            logger.warning(
                "[CircleMemory] caption_provider_id=%s 未找到对应 provider，退回当前会话 provider",
                provider_id,
            )
        return await self.star.context.get_using_provider_async(umo)

    async def _caption_image(self, url: str, umo: str | None = None) -> str:
        """调用 LLM 转述图片（用 caption_provider_id 或触发者会话 provider；失败返回空串）。"""
        try:
            provider = await self._get_caption_provider(umo)
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
        # 共享会话的 conversation_id 本身就是组 ID（group_cid 设计），
        # 直接按 id 找组，不必再经 merged（组名→cid）绕一道。
        cid = req.conversation.cid if req.conversation else ""
        if not cid:
            return
        group = next(
            (g for g in self.star.config.get("user_groups", []) if g.get("id") == cid),
            None,
        )
        if not group:
            return
        members = group.get("umos", [])
        if not members:
            return
        aliases = (self.star.config.get("aliases") or {}).get(cid) or {}
        # 组置顶记忆（pin）置于最前
        pins = (self.star.config.get("pins") or {}).get(cid) or ""
        head = ""
        if pins:
            head = f"【组置顶】{pins}\n\n"
        lines = ["共享会话成员身份（发言时用昵称称呼对方）："]
        for m in members:
            nick = aliases.get(m) or ""
            platform = m.split(":", 1)[0] if ":" in m else m
            who = f"{nick}（{platform}）" if nick else platform
            lines.append(f"- {who}")
        block = head + "\n".join(lines)
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

    async def _apply_budget(self, req: ProviderRequest, umo: str | None = None) -> None:
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
                    provider = await self.star.context.get_using_provider_async(umo)
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
