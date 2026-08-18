"""组领域纯函数：命令识别、组名校验、UMO 匹配、组 ID、权限视图。

全部为纯函数（不依赖 event / config 对象），可直接单测。
"""

import fnmatch
import secrets

from circle_memory_core.constants import KNOWN_COMMANDS, MAX_GROUP_NAME_LEN


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


def valid_group_name(name: str) -> bool:
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


def resolve_remove_target(arg: str, user_groups: list, umo: str) -> tuple[str, str] | None:
    """解析 /shared remove 参数 → (组名, 目标成员 UMO)。

    两种形式：
    - 显式组名：`remove <组名> <成员UMO>`（组外管理员可用）
    - 省略组名：`remove <成员UMO>`（当前会话必须已在某组，即组管理员在自己的组内踢人）
    返回 None 表示参数无法解析。
    """
    parts = [p for p in (arg or "").split() if p]
    if not parts:
        return None
    if len(parts) == 1:
        # 省略组名：目标成员 UMO 即唯一参数
        target_umo = parts[0]
        group_name = group_for_umo(user_groups, umo)
        if not group_name:
            return None
        return group_name, target_umo
    if len(parts) == 2:
        group_name, target_umo = parts
        # 仅当第一个词是已知组名时才按「组名+成员」解析，否则视为无法解析
        if group_name in [g.get("name") for g in user_groups]:
            return group_name, target_umo
        return None
    return None
