import asyncio
import os
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger

from ai_router import AIRouter
from collector import ConfigLoader, DataCollector
from storage import StorageManager


BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"

config = None
storage_manager = None
ai_router = None
collector_instance = None
main_loop = None


def on_ai_trigger(frame_data):
    """Bridge collector thread events into the FastAPI event loop."""
    if main_loop and ai_router:
        asyncio.run_coroutine_threadsafe(ai_router.analyze_frame(frame_data), main_loop)


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

    config = ConfigLoader.load("config.yaml")
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
    if storage_manager and storage_manager.current_session:
        storage_manager._append_to_jsonl(storage_manager.current_session)
    logger.info("[System] Shutdown complete")


app = FastAPI(
    title="AI Supervisor Engine",
    description="Local AI supervisor with live dashboard and session analytics.",
    version="2.1",
    lifespan=lifespan,
)


@app.get("/")
async def dashboard():
    return render_template("dashboard.html", "ScreenMonitor Dashboard")


@app.get("/history")
async def history_page():
    return render_template("history.html", "ScreenMonitor History")


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


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000)
