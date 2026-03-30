import datetime
import time
from collections import deque
from pathlib import Path

import mss
import psutil
import win32api
import win32gui
import win32process
import yaml
from loguru import logger
from PIL import Image, ImageChops, ImageStat


class ConfigLoader:
    """Load YAML configuration."""

    @staticmethod
    def load(config_path=None):
        if config_path is None:
            config_path = Path(__file__).resolve().parents[2] / "config.yaml"
        with open(config_path, "r", encoding="utf-8") as file:
            return yaml.safe_load(file)


class DataCollector:
    def __init__(self, config):
        self.config = config
        queue_size = self.config["storage"]["screenshot_retention_seconds"] // self.config["capture"][
            "interval_seconds"
        ]
        self.memory_queue = deque(maxlen=queue_size)

        self.sct = None
        self.last_app_name = ""
        self.last_window_title = ""
        self.static_count = 0

        self.interval = self.config["capture"]["interval_seconds"]
        self.scale = self.config["capture"]["scale_percent"] / 100.0
        self.quality = self.config["capture"]["quality"]
        self.format = self.config["capture"]["format"]
        self.target_screen = self.config["capture"].get("target_screen", "main")
        self.last_ai_trigger_time = time.time()
        self.running = True

    def get_idle_time(self):
        """Return the Windows idle time in seconds."""
        last_input = win32api.GetLastInputInfo()
        current_tick = win32api.GetTickCount()

        if current_tick < last_input:
            idle_ms = ((2 ** 32) - last_input) + current_tick
        else:
            idle_ms = current_tick - last_input

        return idle_ms / 1000.0

    def get_active_window_info(self):
        """Return the foreground process name and window title."""
        try:
            hwnd = win32gui.GetForegroundWindow()
            window_title = win32gui.GetWindowText(hwnd)
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            process = psutil.Process(pid)
            app_name = process.name()
            return app_name, window_title
        except Exception:
            return "unknown", "unknown"

    def capture_and_compress(self):
        """Capture the main monitor and resize in memory."""
        monitor_index = 0 if self.target_screen == "all" else 1
        monitor = self.sct.monitors[monitor_index]
        sct_img = self.sct.grab(monitor)
        image = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
        new_size = (int(image.width * self.scale), int(image.height * self.scale))
        return image.resize(new_size, Image.Resampling.LANCZOS)

    @staticmethod
    def calculate_diff(img1, img2):
        """Compute a fast grayscale diff percentage."""
        if not img1 or not img2:
            return 100.0

        left = img1.resize((64, 64)).convert("L")
        right = img2.resize((64, 64)).convert("L")
        diff = ImageChops.difference(left, right)
        stat = ImageStat.Stat(diff)
        return (stat.mean[0] / 255.0) * 100

    def run_loop(self):
        """Main capture loop."""
        self.sct = mss.mss()
        logger.info(
            f"Collector started (interval: {self.interval}s, queue_size: {self.memory_queue.maxlen})"
        )

        while self.running:
            started_at = time.time()
            timestamp = datetime.datetime.now().isoformat()

            app_name, window_title = self.get_active_window_info()
            current_img = self.capture_and_compress()
            idle_seconds = self.get_idle_time()

            diff_percent = 0.0
            is_window_switched = False
            if self.memory_queue:
                last_frame = self.memory_queue[-1]
                diff_percent = self.calculate_diff(last_frame["image"], current_img)
                is_window_switched = (
                    app_name != self.last_app_name or window_title != self.last_window_title
                )

            static_threshold = self.config["triggers"].get("static_screen_threshold", 2.0)
            is_static = diff_percent < static_threshold

            if is_static and not is_window_switched:
                self.static_count += 1
            else:
                self.static_count = 0

            frame_data = {
                "timestamp": timestamp,
                "app": app_name,
                "title": window_title,
                "image": current_img,
                "diff_percent": diff_percent,
                "is_switched": is_window_switched,
                "static_count": self.static_count,
                "idle_seconds": idle_seconds,
            }
            self.memory_queue.append(frame_data)
            self.last_app_name = app_name
            self.last_window_title = window_title

            current_time = time.time()
            time_since_last_ai = current_time - self.last_ai_trigger_time
            need_ai_analysis = False
            trigger_reason = ""

            if idle_seconds > self.config["triggers"].get("away_from_keyboard_seconds", 600):
                trigger_reason = "无人操作(休眠/离开)"
                self._dispatch_pseudo_event("idle", app_name, "已锁屏或长时间离开设备", True)
            elif is_window_switched and self.config["triggers"].get("trigger_on_window_switch", True):
                need_ai_analysis = True
                trigger_reason = "窗口切换"
            elif not is_static:
                need_ai_analysis = True
                trigger_reason = "画面显著变化"
            else:
                if time_since_last_ai > self.config["triggers"].get("static_recheck_ai_seconds", 300):
                    need_ai_analysis = True
                    trigger_reason = "静态强制复查"
                elif idle_seconds > self.config["triggers"].get("no_input_suspect_seconds", 180):
                    trigger_reason = "无有效输入(疑似停滞)"
                    self._dispatch_pseudo_event("idle", app_name, "长时间停留在当前界面且无有效操作", True)
                else:
                    trigger_reason = "静态延续(阅读/思考)"

            logger.info(
                f"🎯 {app_name} | 窗口: {window_title[:15]}... | 差异: {diff_percent:.1f}% | "
                f"空闲: {idle_seconds:.0f}s | 触发AI: {need_ai_analysis} ({trigger_reason})"
            )

            if need_ai_analysis:
                min_interval = self.config["triggers"].get("min_ai_interval_seconds", 30)
                if time_since_last_ai >= min_interval:
                    self.last_ai_trigger_time = current_time
                    if hasattr(self, "trigger_ai_callback"):
                        self.trigger_ai_callback(frame_data)
                else:
                    logger.debug(
                        f"AI trigger skipped due to cooldown ({time_since_last_ai:.1f}s since last call)"
                    )

            elapsed = time.time() - started_at
            time.sleep(max(0, self.interval - elapsed))

        if self.sct:
            self.sct.close()
            self.sct = None

    def stop(self):
        """Request the collector loop to stop gracefully."""
        self.running = False

    def _dispatch_pseudo_event(self, category, app_name, summary, is_deviated):
        """Send a synthetic event through the same callback path as AI results."""
        if not hasattr(self, "trigger_ai_callback"):
            return

        pseudo_result = {
            "timestamp": datetime.datetime.now().isoformat(),
            "app": app_name,
            "title": "系统判定",
            "image": None,
            "ai_direct_result": {
                "summary": summary,
                "category": category,
                "is_deviated": is_deviated,
                "confidence": 1.0,
            },
        }
        self.trigger_ai_callback(pseudo_result)


if __name__ == "__main__":
    config = ConfigLoader.load()
    collector = DataCollector(config)
    collector.run_loop()
