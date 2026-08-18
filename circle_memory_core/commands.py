"""/shared 命令处理器。

依赖注入：config（插件配置）、context（AstrBot Star context）、
codes（CodeManager）、sessions（SharedSessionManager）。
"""

import logging

from circle_memory_core.constants import KNOWN_COMMANDS, MAX_GROUP_NAME_LEN, USAGE_TEXT
from circle_memory_core.groups import (
    can_query_group_id,
    group_cid,
    group_for_umo,
    list_group_views,
    normalize_groups,
    resolve_target_group,
    transfer_ownership,
    valid_group_name,
)
from circle_memory_core.storage import find_group, save_user_groups

logger = logging.getLogger(__name__)


class CommandHandlers:
    def __init__(self, star, codes, sessions) -> None:
        self.star = star
        self.codes = codes
        self.sessions = sessions

    # ---------- 路由 ----------

    async def dispatch(self, event, cmd: str, arg: str) -> None:
        if cmd == "create":
            await self.cmd_create(event, arg)
        elif cmd == "join":
            # 组名可含空格：从右侧切出最后一个词作为验证码
            name, _, code = arg.rpartition(" ")
            await self.cmd_join(event, name.strip(), code.strip())
        elif cmd == "leave":
            await self.cmd_leave(event, arg)
        elif cmd == "list":
            await self.cmd_list(event)
        elif cmd == "dissolve":
            await self.cmd_dissolve(event, arg)
        elif cmd == "code":
            await self.cmd_code(event, arg)
        elif cmd == "id":
            await self.cmd_id(event, arg)
        else:
            await event.send(event.plain_result(USAGE_TEXT))

    # ---------- 创建 ----------

    async def cmd_create(self, event, name: str) -> None:
        if not name:
            await event.send(event.plain_result("用法: /shared create <组名>"))
            return
        if not valid_group_name(name):
            await event.send(event.plain_result(
                f"组名不合法：需为 1-{MAX_GROUP_NAME_LEN} 个可见字符，且不含换行/控制字符"
            ))
            return
        umo = event.unified_msg_origin or ""
        user_groups = list(self.star.config.get("user_groups", []))
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
        save_user_groups(self.star.config, user_groups)
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

    # ---------- 加入 ----------

    async def cmd_join(self, event, name: str, code: str) -> None:
        if not name or not code:
            await event.send(event.plain_result("用法: /shared join <组名> <验证码>"))
            return
        umo = event.unified_msg_origin or ""
        user_groups = list(self.star.config.get("user_groups", []))
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

        ok, msg = self.codes.verify(name, code)
        if not ok:
            await event.send(event.plain_result(msg))
            return
        self.codes.pop(name)

        target["umos"] = list(target.get("umos", [])) + [umo]
        save_user_groups(self.star.config, user_groups)
        gid = group_cid(target)
        await event.send(event.plain_result(
            f"验证码正确，已将当前会话加入组「{name}」（下次对话起与组内其他会话共享上下文）。\n"
            f"组 ID: {gid}"
        ))

    # ---------- 退出 ----------

    async def cmd_leave(self, event, name: str) -> None:
        """退出组。组名可省略：省略时退出当前会话所在组。"""
        umo = event.unified_msg_origin or ""
        user_groups = list(self.star.config.get("user_groups", []))
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
        cm = self.star.context.conversation_manager
        try:
            await cm.new_conversation(umo)
        except Exception as e:
            logger.error("[CircleMemory] 重置会话失败，已取消退出: %s", e)
            await event.send(event.plain_result("退出失败：会话重置出错，请重试"))
            return

        # 2. 移除成员；创建者退出 → 移交组管理员；最后一个会话退出 → 物理删除共享会话并自动解散组
        target["umos"] = [u for u in target.get("umos", []) if u != umo]
        remaining = target.get("umos", [])
        if remaining and target.get("owner") == umo:
            transfer_ownership(target)
        if not remaining:
            await self.sessions.delete_shared_conversation(cm, target_name)
            user_groups = [g for g in user_groups if g.get("name") != target_name]
            merged = dict(self.star.config.get("merged", {}))
            merged.pop(target_name, None)
            self.star.config["merged"] = merged
        save_user_groups(self.star.config, user_groups)

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

    # ---------- 列表 ----------

    async def cmd_list(self, event) -> None:
        """查看组列表。权限：组外普通会话仅见组名；组内成员见自己组明细；管理员全量可见。"""
        user_groups = self.star.config.get("user_groups", [])
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

    # ---------- 查 ID ----------

    async def cmd_id(self, event, name: str = "") -> None:
        """查看组 ID（其他插件可直接用作会话 ID）。

        权限：组内成员可查自己所在组；管理员可查任意组（含组外）；
        组外普通会话不可查询。
        """
        umo = event.unified_msg_origin or ""
        user_groups = self.star.config.get("user_groups", [])
        is_admin = getattr(event, "role", "") == "admin"

        if name:
            # 显式指定组名：管理员可查任意组；普通会话仅限自己所在组
            if not can_query_group_id(user_groups, umo, is_admin, name):
                await event.send(event.plain_result(f"你不是组「{name}」的成员，无法查询其 ID"))
                return
            target = find_group(self.star.config, name)
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
            target = find_group(self.star.config, cur)
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

    # ---------- 解散 ----------

    async def cmd_dissolve(self, event, name: str) -> None:
        """解散组。仅创建组的会话（组管理员）可操作；组名可省略：省略时取当前会话所在组。

        兼容兜底：若组缺少 owner（异常数据），平台管理员可代为解散。
        """
        umo = event.unified_msg_origin or ""
        user_groups = list(self.star.config.get("user_groups", []))
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
        cm = self.star.context.conversation_manager
        # 1. 先重置所有成员为独立空对话（成功才继续；任一失败则中止解散）
        for member in members:
            try:
                await cm.new_conversation(member)
            except Exception as e:
                logger.error("[CircleMemory] 重置会话 %s 失败，已取消解散: %s", member, e)
                await event.send(event.plain_result("解散失败：会话重置出错，请重试"))
                return

        # 2. 物理删除共享会话（含全部共享历史）
        await self.sessions.delete_shared_conversation(cm, target_name)

        # 3. 移除组配置
        user_groups = [g for g in user_groups if g.get("name") != target_name]
        merged = dict(self.star.config.get("merged", {}))
        merged.pop(target_name, None)
        self.star.config["merged"] = merged
        save_user_groups(self.star.config, user_groups)

        await event.send(event.plain_result(
            f"组「{target_name}」已解散。所有成员已断开共享上下文，共享历史已删除。"
        ))

    # ---------- 邀请码 ----------

    async def cmd_code(self, event, name: str = "") -> None:
        """生成组邀请码。仅组内成员可用。组名可省略：省略时取当前会话所在组。"""
        umo = event.unified_msg_origin or ""
        user_groups = self.star.config.get("user_groups", [])
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

        code = self.codes.issue(name)
        await event.send(event.plain_result(
            f"组「{name}」邀请码: {code}\n"
            f"5 分钟内有效，把此码发给想加入的人，\n"
            f"让对方在对应平台发送 /shared join {name} {code}"
        ))
