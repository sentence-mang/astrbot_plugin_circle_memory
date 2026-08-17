"""纯函数最小单测（无框架依赖）。

运行（容器内）:
    docker exec astrbot python3 data/plugins/astrbot_plugin_shared_context/test_pure.py
"""

import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from main import (  # noqa: E402
    MAX_CODE_ATTEMPTS,
    SharedContextStar,
    _valid_group_name,
    can_query_group_id,
    generate_group_id,
    group_cid,
    group_for_umo,
    is_shared_command,
    list_group_views,
    normalize_groups,
    resolve_target_group,
    transfer_ownership,
    umo_match,
)


def test_umo_match():
    # 精确匹配
    assert umo_match("feishu::u1", "feishu::u1")
    # 平台不同 → 不匹配
    assert not umo_match("feishu::u1", "qq::u1")
    # 通配符：空段匹配任意
    assert umo_match("feishu::", "feishu::u1")
    assert umo_match("feishu:g1:", "feishu:g1:u1")
    assert not umo_match("feishu:g1:", "feishu:g2:u1")
    # 段数不足 → 不匹配
    assert not umo_match("feishu", "feishu::u1")


def test_group_for_umo():
    groups = [
        {"name": "g1", "umos": ["feishu::u1"]},
        {"name": "g2", "umos": ["qq::", "wechat::u2"]},
    ]
    assert group_for_umo(groups, "feishu::u1") == "g1"
    assert group_for_umo(groups, "qq::u9") == "g2"
    assert group_for_umo(groups, "telegram::u1") is None
    assert group_for_umo([], "feishu::u1") is None


def test_verify_code():
    s = SharedContextStar(None, {})
    # 无码
    assert not s._verify_code("g", "123456")[0]
    # 有效
    s._pending_codes["g"] = {"code": "123456", "expires": time.time() + 100}
    assert s._verify_code("g", "123456")[0]
    # 错误码
    assert not s._verify_code("g", "000000")[0]
    # 过期（过期后清除 pending 码）
    s._pending_codes["g"] = {"code": "123456", "expires": time.time() - 1}
    assert not s._verify_code("g", "123456")[0]
    assert "g" not in s._pending_codes


def test_group_id_format_and_uniqueness():
    """组 ID 形如 g-<8位hex>，且不与既有 ID 冲突。"""
    existing = {"g-aaaaaaaa", "g-bbbbbbbb"}
    for _ in range(50):
        gid = generate_group_id(existing)
        assert re.fullmatch(r"g-[0-9a-f]{8}", gid), f"非法格式: {gid}"
        assert gid not in existing
    # 空既有集
    gid = generate_group_id(set())
    assert re.fullmatch(r"g-[0-9a-f]{8}", gid)


def test_normalize_groups_assigns_and_keeps_ids():
    """无 id 的组自动补 id；已有 id 保留；重复调用幂等（不重复分配）。"""
    groups = [
        {"name": "g1", "umos": ["feishu::u1"]},
        {"name": "g2", "id": "g-abcdef12", "umos": ["qq::u2"]},
    ]
    out1 = normalize_groups(groups)
    assert len(out1) == 2
    # g1 补了 id，g2 保留原 id
    assert re.fullmatch(r"g-[0-9a-f]{8}", out1[0]["id"])
    assert out1[1]["id"] == "g-abcdef12"
    # 幂等：再次调用不改变 id，也不改变原有字段
    out2 = normalize_groups(out1)
    assert out2[0]["id"] == out1[0]["id"]
    assert out2[1]["id"] == "g-abcdef12"
    assert out2[0]["umos"] == ["feishu::u1"]
    assert out2[1]["umos"] == ["qq::u2"]
    # 不修改入参
    assert "id" not in groups[0]


def test_group_cid_is_group_id():
    """组共享会话的 cid 恒等于组 id（其他插件按组 id 即可定位）。"""
    g = {"name": "g1", "id": "g-abcdef12", "umos": []}
    assert group_cid(g) == "g-abcdef12"


def test_is_shared_command_only_intercepts_known_commands():
    """只拦截 /shared 命令；以 shared 开头的普通聊天必须放行。"""
    # 命令形式（兼容唤醒词剥离后的无斜杠形式）
    assert is_shared_command("/shared id")
    assert is_shared_command("shared id")
    assert is_shared_command("/shared")
    assert is_shared_command("shared")
    assert is_shared_command("/shared create 家庭组")
    assert is_shared_command("  shared list  ")
    # 普通聊天：不得误判为命令
    assert not is_shared_command("shared documents please")
    assert not is_shared_command("shared experience")
    assert not is_shared_command("sharing is caring")
    assert not is_shared_command("hello shared")
    assert not is_shared_command("")
    assert not is_shared_command("   ")


def test_normalize_groups_dedups_conflicting_ids():
    """两个组配了相同 id（手工编辑）→ 后者必须重新分配，避免共享同一 cid。"""
    groups = [
        {"name": "a", "id": "g-abcdef12", "umos": ["feishu::u1"]},
        {"name": "b", "id": "g-abcdef12", "umos": ["qq::u2"]},
    ]
    out = normalize_groups(groups)
    ids = [g["id"] for g in out]
    assert len(set(ids)) == 2, f"重复 id 未消除: {ids}"
    assert "g-abcdef12" in ids
    assert out[0]["id"] == "g-abcdef12"
    assert re.fullmatch(r"g-[0-9a-f]{8}", out[1]["id"])
    # 幂等
    out2 = normalize_groups(out)
    assert [g["id"] for g in out2] == ids


def test_verify_code_attempt_limit_blocks_bruteforce():
    """邀请码 5 次失败即作废（防暴力枚举）。"""
    s = SharedContextStar(None, {})
    s._pending_codes["g"] = {"code": "123456", "expires": time.time() + 100, "attempts": 0}
    for i in range(MAX_CODE_ATTEMPTS):
        ok, msg = s._verify_code("g", "000000")
        assert not ok, f"第 {i+1} 次错误尝试应失败"
        assert "剩余尝试" in msg or "作废" in msg, msg
    # 超过上限 → 作废（再试直接报无有效邀请码）
    ok, msg = s._verify_code("g", "123456")
    assert not ok
    assert "g" not in s._pending_codes
    # 正确码在限额内仍可成功
    s._pending_codes["g"] = {"code": "123456", "expires": time.time() + 100, "attempts": 2}
    assert s._verify_code("g", "123456")[0]


def test_valid_group_name():
    assert _valid_group_name("家庭组")
    assert _valid_group_name("a")
    assert _valid_group_name("my group")
    assert not _valid_group_name("")
    assert not _valid_group_name("a" * 33)
    assert not _valid_group_name("bad\nname")
    assert not _valid_group_name("bad\x00name")


def test_resolve_target_group():
    """命令目标组：显式参数优先；否则取当前会话所在组；两者皆无返回 None。"""
    groups = [{"name": "fam", "umos": ["feishu::u1"]}]
    # 显式参数优先
    assert resolve_target_group("fam", groups, "feishu::u1") == "fam"
    # 无参 → 取当前会话所在组
    assert resolve_target_group("", groups, "feishu::u1") == "fam"
    # 无参且不在任何组 → None
    assert resolve_target_group("", groups, "telegram::x") is None
    assert resolve_target_group("", [], "x") is None


def test_list_group_views_permissions():
    """组列表可见性：组 ID 是授权凭证，组外普通会话不可见；仅管理员可组外全量查看。"""
    groups = [
        {"name": "a", "id": "g-aaaaaaaa", "umos": ["feishu::u1"]},
        {"name": "b", "id": "g-bbbbbbbb", "umos": ["qq::u2"]},
    ]
    # 组外普通会话：只见组名，不见任何 id / 成员
    outsider = list_group_views(groups, "telegram::x")
    assert [v["name"] for v in outsider] == ["a", "b"]
    assert all("id" not in v and "members" not in v for v in outsider)
    # 组内成员：自己组有 id + 成员明细；其他组仅组名
    member = list_group_views(groups, "feishu::u1")
    va, vb = member
    assert va["id"] == "g-aaaaaaaa" and va["members"] == ["feishu::u1"] and va["is_member"]
    assert "id" not in vb and "members" not in vb and not vb["is_member"]
    # 管理员（即使组外）：全部组有 id + 成员明细
    admin = list_group_views(groups, "telegram::x", is_admin=True)
    assert all("id" in v and "members" in v for v in admin)
    assert admin[0]["id"] == "g-aaaaaaaa" and admin[1]["id"] == "g-bbbbbbbb"


def test_can_query_group_id_permissions():
    """查询组 ID 权限：管理员可查任意组；普通会话仅限自己所在组。"""
    groups = [{"name": "a", "id": "g-aaaaaaaa", "umos": ["feishu::u1"]}]
    # 管理员：任意组（存在性由调用方另行检查）
    assert can_query_group_id(groups, "telegram::x", True, "a")
    assert can_query_group_id(groups, "telegram::x", True, "nonexistent")
    # 组内成员：可查自己组
    assert can_query_group_id(groups, "feishu::u1", False, "a")
    # 组外普通会话：不可查
    assert not can_query_group_id(groups, "telegram::x", False, "a")
    # 成员不可查其他组
    assert not can_query_group_id(groups, "feishu::u1", False, "b")


def test_normalize_groups_assigns_owner():
    """无 owner 的旧组：默认第一个成员为创建者（组管理员）；已有 owner 保留；幂等。"""
    groups = [
        {"name": "old", "umos": ["feishu::u1", "qq::u2"]},   # 旧数据无 owner
        {"name": "new", "umos": ["qq::u9"], "owner": "qq::u9"},  # 已有 owner
        {"name": "empty", "umos": []},                        # 无成员
    ]
    out = normalize_groups(groups)
    assert out[0]["owner"] == "feishu::u1", "旧组应以第一个成员为 owner"
    assert out[1]["owner"] == "qq::u9", "已有 owner 应保留"
    assert out[2]["owner"] is None, "无成员组 owner 应为 None"
    # 幂等：再次规范化不改变 owner
    out2 = normalize_groups(out)
    assert out2[0]["owner"] == "feishu::u1"
    assert out2[1]["owner"] == "qq::u9"
    # 不修改入参
    assert "owner" not in groups[0]


def test_transfer_ownership_after_leave():
    """创建者退出后：owner 移交给剩余成员中第一个；无剩余成员则置空。"""
    g = {"name": "fam", "umos": ["feishu::u1", "qq::u2", "wechat::u3"], "owner": "feishu::u1"}
    transfer_ownership(g)
    assert g["owner"] == "qq::u2", "owner 应移交剩余第一成员"
    g2 = {"name": "fam", "umos": [], "owner": "feishu::u1"}
    transfer_ownership(g2)
    assert g2["owner"] is None, "无剩余成员时 owner 置空"


if __name__ == "__main__":
    test_umo_match()
    test_group_for_umo()
    test_verify_code()
    test_group_id_format_and_uniqueness()
    test_normalize_groups_assigns_and_keeps_ids()
    test_group_cid_is_group_id()
    test_is_shared_command_only_intercepts_known_commands()
    test_normalize_groups_dedups_conflicting_ids()
    test_verify_code_attempt_limit_blocks_bruteforce()
    test_valid_group_name()
    test_resolve_target_group()
    test_list_group_views_permissions()
    test_can_query_group_id_permissions()
    test_normalize_groups_assigns_owner()
    test_transfer_ownership_after_leave()
    print("OK: 全部断言通过")
