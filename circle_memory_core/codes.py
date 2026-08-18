"""邀请码管理：生成、校验、防暴力枚举。

验证码保存在内存（重启即失效），与配置持久化解耦。
"""

import secrets
import time

from circle_memory_core.constants import CODE_TTL, MAX_CODE_ATTEMPTS


class CodeManager:
    """组邀请码管理器。以组名为键，内存态。"""

    def __init__(self) -> None:
        self._pending_codes: dict[str, dict] = {}  # group_name -> {"code", "expires", "attempts"}

    def issue(self, group_name: str) -> str:
        """生成新邀请码（6 位数字，CODE_TTL 秒有效）。"""
        code = f"{secrets.randbelow(1000000):06d}"
        self._pending_codes[group_name] = {
            "code": code,
            "expires": time.time() + CODE_TTL,
            "attempts": 0,
        }
        return code

    def verify(self, group_name: str, code: str) -> tuple[bool, str]:
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

    def pop(self, group_name: str) -> None:
        """验证成功后清除邀请码。"""
        self._pending_codes.pop(group_name, None)
