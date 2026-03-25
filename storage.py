import sqlite3
import json
from loguru import logger
import os
import uuid
from datetime import datetime


class StorageManager:
    def __init__(self, config, db_path="supervisor.db", jsonl_path="study_log.jsonl"):
        self.config = config
        self.db_path = db_path
        self.jsonl_path = jsonl_path

        # 内存中维护的当前 Session 状态，用于快速对比判断是否需要合并
        self.current_session = None

        self.init_db()

    def init_db(self):
        """初始化 SQLite 数据库和表结构"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # 创建核心表：activity_sessions (活动会话表)
        cursor.execute('''
                       CREATE TABLE IF NOT EXISTS activity_sessions
                       (
                           session_id TEXT PRIMARY KEY,
                           start_time TEXT,
                           end_time TEXT,
                           app_name TEXT,
                           window_title TEXT,
                           ai_summary TEXT,
                           category TEXT,    -- 'study', 'entertainment', 'unknown' 等
                           is_deviated BOOLEAN, -- 是否偏离学习目标
                           confidence REAL,    -- AI 识别置信度
                           evidence_image_path TEXT     -- 如果严重偏离，保留的截图证据路径
                       )
                       ''')

        # 顺手建一个索引，以后按时间范围查小时报告会极快
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_time ON activity_sessions(start_time, end_time)')

        conn.commit()
        conn.close()
        logger.info(f"📦 数据库初始化完成: {self.db_path}")

    def _append_to_jsonl(self, session_data):
        """将数据追加到 JSONL 文件（备用/导出用）"""
        if self.config['storage']['log_format'] != "jsonl":
            return

        with open(self.jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(session_data, ensure_ascii=False) + "\n")

    def is_same_session(self, new_event):
        """核心逻辑：判断新事件是否可以与当前 Session 合并"""
        if not self.current_session:
            return False

        # 1. 如果换了软件，绝对不是同一个 session
        if self.current_session['app_name'] != new_event['app_name']:
            return False

        # 2. 如果行为类别发生变化（比如从'学习'变成了'娱乐'），不能合并
        if self.current_session.get('category') != new_event.get('category'):
            return False

        # 3. 如果时间断层太大（例如断网或休眠了 10 分钟），不能合并
        # 这里为了简化，假设按 5-30 秒正常传递。实际可加入 datetime 时间差判断

        return True

    def log_event(self, event_data):
        """
        接收来自采集器或 AI 的最新事件，执行归并或新建操作。
        event_data 包含: timestamp, app_name, window_title, category, ai_summary, is_deviated 等
        """
        current_time = event_data['timestamp']

        # 如果判定为同一个连续行为，只更新结束时间
        if self.is_same_session(event_data):
            self.current_session['end_time'] = current_time
            # 窗口标题可能会稍微变化（比如翻页），保留最新的
            self.current_session['window_title'] = event_data['window_title']

            # 更新 SQLite 中当前 session 的结束时间
            self._update_session_in_db()
            logger(f"🔄 [合并记录] 延续状态: {self.current_session['app_name']} | 持续至 {current_time[11:19]}")

        # 如果判定为新行为，创建全新的 Session
        else:
            session_id = str(uuid.uuid4())

            # 如果之前有老 session，可以在这里把最终状态落盘 JSONL
            if self.current_session:
                self._append_to_jsonl(self.current_session)

            # 初始化新 Session
            self.current_session = {
                'session_id': session_id,
                'start_time': current_time,
                'end_time': current_time,
                'app_name': event_data['app_name'],
                'window_title': event_data['window_title'],
                'ai_summary': event_data.get('ai_summary', '本地规则匹配'),
                'category': event_data.get('category', 'unknown'),
                'is_deviated': event_data.get('is_deviated', False),
                'confidence': event_data.get('confidence', 1.0),
                'evidence_image_path': event_data.get('evidence_image_path', '')
            }

            # 插入 SQLite
            self._insert_session_to_db()
            logger(f"🆕 [新建记录] 发现新状态: {event_data['app_name']} - {self.current_session['ai_summary']}")

    def _insert_session_to_db(self):
        """向 SQLite 插入新会话"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        s = self.current_session
        cursor.execute('''
                       INSERT INTO activity_sessions
                       (session_id, start_time, end_time, app_name, window_title, ai_summary, category, is_deviated,
                        confidence, evidence_image_path)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ''', (s['session_id'], s['start_time'], s['end_time'], s['app_name'], s['window_title'],
                             s['ai_summary'], s['category'], s['is_deviated'], s['confidence'],
                             s['evidence_image_path']))
        conn.commit()
        conn.close()

    def _update_session_in_db(self):
        """更新 SQLite 中当前会话的结束时间和窗口标题"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        s = self.current_session
        cursor.execute('''
                       UPDATE activity_sessions
                       SET end_time     = ?,
                           window_title = ?
                       WHERE session_id = ?
                       ''', (s['end_time'], s['window_title'], s['session_id']))
        conn.commit()
        conn.close()


# 测试代码
if __name__ == "__main__":
    from collector import ConfigLoader
    import time

    config = ConfigLoader.load()
    storage = StorageManager(config)

    # 模拟三次事件，前两次是同一软件，第三次切换软件
    base_time = datetime.now()

    event1 = {
        "timestamp": base_time.isoformat(),
        "app_name": "Code.exe",
        "window_title": "main.py - VS Code",
        "category": "study",
        "ai_summary": "正在编写 Python 代码",
        "is_deviated": False
    }

    event2 = {
        "timestamp": datetime.now().isoformat(),  # 假设过了几秒
        "app_name": "Code.exe",
        "window_title": "storage.py - VS Code",  # 哪怕标题微调，也能合并
        "category": "study",
        "ai_summary": "正在编写 Python 代码",
        "is_deviated": False
    }

    event3 = {
        "timestamp": datetime.now().isoformat(),
        "app_name": "chrome.exe",
        "window_title": "Bilibili - 热门视频",
        "category": "entertainment",
        "ai_summary": "正在浏览娱乐视频网站",
        "is_deviated": True,  # 偏离目标！
        "evidence_image_path": "/temp/bad_boy.jpg"
    }

    storage.log_event(event1)
    time.sleep(1)
    storage.log_event(event2)  # 应该触发 [合并记录]
    time.sleep(1)
    storage.log_event(event3)  # 应该触发 [新建记录]