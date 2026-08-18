"""astrbot_plugin_circle_memory：跨平台共享上下文插件（入口薄壳）。

同一用户组的会话共享同一份对话上下文：
在飞书聊完，微信/QQ 接着聊，历史按时间顺序在同一份 conversation 里。

原理：LLM 请求前（on_waiting_llm_request，在 build 之前触发），
把组内所有 UMO 的 conversation 切换到组共享的 conversation_id。
AstrBot 原生按 UMO → conversation_id 读写历史，因此天然共享，O(1) 无注入。

会话组 ID：每个组拥有稳定短 ID（g-<8位hex>），组共享会话在数据库中
以「组 ID」作为真实 conversation_id（cid）。因此任何其他插件按
conversation_id 读写会话时（get_conversation / add_message_pair /
get_conversation_by_id / update_conversation …），直接填写组 ID 即可
定位到组共享会话，无需其他插件做任何改动。
（旧版组的共享会话为随机 uuid：加载时自动迁移到组 ID，历史保留。）

命令（/shared）通过 AdapterMessageEvent 高优先级手动拦截，
避免被 follow_up（活跃 agent 续聊）机制吞掉；仅已知子命令会被
拦截，以 shared 开头的普通聊天放行。邀请码 5 次错误尝试即作废：
  /shared create <group>          创建共享组
  /shared code                    获取当前会话验证码（随机码，5 分钟有效）
  /shared join <group> <code>     把当前会话加入组
  /shared leave <group>           把当前会话移出组
  /shared id                      查看当前会话所在组的 ID（其他插件可直接使用）
  /shared dissolve <group>        解散组
  /shared list                    查看所有组及成员

模块结构：
  circle_memory_core/  内核模块包
    constants.py       常量与配置项
    groups.py          组领域纯函数
    codes.py           邀请码管理
    storage.py         配置读写
    shared_session.py  会话合并引擎
    commands.py        命令处理器
"""

import logging
import os
import sys

# AstrBot 以 data.plugins.<插件目录>.main 方式加载插件，插件目录本身
# 不在 sys.path；此处显式加入，使 circle_memory_core 子包可被导入。
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import on_llm_request, on_waiting_llm_request
from astrbot.api.star import Context, Star

from circle_memory_core.codes import CodeManager
from circle_memory_core.commands import CommandHandlers
from circle_memory_core.context_enhance import ContextEnhancer
from circle_memory_core.constants import (
    KNOWN_COMMANDS,
    MAX_CODE_ATTEMPTS,
    PLUGIN_NAME,
    PRIORITY,
)
from circle_memory_core.groups import (
    can_query_group_id,
    generate_group_id,
    group_cid,
    group_for_umo,
    is_shared_command,
    list_group_views,
    normalize_groups,
    resolve_alias_target,
    resolve_remove_target,
    resolve_target_group,
    transfer_ownership,
    umo_match,
    valid_group_name,
)
from circle_memory_core.shared_session import SharedSessionManager
from circle_memory_core.storage import save_user_groups

logger = logging.getLogger(__name__)

# 兼容旧 import（测试脚本直接引用）：保持符号可从 main 导入
__all__ = [
    "CircleMemoryStar",
    "PLUGIN_NAME",
    "MAX_CODE_ATTEMPTS",
    "is_shared_command",
    "valid_group_name",
    "_valid_group_name",
    "umo_match",
    "group_for_umo",
    "generate_group_id",
    "normalize_groups",
    "group_cid",
    "resolve_alias_target",
    "resolve_remove_target",
    "resolve_target_group",
    "list_group_views",
    "can_query_group_id",
    "transfer_ownership",
]

# 旧版符号兼容：1.x 时代 main 导出的下划线版本
_valid_group_name = valid_group_name


class CircleMemoryStar(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.codes = CodeManager()
        self.sessions = SharedSessionManager(self)
        self.handlers = CommandHandlers(self, self.codes, self.sessions)
        self.enhancer = ContextEnhancer(self)

        # 为旧配置中缺失 id 的组补充组 ID（幂等）
        try:
            user_groups = normalize_groups(self.config.get("user_groups", []))
            if user_groups != self.config.get("user_groups", []):
                self.config["user_groups"] = user_groups
                save_user_groups(self.config, user_groups)
        except Exception as e:
            logger.error("[CircleMemory] 组 ID 规范化失败: %s", e)

        # 迁移在 initialize()（激活时，事件循环内）与首次 LLM 请求（懒迁移）执行

    async def initialize(self) -> None:
        """插件激活时执行：迁移旧版共享会话（uuid → 组 ID）。幂等。"""
        try:
            await super().initialize()
        except Exception as e:
            logger.debug("[CircleMemory] 基类 initialize 异常（忽略）: %s", e)
        await self.sessions.migrate_all_groups()

    # ---------- 命令拦截（AdapterMessageEvent，早于 follow_up）----------

    @filter.event_message_type(filter.EventMessageType.ALL, priority=PRIORITY)
    async def on_adapter_message(self, event: AstrMessageEvent):
        if not self.config.get("enabled", True):
            return
        text = event.message_str.strip()
        logger.debug("[CircleMemory] 收到消息: %s (umo=%s)", text[:40], event.unified_msg_origin)
        # waking_check 已剥离 wake_prefix（/），所以同时接受 "shared" 与 "/shared"。
        # 只有真正命中已知命令才拦截；以 shared 开头的普通聊天一律放行。
        if not is_shared_command(text):
            # 非命令消息：记录组内消息流水（mine_only 数据源，best-effort）
            self.handlers.record_message(event)
            return
        parts = text.lstrip("/").split(maxsplit=2)
        cmd = parts[1] if len(parts) > 1 else ""
        if cmd not in KNOWN_COMMANDS:
            logger.debug("[CircleMemory] 非命令的 shared 开头消息，放行: %s", text[:40])
            return

        # 处理命令并阻止后续流程（含 follow_up）
        try:
            arg = parts[2].strip() if len(parts) > 2 else ""
            await self.handlers.dispatch(event, cmd, arg)
        except Exception as e:
            logger.error("[CircleMemory] 命令处理失败: %s", e, exc_info=True)
            await event.send(event.plain_result("命令执行出错，请重试"))
        event.stop_event()

    # ---------- 兼容代理（既有测试/第三方调用入口；逻辑在 handlers/sessions/codes）----------

    @property
    def _pending_codes(self):
        return self.codes._pending_codes

    def _verify_code(self, group_name: str, code: str) -> tuple[bool, str]:
        return self.codes.verify(group_name, code)

    async def _ensure_group_shared(self, group_name: str, umo: str):
        return await self.sessions.ensure_group_shared(group_name, umo)

    async def _cmd_create(self, event, name: str):
        return await self.handlers.cmd_create(event, name)

    async def _cmd_join(self, event, name: str, code: str):
        return await self.handlers.cmd_join(event, name, code)

    async def _cmd_leave(self, event, name: str):
        return await self.handlers.cmd_leave(event, name)

    async def _cmd_remove(self, event, arg: str):
        return await self.handlers.cmd_remove(event, arg)

    async def _cmd_alias(self, event, arg: str = ""):
        return await self.handlers.cmd_alias(event, arg)

    async def _cmd_list(self, event):
        return await self.handlers.cmd_list(event)

    async def _cmd_id(self, event, name: str = ""):
        return await self.handlers.cmd_id(event, name)

    async def _cmd_dissolve(self, event, name: str):
        return await self.handlers.cmd_dissolve(event, name)

    async def _cmd_code(self, event, name: str = ""):
        return await self.handlers.cmd_code(event, name)

    # ---------- 核心：LLM 请求前，把组内会话切到共享 conversation ----------

    @on_waiting_llm_request(priority=200)
    async def on_waiting_llm_request(self, event: AstrMessageEvent):
        """LLM 请求前钩子（build 之前）：切换共享会话。异常放行原流程。"""
        try:
            await self.sessions.ensure_llm_shared(event)
        except Exception as e:
            logger.error("[CircleMemory] 请求前共享处理失败（已放行原流程）: %s", e, exc_info=True)

    @on_llm_request(priority=100)
    async def on_llm_request_hook(self, event: AstrMessageEvent, req):
        """LLM 请求构建后钩子：共享会话上下文增强（媒体降级/成员标注/预算折叠）。"""
        try:
            await self.enhancer.apply(event, req)
        except Exception as e:
            logger.error("[CircleMemory] 请求增强失败（已放行原请求）: %s", e)
