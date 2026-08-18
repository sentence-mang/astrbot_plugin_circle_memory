"""circle_memory_core：astrbot_plugin_circle_memory 的内核模块包。

唯一命名的子包，避免与 AstrBot 全局 sys.path 中其他插件的
通用模块名（codes/groups/storage 等）冲突。
"""

from .codes import CodeManager
from .commands import CommandHandlers
from .constants import (
    CODE_TTL,
    KNOWN_COMMANDS,
    MAX_CODE_ATTEMPTS,
    MAX_GROUP_NAME_LEN,
    PLUGIN_NAME,
    PRIORITY,
    USAGE_TEXT,
)
from .groups import (
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
    valid_group_name,
)
from .shared_session import SharedSessionManager
from .storage import find_group, save_merged, save_user_groups

__all__ = [
    "CodeManager",
    "CommandHandlers",
    "SharedSessionManager",
    "CODE_TTL",
    "KNOWN_COMMANDS",
    "MAX_CODE_ATTEMPTS",
    "MAX_GROUP_NAME_LEN",
    "PLUGIN_NAME",
    "PRIORITY",
    "USAGE_TEXT",
    "can_query_group_id",
    "find_group",
    "generate_group_id",
    "group_cid",
    "group_for_umo",
    "is_shared_command",
    "list_group_views",
    "normalize_groups",
    "resolve_target_group",
    "save_merged",
    "save_user_groups",
    "transfer_ownership",
    "umo_match",
    "valid_group_name",
]
