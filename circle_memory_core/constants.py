"""常量与配置项定义。"""

import sys

PLUGIN_NAME = "astrbot_plugin_circle_memory"

CODE_TTL = 300  # 邀请码有效期（秒）
MAX_CODE_ATTEMPTS = 5  # 邀请码最大尝试次数（防暴力枚举）
MAX_GROUP_NAME_LEN = 32  # 组名最大长度
# 必须高于 AstrBot 内置 handle_session_control_agent 的 priority=maxsize，
# 否则活跃 agent 续聊会先 stop_event()，我们的命令 handler 永远不会执行。
PRIORITY = sys.maxsize + 1

KNOWN_COMMANDS = frozenset({"create", "code", "join", "leave", "dissolve", "id", "list"})

# 命令帮助文本（/shared 无参数或未知子命令时展示）
USAGE_TEXT = (
    "用法:\n"
    "/shared create <组名>\n"
    "/shared code [组名]\n"
    "/shared join <组名> <验证码>\n"
    "/shared leave [组名]\n"
    "/shared dissolve [组名]\n"
    "/shared id [组名]（管理员可查任意组）\n"
    "/shared list"
)
