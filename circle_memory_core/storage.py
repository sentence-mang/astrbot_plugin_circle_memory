"""配置读写：统一经此模块持久化 user_groups 等字段。

后续归档（解散组历史导出）也落在此层。
"""

import logging

logger = logging.getLogger(__name__)


def save_user_groups(config, user_groups: list) -> None:
    """保存组配置到插件配置（AstrBot config.save_config）。

    失败仅记日志（与旧行为一致）；调用方按需提示用户。
    """
    config["user_groups"] = user_groups
    try:
        if hasattr(config, "save_config"):
            config.save_config()
            logger.info("[CircleMemory] 配置已保存")
    except Exception as e:
        logger.error("[CircleMemory] 保存配置失败: %s", e)


def save_merged(config, merged: dict) -> None:
    """保存 merged（组名 → 共享会话 cid）映射。"""
    config["merged"] = merged
    try:
        if hasattr(config, "save_config"):
            config.save_config()
            logger.info("[CircleMemory] merged 已保存")
    except Exception as e:
        logger.error("[CircleMemory] 保存 merged 失败: %s", e)


def find_group(config, name: str) -> dict | None:
    """按组名查找组配置；不存在返回 None（不做假设，调用方自行处理）。"""
    for g in config.get("user_groups", []):
        if g.get("name") == name:
            return g
    return None
