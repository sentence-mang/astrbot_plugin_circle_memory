"""集成测试：组共享会话以组 ID 为 cid 入库，其他插件按 cid 读写即可识别。

运行（容器内）:
    docker exec astrbot python3 /tmp/test_integration.py
使用临时 SQLite 数据库，不触碰生产数据。
"""

import asyncio
import json
import os
import sys
import tempfile

# 桩掉 astrbot.core.sp（生产共享偏好存储），防止测试写入生产数据
import astrbot.core.conversation_mgr as cm_module


class StubSP:
    async def session_put(self, *a, **k):
        return None

    async def session_get(self, *a, **k):
        return None

    async def session_remove(self, *a, **k):
        return None


cm_module.sp = StubSP()

from astrbot.core.conversation_mgr import ConversationManager  # noqa: E402
from astrbot.core.db.sqlite import SQLiteDatabase  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from main import CircleMemoryStar, group_cid  # noqa: E402

from types import SimpleNamespace  # noqa: E402

OLD_CID = "11111111-1111-1111-1111-111111111111"
CONTENT = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好呀"}]


async def make_cm(tmp):
    db = SQLiteDatabase(str(tmp / "test.db"))
    return ConversationManager(db)


async def test_migration_and_group_id_readwrite(tmp):
    cm = await make_cm(tmp)
    # 旧版组：无 id，共享会话为随机 uuid，已有历史
    await cm.db.create_conversation(
        user_id="feishu::u1",
        platform_id="feishu",
        content=[dict(x) for x in CONTENT],
        title="旧标题",
        cid=OLD_CID,
    )
    await cm.switch_conversation("feishu::u1", OLD_CID)

    config = {
        "user_groups": [{"name": "fam", "umos": ["feishu::u1", "qq::u2"]}],
        "merged": {"fam": OLD_CID},
    }
    star = CircleMemoryStar(None, config)
    star.context = SimpleNamespace(conversation_manager=cm)

    # __init__ 规范化：旧组自动补 ID
    group = config["user_groups"][0]
    gid = group_cid(group)
    assert gid.startswith("g-"), f"组未获得 ID: {group}"

    # 迁移
    await star._ensure_group_shared("fam", "feishu::u1")

    # 1) 组共享会话以组 ID 为 cid 存在，历史保留
    conv = await cm.db.get_conversation_by_id(cid=gid)
    assert conv is not None, f"组会话 {gid} 不存在"
    assert conv.content == CONTENT, f"历史未保留: {conv.content}"
    assert conv.title == "旧标题"

    # 2) 旧 uuid 会话已清理
    old = await cm.db.get_conversation_by_id(cid=OLD_CID)
    assert old is None, "旧 uuid 会话未删除"

    # 3) 成员已切换
    assert await cm.get_curr_conversation_id("feishu::u1") == gid
    assert await cm.get_curr_conversation_id("qq::u2") == gid

    # 4) 其他插件路径：get_conversation(umo, 组ID) 可读
    conv_v1 = await cm.get_conversation("feishu::u1", gid)
    assert conv_v1 is not None
    assert json.loads(conv_v1.history) == CONTENT

    # 5) 其他插件路径：add_message_pair(组ID, ...) 可写
    await cm.add_message_pair(
        gid,
        {"role": "user", "content": "追加问题"},
        {"role": "assistant", "content": "追加回答"},
    )
    conv_after = await cm.db.get_conversation_by_id(cid=gid)
    assert len(conv_after.content) == len(CONTENT) + 2, f"写入未生效: {len(conv_after.content)}"

    # 6) 幂等：再次 ensure 不产生重复会话、不报错
    before = [c for c in (await cm.db.get_conversations(user_id=None)) if c.conversation_id == gid]
    await star._ensure_group_shared("fam", "feishu::u1")
    after = [c for c in (await cm.db.get_conversations(user_id=None)) if c.conversation_id == gid]
    assert len(after) == len(before) == 1, f"幂等失败: {len(before)} -> {len(after)}"
    assert config["merged"]["fam"] == gid

    print("PASS migration + group-id read/write + idempotency")


async def test_new_group_adopts_member_history(tmp):
    cm = await make_cm(tmp)
    own_cid = "22222222-2222-2222-2222-222222222222"
    await cm.db.create_conversation(
        user_id="wechat::u9",
        platform_id="wechat",
        content=[dict(x) for x in CONTENT],
        title="个人会话",
        cid=own_cid,
    )
    await cm.switch_conversation("wechat::u9", own_cid)

    config = {"user_groups": [{"name": "new", "umos": ["wechat::u9"]}], "merged": {}}
    star = CircleMemoryStar(None, config)
    star.context = SimpleNamespace(conversation_manager=cm)
    gid = group_cid(config["user_groups"][0])

    await star._ensure_group_shared("new", "wechat::u9")

    conv = await cm.db.get_conversation_by_id(cid=gid)
    assert conv is not None
    assert conv.content == CONTENT, "新组未继承创建者历史"
    assert await cm.get_curr_conversation_id("wechat::u9") == gid
    assert await cm.db.get_conversation_by_id(cid=own_cid) is None

    print("PASS new group adopts member history at group-id cid")


async def test_concurrent_ensure_single_create(tmp):
    """并发 ensure 不得重复创建组会话（幂等 + 竞态安全）。"""
    cm = await make_cm(tmp)
    await cm.db.create_conversation(
        user_id="feishu::u1",
        platform_id="feishu",
        content=[dict(x) for x in CONTENT],
        title="旧标题",
        cid=OLD_CID,
    )
    await cm.switch_conversation("feishu::u1", OLD_CID)

    config = {
        "user_groups": [{"name": "fam", "umos": ["feishu::u1", "qq::u2"]}],
        "merged": {"fam": OLD_CID},
    }
    star = CircleMemoryStar(None, config)
    star.context = SimpleNamespace(conversation_manager=cm)
    gid = group_cid(config["user_groups"][0])

    creates = []

    orig_create = cm.db.create_conversation

    async def counting_create(**kwargs):
        creates.append(kwargs.get("cid"))
        return await orig_create(**kwargs)

    cm.db.create_conversation = counting_create

    await asyncio.gather(*(star._ensure_group_shared("fam", "feishu::u1") for _ in range(10)))

    assert len(creates) == 1, f"组会话被创建了 {len(creates)} 次: {creates}"
    convs = [c for c in (await cm.db.get_conversations(user_id=None)) if c.conversation_id == gid]
    assert len(convs) == 1, f"存在 {len(convs)} 个组会话"
    assert config["merged"]["fam"] == gid
    print("PASS concurrent ensures create exactly one group conversation")


async def test_initialize_runs_migration(tmp):
    """插件激活钩子 initialize() 必须完成迁移（加载即迁移，无需等首条消息）。"""
    cm = await make_cm(tmp)
    await cm.db.create_conversation(
        user_id="feishu::u1",
        platform_id="feishu",
        content=[dict(x) for x in CONTENT],
        title="旧标题",
        cid=OLD_CID,
    )
    await cm.switch_conversation("feishu::u1", OLD_CID)

    config = {
        "user_groups": [{"name": "fam", "umos": ["feishu::u1", "qq::u2"]}],
        "merged": {"fam": OLD_CID},
    }
    star = CircleMemoryStar(None, config)
    star.context = SimpleNamespace(conversation_manager=cm)
    gid = group_cid(config["user_groups"][0])

    # 只调用激活钩子，不手动触发任何 ensure
    await star.initialize()

    conv = await cm.db.get_conversation_by_id(cid=gid)
    assert conv is not None, "initialize() 未创建组 ID 会话"
    assert conv.content == CONTENT, "initialize() 未保留历史"
    assert await cm.get_curr_conversation_id("feishu::u1") == gid
    assert await cm.get_curr_conversation_id("qq::u2") == gid
    assert await cm.db.get_conversation_by_id(cid=OLD_CID) is None
    assert config["merged"]["fam"] == gid
    print("PASS initialize() migrates group on activation")


async def test_group_without_id_is_rejected_safely(tmp):
    """组缺 id（配置被外部编辑）时，不得创建 cid='' 会话或切换成员。"""
    cm = await make_cm(tmp)
    own_cid = "33333333-3333-3333-3333-333333333333"
    await cm.db.create_conversation(
        user_id="feishu::u9",
        platform_id="feishu",
        content=[dict(x) for x in CONTENT],
        title="个人会话",
        cid=own_cid,
    )
    await cm.switch_conversation("feishu::u9", own_cid)

    # 手工配置的组没有 id（模拟 web UI 直接编辑且未经过 __init__ 规范化）
    config = {"user_groups": [{"name": "noid", "umos": ["feishu::u9"]}], "merged": {}}
    star = CircleMemoryStar(None, config)
    star.context = SimpleNamespace(conversation_manager=cm)
    del config["user_groups"][0]["id"]  # __init__ 已补 id，删掉模拟外部编辑

    await star._ensure_group_shared("noid", "feishu::u9")

    # 不得产生空 cid 会话
    empty = await cm.db.get_conversation_by_id(cid="")
    assert empty is None, "空 cid 会话被创建"
    # 成员会话不得被切换为空 cid
    assert await cm.get_curr_conversation_id("feishu::u9") == own_cid
    # 配置不被破坏
    assert config["merged"].get("noid") is None

    print("PASS group without id is rejected safely")


class FakeEvent:
    """命令级测试用的事件桩：记录回复，支持 role 权限。"""

    def __init__(self, umo, role="member"):
        self.unified_msg_origin = umo
        self.role = role
        self.replies = []

    def plain_result(self, text):
        return ("plain", text)

    async def send(self, msg):
        self.replies.append(msg)


async def test_command_permission_isolation(tmp):
    """权限隔离：组外普通会话不可见任何组 ID；仅管理员可组外查询；leave/dissolve 组名可省略。"""
    cm = await make_cm(tmp)
    groups = [
        {"name": "fam", "id": "g-aaaaaaaa", "umos": ["feishu::u1"]},
        {"name": "work", "id": "g-bbbbbbbb", "umos": ["qq::u2"]},
    ]
    config = {"user_groups": groups, "merged": {"fam": "g-aaaaaaaa", "work": "g-bbbbbbbb"}}
    star = CircleMemoryStar(None, config)
    star.context = SimpleNamespace(conversation_manager=cm)

    # --- list：组外普通会话只见组名，无 ID、无成员 ---
    outsider = FakeEvent("telegram::x", role="member")
    await star._cmd_list(outsider)
    out = outsider.replies[0][1]
    assert "g-aaaaaaaa" not in out and "g-bbbbbbbb" not in out, f"组外泄露组 ID: {out}"
    assert "feishu::u1" not in out, f"组外泄露成员: {out}"
    assert "fam" in out and "work" in out

    # --- list：组内成员只见自己组的 ID 与成员，其他组仅组名 ---
    member = FakeEvent("feishu::u1", role="member")
    await star._cmd_list(member)
    out = member.replies[0][1]
    assert "g-aaaaaaaa" in out and "feishu::u1" in out
    assert "g-bbbbbbbb" not in out and "qq::u2" not in out, f"跨组泄露: {out}"

    # --- list：管理员（组外）可见全部组的 ID 与成员 ---
    admin = FakeEvent("telegram::x", role="admin")
    await star._cmd_list(admin)
    out = admin.replies[0][1]
    assert "g-aaaaaaaa" in out and "g-bbbbbbbb" in out
    assert "feishu::u1" in out and "qq::u2" in out

    # --- id：组外普通会话（无参）→ 拒绝；带组名 → 拒绝 ---
    outsider = FakeEvent("telegram::x", role="member")
    await star._cmd_id(outsider)
    assert "g-aaaaaaaa" not in outsider.replies[0][1]
    outsider2 = FakeEvent("telegram::x", role="member")
    await star._cmd_id(outsider2, "fam")
    assert "g-aaaaaaaa" not in outsider2.replies[0][1]

    # --- id：组内成员可查自己组 ---
    member = FakeEvent("feishu::u1", role="member")
    await star._cmd_id(member)
    assert "g-aaaaaaaa" in member.replies[0][1]

    # --- id：管理员组外可查任意组 ---
    admin = FakeEvent("telegram::x", role="admin")
    await star._cmd_id(admin, "fam")
    assert "g-aaaaaaaa" in admin.replies[0][1]

    # --- leave 无参：组内成员直接退出当前所在组（不再强制组名） ---
    leaver = FakeEvent("feishu::u1", role="member")
    await star._cmd_leave(leaver, "")
    assert leaver.replies, "leave 无参未产生任何回复"
    # 成员已从组中移除
    assert "feishu::u1" not in config["user_groups"][0]["umos"]

    # --- dissolve 无参：组内成员直接解散当前所在组 ---
    dissolver = FakeEvent("qq::u2", role="member")
    await star._cmd_dissolve(dissolver, "")
    assert "work" not in [g["name"] for g in config["user_groups"]]

    print("PASS command permission isolation + optional group name")


async def test_dissolve_requires_creator(tmp):
    """解散权限：仅创建组的会话（组管理员）可解散；其他成员拒绝。"""
    cm = await make_cm(tmp)
    groups = [{
        "name": "fam", "id": "g-aaaaaaaa",
        "umos": ["feishu::u1", "qq::u2"], "owner": "feishu::u1",
    }]
    config = {"user_groups": groups, "merged": {"fam": "g-aaaaaaaa"}}
    star = CircleMemoryStar(None, config)
    star.context = SimpleNamespace(conversation_manager=cm)

    # 非创建者成员 → 拒绝，组保留
    member = FakeEvent("qq::u2", role="member")
    await star._cmd_dissolve(member, "")
    assert "创建" in member.replies[0][1], member.replies[0][1]
    assert any(g["name"] == "fam" for g in config["user_groups"]), "非创建者不应能解散"

    # 创建者 → 成功解散
    creator = FakeEvent("feishu::u1", role="member")
    await star._cmd_dissolve(creator, "")
    assert not any(g["name"] == "fam" for g in config["user_groups"]), "创建者应能解散"

    print("PASS dissolve requires creator (owner)")


async def test_owner_transfer_on_leave(tmp):
    """创建者退出后：组主移交剩余第一成员，组仍可被新组主解散。"""
    cm = await make_cm(tmp)
    groups = [{
        "name": "fam", "id": "g-aaaaaaaa",
        "umos": ["feishu::u1", "qq::u2", "wechat::u3"], "owner": "feishu::u1",
    }]
    config = {"user_groups": groups, "merged": {"fam": "g-aaaaaaaa"}}
    star = CircleMemoryStar(None, config)
    star.context = SimpleNamespace(conversation_manager=cm)

    creator = FakeEvent("feishu::u1", role="member")
    await star._cmd_leave(creator, "")
    g = config["user_groups"][0]
    assert g["owner"] == "qq::u2", f"owner 应移交剩余第一成员，实际 {g['owner']}"

    # 新组主可解散
    new_owner = FakeEvent("qq::u2", role="member")
    await star._cmd_dissolve(new_owner, "")
    assert not any(g["name"] == "fam" for g in config["user_groups"])

    print("PASS owner transfers to first remaining member on leave")


async def test_create_records_owner(tmp):
    """创建组时记录创建者会话为组主（组管理员）。"""
    cm = await make_cm(tmp)
    config = {"user_groups": [], "merged": {}}
    star = CircleMemoryStar(None, config)
    star.context = SimpleNamespace(conversation_manager=cm)

    creator = FakeEvent("feishu::u1", role="member")
    await star._cmd_create(creator, "fam")
    g = config["user_groups"][0]
    assert g["name"] == "fam" and g["owner"] == "feishu::u1", f"owner 未记录: {g}"

    print("PASS create records creator as owner")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as td:
        tmp = type("Tmp", (), {"__truediv__": lambda self, x: os.path.join(td, x)})()
        asyncio.run(test_migration_and_group_id_readwrite(tmp))
        asyncio.run(test_new_group_adopts_member_history(tmp))
        asyncio.run(test_concurrent_ensure_single_create(tmp))
        asyncio.run(test_initialize_runs_migration(tmp))
        asyncio.run(test_group_without_id_is_rejected_safely(tmp))
        asyncio.run(test_command_permission_isolation(tmp))
        asyncio.run(test_dissolve_requires_creator(tmp))
        asyncio.run(test_owner_transfer_on_leave(tmp))
        asyncio.run(test_create_records_owner(tmp))
    print("OK: 集成测试全部通过")
