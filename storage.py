import json
import sqlite3
import uuid
from datetime import datetime

from loguru import logger


class StorageManager:
    def __init__(self, config, db_path="supervisor.db", jsonl_path="study_log.jsonl"):
        self.config = config
        self.db_path = db_path
        self.jsonl_path = jsonl_path
        self.current_session = None

        self.init_db()

    def init_db(self):
        """Initialize the SQLite database and keep old databases migratable."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS activity_sessions
            (
                session_id TEXT PRIMARY KEY,
                start_time TEXT,
                end_time TEXT,
                duration_seconds INTEGER DEFAULT 0,
                app_name TEXT,
                window_title TEXT,
                ai_summary TEXT,
                category TEXT,
                is_deviated BOOLEAN,
                confidence REAL,
                model_used TEXT DEFAULT '',
                updated_at TEXT DEFAULT '',
                evidence_image_path TEXT
            )
            """
        )

        self._ensure_column(cursor, "activity_sessions", "duration_seconds", "INTEGER DEFAULT 0")
        self._ensure_column(cursor, "activity_sessions", "model_used", "TEXT DEFAULT ''")
        self._ensure_column(cursor, "activity_sessions", "updated_at", "TEXT DEFAULT ''")

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_time ON activity_sessions(start_time, end_time)")

        conn.commit()
        conn.close()
        logger.info(f"Database initialized: {self.db_path}")

    @staticmethod
    def _ensure_column(cursor, table_name, column_name, column_definition):
        """Add a column only when it does not exist yet."""
        cursor.execute(f"PRAGMA table_info({table_name})")
        existing_columns = {row[1] for row in cursor.fetchall()}
        if column_name not in existing_columns:
            cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")

    @staticmethod
    def _calculate_duration_seconds(start_time, end_time):
        """Compute duration from two ISO timestamps."""
        try:
            start_dt = datetime.fromisoformat(start_time)
            end_dt = datetime.fromisoformat(end_time)
        except (TypeError, ValueError):
            return 0

        return max(0, int((end_dt - start_dt).total_seconds()))

    def _append_to_jsonl(self, session_data):
        """Append finalized session data to the JSONL backup."""
        if self.config["storage"]["log_format"] != "jsonl":
            return

        with open(self.jsonl_path, "a", encoding="utf-8") as file:
            file.write(json.dumps(session_data, ensure_ascii=False) + "\n")

    def is_same_session(self, new_event):
        """Decide whether the new event should be merged into current session."""
        if not self.current_session:
            return False

        if self.current_session["app_name"] != new_event["app_name"]:
            return False

        if self.current_session.get("category") != new_event.get("category"):
            return False

        return True

    def log_event(self, event_data):
        """
        Merge incoming events into the current session or create a new session.
        event_data includes timestamp, app_name, window_title, category, ai_summary, is_deviated, etc.
        """
        current_time = event_data["timestamp"]

        if self.is_same_session(event_data):
            self.current_session["end_time"] = current_time
            self.current_session["updated_at"] = current_time
            self.current_session["duration_seconds"] = self._calculate_duration_seconds(
                self.current_session["start_time"], self.current_session["end_time"]
            )
            self.current_session["window_title"] = event_data["window_title"]

            self._update_session_in_db()
            logger.info(
                f"[Merged] {self.current_session['app_name']} continued until {current_time[11:19]}"
            )
            return

        if self.current_session:
            self._append_to_jsonl(self.current_session)

        session_id = str(uuid.uuid4())
        self.current_session = {
            "session_id": session_id,
            "start_time": current_time,
            "end_time": current_time,
            "duration_seconds": 0,
            "app_name": event_data["app_name"],
            "window_title": event_data["window_title"],
            "ai_summary": event_data.get("ai_summary", "local_rule_match"),
            "category": event_data.get("category", "unknown"),
            "is_deviated": event_data.get("is_deviated", False),
            "confidence": event_data.get("confidence", 1.0),
            "model_used": event_data.get("model_used", ""),
            "updated_at": current_time,
            "evidence_image_path": event_data.get("evidence_image_path", ""),
        }

        self._insert_session_to_db()
        logger.info(f"[New Session] {event_data['app_name']} - {self.current_session['ai_summary']}")

    def _insert_session_to_db(self):
        """Insert the current session into SQLite."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        session = self.current_session
        cursor.execute(
            """
            INSERT INTO activity_sessions
            (
                session_id, start_time, end_time, duration_seconds, app_name, window_title,
                ai_summary, category, is_deviated, confidence, model_used, updated_at,
                evidence_image_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session["session_id"],
                session["start_time"],
                session["end_time"],
                session["duration_seconds"],
                session["app_name"],
                session["window_title"],
                session["ai_summary"],
                session["category"],
                session["is_deviated"],
                session["confidence"],
                session["model_used"],
                session["updated_at"],
                session["evidence_image_path"],
            ),
        )
        conn.commit()
        conn.close()

    def _update_session_in_db(self):
        """Update the current session end time, duration, and heartbeat."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        session = self.current_session
        cursor.execute(
            """
            UPDATE activity_sessions
            SET end_time = ?,
                duration_seconds = ?,
                window_title = ?,
                updated_at = ?
            WHERE session_id = ?
            """,
            (
                session["end_time"],
                session["duration_seconds"],
                session["window_title"],
                session["updated_at"],
                session["session_id"],
            ),
        )
        conn.commit()
        conn.close()


if __name__ == "__main__":
    import time

    from collector import ConfigLoader

    config = ConfigLoader.load()
    storage = StorageManager(config)

    base_time = datetime.now()

    event1 = {
        "timestamp": base_time.isoformat(),
        "app_name": "Code.exe",
        "window_title": "main.py - VS Code",
        "category": "study",
        "ai_summary": "Writing Python code",
        "is_deviated": False,
        "model_used": "local",
    }

    event2 = {
        "timestamp": datetime.now().isoformat(),
        "app_name": "Code.exe",
        "window_title": "storage.py - VS Code",
        "category": "study",
        "ai_summary": "Writing Python code",
        "is_deviated": False,
        "model_used": "local",
    }

    event3 = {
        "timestamp": datetime.now().isoformat(),
        "app_name": "chrome.exe",
        "window_title": "Bilibili - Hot Videos",
        "category": "entertainment",
        "ai_summary": "Browsing entertainment videos",
        "is_deviated": True,
        "model_used": "gemini",
        "evidence_image_path": "/temp/bad_boy.jpg",
    }

    storage.log_event(event1)
    time.sleep(1)
    storage.log_event(event2)
    time.sleep(1)
    storage.log_event(event3)
