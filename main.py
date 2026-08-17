"""跨平台共享上下文插件。

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
"""

import asyncio
import fnmatch
import logging
import secrets
import sys
import time

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import on_waiting_llm_request
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request

logger = logging.getLogger(__name__)

PLUGIN_NAME = "astrbot_plugin_shared_context"

CODE_TTL = 300  # 验证码有效期（秒）
MAX_CODE_ATTEMPTS = 5  # 验证码最大尝试次数（防暴力枚举）
MAX_GROUP_NAME_LEN = 32  # 组名最大长度
# 必须高于 AstrBot 内置 handle_session_control_agent 的 priority=maxsize，
# 否则活跃 agent 续聊会先 stop_event()，我们的命令 handler 永远不会执行。
PRIORITY = sys.maxsize + 1

KNOWN_COMMANDS = frozenset({"create", "code", "join", "leave", "dissolve", "id", "list"})


def is_shared_command(text: str) -> bool:
    """判断消息是否为 /shared 命令。

    兼容两种形式：带斜杠（/shared id）与唤醒词剥离后的无斜杠形式（shared id）。
    仅当第二个词命中已知子命令（create/code/join/leave/dissolve/id/list）时
    才视为命令，避免误拦截 "shared documents please" 这类普通聊天。
    """
    t = text.strip()
    if not t:
        return False
    if t == "shared" or t == "/shared":
        return True
    if t.startswith("shared ") or t.startswith("/shared "):
        parts = t.lstrip("/").split(maxsplit=2)
        return len(parts) >= 2 and parts[1] in KNOWN_COMMANDS
    return False


def _valid_group_name(name: str) -> bool:
    """组名校验：非空、长度受限、不含控制字符。"""
    if not name or len(name) > MAX_GROUP_NAME_LEN:
        return False
    return not any(ord(c) < 32 for c in name)


def umo_match(pattern: str, umo: str) -> bool:
    p = pattern.split(":", 2)
    u = umo.split(":", 2)
    if len(p) != 3 or len(u) != 3:
        return False
    return all(pp == "" or fnmatch.fnmatchcase(tt, pp) for pp, tt in zip(p, u))


def group_for_umo(user_groups: list, umo: str) -> str | None:
    for group in user_groups:
        if any(umo_match(p, umo) for p in group.get("umos", [])):
            return group.get("name")
    return None


def generate_group_id(existing_ids: set) -> str:
    """生成唯一组 ID：g-<8位hex>（不与既有 ID 冲突）。"""
    while True:
        gid = "g-" + secrets.token_hex(4)
        if gid not in existing_ids:
            return gid


def normalize_groups(user_groups: list) -> list:
    """为缺失或重复 id 的组补充唯一短 ID；缺失 owner 的组补创建者；不修改入参。

    - 重复 id 会导致两个组共享同一共享会话（cid），互相污染历史，
      因此第二个出现的重复 id 会被重新分配。
    - owner（组管理员，即创建组的会话）缺失时默认第一个成员，
      兼容 1.1.2 及更早版本创建的组。
    """
    seen: set = set()
    out = []
    for group in user_groups:
        group = dict(group)
        gid = group.get("id") or ""
        if not gid or gid in seen:
            gid = generate_group_id(seen)
            group["id"] = gid
        seen.add(gid)
        if not group.get("owner"):
            members = group.get("umos") or []
            group["owner"] = members[0] if members else None
        out.append(group)
    return out


def group_cid(group: dict) -> str:
    """组共享会话的 conversation_id = 组 id（其他插件可直接使用）。"""
    return group.get("id") or ""


def resolve_target_group(arg: str, user_groups: list, umo: str) -> str | None:
    """解析命令目标组：显式参数优先；否则取当前会话所在组；两者皆无返回 None。"""
    if arg:
        return arg
    return group_for_umo(user_groups, umo)


def list_group_views(user_groups: list, umo: str, is_admin: bool = False) -> list[dict]:
    """按权限计算组列表可见性，供 /shared list 渲染。

    权限规则（组 ID 是共享会话的授权凭证，不得向组外泄露）：
    - 管理员（含组外）：全部组显示 ID 与成员明细；
    - 组内成员：自己所在的组显示 ID 与成员明细，其他组仅显示组名；
    - 组外普通会话：全部组仅显示组名。
    """
    views = []
    for g in user_groups:
        members = list(g.get("umos", []))
        name = g.get("name")
        if is_admin or umo in members:
            views.append({
                "name": name,
                "id": group_cid(g),
                "members": members,
                "owner": g.get("owner"),
                "is_member": umo in members,
            })
        else:
            views.append({"name": name, "is_member": False})
    return views


def can_query_group_id(user_groups: list, umo: str, is_admin: bool, target_name: str) -> bool:
    """查询组 ID 的权限：管理员可查任意组；普通会话仅限自己所在组。"""
    if is_admin:
        return True
    return group_for_umo(user_groups, umo) == target_name


def transfer_ownership(group: dict) -> None:
    """组创建者退出后：将组主（组管理员）移交给剩余成员中第一个（跳过本人）；无剩余成员则置空。"""
    current = group.get("owner")
    members = [m for m in (group.get("umos") or []) if m != current]
    group["owner"] = members[0] if members else None


class SharedContextStar(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self._pending_codes: dict[str, dict] = {}  # umo -> {"code", "expires"}
        self._group_locks: dict[str, asyncio.Lock] = {}  # 组名 -> 迁移/补齐锁

        # 为旧配置中缺失 id 的组补充组 ID（幂等）
        try:
            user_groups = normalize_groups(self.config.get("user_groups", []))
            if user_groups != self.config.get("user_groups", []):
                self.config["user_groups"] = user_groups
                self._save_user_groups(user_groups)
        except Exception as e:
            logger.error("[SharedContext] 组 ID 规范化失败: %s", e)

        # 迁移在 initialize()（激活时，事件循环内）与首次 LLM 请求（懒迁移）执行

    async def initialize(self) -> None:
        """插件激活时执行：迁移旧版共享会话（uuid → 组 ID）。幂等。"""
        try:
            await super().initialize()
        except Exception as e:
            logger.debug("[SharedContext] 基类 initialize 异常（忽略）: %s", e)
        await self._migrate_all_groups()

    # ---------- 命令拦截（AdapterMessageEvent，早于 follow_up）----------

    @filter.event_message_type(filter.EventMessageType.ALL, priority=PRIORITY)
    async def on_adapter_message(self, event: AstrMessageEvent):
        if not self.config.get("enabled", True):
            return
        text = event.message_str.strip()
        logger.debug("[SharedContext] 收到消息: %s (umo=%s)", text[:40], event.unified_msg_origin)
        # waking_check 已剥离 wake_prefix（/），所以同时接受 "shared" 与 "/shared"。
        # 只有真正命中已知命令才拦截；以 shared 开头的普通聊天一律放行。
        if not is_shared_command(text):
            return
        parts = text.lstrip("/").split(maxsplit=2)
        cmd = parts[1] if len(parts) > 1 else ""
        if cmd not in KNOWN_COMMANDS:
            logger.debug("[SharedContext] 非命令的 shared 开头消息，放行: %s", text[:40])
            return

        # 处理命令并阻止后续流程（含 follow_up）
        try:
            await self._handle_command(event, text)
        except Exception as e:
            logger.error("[SharedContext] 命令处理失败: %s", e, exc_info=True)
            await event.send(event.plain_result("命令执行出错，请重试"))
        event.stop_event()

    async def _handle_command(self, event: AstrMessageEvent, text: str):
        # 兼容带/或不带/（waking_check 已剥离前缀）
        parts = text.lstrip("/").split(maxsplit=2)
        cmd = parts[1] if len(parts) > 1 else ""
        arg = parts[2].strip() if len(parts) > 2 else ""

        if cmd == "create":
            await self._cmd_create(event, arg)
        elif cmd == "join":
            # 组名可含空格：从右侧切出最后一个词作为验证码
            name, _, code = arg.rpartition(" ")
            await self._cmd_join(event, name.strip(), code.strip())
        elif cmd == "leave":
            await self._cmd_leave(event, arg)
        elif cmd == "list":
            await self._cmd_list(event)
        elif cmd == "dissolve":
            await self._cmd_dissolve(event, arg)
        elif cmd == "code":
            await self._cmd_code(event, arg)
        elif cmd == "id":
            await self._cmd_id(event, arg)
        else:
            await event.send(event.plain_result(
                "用法:\n"
                "/shared create <组名>\n"
                "/shared code [组名]\n"
                "/shared join <组名> <验证码>\n"
                "/shared leave [组名]\n"
                "/shared dissolve [组名]\n"
                "/shared id [组名]（管理员可查任意组）\n"
                "/shared list"
            ))
    async def _cmd_create(self, event: AstrMessageEvent, name: str):
        if not name:
            await event.send(event.plain_result("用法: /shared create <组名>"))
            return
        if not _valid_group_name(name):
            await event.send(event.plain_result(
                f"组名不合法：需为 1-{MAX_GROUP_NAME_LEN} 个可见字符，且不含换行/控制字符"
            ))
            return
        umo = event.unified_msg_origin or ""
        user_groups = list(self.config.get("user_groups", []))
        # 1. 当前会话已在组「name」→ 提示已在组内
        target = next((g for g in user_groups if g.get("name") == name), None)
        if target and umo in target.get("umos", []):
            await event.send(event.plain_result(f"你已在会话组「{name}」中"))
            return
        # 2. 组已存在（当前会话不在）→ 提示用 join 加入
        if target:
            await event.send(event.plain_result(f"组「{name}」已存在，用 /shared join {name} <邀请码> 加入"))
            return
        # 3. 已在其他组 → 拒绝创建（一个会话只属于一个组）
        cur_group = group_for_umo(user_groups, umo)
        if cur_group:
            await event.send(event.plain_result(f"你已在会话组「{cur_group}」中，请先 /shared leave {cur_group} 退出，再创建新组"))
            return
        # 4. 创建新组并自动加入当前会话（创建者即首个成员，也是组管理员）
        user_groups.append({"name": name, "umos": [umo], "owner": umo})
        user_groups = normalize_groups(user_groups)
        self._save_user_groups(user_groups)
        new_group = next((g for g in user_groups if g.get("name") == name), None)
        gid = group_cid(new_group) if new_group else "?"
        await event.send(event.plain_result(
            f"已创建组「{name}」并加入当前会话。\n"
            f"组 ID: {gid}\n"
            f"你是本组的组管理员（创建者），可解散该组。\n"
            f"（其他插件需要会话 ID 时可直接填组 ID）\n"
            f"其他平台加入：在对应平台 /shared code 获取验证码后，\n"
            f"/shared join {name} <验证码>"
        ))
    async def _cmd_join(self, event: AstrMessageEvent, name: str, code: str):
        if not name or not code:
            await event.send(event.plain_result("用法: /shared join <组名> <验证码>"))
            return
        umo = event.unified_msg_origin or ""
        user_groups = list(self.config.get("user_groups", []))
        target = next((g for g in user_groups if g.get("name") == name), None)
        if target is None:
            await event.send(event.plain_result(f"组「{name}」不存在"))
            return
        if umo in target.get("umos", []):
            await event.send(event.plain_result(f"当前会话已在组「{name}」中"))
            return
        # 已在其他组 → 拒绝加入（一个会话只属于一个组）
        cur_group = group_for_umo(user_groups, umo)
        if cur_group:
            await event.send(event.plain_result(f"你已在会话组「{cur_group}」中，请先 /shared leave {cur_group} 退出，再加入「{name}」"))
            return

        ok, msg = self._verify_code(name, code)
        if not ok:
            await event.send(event.plain_result(msg))
            return
        self._pending_codes.pop(name, None)

        target["umos"] = list(target.get("umos", [])) + [umo]
        self._save_user_groups(user_groups)
        gid = group_cid(target)
        await event.send(event.plain_result(
            f"验证码正确，已将当前会话加入组「{name}」（下次对话起与组内其他会话共享上下文）。\n"
            f"组 ID: {gid}"
        ))
    async def _cmd_leave(self, event: AstrMessageEvent, name: str):
        """退出组。组名可省略：省略时退出当前会话所在组。"""
        umo = event.unified_msg_origin or ""
        user_groups = list(self.config.get("user_groups", []))
        target_name = resolve_target_group(name, user_groups, umo)
        if target_name is None:
            await event.send(event.plain_result("你不在任何会话组中，无需退出"))
            return
        target = next((g for g in user_groups if g.get("name") == target_name), None)
        if target is None:
            await event.send(event.plain_result(f"组「{target_name}」不存在"))
            return
        if umo not in target.get("umos", []):
            await event.send(event.plain_result(f"当前会话不在组「{target_name}」中"))
            return

        # 1. 先重置会话为独立空对话（成功才继续；失败则保持组内身份不变）
        cm = self.context.conversation_manager
        try:
            await cm.new_conversation(umo)
        except Exception as e:
            logger.error("[SharedContext] 重置会话失败，已取消退出: %s", e)
            await event.send(event.plain_result("退出失败：会话重置出错，请重试"))
            return

        # 2. 移除成员；创建者退出 → 移交组管理员；最后一个会话退出 → 物理删除共享会话并自动解散组
        target["umos"] = [u for u in target.get("umos", []) if u != umo]
        remaining = target.get("umos", [])
        if remaining and target.get("owner") == umo:
            transfer_ownership(target)
        if not remaining:
            await self._delete_shared_conversation(cm, target_name)
            user_groups = [g for g in user_groups if g.get("name") != target_name]
            merged = dict(self.config.get("merged", {}))
            merged.pop(target_name, None)
            self.config["merged"] = merged
        self._save_user_groups(user_groups)

        if not remaining:
            await event.send(event.plain_result(
                f"已退出组「{target_name}」，组内无剩余成员，组已自动解散。\n"
                f"注意：你已失去该组的共享上下文，之前的对话不会随你跨平台延续。"
            ))
        else:
            await event.send(event.plain_result(
                f"已退出组「{target_name}」。\n"
                f"注意：你已失去该组的共享上下文，之前在其他平台的对话将无法继续接续。"
            ))
    async def _cmd_list(self, event: AstrMessageEvent):
        """查看组列表。权限：组外普通会话仅见组名；组内成员见自己组明细；管理员全量可见。"""
        user_groups = self.config.get("user_groups", [])
        if not user_groups:
            await event.send(event.plain_result("暂无分组。用 /shared create <组名> 创建"))
            return
        umo = event.unified_msg_origin or ""
        is_admin = getattr(event, "role", "") == "admin"
        views = list_group_views(user_groups, umo, is_admin)
        lines = ["现有共享组:"]
        for v in views:
            if "id" in v:
                member_lines = []
                for m in v["members"]:
                    marker = ""
                    if m == v.get("owner"):
                        marker += "（创建者）"
                    if m == umo:
                        marker += " ← 当前会话"
                    member_lines.append(f"    · {m}{marker}")
                lines.append(f"· {v['name']}（id: {v['id']}，{len(v['members'])} 名成员）:\n" + "\n".join(member_lines))
            else:
                lines.append(f"· {v['name']}")
        await event.send(event.plain_result("\n".join(lines)))

    async def _cmd_id(self, event: AstrMessageEvent, name: str = ""):
        """查看组 ID（其他插件可直接用作会话 ID）。

        权限：组内成员可查自己所在组；管理员可查任意组（含组外）；
        组外普通会话不可查询。
        """
        umo = event.unified_msg_origin or ""
        user_groups = self.config.get("user_groups", [])
        is_admin = getattr(event, "role", "") == "admin"

        if name:
            # 显式指定组名：管理员可查任意组；普通会话仅限自己所在组
            if not can_query_group_id(user_groups, umo, is_admin, name):
                await event.send(event.plain_result(f"你不是组「{name}」的成员，无法查询其 ID"))
                return
            target = self._find_group(name)
            if target is None:
                await event.send(event.plain_result(f"组「{name}」不存在"))
                return
            await event.send(event.plain_result(
                f"组「{name}」的 ID: {group_cid(target)}\n"
                f"其他插件需要会话 ID 时，直接填组 ID 即可定位到组共享会话。"
            ))
            return

        # 无参：查询当前会话所在组
        cur = group_for_umo(user_groups, umo)
        if cur:
            target = self._find_group(cur)
            await event.send(event.plain_result(
                f"当前会话所在组: {cur}\n"
                f"组 ID: {group_cid(target) if target else '?'}\n"
                f"其他插件需要会话 ID 时，直接填组 ID 即可定位到组共享会话。"
            ))
            return
        if is_admin:
            await event.send(event.plain_result(
                "当前会话不在任何组中。管理员可用 /shared id <组名> 查询任意组的 ID"
            ))
            return
        await event.send(event.plain_result("你不在任何会话组中，用 /shared create <组名> 创建"))

    async def _cmd_dissolve(self, event: AstrMessageEvent, name: str):
        """解散组。仅创建组的会话（组管理员）可操作；组名可省略：省略时取当前会话所在组。

        兼容兜底：若组缺少 owner（异常数据），平台管理员可代为解散。
        """
        umo = event.unified_msg_origin or ""
        user_groups = list(self.config.get("user_groups", []))
        target_name = resolve_target_group(name, user_groups, umo)
        if target_name is None:
            await event.send(event.plain_result("你不在任何会话组中，无可解散的组"))
            return
        target = next((g for g in user_groups if g.get("name") == target_name), None)
        if target is None:
            await event.send(event.plain_result(f"组「{target_name}」不存在"))
            return
        if umo not in target.get("umos", []):
            await event.send(event.plain_result(f"你不在组「{target_name}」中，无权解散"))
            return
        # 仅创建组的会话（组管理员）可解散；owner 缺失时平台管理员兜底
        if umo != target.get("owner"):
            is_admin = getattr(event, "role", "") == "admin"
            if target.get("owner") or not is_admin:
                await event.send(event.plain_result(
                    f"只有创建组「{target_name}」的会话（组管理员）可以解散该组"
                ))
                return

        members = list(target.get("umos", []))
        cm = self.context.conversation_manager
        # 1. 先重置所有成员为独立空对话（成功才继续；任一失败则中止解散）
        for member in members:
            try:
                await cm.new_conversation(member)
            except Exception as e:
                logger.error("[SharedContext] 重置会话 %s 失败，已取消解散: %s", member, e)
                await event.send(event.plain_result("解散失败：会话重置出错，请重试"))
                return

        # 2. 物理删除共享会话（含全部共享历史）
        await self._delete_shared_conversation(cm, target_name)

        # 3. 移除组配置
        user_groups = [g for g in user_groups if g.get("name") != target_name]
        merged = dict(self.config.get("merged", {}))
        merged.pop(target_name, None)
        self.config["merged"] = merged
        self._save_user_groups(user_groups)

        await event.send(event.plain_result(
            f"组「{target_name}」已解散。所有成员已断开共享上下文，共享历史已删除。"
        ))

    async def _cmd_code(self, event: AstrMessageEvent, name: str = ""):
        """生成组邀请码。仅组内成员可用。组名可省略：省略时取当前会话所在组。"""
        umo = event.unified_msg_origin or ""
        user_groups = self.config.get("user_groups", [])
        name = resolve_target_group(name, user_groups, umo) or ""
        if not name:
            await event.send(event.plain_result("你不在任何组里，无法生成邀请码。请先 /shared create <组名> 创建组"))
            return
        # 仅组内成员可生成邀请码
        target = next((g for g in user_groups if g.get("name") == name), None)
        if target is None:
            await event.send(event.plain_result(f"组「{name}」不存在"))
            return
        if umo not in target.get("umos", []):
            await event.send(event.plain_result(f"你不在组「{name}」中，无权生成邀请码"))
            return

        code = f"{secrets.randbelow(1000000):06d}"
        self._pending_codes[name] = {
            "code": code,
            "expires": time.time() + CODE_TTL,
            "attempts": 0,
        }
        await event.send(event.plain_result(
            f"组「{name}」邀请码: {code}\n"
            f"5 分钟内有效，把此码发给想加入的人，\n"
            f"让对方在对应平台发送 /shared join {name} {code}"
        ))

    async def _delete_shared_conversation(self, cm, group: str):
        """物理删除组共享会话及其全部历史（best-effort，失败仅记日志）。"""
        merged = dict(self.config.get("merged", {}))
        shared_cid = merged.get(group)
        if not shared_cid:
            return
        try:
            # unified_msg_origin 传空字符串：conversation_id 非空时不触碰任何 UMO 的内存映射
            await cm.delete_conversation("", conversation_id=shared_cid)
            logger.info("[SharedContext] 组 %s 共享会话 %s 已物理删除", group, shared_cid)
        except Exception as e:
            logger.error("[SharedContext] 删除共享会话 %s 失败: %s", shared_cid, e)

    def _verify_code(self, group_name: str, code: str) -> tuple[bool, str]:
        """校验组邀请码：未过期、未超限、匹配（任一失败即拒绝）。

        为防止暴力枚举 6 位数字码，每个邀请码最多允许 MAX_CODE_ATTEMPTS 次
        错误尝试，超限立即作废，需组内成员重新生成。
        """
        pending = self._pending_codes.get(group_name)
        if not pending:
            return False, "该组没有有效的邀请码，请让组内成员 /shared code 生成"
        if time.time() > pending["expires"]:
            self._pending_codes.pop(group_name, None)
            return False, "邀请码已过期，请让组内成员重新生成"
        if pending.get("attempts", 0) >= MAX_CODE_ATTEMPTS:
            self._pending_codes.pop(group_name, None)
            return False, "邀请码尝试次数过多已作废，请让组内成员重新生成"
        if code != pending["code"]:
            pending["attempts"] = pending.get("attempts", 0) + 1
            remaining = MAX_CODE_ATTEMPTS - pending["attempts"]
            if remaining <= 0:
                self._pending_codes.pop(group_name, None)
                return False, "邀请码尝试次数过多已作废，请让组内成员重新生成"
            return False, f"邀请码错误（剩余尝试 {remaining} 次），无法加入"
        return True, ""

    # ---------- 核心：LLM 请求前，把组内会话切到共享 conversation ----------

    @on_waiting_llm_request(priority=200)
    async def on_waiting_llm_request(self, event: AstrMessageEvent):
        """LLM 请求前钩子。任何异常都不允许打断用户请求，放行原流程。"""
        try:
            await self._ensure_llm_shared(event)
        except Exception as e:
            logger.error("[SharedContext] 请求前共享处理失败（已放行原流程）: %s", e, exc_info=True)

    async def _ensure_llm_shared(self, event: AstrMessageEvent):
        if not self.config.get("enabled", True):
            return

        umo = event.unified_msg_origin or ""
        if not umo:
            return

        # 运行时防御：组缺 id（配置被外部编辑）→ 立即补齐并持久化
        groups = self.config.get("user_groups", [])
        if any(not g.get("id") for g in groups):
            groups = normalize_groups(groups)
            self.config["user_groups"] = groups
            self._save_user_groups(groups)

        group = group_for_umo(groups, umo)
        if group is None:
            return

        target = self._find_group(group)
        if target is None:
            return
        gid = group_cid(target)
        if not gid:
            return

        cm = self.context.conversation_manager
        merged = dict(self.config.get("merged", {}))

        # 组共享会话未就位（新组/旧版 uuid/迁移中断）→ 幂等补齐并迁移
        if merged.get(group) != gid:
            await self._ensure_group_shared(group, umo)
            merged = dict(self.config.get("merged", {}))

        cid = await cm.get_curr_conversation_id(umo)
        if cid != gid:
            await cm.switch_conversation(umo, gid)
            logger.info("[SharedContext] 会话 %s 已切到组 %s 共享会话 %s", umo, group, gid)

    # ---------- 组共享会话管理（cid = 组 ID） ----------

    async def _ensure_group_conversation(self, cm, group: dict, source_cid: str | None = None) -> str:
        """幂等：保证组共享会话以 cid=组 id 存在于 DB；source_cid 提供时继承其内容。返回组 cid。"""
        gid = group_cid(group)
        try:
            existing = await cm.db.get_conversation_by_id(cid=gid)
            if existing:
                return gid
        except Exception as e:
            logger.error("[SharedContext] 查询组会话 %s 失败: %s", gid, e)
            return gid

        source = None
        if source_cid:
            try:
                source = await cm.db.get_conversation_by_id(cid=source_cid)
            except Exception as e:
                logger.error("[SharedContext] 读取源会话 %s 失败: %s", source_cid, e)

        try:
            await cm.db.create_conversation(
                user_id=(source.user_id if source else "shared_context"),
                platform_id=(source.platform_id if source else "unknown"),
                content=(source.content or []) if source else [],
                title=(source.title if source else group.get("name")),
                persona_id=(source.persona_id if source else None),
                cid=gid,
            )
            logger.info("[SharedContext] 组 %s 共享会话已以组 id %s 创建", group.get("name"), gid)
        except Exception as e:
            # 并发竞态下另一任务可能已创建成功（UNIQUE 约束）：视为幂等成功
            try:
                existing = await cm.db.get_conversation_by_id(cid=gid)
                if existing:
                    return gid
            except Exception:
                pass
            logger.error("[SharedContext] 创建组会话 %s 失败: %s", gid, e)
            return gid

        # 继承完成后清理旧会话（失败仅记日志，下次加载幂等续迁）
        if source_cid and source is not None and source_cid != gid:
            try:
                await cm.db.delete_conversation(cid=source_cid)
                logger.info("[SharedContext] 旧共享会话 %s 已删除", source_cid)
            except Exception as e:
                logger.error("[SharedContext] 删除旧会话 %s 失败（下次加载幂等续迁）: %s", source_cid, e)
        return gid

    async def _ensure_group_shared(self, group_name: str, umo: str):
        """把组迁移/补齐为「组 ID 即共享会话 cid」，并切换组内成员。幂等、竞态安全。"""
        lock = self._group_locks.setdefault(group_name, asyncio.Lock())
        async with lock:
            await self._ensure_group_shared_locked(group_name, umo)

    async def _ensure_group_shared_locked(self, group_name: str, umo: str):
        cm = self.context.conversation_manager
        merged = dict(self.config.get("merged", {}))
        group = self._find_group(group_name)
        if group is None:
            logger.warning("[SharedContext] 组 %s 不存在于配置，跳过迁移", group_name)
            return
        gid = group_cid(group)
        if not gid:
            logger.warning("[SharedContext] 组 %s 缺少 id，跳过迁移（请检查配置）", group_name)
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
                logger.error("[SharedContext] 读取当前会话失败: %s", e)
                source_cid = None

        await self._ensure_group_conversation(cm, group, source_cid)

        # 组内所有成员切换到组 ID 会话（曾指向旧 uuid 的成员一并迁移）
        for m in group.get("umos", []):
            if not m:
                continue
            try:
                mc = await cm.get_curr_conversation_id(m)
                if not mc or mc != gid:
                    await cm.switch_conversation(m, gid)
            except Exception as e:
                logger.error("[SharedContext] 成员 %s 切换会话失败: %s", m, e)

        merged[group_name] = gid
        self.config["merged"] = merged
        try:
            if hasattr(self.config, "save_config"):
                self.config.save_config()
                logger.info("[SharedContext] 组 %s 共享会话已迁移为组 id %s", group_name, gid)
        except Exception as e:
            logger.error("[SharedContext] 保存 merged 失败: %s", e)

    async def _migrate_all_groups(self):
        """加载后扫描全部组并迁移（幂等；单组失败不阻塞其他组）。"""
        try:
            user_groups = self.config.get("user_groups", [])
            for group in user_groups:
                umos = group.get("umos") or []
                try:
                    await self._ensure_group_shared(group.get("name"), umos[0] if umos else "")
                except Exception as e:
                    logger.error("[SharedContext] 组 %s 迁移失败: %s", group.get("name"), e)
        except Exception as e:
            logger.error("[SharedContext] 全量迁移失败: %s", e)

    # ---------- 内部 ----------

    def _find_group(self, name: str) -> dict | None:
        """按组名查找组配置；不存在返回 None（不做假设，调用方自行处理）。"""
        for g in self.config.get("user_groups", []):
            if g.get("name") == name:
                return g
        return None

    def _save_user_groups(self, user_groups: list):
        self.config["user_groups"] = user_groups
        try:
            if hasattr(self.config, "save_config"):
                self.config.save_config()
                logger.info("[SharedContext] 配置已保存")
        except Exception as e:
            logger.error("[SharedContext] 保存配置失败: %s", e)
