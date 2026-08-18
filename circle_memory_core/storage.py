"""配置读写、消息流水与归档：统一经此模块持久化。

- 配置：user_groups / merged（AstrBot config.save_config）
- 消息流水：archive/message_log/<group>.jsonl（mine_only 与成员标注的数据源）
- 归档：解散/末人退出时整组历史 → archive/groups/；个人发言 → archive/personal/
"""

import json
import logging
import os
import re
import time
from pathlib import Path

from .constants import ARCHIVE_KEEP, MESSAGE_LOG_MAX

logger = logging.getLogger(__name__)


def get_plugin_data_dir() -> Path:
    """插件数据目录（AstrBot data 路径下）；失败时回退到本地 data/plugins。"""
    try:
        from astrbot.core.utils.io import get_astrbot_plugin_data_path

        base = Path(get_astrbot_plugin_data_path())
    except Exception:
        base = Path("data/plugins")
    d = base / "astrbot_plugin_circle_memory"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.error("[CircleMemory] 创建插件数据目录失败: %s", e)
    return d


def _safe_name(name: str) -> str:
    """文件系统安全化：仅保留字母数字与常见符号，其余替换为 _。"""
    return re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]", "_", name or "unnamed")[:64] or "unnamed"


# ---------- 配置 ----------


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


def save_aliases(config, aliases: dict) -> None:
    """保存成员昵称映射（组名 → {UMO: 昵称}）。"""
    config["aliases"] = aliases
    try:
        if hasattr(config, "save_config"):
            config.save_config()
            logger.info("[CircleMemory] aliases 已保存")
    except Exception as e:
        logger.error("[CircleMemory] 保存 aliases 失败: %s", e)


def find_group(config, name: str) -> dict | None:
    """按组名查找组配置；不存在返回 None（不做假设，调用方自行处理）。"""
    for g in config.get("user_groups", []):
        if g.get("name") == name:
            return g
    return None


# ---------- 消息流水（mine_only 数据源） ----------


def _message_log_path(group_name: str) -> Path:
    return get_plugin_data_dir() / "message_log" / f"{_safe_name(group_name)}.jsonl"


def append_message_log(group_name: str, umo: str, sender_name: str, text: str) -> None:
    """追加一条组内消息流水（best-effort）。

    记录内容：时间、会话 UMO、发送者昵称、纯文本。作为 mine_only
    （退出者个人发言导出）与后续成员标注的数据源。
    """
    try:
        path = _message_log_path(group_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": int(time.time()),
            "umo": umo,
            "sender": sender_name or umo.split(":", 2)[-1],
            "text": text[:2000],
        }
        # 行数超限时保留最近一半，避免文件无限膨胀
        if path.exists():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) > MESSAGE_LOG_MAX:
                path.write_text(
                    "\n".join(lines[-(MESSAGE_LOG_MAX // 2):]) + "\n",
                    encoding="utf-8",
                )
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error("[CircleMemory] 写入消息流水失败: %s", e)


def read_message_log(group_name: str, umo: str | None = None) -> list[dict]:
    """读取组消息流水；umo 指定时仅返回该会话的消息。"""
    path = _message_log_path(group_name)
    if not path.exists():
        return []
    out = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if umo is None or rec.get("umo") == umo:
                out.append(rec)
    except Exception as e:
        logger.error("[CircleMemory] 读取消息流水失败: %s", e)
    return out


# ---------- 归档 ----------


def write_group_archive(
    group_name: str, gid: str, members: list, messages: list, reason: str
) -> Path | None:
    """末人退出/解散时归档整组共享历史（服务器资产，不发送给退出者）。

    返回归档文件路径；失败返回 None（best-effort，不阻塞解散）。
    """
    try:
        d = get_plugin_data_dir() / "archive" / "groups"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{int(time.time())}_{_safe_name(group_name)}.json"
        payload = {
            "group": group_name,
            "group_id": gid,
            "archived_at": int(time.time()),
            "reason": reason,
            "members": members,
            "messages": messages,
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        cleanup_archives()
        return path
    except Exception as e:
        logger.error("[CircleMemory] 归档组 %s 失败: %s", group_name, e)
        return None


def cleanup_archives(keep: int | None = None) -> None:
    """清理组归档，保留最近 keep 份（默认 ARCHIVE_KEEP）。"""
    keep = ARCHIVE_KEEP if keep is None else keep
    try:
        d = get_plugin_data_dir() / "archive" / "groups"
        if not d.exists():
            return
        files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for old in files[:-keep]:
            try:
                old.unlink()
            except OSError:
                pass
    except Exception as e:
        logger.error("[CircleMemory] 清理归档失败: %s", e)


def write_personal_archive(
    group_name: str, umo: str, sender_name: str, messages: list
) -> Path | None:
    """mine_only：导出该成员自己的发言（不含他人发言/系统注入）到服务器留档。"""
    try:
        d = get_plugin_data_dir() / "archive" / "personal"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{int(time.time())}_{_safe_name(group_name)}.json"
        payload = {
            "group": group_name,
            "member_umo": umo,
            "member_name": sender_name or umo,
            "archived_at": int(time.time()),
            "messages": messages,
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return path
    except Exception as e:
        logger.error("[CircleMemory] 个人发言归档失败: %s", e)
        return None
