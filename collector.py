import win32api
import yaml
import time
import datetime
import io
from loguru import logger
import psutil
from collections import deque
from PIL import Image, ImageChops, ImageStat
import mss
import win32gui
import win32process


class ConfigLoader:
    """配置加载器"""

    @staticmethod
    def load(config_path="config.yaml"):
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)


class DataCollector:
    def __init__(self, config):
        self.config = config
        # 计算 1 分钟内需要保留的截图数量 (例如: 60秒 / 5秒 = 12张)
        queue_size = self.config['storage']['screenshot_retention_seconds'] // self.config['capture'][
            'interval_seconds']

        # 使用 deque，当达到 maxlen 时，最早加入的数据会自动被挤出丢弃，完美契合“只保留最近一分钟”的需求
        self.memory_queue = deque(maxlen=queue_size)

        self.sct = None
        self.last_app_name = ""
        self.last_window_title = ""
        self.static_count = 0  # 连续静止帧计数

        # 预抓取配置，减少循环内的字典查询开销
        self.interval = self.config['capture']['interval_seconds']
        self.scale = self.config['capture']['scale_percent'] / 100.0
        self.quality = self.config['capture']['quality']
        self.format = self.config['capture']['format']

    def get_idle_time(self):
        """获取 Windows 系统的真实键鼠空闲时间（秒）"""
        last_input = win32api.GetLastInputInfo()
        current_tick = win32api.GetTickCount()

        # 处理系统运行过久导致 tick 回绕的极罕见情况
        if current_tick < last_input:
            idle_ms = ((2 ** 32) - last_input) + current_tick
        else:
            idle_ms = current_tick - last_input

        return idle_ms / 1000.0

    def get_active_window_info(self):
        """获取 Windows 当前前台活跃窗口的标题和进程名"""
        try:
            hwnd = win32gui.GetForegroundWindow()
            window_title = win32gui.GetWindowText(hwnd)

            # 获取进程 ID 并通过 psutil 查进程名
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            process = psutil.Process(pid)
            app_name = process.name()

            return app_name, window_title
        except Exception as e:
            # 容错：窗口刚好关闭等极短时间的异常
            return "unknown", "unknown"

    def capture_and_compress(self):
        """截图并按配置进行缩放和压缩（都在内存中完成，不写硬盘）"""
        monitor = self.sct.monitors[1]  # 1 表示主屏 (0是所有屏幕合成)
        sct_img = self.sct.grab(monitor)

        # 转为 PIL Image
        img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

        # 缩放 (例如 50%)，大幅降低内存占用和后续对比的计算量
        new_size = (int(img.width * self.scale), int(img.height * self.scale))
        img = img.resize(new_size, Image.Resampling.LANCZOS)

        return img

    def calculate_diff(self, img1, img2):
        """本地极速图像差异计算（返回 0~100 的差异百分比）"""
        if not img1 or not img2:
            return 100.0

        # 将图片缩小到 64x64 再转灰度，对比速度极快
        i1 = img1.resize((64, 64)).convert("L")
        i2 = img2.resize((64, 64)).convert("L")

        diff = ImageChops.difference(i1, i2)
        stat = ImageStat.Stat(diff)
        # stat.mean[0] 是平均像素差异值 (0-255)，转为百分比
        diff_percent = (stat.mean[0] / 255.0) * 100
        return diff_percent

    def run_loop(self):
        """采集器主循环"""
        self.sct = mss.mss()
        logger.info(f"🚀 数据采集器已启动... (采集间隔: {self.interval}s, 队列容量: {self.memory_queue.maxlen})")

        self.last_ai_trigger_time = time.time()

        while True:
            start_time = time.time()
            timestamp = datetime.datetime.now().isoformat()

            # 1. 获取所有基础信息 (窗口、图像、空闲时间)
            app_name, window_title = self.get_active_window_info()
            current_img = self.capture_and_compress()
            idle_seconds = self.get_idle_time()

            # 2. 本地画面变化分析
            diff_percent = 0.0
            is_window_switched = False

            if len(self.memory_queue) > 0:
                last_frame = self.memory_queue[-1]
                diff_percent = self.calculate_diff(last_frame['image'], current_img)
                is_window_switched = (app_name != self.last_app_name or window_title != self.last_window_title)

            # 获取静态阈值配置
            static_threshold = self.config['triggers'].get('static_screen_threshold', 2.0)
            is_static = diff_percent < static_threshold

            # 3. 更新静态帧计数
            if is_static and not is_window_switched:
                self.static_count += 1
            else:
                self.static_count = 0

            # 4. 组装帧数据入队
            frame_data = {
                "timestamp": timestamp,
                "app": app_name,
                "title": window_title,
                "image": current_img,
                "diff_percent": diff_percent,
                "is_switched": is_window_switched,
                "static_count": self.static_count,
                "idle_seconds": idle_seconds  # 加进去方便调试
            }
            self.memory_queue.append(frame_data)
            self.last_app_name = app_name
            self.last_window_title = window_title

            # ==========================================
            # 🎯 核心调度大脑：唯一决策树
            # ==========================================
            current_time = time.time()
            time_since_last_ai = current_time - self.last_ai_trigger_time

            need_ai_analysis = False
            trigger_reason = ""

            # [优先级 1] 彻底离开电脑 (超过 10 分钟无操作)
            if idle_seconds > self.config['triggers'].get('away_from_keyboard_seconds', 600):
                need_ai_analysis = False
                trigger_reason = "无人操作(休眠/离开)"
                self._dispatch_pseudo_event("away", "无人操作", "已离开工位或挂机休眠", is_deviated=True)

            # [优先级 2] 窗口切换
            elif is_window_switched and self.config['triggers'].get('trigger_on_window_switch', True):
                need_ai_analysis = True
                trigger_reason = "窗口切换"

            # [优先级 3] 画面显著变化 (比如翻页、打字)
            elif not is_static:
                need_ai_analysis = True
                trigger_reason = "画面显著变化"

            # [优先级 4] 画面静止，进入进阶判断模式
            else:
                # 4.1 强制复查期 (挂机时间过长，强行让AI看一眼)
                if time_since_last_ai > self.config['triggers'].get('static_recheck_ai_seconds', 300):
                    need_ai_analysis = True
                    trigger_reason = "静态强制复查"

                # 4.2 质疑期 (超过 3 分钟没碰键鼠)
                elif idle_seconds > self.config['triggers'].get('no_input_suspect_seconds', 180):
                    need_ai_analysis = False
                    trigger_reason = "无有效输入(疑似停滞)"
                    self._dispatch_pseudo_event("suspect", app_name, "长时间停留在当前界面，无有效操作",
                                                is_deviated=True)

                # 4.3 正常阅读/思考期 (静止，且在宽限期内，直接不调AI)
                else:
                    need_ai_analysis = False
                    trigger_reason = "静态延续(阅读/思考)"

            # 5. 打印一条干净统一的日志
            logger.info(
                f"🎯 {app_name} | 窗口: {window_title[:15]}... | 差异: {diff_percent:.1f}% | 空闲: {idle_seconds:.0f}s | 触发AI: {need_ai_analysis} ({trigger_reason})"
            )

            # 6. 最终执行 AI 调用（并加入冷却时间防刷保护）
            if need_ai_analysis:
                min_interval = self.config['triggers'].get('min_ai_interval_seconds', 30)
                if time_since_last_ai >= min_interval:
                    self.last_ai_trigger_time = current_time
                    if hasattr(self, 'trigger_ai_callback'):
                        self.trigger_ai_callback(frame_data)
                else:
                    logger.debug(f"⏳ 触发被拦截：处于 AI 调用冷却期 (距上次仅 {time_since_last_ai:.1f}s)")

            # 7. 精准休眠
            elapsed = time.time() - start_time
            sleep_time = max(0, self.interval - elapsed)
            time.sleep(sleep_time)

    def _dispatch_pseudo_event(self, category, app_name, summary, is_deviated):
        """发送不经过 AI 的直接判定事件到存储层"""
        if hasattr(self, 'trigger_ai_callback'):
            # 直接构造一个符合 AI 输出格式的结果，假装是 AI 判定的
            pseudo_result = {
                "timestamp": datetime.datetime.now().isoformat(),
                "app": app_name,
                "title": "系统判定",
                "image": None,  # 丢弃图像
                "ai_direct_result": {  # 让 router 知道这是直通数据
                    "summary": summary,
                    "category": category,
                    "is_deviated": is_deviated,
                    "confidence": 1.0
                }
            }


if __name__ == "__main__":
    # 测试运行代码
    config = ConfigLoader.load()
    collector = DataCollector(config)
    collector.run_loop()