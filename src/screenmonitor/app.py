import asyncio
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import uvicorn
import yaml
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger

from .ai_router import AIRouter
from .collector import ConfigLoader, DataCollector
from .storage import StorageManager


BASE_DIR = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = BASE_DIR / "templates"
CONFIG_PATH = BASE_DIR / "config.yaml"
AUTH_PATH = BASE_DIR / "admin_auth.json"
UNLOCK_TTL_SECONDS = 600

config = None
storage_manager = None
ai_router = None
collector_instance = None
main_loop = None
unlock_sessions: dict[str, float] = {}

SETTINGS_FIELDS = [
    {
        "id": "current_goal",
        "path": ["context", "current_goal"],
        "group": "basic",
        "label": "当前目标",
        "type": "text",
        "help": "用于指导判定用户当前行为是否与目标一致。",
        "effect": "保存后立即生效",
    },
    {
        "id": "interval_seconds",
        "path": ["capture", "interval_seconds"],
        "group": "basic",
        "label": "截图间隔",
        "type": "number",
        "kind": "int",
        "help": "单位：秒。",
        "effect": "保存后立即生效",
    },
    {
        "id": "static_reading_grace_seconds",
        "path": ["triggers", "static_reading_grace_seconds"],
        "group": "basic",
        "label": "静态判定宽限",
        "type": "number",
        "kind": "int",
        "help": "单位：秒。",
        "effect": "保存后立即生效",
    },
    {
        "id": "hourly_summary_interval_hours",
        "path": ["notifications", "hourly_summary_interval_hours"],
        "group": "basic",
        "label": "小时汇总频率",
        "type": "number",
        "kind": "int",
        "help": "单位：小时。",
        "effect": "保存后立即生效",
    },
    {
        "id": "daily_summary_time",
        "path": ["notifications", "daily_summary_time"],
        "group": "basic",
        "label": "每日总结时间",
        "type": "time",
        "help": "格式：HH:MM。",
        "effect": "保存后立即生效",
    },
    {
        "id": "screenshot_retention_seconds",
        "path": ["storage", "screenshot_retention_seconds"],
        "group": "basic",
        "label": "截图保留时间",
        "type": "number",
        "kind": "int",
        "help": "单位：秒。",
        "effect": "保存后立即生效",
    },
    {
        "id": "idle_timeout_seconds",
        "path": ["triggers", "idle_timeout_seconds"],
        "group": "rules",
        "label": "空闲判定阈值",
        "type": "number",
        "kind": "int",
        "help": "单位：秒。",
        "effect": "保存后立即生效",
    },
    {
        "id": "static_skip_count",
        "path": ["triggers", "static_skip_count"],
        "group": "rules",
        "label": "静态跳过次数",
        "type": "number",
        "kind": "int",
        "help": "连续静态帧达到该次数后减少 AI 调用。",
        "effect": "保存后立即生效",
    },
    {
        "id": "trigger_on_window_switch",
        "path": ["triggers", "trigger_on_window_switch"],
        "group": "rules",
        "label": "窗口切换立即判定",
        "type": "checkbox",
        "help": "检测到前台窗口变化时立即触发一次分析。",
        "effect": "保存后立即生效",
    },
    {
        "id": "static_screen_threshold",
        "path": ["triggers", "static_screen_threshold"],
        "group": "rules",
        "label": "静态屏幕阈值",
        "type": "number",
        "kind": "float",
        "step": "0.1",
        "help": "差异低于该值时视为静态。",
        "effect": "保存后立即生效",
    },
    {
        "id": "inherit_previous_state",
        "path": ["triggers", "inherit_previous_state"],
        "group": "rules",
        "label": "静态时继承前一状态",
        "type": "checkbox",
        "help": "降低静态阅读场景下的误判。",
        "effect": "保存后立即生效",
    },
    {
        "id": "no_input_suspect_seconds",
        "path": ["triggers", "no_input_suspect_seconds"],
        "group": "rules",
        "label": "无输入疑似停滞阈值",
        "type": "number",
        "kind": "int",
        "help": "单位：秒。",
        "effect": "保存后立即生效",
    },
    {
        "id": "away_from_keyboard_seconds",
        "path": ["triggers", "away_from_keyboard_seconds"],
        "group": "rules",
        "label": "离开设备阈值",
        "type": "number",
        "kind": "int",
        "help": "单位：秒。",
        "effect": "保存后立即生效",
    },
    {
        "id": "run_on_startup",
        "path": ["system", "run_on_startup"],
        "group": "system",
        "label": "开机启动",
        "type": "checkbox",
        "help": "保存后下次系统启动生效。",
        "effect": "保存后下次监控启动生效",
    },
    {
        "id": "auto_start_monitoring",
        "path": ["system", "auto_start_monitoring"],
        "group": "system",
        "label": "自动进入监督",
        "type": "checkbox",
        "help": "程序启动后自动开始监控。",
        "effect": "保存后立即生效",
    },
    {
        "id": "silent_mode",
        "path": ["system", "silent_mode"],
        "group": "system",
        "label": "静默运行",
        "type": "checkbox",
        "help": "减少弹窗和打扰。",
        "effect": "保存后立即生效",
    },
    {
        "id": "show_tray_icon",
        "path": ["system", "show_tray_icon"],
        "group": "system",
        "label": "托盘图标",
        "type": "checkbox",
        "help": "保存后下次监控启动生效。",
        "effect": "保存后下次监控启动生效",
    },
    {
        "id": "log_retention_days",
        "path": ["storage", "log_retention_days"],
        "group": "system",
        "label": "日志保留天数",
        "type": "number",
        "kind": "int",
        "help": "单位：天。",
        "effect": "保存后立即生效",
    },
    {
        "id": "anomaly_image_retention_days",
        "path": ["storage", "anomaly_image_retention_days"],
        "group": "system",
        "label": "异常截图保留天数",
        "type": "number",
        "kind": "int",
        "help": "单位：天。",
        "effect": "保存后立即生效",
    },
    {
        "id": "save_raw_model_output",
        "path": ["storage", "save_raw_model_output"],
        "group": "system",
        "label": "保存原始模型输出",
        "type": "checkbox",
        "help": "用于调试模型返回。",
        "effect": "保存后立即生效",
    },
    {
        "id": "primary_model",
        "path": ["ai_providers", "gemini", "model"],
        "group": "secure",
        "label": "主模型",
        "type": "text",
        "default": "gemini-2.5-flash",
        "help": "保存后建议重启服务。",
        "effect": "保存后建议重启服务",
        "secure": True,
    },
    {
        "id": "gemini_enabled",
        "path": ["ai_providers", "gemini", "enabled"],
        "group": "secure",
        "label": "启用 Gemini",
        "type": "checkbox",
        "default": True,
        "help": "关闭后即使配置了 API Key 也不会调用 Gemini。",
        "effect": "保存后立即生效",
        "secure": True,
    },
    {
        "id": "fallback_model",
        "path": ["ai_providers", "kimi", "model"],
        "group": "secure",
        "label": "Kimi 模型",
        "type": "text",
        "default": "kimi-k2.5",
        "help": "保存后建议重启服务。",
        "effect": "保存后建议重启服务",
        "secure": True,
    },
    {
        "id": "kimi_enabled",
        "path": ["ai_providers", "kimi", "enabled"],
        "group": "secure",
        "label": "启用 Kimi",
        "type": "checkbox",
        "default": True,
        "help": "关闭后即使配置了 API Key 也不会调用 Kimi 兜底。",
        "effect": "保存后立即生效",
        "secure": True,
    },
    {
        "id": "qwen_fallback_model",
        "path": ["ai_providers", "qwen", "model"],
        "group": "secure",
        "label": "Qwen 兜底模型",
        "type": "text",
        "default": "qwen-vl-plus",
        "help": "推荐使用 qwen-vl-plus 或 qwen-vl-max 这类视觉理解模型。",
        "effect": "保存后建议重启服务",
        "secure": True,
    },
    {
        "id": "qwen_enabled",
        "path": ["ai_providers", "qwen", "enabled"],
        "group": "secure",
        "label": "启用 Qwen",
        "type": "checkbox",
        "default": True,
        "help": "关闭后即使配置了 API Key 也不会调用 Qwen 兜底。",
        "effect": "保存后立即生效",
        "secure": True,
    },
    {
        "id": "ai_provider_order",
        "path": ["ai_models", "ai_provider_order"],
        "group": "secure",
        "label": "AI 调用顺序",
        "type": "comma_list",
        "default": ["gemini", "jeniya", "qwen", "kimi"],
        "help": "用英文逗号分隔，例如 gemini,jeniya,qwen,kimi。新 provider 加入 ai_providers 后可写到这里。",
        "effect": "保存后立即生效",
        "secure": True,
    },
    {
        "id": "jeniya_enabled",
        "path": ["ai_providers", "jeniya", "enabled"],
        "group": "secure",
        "label": "启用 Jeniya 中转站",
        "type": "checkbox",
        "default": False,
        "help": "关闭后即使配置了 API Key 也不会调用 Jeniya 中转站。",
        "effect": "保存后立即生效",
        "secure": True,
    },
    {
        "id": "jeniya_model",
        "path": ["ai_providers", "jeniya", "model"],
        "group": "secure",
        "label": "Jeniya 模型",
        "type": "text",
        "default": "gemini-2.5-pro",
        "help": "用于 /v1beta/models/{model}:generateContent 的模型名。",
        "effect": "保存后立即生效",
        "secure": True,
    },
    {
        "id": "jeniya_base_url",
        "path": ["ai_providers", "jeniya", "base_url"],
        "group": "secure",
        "label": "Jeniya Base URL",
        "type": "text",
        "default": "https://jeniya.top",
        "help": "Gemini generateContent 兼容接口地址。",
        "effect": "保存后立即生效",
        "secure": True,
    },
    {
        "id": "aitools_enabled",
        "path": ["ai_providers", "aitools", "enabled"],
        "group": "secure",
        "label": "启用中转站",
        "type": "checkbox",
        "default": False,
        "help": "关闭后即使配置了 API Key 也不会调用中转站。",
        "effect": "保存后立即生效",
        "secure": True,
    },
    {
        "id": "aitools_model",
        "path": ["ai_providers", "aitools", "model"],
        "group": "secure",
        "label": "中转站模型",
        "type": "select",
        "options": [
            "qwen/qwen2.5-vl-32b",
            "google/gemini-2.0-flash-exp",
        ],
        "default": "qwen/qwen2.5-vl-32b",
        "help": "建议优先选择支持视觉输入的模型用于截图分析。",
        "effect": "保存后立即生效",
        "secure": True,
    },
    {
        "id": "aitools_base_url",
        "path": ["ai_providers", "aitools", "base_url"],
        "group": "secure",
        "label": "中转站 Base URL",
        "type": "text",
        "default": "https://platform.aitools.cfd/api/v1",
        "help": "OpenAI-compatible 接口地址，系统会自动调用 /chat/completions。",
        "effect": "保存后立即生效",
        "secure": True,
    },
    {
        "id": "aitools_input_mode",
        "path": ["ai_providers", "aitools", "input_mode"],
        "group": "secure",
        "label": "中转站输入模式",
        "type": "select",
        "options": ["text", "vision"],
        "default": "vision",
        "help": "vision 会把截图 base64 发送给中转站；仅用于支持图片输入的模型。",
        "effect": "保存后立即生效",
        "secure": True,
    },
    {
        "id": "qwen_base_url",
        "path": ["ai_providers", "qwen", "base_url"],
        "group": "secure",
        "label": "Qwen Base URL",
        "type": "text",
        "default": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "help": "OpenAI-compatible 接口地址。",
        "effect": "保存后立即生效",
        "secure": True,
    },
    {
        "id": "kimi_base_url",
        "path": ["ai_providers", "kimi", "base_url"],
        "group": "secure",
        "label": "Kimi Base URL",
        "type": "text",
        "default": "https://api.moonshot.cn/v1",
        "help": "OpenAI-compatible 接口地址。",
        "effect": "保存后立即生效",
        "secure": True,
    },
    {
        "id": "timeout_seconds",
        "path": ["ai_models", "timeout_seconds"],
        "group": "secure",
        "label": "模型超时时间",
        "type": "number",
        "kind": "int",
        "help": "单位：秒。",
        "effect": "保存后建议重启服务",
        "secure": True,
    },
    {
        "id": "max_retries",
        "path": ["ai_models", "max_retries"],
        "group": "secure",
        "label": "最大重试次数",
        "type": "number",
        "kind": "int",
        "help": "模型调用失败时的重试次数。",
        "effect": "保存后建议重启服务",
        "secure": True,
    },
    {
        "id": "target_screen",
        "path": ["capture", "target_screen"],
        "group": "secure",
        "label": "截图目标屏幕",
        "type": "select",
        "options": ["main", "all"],
        "help": "保存后建议重启服务。",
        "effect": "保存后建议重启服务",
        "secure": True,
    },
    {
        "id": "cpu_limit_strategy",
        "path": ["system", "cpu_limit_strategy"],
        "group": "secure",
        "label": "CPU 策略",
        "type": "select",
        "options": ["low", "throttle"],
        "help": "保存后建议重启服务。",
        "effect": "保存后建议重启服务",
        "secure": True,
    },
    {
        "id": "cache_tasks_offline",
        "path": ["system", "cache_tasks_offline"],
        "group": "secure",
        "label": "离线缓存策略",
        "type": "checkbox",
        "help": "网络不可用时缓存待处理任务。",
        "effect": "保存后建议重启服务",
        "secure": True,
    },
    {
        "id": "retry_on_reconnect",
        "path": ["system", "retry_on_reconnect"],
        "group": "secure",
        "label": "重连后重试策略",
        "type": "checkbox",
        "help": "网络恢复后重试未完成任务。",
        "effect": "保存后建议重启服务",
        "secure": True,
    },
    {
        "id": "gemini_api_key",
        "path": ["api_keys", "gemini"],
        "group": "secure",
        "label": "Gemini API Key",
        "type": "password",
        "help": "敏感字段，仅显示掩码。",
        "effect": "保存后建议重启服务",
        "secure": True,
        "secret": True,
    },
    {
        "id": "qwen_api_key",
        "path": ["api_keys", "qwen"],
        "group": "secure",
        "label": "Qwen API Key",
        "type": "password",
        "help": "敏感字段，仅显示掩码。支持 DashScope 兼容模式。",
        "effect": "保存后建议重启服务",
        "secure": True,
        "secret": True,
    },
    {
        "id": "kimi_api_key",
        "path": ["api_keys", "kimi"],
        "group": "secure",
        "label": "Kimi API Key",
        "type": "password",
        "help": "敏感字段，仅显示掩码。",
        "effect": "保存后建议重启服务",
        "secure": True,
        "secret": True,
    },
    {
        "id": "aitools_api_key",
        "path": ["api_keys", "aitools"],
        "group": "secure",
        "label": "中转站 API Key",
        "type": "password",
        "help": "Authorization: Bearer 使用的 API Key。",
        "effect": "保存后立即生效",
        "secure": True,
        "secret": True,
    },
    {
        "id": "jeniya_api_key",
        "path": ["api_keys", "jeniya"],
        "group": "secure",
        "label": "Jeniya API Key",
        "type": "password",
        "help": "Jeniya Authorization Bearer token，同时会作为 generateContent 的 key 参数。",
        "effect": "保存后立即生效",
        "secure": True,
        "secret": True,
    },
    {
        "id": "feishu_webhook",
        "path": ["api_keys", "feishu_webhook"],
        "group": "secure",
        "label": "Feishu Webhook",
        "type": "password",
        "help": "敏感字段，仅显示掩码。",
        "effect": "保存后建议重启服务",
        "secure": True,
        "secret": True,
    },
]


def on_ai_trigger(frame_data):
    """Bridge collector thread events into the FastAPI event loop."""
    if not main_loop or not ai_router or main_loop.is_closed():
        logger.debug("Skip AI trigger because the event loop is unavailable.")
        return

    coroutine = ai_router.analyze_frame(frame_data)
    try:
        asyncio.run_coroutine_threadsafe(coroutine, main_loop)
    except RuntimeError:
        coroutine.close()
        logger.debug("Skip AI trigger because the event loop is already closed.")


def load_config_from_disk():
    return ConfigLoader.load(str(CONFIG_PATH))


def save_config_to_disk(new_config):
    with open(CONFIG_PATH, "w", encoding="utf-8") as file:
        yaml.safe_dump(new_config, file, allow_unicode=True, sort_keys=False)


def get_db_path() -> Path:
    if storage_manager and getattr(storage_manager, "db_path", None):
        return Path(storage_manager.db_path)
    return BASE_DIR / "supervisor.db"


def get_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(get_db_path())
    connection.row_factory = sqlite3.Row
    return connection


def row_to_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


def render_template(template_name: str, title: str) -> HTMLResponse:
    template_path = TEMPLATES_DIR / template_name
    html = template_path.read_text(encoding="utf-8")
    html = html.replace("{{ title }}", title)
    return HTMLResponse(html)


def normalize_date(date_str: str | None) -> str:
    if not date_str:
        return datetime.now().strftime("%Y-%m-%d")
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD") from exc


def get_path_value(data: dict, path: list[str]):
    current = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def set_path_value(data: dict, path: list[str], value):
    current = data
    for key in path[:-1]:
        current = current.setdefault(key, {})
    current[path[-1]] = value


def field_map():
    return {field["id"]: field for field in SETTINGS_FIELDS}


def query_summary_by_date(cursor: sqlite3.Cursor, target_date: str) -> dict:
    cursor.execute(
        """
        SELECT
            COUNT(*) AS session_count,
            COALESCE(SUM(duration_seconds), 0) AS total_seconds,
            COALESCE(SUM(CASE WHEN category = 'study' THEN duration_seconds ELSE 0 END), 0) AS study_seconds,
            COALESCE(SUM(CASE WHEN category = 'entertainment' THEN duration_seconds ELSE 0 END), 0) AS entertainment_seconds,
            COALESCE(SUM(CASE WHEN category = 'unknown' THEN duration_seconds ELSE 0 END), 0) AS unknown_seconds,
            COALESCE(SUM(CASE WHEN category = 'idle' THEN duration_seconds ELSE 0 END), 0) AS idle_seconds,
            COALESCE(SUM(CASE WHEN is_deviated = 1 THEN duration_seconds ELSE 0 END), 0) AS deviated_seconds
        FROM activity_sessions
        WHERE date(start_time) = ?
        """,
        (target_date,),
    )
    return row_to_dict(cursor.fetchone())


def query_sessions_by_date(cursor: sqlite3.Cursor, target_date: str, limit: int | None = None) -> list[dict]:
    sql = """
        SELECT
            session_id,
            start_time,
            end_time,
            duration_seconds,
            app_name,
            category,
            ai_summary,
            is_deviated,
            model_used
        FROM activity_sessions
        WHERE date(start_time) = ?
        ORDER BY COALESCE(updated_at, end_time, start_time) DESC
    """
    params: list[object] = [target_date]
    if limit is not None:
        sql += "\nLIMIT ?"
        params.append(limit)
    cursor.execute(sql, tuple(params))
    return [row_to_dict(row) for row in cursor.fetchall()]


def query_hourly_by_date(cursor: sqlite3.Cursor, target_date: str) -> list[dict]:
    cursor.execute(
        """
        SELECT
            strftime('%H', start_time) AS hour,
            COALESCE(SUM(CASE WHEN category = 'study' THEN duration_seconds ELSE 0 END), 0) AS study_seconds,
            COALESCE(SUM(CASE WHEN is_deviated = 1 THEN duration_seconds ELSE 0 END), 0) AS deviated_seconds
        FROM activity_sessions
        WHERE date(start_time) = ?
        GROUP BY strftime('%H', start_time)
        ORDER BY hour
        """,
        (target_date,),
    )
    rows = {row["hour"]: row_to_dict(row) for row in cursor.fetchall()}
    items = []
    for hour in range(24):
        key = f"{hour:02d}"
        row = rows.get(key, {})
        items.append(
            {
                "hour": key,
                "study_seconds": row.get("study_seconds", 0),
                "deviated_seconds": row.get("deviated_seconds", 0),
            }
        )
    return items


def query_latest_session(cursor: sqlite3.Cursor) -> dict | None:
    cursor.execute(
        """
        SELECT
            session_id,
            start_time,
            end_time,
            duration_seconds,
            updated_at,
            app_name,
            category,
            ai_summary,
            is_deviated,
            model_used
        FROM activity_sessions
        ORDER BY COALESCE(updated_at, end_time, start_time) DESC
        LIMIT 1
        """
    )
    row = cursor.fetchone()
    return row_to_dict(row) if row else None


def mask_secret(value: str) -> str:
    if not value:
        return "未配置"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:2]}****{value[-4:]}"


def hash_password(password: str, salt: bytes, iterations: int) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return digest.hex()


def load_auth_state():
    if not AUTH_PATH.exists():
        return None
    return json.loads(AUTH_PATH.read_text(encoding="utf-8"))


def write_auth_state(password: str):
    salt = secrets.token_bytes(16)
    iterations = 200_000
    payload = {
        "scheme": "pbkdf2_sha256",
        "iterations": iterations,
        "salt": salt.hex(),
        "password_hash": hash_password(password, salt, iterations),
        "created_at": datetime.now().isoformat(),
    }
    AUTH_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def verify_password(password: str) -> bool:
    auth = load_auth_state()
    if not auth:
        return False
    salt = bytes.fromhex(auth["salt"])
    expected = auth["password_hash"]
    actual = hash_password(password, salt, int(auth["iterations"]))
    return hmac.compare_digest(actual, expected)


def issue_unlock_token() -> tuple[str, int]:
    expires_at = int(time.time()) + UNLOCK_TTL_SECONDS
    token = secrets.token_urlsafe(24)
    unlock_sessions[token] = expires_at
    return token, expires_at


def prune_unlock_sessions():
    now = int(time.time())
    expired = [token for token, expiry in unlock_sessions.items() if expiry <= now]
    for token in expired:
        unlock_sessions.pop(token, None)


def require_unlock_token(admin_token: str | None):
    prune_unlock_sessions()
    if not admin_token or unlock_sessions.get(admin_token, 0) <= int(time.time()):
        raise HTTPException(status_code=403, detail="Sensitive settings are locked")


def serialize_field_value(field, current_config, secure_view=False):
    value = get_path_value(current_config, field["path"])
    if value is None and "default" in field:
        value = field["default"]
    if secure_view and field.get("secret"):
        return {
            "configured": bool(value),
            "masked": mask_secret(str(value or "")),
            "secret": True,
        }
    return value


def schema_groups():
    groups = {
        "basic": {
            "title": "基础设置",
            "fields": [field for field in SETTINGS_FIELDS if field["group"] == "basic"],
        },
        "rules": {
            "title": "监督规则",
            "fields": [field for field in SETTINGS_FIELDS if field["group"] == "rules"],
        },
        "system": {
            "title": "系统与存储",
            "fields": [field for field in SETTINGS_FIELDS if field["group"] == "system"],
        },
        "secure": {
            "title": "高级与敏感设置",
            "fields": [field for field in SETTINGS_FIELDS if field["group"] == "secure"],
        },
    }
    return groups


def coerce_field_value(field, value):
    if field["type"] == "checkbox":
        return bool(value)
    if field["type"] == "number":
        return float(value) if field.get("kind") == "float" else int(value)
    if field["type"] == "comma_list":
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return [item.strip() for item in str(value).split(",") if item.strip()]
    return str(value).strip()


def apply_runtime_config(new_config):
    global config, ai_router

    config = new_config

    if storage_manager:
        storage_manager.config = new_config

    if collector_instance:
        collector_instance.config = new_config
        collector_instance.interval = new_config["capture"]["interval_seconds"]
        collector_instance.scale = new_config["capture"]["scale_percent"] / 100.0
        collector_instance.quality = new_config["capture"]["quality"]
        collector_instance.format = new_config["capture"]["format"]
        collector_instance.target_screen = new_config["capture"].get("target_screen", "main")

    if storage_manager:
        ai_router = AIRouter(new_config, storage_manager)


def save_settings_values(payload_values: dict, *, secure_only: bool):
    current_config = load_config_from_disk()
    field_lookup = field_map()
    changed_fields = []

    for field_id, raw_value in payload_values.items():
        field = field_lookup.get(field_id)
        if not field:
            continue
        if secure_only != bool(field.get("secure")):
            continue
        if field.get("secret") and (raw_value is None or str(raw_value).strip() == ""):
            continue

        coerced = coerce_field_value(field, raw_value)
        set_path_value(current_config, field["path"], coerced)
        changed_fields.append(field)

    save_config_to_disk(current_config)
    apply_runtime_config(current_config)
    return changed_fields


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize the monitor stack and flush the current session on shutdown."""
    global config, storage_manager, ai_router, collector_instance, main_loop

    os.makedirs("logs", exist_ok=True)
    logger.add(
        "logs/system_{time:YYYY-MM-DD}.log",
        rotation="00:00",
        retention="30 days",
        encoding="utf-8",
        level="INFO",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
    )

    logger.info("[System] Initializing AI supervisor")
    main_loop = asyncio.get_running_loop()

    config = load_config_from_disk()
    storage_manager = StorageManager(config)
    ai_router = AIRouter(config, storage_manager)
    collector_instance = DataCollector(config)
    collector_instance.trigger_ai_callback = on_ai_trigger

    collector_thread = threading.Thread(
        target=collector_instance.run_loop,
        daemon=True,
        name="DataCollectorThread",
    )
    collector_thread.start()

    logger.info("[System] AI supervisor started")
    yield

    logger.info("[System] Shutting down")
    if collector_instance:
        collector_instance.trigger_ai_callback = None
        collector_instance.stop()
        collector_thread.join(timeout=2)
    if storage_manager and storage_manager.current_session:
        storage_manager._append_to_jsonl(storage_manager.current_session)
    main_loop = None
    logger.info("[System] Shutdown complete")


app = FastAPI(
    title="AI Supervisor Engine",
    description="Local AI supervisor with live dashboard, analytics, and settings.",
    version="2.2",
    lifespan=lifespan,
)


@app.get("/")
async def dashboard():
    return render_template("dashboard.html", "ScreenMonitor Dashboard")


@app.get("/history")
async def history_page():
    return render_template("history.html", "ScreenMonitor History")


@app.get("/settings")
async def settings_page():
    return render_template("settings.html", "ScreenMonitor Settings")


@app.get("/api/health")
async def health():
    return {"status": "ok", "message": "AI Supervisor is watching you..."}


@app.get("/api/session/current")
async def get_current_session():
    if not storage_manager or not storage_manager.current_session:
        return {"message": "No active session"}
    return storage_manager.current_session


@app.get("/api/system/status")
async def get_system_status():
    if not collector_instance:
        return {"error": "System not fully started"}
    return {
        "current_app": collector_instance.last_app_name,
        "current_window": collector_instance.last_window_title,
        "deque_size": len(collector_instance.memory_queue),
        "static_frames_count": collector_instance.static_count,
    }


@app.get("/api/dashboard/summary")
async def dashboard_summary():
    target_date = datetime.now().strftime("%Y-%m-%d")
    connection = get_connection()
    cursor = connection.cursor()
    summary = query_summary_by_date(cursor, target_date)
    connection.close()
    return JSONResponse(summary)


@app.get("/api/dashboard/current")
async def dashboard_current():
    connection = get_connection()
    cursor = connection.cursor()
    current = query_latest_session(cursor)
    connection.close()
    return JSONResponse({"item": current})


@app.get("/api/dashboard/recent")
async def dashboard_recent(limit: int = 20):
    safe_limit = max(1, min(limit, 100))
    target_date = datetime.now().strftime("%Y-%m-%d")
    connection = get_connection()
    cursor = connection.cursor()
    sessions = query_sessions_by_date(cursor, target_date, safe_limit)
    connection.close()
    return JSONResponse({"items": sessions, "limit": safe_limit})


@app.get("/api/dashboard/hourly")
async def dashboard_hourly():
    target_date = datetime.now().strftime("%Y-%m-%d")
    connection = get_connection()
    cursor = connection.cursor()
    items = query_hourly_by_date(cursor, target_date)
    connection.close()
    return JSONResponse({"items": items})


@app.get("/api/dashboard/history")
async def dashboard_history(date: str | None = None):
    target_date = normalize_date(date)
    connection = get_connection()
    cursor = connection.cursor()
    payload = {
        "date": target_date,
        "summary": query_summary_by_date(cursor, target_date),
        "hourly": [
            {
                "hour_label": f"{item['hour']}:00",
                "study_seconds": item["study_seconds"],
                "deviated_seconds": item["deviated_seconds"],
            }
            for item in query_hourly_by_date(cursor, target_date)
        ],
        "sessions": query_sessions_by_date(cursor, target_date),
    }
    connection.close()
    return JSONResponse(payload)


@app.get("/api/settings/schema")
async def get_settings_schema():
    groups = schema_groups()
    return JSONResponse(
        {
            "auth_initialized": AUTH_PATH.exists(),
            "groups": groups,
        }
    )


@app.get("/api/settings/basic")
async def get_basic_settings():
    current_config = load_config_from_disk()
    values = {
        field["id"]: serialize_field_value(field, current_config)
        for field in SETTINGS_FIELDS
        if not field.get("secure")
    }
    return JSONResponse({"values": values})


@app.post("/api/settings/basic")
async def save_basic_settings(payload: dict):
    values = payload.get("values", {})
    changed_fields = save_settings_values(values, secure_only=False)
    return JSONResponse(
        {
            "saved": True,
            "changed_fields": [field["id"] for field in changed_fields],
            "messages": [field["effect"] for field in changed_fields],
        }
    )


@app.post("/api/settings/bootstrap")
async def bootstrap_settings_auth(payload: dict):
    if AUTH_PATH.exists():
        raise HTTPException(status_code=400, detail="Admin password already initialized")

    password = str(payload.get("password", "")).strip()
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    write_auth_state(password)
    token, expires_at = issue_unlock_token()
    return JSONResponse({"initialized": True, "token": token, "expires_at": expires_at})


@app.post("/api/settings/unlock")
async def unlock_secure_settings(payload: dict):
    if not AUTH_PATH.exists():
        return JSONResponse({"needs_bootstrap": True}, status_code=400)

    password = str(payload.get("password", ""))
    if not verify_password(password):
        raise HTTPException(status_code=403, detail="Invalid admin password")

    token, expires_at = issue_unlock_token()
    return JSONResponse({"unlocked": True, "token": token, "expires_at": expires_at})


@app.get("/api/settings/secure")
async def get_secure_settings(x_admin_token: str | None = Header(default=None)):
    require_unlock_token(x_admin_token)
    current_config = load_config_from_disk()
    values = {
        field["id"]: serialize_field_value(field, current_config, secure_view=True)
        for field in SETTINGS_FIELDS
        if field.get("secure")
    }
    return JSONResponse({"values": values, "expires_at": unlock_sessions.get(x_admin_token)})


@app.post("/api/settings/secure")
async def save_secure_settings(payload: dict, x_admin_token: str | None = Header(default=None)):
    require_unlock_token(x_admin_token)
    values = payload.get("values", {})
    changed_fields = save_settings_values(values, secure_only=True)
    return JSONResponse(
        {
            "saved": True,
            "changed_fields": [field["id"] for field in changed_fields],
            "messages": [field["effect"] for field in changed_fields],
        }
    )


@app.post("/api/settings/reload")
async def reload_settings():
    apply_runtime_config(load_config_from_disk())
    return JSONResponse({"reloaded": True})


if __name__ == "__main__":
    uvicorn.run("src.screenmonitor.app:app", host="127.0.0.1", port=8000)
