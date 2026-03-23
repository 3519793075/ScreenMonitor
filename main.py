import asyncio
import threading
import uvicorn
from fastapi import FastAPI
from contextlib import asynccontextmanager
from loguru import logger
import os

from collector import ConfigLoader, DataCollector
from storage import StorageManager
from ai_router import AIRouter

# ================= 全局实例 =================
config = None
storage_manager = None
ai_router = None
collector_instance = None
main_loop = None


def on_ai_trigger(frame_data):
    """
    【跨线程桥梁】
    由于采集器在独立线程运行，而 AI 调用是异步的。
    这个函数负责将采集线程抛出的图像数据，安全地推入 FastAPI 的异步事件循环中。
    """
    if main_loop and ai_router:
        asyncio.run_coroutine_threadsafe(
            ai_router.analyze_frame(frame_data),
            main_loop
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ================= 🟢 新增：日志系统初始化 =================
    os.makedirs("logs", exist_ok=True)
    # 配置 loguru：同时输出到控制台和文件，每天零点新建文件，保留最近30天
    logger.add(
        "logs/system_{time:YYYY-MM-DD}.log",
        rotation="00:00",  # 每天午夜切割文件
        retention="30 days",  # 最长保留30天
        encoding="utf-8",
        level="INFO",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}"  # 日志格式
    )
    # =========================================================

    """FastAPI 生命周期管理：按顺序拉起各个核心模块"""
    global config, storage_manager, ai_router, collector_instance, main_loop

    logger.info("⏳ [系统] 正在初始化 AI 监工系统...")
    # 获取当前的异步事件循环
    main_loop = asyncio.get_running_loop()

    # 1. 加载配置
    config = ConfigLoader.load("config.yaml")

    # 2. 初始化记忆中枢 (Storage)
    storage_manager = StorageManager(config)

    # 3. 初始化大脑枢纽 (AIRouter)
    ai_router = AIRouter(config, storage_manager)

    # 4. 初始化眼睛 (Collector)
    collector_instance = DataCollector(config)

    # 将跨线程触发器动态挂载给采集器
    collector_instance.trigger_ai_callback = on_ai_trigger

    # 5. 在后台拉起高频采集线程
    collector_thread = threading.Thread(
        target=collector_instance.run_loop,
        daemon=True,
        name="DataCollectorThread"
    )
    collector_thread.start()

    logger.info("✅ [系统] 各模块缝合完毕，AI 监工全线启动！")

    yield

    logger.info("🛑 [系统] 接收到关闭信号，正在落盘最终数据...")
    if storage_manager and storage_manager.current_session:
        storage_manager._append_to_jsonl(storage_manager.current_session)
    logger.info("👋 [系统] 运行结束，再见！")


# ================= FastAPI 应用声明 =================
app = FastAPI(
    title="AI Supervisor Engine",
    description="本地学习状态多模态监控系统 (端云混排版)",
    version="2.0",
    lifespan=lifespan
)


# ================= 接口路由区 =================

@app.get("/")
async def root():
    return {"status": "ok", "message": "AI Supervisor is watching you..."}


@app.get("/api/session/current")
async def get_current_session():
    """获取当前的会话状态（你在干什么，持续了多久）"""
    if not storage_manager or not storage_manager.current_session:
        return {"message": "暂无状态"}
    return storage_manager.current_session


@app.get("/api/system/status")
async def get_system_status():
    """获取系统健康度与队列信息"""
    if not collector_instance:
        return {"error": "系统未完全启动"}

    return {
        "current_app": collector_instance.last_app_name,
        "current_window": collector_instance.last_window_title,
        "deque_size": len(collector_instance.memory_queue),
        "static_frames_count": collector_instance.static_count
    }


if __name__ == "__main__":
    # 请确保你在环境变量或 .env 中配置了 GEMINI_API_KEY
    # uvicorn 启动
    uvicorn.run("main:app", host="127.0.0.1", port=8000)