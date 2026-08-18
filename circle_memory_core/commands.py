"""/shared 命令处理器。

依赖注入：star（CircleMemoryStar，动态取 config/context）、
codes（CodeManager）、sessions（SharedSessionManager）。
"""

import datetime
import hashlib
import logging
import time

from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.message_session import MessageSession

from circle_memory_core.constants import (
    EXIT_DATA_POLICY_DEFAULT,
    KNOWN_COMMANDS,
    MAX_ALIAS_LEN,
    MAX_GROUP_NAME_LEN,
    USAGE_TEXT,
)
from circle_memory_core.groups import (
    can_query_group_id,
    group_cid,
    group_for_umo,
    list_group_views,
    normalize_groups,
    resolve_alias_target,
    resolve_remove_target,
    resolve_target_group,
    transfer_ownership,
    valid_group_name,
)
from circle_memory_core.storage import (
    append_message_log,
    find_group,
    read_message_log,
    save_aliases,
    save_pins,
    save_user_groups,
    write_group_archive,
    write_personal_archive,
)

logger = logging.getLogger(__name__)


class CommandHandlers:
    # 消息去重时间窗（秒）：同会话同文本在窗口内只记录一次，防事件重复触发双写
    DEDUP_TTL = 180
    # 去重缓存上限（每组），超过后清理过期项
    DEDUP_CACHE_MAX = 500

    def __init__(self, star, codes, sessions) -> None:
        self.star = star
        self.codes = codes
        self.sessions = sessions
        self._msg_dedup: dict[str, dict[str, float]] = {}  # group -> {指纹: 时间戳}

    # ---------- 消息流水（mine_only 数据源；best-effort） ----------

    def record_message(self, event) -> None:
        """记录组内成员消息到流水（跳过命令消息；同文本窗口内去重）。异常不影响主流程。"""
        try:
            if not self.star.config.get("enabled", True):
                return
            umo = event.unified_msg_origin or ""
            if not umo:
                return
            group = group_for_umo(self.star.config.get("user_groups", []), umo)
            if group is None:
                return
            text = event.message_str.strip()
            if not text:
                return
            # 命令过滤：/shared 系列始终跳过；其他 / 开头命令按配置（默认跳过）
            if text.startswith("/shared") or text.startswith("shared "):
                return
            if self.star.config.get("skip_commands", True) and text.startswith("/"):
                return
            # 指纹去重（防事件重复触发）
            sender = event.get_sender_name() or ""
            fp = hashlib.md5(f"{umo}|{sender}|{text}".encode("utf-8")).hexdigest()
            now = time.time()
            cache = self._msg_dedup.setdefault(group, {})
            if len(cache) > self.DEDUP_CACHE_MAX:
                for k in [k for k, v in cache.items() if now - v > self.DEDUP_TTL]:
                    cache.pop(k, None)
            last = cache.get(fp)
            if last is not None and now - last < self.DEDUP_TTL:
                return
            cache[fp] = now
            append_message_log(group, umo, sender, text)
        except Exception as e:
            logger.debug("[CircleMemory] 记录消息流水失败（忽略）: %s", e)

    # ---------- 跨会话通知（best-effort） ----------

    async def _notify_umo(self, umo: str, text: str) -> bool:
        """向指定会话发送一条通知（尽力而为）。返回是否成功。"""
        try:
            platform_id = umo.split(":", 1)[0]
            platform = self.star.context.get_platform_inst(platform_id)
            if not platform:
                return False
            session = MessageSession.from_str(umo)
            chain = MessageChain().message(text)
            await platform.send_by_session(session, chain)
            return True
        except Exception as e:
            logger.warning("[CircleMemory] 通知会话 %s 失败: %s", umo, e)
            return False

    async def _notify_members(self, umos: list, text: str, exclude: set | None = None) -> int:
        """向组内成员广播通知（排除 exclude），返回成功数。"""
        ok = 0
        for umo in umos:
            if exclude and umo in exclude:
                continue
            if await self._notify_umo(umo, text):
                ok += 1
        return ok

    # ---------- 退出数据处理（双策略） ----------

    async def _handle_exit_data(
        self, event, group_name: str, umo: str, sender_name: str, kicked: bool = False
    ) -> None:
        """按 exit_data_policy 处理退出/被踢成员的数据。

        discard（默认）：无任何数据导出。
        mine_only：从消息流水导出该成员自己的发言 → 服务器留档 + 尽力私聊发送；
                   发送失败明确反馈「已保存到服务器但私聊发送失败」。
        """
        policy = self.star.config.get("exit_data_policy", EXIT_DATA_POLICY_DEFAULT)
        if policy != "mine_only":
            return
        records = read_message_log(group_name, umo)
        if not records:
            return
        lines = []
        for r in records:
            sender = r.get("sender") or ""
            ts = r.get("ts") or 0
            t = datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
            lines.append(f"[{t}] {sender}: {r.get('text', '')}")
        if not lines:
            return
        content = "\n".join(lines)
        path = write_personal_archive(group_name, umo, sender_name, records)
        saved = "（已保存到服务器）" if path else "（服务器保存失败）"
        verb = "被移出" if kicked else "退出"
        msg = (
            f"你已{verb}组「{group_name}」。\n"
            f"你在组内说过的话已导出如下，{saved}：\n\n"
            f"{content[:3000]}"
        )
        ok = await self._notify_umo(umo, msg)
        if not ok:
            await event.send(event.plain_result(
                f"注意：{verb}成员的发言记录已保存到服务器，但私聊发送失败（对方可能无法接收私聊）。"
            ))

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
        elif cmd == "remove":
            await self.cmd_remove(event, arg)
        elif cmd == "alias":
            await self.cmd_alias(event, arg)
        elif cmd == "pin":
            await self.cmd_pin(event, arg)
        elif cmd == "summary":
            await self.cmd_summary(event, arg)
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

    async def _reset_session(self, cm, umo: str) -> bool:
        """重置会话为独立空对话；成功返回 True。"""
        try:
            await cm.new_conversation(umo)
            return True
        except Exception as e:
            logger.error("[CircleMemory] 重置会话失败: %s", e)
            return False

    async def _archive_and_remove_group(
        self, cm, group_name: str, members: list
    ) -> None:
        """末人退出/解散：先归档整组历史，再物理删除共享会话并移除组配置。"""
        # 归档（best-effort，失败不阻塞删除）
        try:
            content = await self.sessions.get_group_content(group_name)
            merged = dict(self.star.config.get("merged", {}))
            gid = merged.get(group_name, "")
            if content:
                write_group_archive(group_name, gid, members, content, "解散/末人退出")
        except Exception as e:
            logger.error("[CircleMemory] 归档组 %s 失败（继续删除）: %s", group_name, e)
        await self.sessions.delete_shared_conversation(cm, group_name)
        merged = dict(self.star.config.get("merged", {}))
        merged.pop(group_name, None)
        self.star.config["merged"] = merged

    async def cmd_leave(self, event, name: str) -> None:
        """退出组。组名可省略；支持序号（按 /shared list 顺序）与 all（当前组）。"""
        umo = event.unified_msg_origin or ""
        user_groups = list(self.star.config.get("user_groups", []))
        # 序号解析：/shared leave 1 → list 中第 1 个可见组
        if name.isdigit():
            is_admin = getattr(event, "role", "") == "admin"
            views = list_group_views(user_groups, umo, is_admin)
            idx = int(name) - 1
            if 0 <= idx < len(views) and "id" in views[idx]:
                name = views[idx]["name"]
            else:
                await event.send(event.plain_result(f"序号无效：可见组共 {len(views)} 个"))
                return
        elif name == "all":
            name = ""  # 一个会话只属于一个组，all 等价于退出当前组
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

        remaining_after = [u for u in target.get("umos", []) if u != umo]
        # 1. 先重置会话为独立空对话（成功才继续；失败则保持组内身份不变）
        cm = self.star.context.conversation_manager
        if not await self._reset_session(cm, umo):
            await event.send(event.plain_result("退出失败：会话重置出错，请重试"))
            return

        # 2. 移除成员；创建者退出 → 移交组管理员；最后一个会话退出 → 归档并自动解散组
        target["umos"] = remaining_after
        remaining = target.get("umos", [])
        if remaining and target.get("owner") == umo:
            transfer_ownership(target)
        if not remaining:
            await self._archive_and_remove_group(cm, target_name, list(target.get("umos", [])) + [umo])
            user_groups = [g for g in user_groups if g.get("name") != target_name]
        save_user_groups(self.star.config, user_groups)

        # 3. 通知剩余成员（best-effort）
        if remaining:
            await self._notify_members(
                remaining,
                f"成员「{event.get_sender_name() or umo}」已退出组「{target_name}」（剩余 {len(remaining)} 人）",
            )

        # 4. 按策略处理退出者数据（mine_only 时导出其个人发言）
        await self._handle_exit_data(event, target_name, umo, event.get_sender_name())

        if not remaining:
            await event.send(event.plain_result(
                f"已退出组「{target_name}」，组内无剩余成员，组已自动解散（历史已归档）。\n"
                f"注意：你已失去该组的共享上下文，之前的对话不会随你跨平台延续。"
            ))
        else:
            await event.send(event.plain_result(
                f"已退出组「{target_name}」。\n"
                f"注意：你已失去该组的共享上下文，之前在其他平台的对话将无法继续接续。"
            ))

    # ---------- 踢人（组管理员） ----------

    async def cmd_remove(self, event, arg: str) -> None:
        """将成员移出组。仅组管理员（owner）可操作；owner 本人不可被移除。

        语法：/shared remove [组名] <成员会话ID>（组内可省略组名）。
        被移除成员与主动退出同等处理：会话重置、组 ID 失效、
        通知剩余成员、按 exit_data_policy 处理其数据。
        """
        umo = event.unified_msg_origin or ""
        user_groups = list(self.star.config.get("user_groups", []))
        parsed = resolve_remove_target(arg, user_groups, umo)
        if parsed is None:
            await event.send(event.plain_result(
                "用法: /shared remove [组名] <成员会话ID>（仅组管理员；组内可省略组名）"
            ))
            return
        group_name, target_umo = parsed
        target = next((g for g in user_groups if g.get("name") == group_name), None)
        if target is None:
            await event.send(event.plain_result(f"组「{group_name}」不存在"))
            return

        # 权限：仅组管理员（owner）；平台管理员兜底
        is_admin = getattr(event, "role", "") == "admin"
        if umo != target.get("owner") and not is_admin:
            await event.send(event.plain_result(
                f"只有组管理员（创建者）可以移除成员，你不是组「{group_name}」的管理员"
            ))
            return
        # owner 本人不可被移除
        if target_umo == target.get("owner"):
            await event.send(event.plain_result(
                f"不能移除组管理员本人。如需解散请用 /shared dissolve {group_name}"
            ))
            return
        if target_umo not in target.get("umos", []):
            await event.send(event.plain_result(f"会话 {target_umo} 不在组「{group_name}」中"))
            return
        if target_umo == umo:
            await event.send(event.plain_result("不能移除自己，如需退出请用 /shared leave"))
            return

        # 1. 重置被移除成员会话（失败则中止移除，保持现状）
        cm = self.star.context.conversation_manager
        if not await self._reset_session(cm, target_umo):
            await event.send(event.plain_result(f"移除失败：成员 {target_umo} 会话重置出错，请重试"))
            return

        # 2. 移除成员
        target["umos"] = [u for u in target.get("umos", []) if u != target_umo]
        remaining = target.get("umos", [])
        if not remaining:
            # 组内只剩被移除者 → 归档并解散组
            await self._archive_and_remove_group(cm, group_name, list(target.get("umos", [])) + [target_umo])
            user_groups = [g for g in user_groups if g.get("name") != group_name]
        save_user_groups(self.star.config, user_groups)

        # 3. 通知剩余成员
        if remaining:
            await self._notify_members(
                remaining,
                f"成员 {target_umo} 已被移出组「{group_name}」（剩余 {len(remaining)} 人）",
            )

        # 4. 按策略处理被移除成员数据
        await self._handle_exit_data(event, group_name, target_umo, "", kicked=True)

        if not remaining:
            await event.send(event.plain_result(
                f"已移除 {target_umo}，组内已无剩余成员，组「{group_name}」已解散（历史已归档）。"
            ))
        else:
            await event.send(event.plain_result(
                f"已移除 {target_umo}。对方已失去该组的共享上下文，且无法再查看组 ID。"
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

        需要二次确认：/shared dissolve [组名] 确认（防误触，历史归档后删除）。
        兼容兜底：若组缺少 owner（异常数据），平台管理员可代为解散。
        """
        umo = event.unified_msg_origin or ""
        user_groups = list(self.star.config.get("user_groups", []))
        # 二次确认解析：从右侧切出「确认」标记
        confirm = False
        split = (name or "").rsplit(maxsplit=1)
        if len(split) == 2 and split[1] in ("确认", "yes", "y"):
            confirm = True
            name = split[0]
        elif (name or "").strip() in ("确认", "yes", "y"):
            confirm = True
            name = ""
        if not confirm:
            show = name or "（当前组）"
            await event.send(event.plain_result(
                f"解散将归档并删除组「{show}」的全部共享历史，且不可恢复。\n"
                f"确认请发送：/shared dissolve {name} 确认"
            ))
            return
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
            if not await self._reset_session(cm, member):
                await event.send(event.plain_result("解散失败：会话重置出错，请重试"))
                return

        # 2. 归档整组历史，再物理删除共享会话并移除组配置
        await self._archive_and_remove_group(cm, target_name, members)
        user_groups = [g for g in user_groups if g.get("name") != target_name]
        save_user_groups(self.star.config, user_groups)

        await event.send(event.plain_result(
            f"组「{target_name}」已解散。所有成员已断开共享上下文，共享历史已归档后删除。"
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

    # ---------- 成员昵称（alias） ----------

    async def cmd_alias(self, event, arg: str) -> None:
        """设置/查看成员昵称。

        语法：/shared alias [组名] [<成员会话ID> <昵称>]
        - 无参数：查看当前组昵称
        - 一个词：查看指定组昵称
        - 省略组名：alias <成员UMO> <昵称>（组内）
        - 显式组名：alias <组名> <成员UMO> <昵称>
        - 昵称 "-"：删除该成员昵称
        权限：成员可设置自己的昵称；组管理员（owner）可设置任意成员；平台管理员兜底。
        """
        umo = event.unified_msg_origin or ""
        user_groups = list(self.star.config.get("user_groups", []))
        parts = [p for p in (arg or "").split()]

        # ---- 查看模式 ----
        if len(parts) <= 1:
            group_name = parts[0] if parts else group_for_umo(user_groups, umo)
            if not group_name:
                await event.send(event.plain_result(
                    "用法: /shared alias [组名] <成员会话ID> <昵称>（- 删除；组内可省略组名）"
                ))
                return
            target = find_group(self.star.config, group_name)
            if target is None:
                await event.send(event.plain_result(f"组「{group_name}」不存在"))
                return
            aliases = (self.star.config.get("aliases") or {}).get(group_name) or {}
            lines = [f"组「{group_name}」成员昵称："]
            for m in target.get("umos", []):
                nick = aliases.get(m)
                lines.append(f"  · {m} → {nick if nick else '（未设置）'}")
            await event.send(event.plain_result("\n".join(lines)))
            return

        # ---- 设置/删除模式 ----
        parsed = resolve_alias_target(arg, user_groups, umo)
        if parsed is None:
            await event.send(event.plain_result(
                "用法: /shared alias [组名] <成员会话ID> <昵称>（- 删除；组内可省略组名）"
            ))
            return
        group_name, target_umo, nick = parsed
        target = find_group(self.star.config, group_name)
        if target is None:
            await event.send(event.plain_result(f"组「{group_name}」不存在"))
            return
        if target_umo not in target.get("umos", []):
            await event.send(event.plain_result(f"会话 {target_umo} 不在组「{group_name}」中"))
            return
        # 权限：本人可设自己；owner 可设任意成员；平台管理员兜底
        is_admin = getattr(event, "role", "") == "admin"
        if target_umo != umo and umo != target.get("owner") and not is_admin:
            await event.send(event.plain_result(
                "只能设置自己的昵称，或由组管理员（创建者）设置任意成员"
            ))
            return
        if nick != "-":
            if len(nick) > MAX_ALIAS_LEN or any(ord(c) < 32 for c in nick):
                await event.send(event.plain_result(
                    f"昵称不合法：需为 1-{MAX_ALIAS_LEN} 个可见字符，且不含换行/控制字符"
                ))
                return

        aliases = dict((self.star.config.get("aliases") or {}).get(group_name) or {})
        if nick == "-":
            aliases.pop(target_umo, None)
            msg = f"已删除 {target_umo} 的昵称"
        else:
            aliases[target_umo] = nick
            msg = f"已设置 {target_umo} 的昵称为「{nick}」"
        all_aliases = dict(self.star.config.get("aliases") or {})
        all_aliases[group_name] = aliases
        save_aliases(self.star.config, all_aliases)
        await event.send(event.plain_result(msg))

    # ---------- 组置顶（pin，组管理员） ----------

    async def cmd_pin(self, event, arg: str) -> None:
        """设置/清除组置顶记忆（组管理员）。置顶内容每轮注入共享会话请求最前。

        语法：/shared pin [组名] <内容>；内容为 - 时清除置顶。
        """
        umo = event.unified_msg_origin or ""
        user_groups = list(self.star.config.get("user_groups", []))
        parts = arg.split(maxsplit=1)
        # 解析：一个词=当前组+内容；两个词且首词是组名=组名+内容
        if len(parts) == 1:
            group_name = group_for_umo(user_groups, umo)
            content = parts[0]
        elif len(parts) == 2 and parts[0] in [g.get("name") for g in user_groups]:
            group_name, content = parts
        elif len(parts) == 2:
            group_name = group_for_umo(user_groups, umo)
            content = arg
        else:
            group_name, content = None, ""
        if not group_name:
            await event.send(event.plain_result(
                "用法: /shared pin [组名] <内容>（组管理员；- 清除置顶）"
            ))
            return
        target = find_group(self.star.config, group_name)
        if target is None:
            await event.send(event.plain_result(f"组「{group_name}」不存在"))
            return
        # 权限：仅组管理员（owner）；平台管理员兜底
        is_admin = getattr(event, "role", "") == "admin"
        if umo != target.get("owner") and not is_admin:
            await event.send(event.plain_result(
                f"只有组管理员（创建者）可以设置置顶，你不是组「{group_name}」的管理员"
            ))
            return

        pins = dict(self.star.config.get("pins") or {})
        if content == "-":
            pins.pop(group_name, None)
            msg = f"已清除组「{group_name}」的置顶"
        else:
            if len(content) > 500 or any(ord(c) < 32 for c in content):
                await event.send(event.plain_result("置顶内容不合法：需为 1-500 个可见字符"))
                return
            pins[group_name] = content
            msg = f"已设置组「{group_name}」置顶（每轮共享会话请求都会带上）"
        save_pins(self.star.config, pins)
        await event.send(event.plain_result(msg))

    # ---------- 组摘要（组管理员） ----------

    async def cmd_summary(self, event, arg: str = "") -> None:
        """生成组共享历史摘要（组管理员）。调用当前会话 provider，失败给出提示。"""
        umo = event.unified_msg_origin or ""
        user_groups = list(self.star.config.get("user_groups", []))
        group_name = resolve_target_group(arg, user_groups, umo)
        if not group_name:
            await event.send(event.plain_result("你不在任何会话组中，无可摘要的组"))
            return
        target = find_group(self.star.config, group_name)
        if target is None:
            await event.send(event.plain_result(f"组「{group_name}」不存在"))
            return
        if umo not in target.get("umos", []):
            await event.send(event.plain_result(f"你不在组「{group_name}」中，无权生成摘要"))
            return
        # 权限：仅组管理员（owner）；平台管理员兜底
        is_admin = getattr(event, "role", "") == "admin"
        if umo != target.get("owner") and not is_admin:
            await event.send(event.plain_result(
                f"只有组管理员（创建者）可以生成摘要，你不是组「{group_name}」的管理员"
            ))
            return

        content = await self.sessions.get_group_content(group_name)
        if not content:
            await event.send(event.plain_result(f"组「{group_name}」暂无共享历史"))
            return
        try:
            provider = self.star.context.get_using_provider()
            if not provider:
                await event.send(event.plain_result("当前没有可用的 LLM provider，无法生成摘要"))
                return
            texts = []
            for c in content:
                ct = c.get("content", "")
                if isinstance(ct, str) and ct:
                    texts.append(ct)
                elif isinstance(ct, list):
                    texts.append("".join(
                        p.get("text", "") for p in ct if isinstance(p, dict) and p.get("type") == "text"
                    ))
            sample = "\n".join(texts)[-6000:]
            resp = await provider.text_chat(
                prompt=(
                    f"以下是共享会话组「{group_name}」的历史消息，请用中文生成一份不超过 "
                    "600 字的会话摘要（要点式，覆盖主要话题与结论）：\n\n" + sample
                ),
                persist=False,
            )
            summary = (getattr(resp, "completion_text", "") or "").strip()
            if not summary:
                await event.send(event.plain_result("摘要生成失败：模型返回为空"))
                return
            await event.send(event.plain_result(
                f"组「{group_name}」会话摘要（{len(content)} 条消息）：\n\n{summary}"
            ))
        except Exception as e:
            logger.error("[CircleMemory] 生成组摘要失败: %s", e)
            await event.send(event.plain_result("摘要生成失败，请稍后重试"))
