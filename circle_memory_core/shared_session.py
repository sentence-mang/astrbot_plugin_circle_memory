"""会话合并引擎：组共享会话（cid=组 ID）的创建、迁移、切换与删除。

核心思想：LLM 请求前（on_waiting_llm_request，在 build 之前触发），
把组内所有 UMO 的 conversation 切换到组共享的 conversation_id。
AstrBot 原生按 UMO → conversation_id 读写历史，因此天然共享，O(1) 无注入。
"""

import asyncio
import logging

from circle_memory_core.groups import group_cid, group_for_umo, normalize_groups
from circle_memory_core.storage import find_group, save_merged, save_user_groups

logger = logging.getLogger(__name__)


class SharedSessionManager:
    """管理组共享会话生命周期。

    持有 star 而非 context/config 快照：context 与 config 动态从
    star 读取（测试会替换 star.context / 外部配置对象可变）。
    """

    def __init__(self, star) -> None:
        self.star = star
        self._group_locks: dict[str, asyncio.Lock] = {}  # 组名 -> 迁移/补齐锁

    def find_group(self, name: str) -> dict | None:
        return find_group(self.star.config, name)

    # ---------- 删除 ----------

    async def delete_shared_conversation(self, cm, group: str) -> None:
        """物理删除组共享会话及其全部历史（best-effort，失败仅记日志）。"""
        merged = dict(self.star.config.get("merged", {}))
        shared_cid = merged.get(group)
        if not shared_cid:
            return
        try:
            # unified_msg_origin 传空字符串：conversation_id 非空时不触碰任何 UMO 的内存映射
            await cm.delete_conversation("", conversation_id=shared_cid)
            logger.info("[CircleMemory] 组 %s 共享会话 %s 已物理删除", group, shared_cid)
        except Exception as e:
            logger.error("[CircleMemory] 删除共享会话 %s 失败: %s", shared_cid, e)

    # ---------- 创建/迁移 ----------

    async def ensure_group_conversation(self, cm, group: dict, source_cid: str | None = None) -> str:
        """幂等：保证组共享会话以 cid=组 id 存在于 DB；source_cid 提供时继承其内容。返回组 cid。"""
        gid = group_cid(group)
        try:
            existing = await cm.db.get_conversation_by_id(cid=gid)
            if existing:
                return gid
        except Exception as e:
            logger.error("[CircleMemory] 查询组会话 %s 失败: %s", gid, e)
            return gid

        source = None
        if source_cid:
            try:
                source = await cm.db.get_conversation_by_id(cid=source_cid)
            except Exception as e:
                logger.error("[CircleMemory] 读取源会话 %s 失败: %s", source_cid, e)

        try:
            await cm.db.create_conversation(
                user_id=(source.user_id if source else "circle_memory"),
                platform_id=(source.platform_id if source else "unknown"),
                content=(source.content or []) if source else [],
                title=(source.title if source else group.get("name")),
                persona_id=(source.persona_id if source else None),
                cid=gid,
            )
            logger.info("[CircleMemory] 组 %s 共享会话已以组 id %s 创建", group.get("name"), gid)
        except Exception as e:
            # 并发竞态下另一任务可能已创建成功（UNIQUE 约束）：视为幂等成功
            try:
                existing = await cm.db.get_conversation_by_id(cid=gid)
                if existing:
                    return gid
            except Exception:
                pass
            logger.error("[CircleMemory] 创建组会话 %s 失败: %s", gid, e)
            return gid

        # 继承完成后清理旧会话（失败仅记日志，下次加载幂等续迁）
        if source_cid and source is not None and source_cid != gid:
            try:
                await cm.db.delete_conversation(cid=source_cid)
                logger.info("[CircleMemory] 旧共享会话 %s 已删除", source_cid)
            except Exception as e:
                logger.error("[CircleMemory] 删除旧会话 %s 失败（下次加载幂等续迁）: %s", source_cid, e)
        return gid

    async def ensure_group_shared(self, group_name: str, umo: str) -> None:
        """把组迁移/补齐为「组 ID 即共享会话 cid」，并切换组内成员。幂等、竞态安全。"""
        lock = self._group_locks.setdefault(group_name, asyncio.Lock())
        async with lock:
            await self._ensure_group_shared_locked(group_name, umo)

    async def _ensure_group_shared_locked(self, group_name: str, umo: str) -> None:
        cm = self.star.context.conversation_manager
        merged = dict(self.star.config.get("merged", {}))
        group = self.find_group(group_name)
        if group is None:
            logger.warning("[CircleMemory] 组 %s 不存在于配置，跳过迁移", group_name)
            return
        gid = group_cid(group)
        if not gid:
            logger.warning("[CircleMemory] 组 %s 缺少 id，跳过迁移（请检查配置）", group_name)
            return
        old_cid = merged.get(group_name)

        if old_cid == gid:
            return

        source_cid = old_cid
        if source_cid is None:
            # 全新组：以当前成员正在使用的会话作为共享历史来源
            try:
                source_cid = await cm.get_curr_conversation_id(umo) or None
            except Exception as e:
                logger.error("[CircleMemory] 读取当前会话失败: %s", e)
                source_cid = None

        await self.ensure_group_conversation(cm, group, source_cid)

        # 组内所有成员切换到组 ID 会话（曾指向旧 uuid 的成员一并迁移）
        for m in group.get("umos", []):
            if not m:
                continue
            try:
                mc = await cm.get_curr_conversation_id(m)
                if not mc or mc != gid:
                    await cm.switch_conversation(m, gid)
            except Exception as e:
                logger.error("[CircleMemory] 成员 %s 切换会话失败: %s", m, e)

        merged[group_name] = gid
        save_merged(self.star.config, merged)

    async def migrate_all_groups(self) -> None:
        """加载后扫描全部组并迁移（幂等；单组失败不阻塞其他组）。"""
        try:
            user_groups = self.star.config.get("user_groups", [])
            for group in user_groups:
                umos = group.get("umos") or []
                try:
                    await self.ensure_group_shared(group.get("name"), umos[0] if umos else "")
                except Exception as e:
                    logger.error("[CircleMemory] 组 %s 迁移失败: %s", group.get("name"), e)
        except Exception as e:
            logger.error("[CircleMemory] 全量迁移失败: %s", e)

    # ---------- LLM 请求前切换 ----------

    async def ensure_llm_shared(self, event) -> None:
        """LLM 请求前把组内会话切到共享 conversation。任何异常交由调用方放行原流程。"""
        if not self.star.config.get("enabled", True):
            return

        umo = event.unified_msg_origin or ""
        if not umo:
            return

        # 运行时防御：组缺 id（配置被外部编辑）→ 立即补齐并持久化
        groups = self.star.config.get("user_groups", [])
        if any(not g.get("id") for g in groups):
            groups = normalize_groups(groups)
            self.star.config["user_groups"] = groups
            save_user_groups(self.star.config, groups)

        group = group_for_umo(groups, umo)
        if group is None:
            return

        target = self.find_group(group)
        if target is None:
            return
        gid = group_cid(target)
        if not gid:
            return

        cm = self.star.context.conversation_manager
        merged = dict(self.star.config.get("merged", {}))

        # 组共享会话未就位（新组/旧版 uuid/迁移中断）→ 幂等补齐并迁移
        if merged.get(group) != gid:
            await self.ensure_group_shared(group, umo)
            merged = dict(self.star.config.get("merged", {}))

        cid = await cm.get_curr_conversation_id(umo)
        if cid != gid:
            await cm.switch_conversation(umo, gid)
            logger.info("[CircleMemory] 会话 %s 已切到组 %s 共享会话 %s", umo, group, gid)
