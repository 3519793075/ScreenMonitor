import os
import json
from loguru import logger
import asyncio
import base64
from io import BytesIO
import google.generativeai as genai
from google import genai
from google.genai import types
# 如果使用 Kimi Vision，通常可以通过 OpenAI SDK 兼容调用
from openai import AsyncOpenAI


class AIRouter:
    def __init__(self, config, storage_manager):
        self.config = config
        self.storage = storage_manager

        # 本地学习目标上下文
        self.goal = self.config.get('context', {}).get('current_goal', '未知目标')

        # 🟢 修改点：直接从 config.yaml 中读取 API Key

        self.gemini_api_key = self.config.get('api_keys', {}).get('gemini', '')
        self.kimi_api_key = self.config.get('api_keys', {}).get('kimi', '')

        # 初始化 Gemini
        if self.gemini_api_key and self.gemini_api_key != "在这里填入你的_Gemini_API_Key":
            self.gemini_client = genai.Client(api_key=self.gemini_api_key)
            self.gemini_model_name = self.config['ai_models']['primary']
        else:
            logger.warning("⚠️ 警告: 未配置 Gemini API Key，AI 分析功能将失效！")

        # 初始化 Kimi (备选)
        if self.kimi_api_key and self.kimi_api_key != "在这里填入你的_Kimi_API_Key_如果不用可留空":
            self.kimi_client = AsyncOpenAI(
                api_key=self.kimi_api_key,
                base_url="https://api.moonshot.cn/v1",
            )

    def _local_rule_engine(self, app_name, window_title):
        """本地一级分类器：极速匹配黑白名单"""
        app_lower = app_name.lower()
        title_lower = window_title.lower()

        # 假设这里配置了硬编码或从 yaml 读取的规则
        study_apps = ['code.exe', 'pycharm64.exe', 'obsidian.exe']
        entertainment_keywords = ['bilibili', 'youtube', '爱奇艺', 'steam', '游戏']
        chat_apps = ['wechat.exe', 'qq.exe', 'feishu.exe']

        if app_lower in study_apps:
            return {"summary": f"正在使用 {app_name} 学习/工作", "category": "study", "is_deviated": False,
                    "confidence": 0.95}

        if any(keyword in title_lower for keyword in entertainment_keywords):
            return {"summary": f"浏览娱乐内容: {window_title}", "category": "entertainment", "is_deviated": True,
                    "confidence": 0.95}

        if app_lower in chat_apps:
            return {"summary": "正在使用通讯软件", "category": "communication", "is_deviated": True, "confidence": 0.90}

        # 本地无法确定，需要交给 AI
        return None

    def _image_to_base64(self, pil_image):
        """将内存中的 PIL 图像转为 Base64"""
        buffered = BytesIO()
        pil_image.save(buffered, format=self.config['capture']['format'].upper(),
                       quality=self.config['capture']['quality'])
        return base64.b64encode(buffered.getvalue()).decode('utf-8')

    def _build_prompt(self, app_name, window_title):
        return f"""
你是一个严格的自律监督助手。
当前用户的学习目标是：{self.goal}
当前前台软件：{app_name}
当前窗口标题：{window_title}

请结合截图内容和上述信息，判断用户正在做什么。
强制要求：请直接输出合法的 JSON 格式，不要包含任何 Markdown 标记或多余文字。
JSON 字段要求：
- "summary": 1-2句话描述当前行为。
- "category": 枚举值，必须是 "study", "entertainment", "communication", 或 "unknown" 之一。
- "is_deviated": 布尔值(true/false)，判断当前行为是否偏离了学习目标。
- "confidence": 浮点数(0.0到1.0)，表示你对该判断的置信度。
"""

    async def _call_gemini(self, pil_image, prompt):
        """异步调用 Gemini 分析图像 (使用新版 SDK)"""
        try:
            response = await asyncio.to_thread(
                self.gemini_client.models.generate_content,
                model=self.gemini_model_name,
                contents=[prompt, pil_image]
            )
            return self._parse_json_response(response.text)
        except Exception as e:
            logger.error(f"⚠️ Gemini 调用异常: {e}")
            return None

    async def _call_kimi_fallback(self, base64_image, prompt):
        """兜底策略：异步调用 Kimi Vision"""
        if not self.kimi_api_key:
            return None
        logger.info("🔄 触发 Kimi Vision 兜底分析...")
        try:
            response = await self.kimi_client.chat.completions.create(
                model=self.config['ai_models']['fallback'],
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
                            }
                        ]
                    }
                ],
                max_tokens=200,
                temperature=0.1
            )
            return self._parse_json_response(response.choices[0].message.content)
        except Exception as e:
            logger.error(f"❌ Kimi 调用也失败了: {e}")
            return None

    def _parse_json_response(self, text):
        """清理 LLM 输出并解析为字典"""
        try:
            # 移除可能存在的 Markdown 格式，例如 ```json 和 ```
            text = text.strip()
            if text.startswith("```json"):
                text = text[7:]
            if text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
            return json.loads(text.strip())
        except Exception as e:
            logger.error(f"解析 AI JSON 输出失败: {e} | 原始输出: {text[:50]}...")
            return None

    async def analyze_frame(self, frame_data):
        """核心调度入口：接收一帧数据进行处理"""
        app_name = frame_data['app']
        window_title = frame_data['title']
        timestamp = frame_data['timestamp']

        # 1. 本地规则引擎预判
        local_result = self._local_rule_engine(app_name, window_title)
        if local_result:
            logger.info(f"⚡ 本地规则命中: {local_result['summary']}")
            self._log_to_storage(frame_data, local_result, source="local")
            return

        # 2. 准备 AI 分析
        prompt = self._build_prompt(app_name, window_title)
        pil_image = frame_data['image']

        # 3. 调用主模型 Gemini
        ai_result = await self._call_gemini(pil_image, prompt)
        source = "gemini"

        # 4. 评估结果，决定是否触发备用模型
        low_confidence_threshold = self.config['ai_models']['low_confidence_threshold']

        if not ai_result or ai_result.get('confidence', 0.0) < low_confidence_threshold:
            # 触发兜底
            base64_img = self._image_to_base64(pil_image)
            fallback_result = await self._call_kimi_fallback(base64_img, prompt)
            if fallback_result:
                ai_result = fallback_result
                source = "kimi"

        # 5. 如果全军覆没，给出默认未知状态
        if not ai_result:
            ai_result = {
                "summary": f"无法识别的内容 ({app_name})",
                "category": "unknown",
                "is_deviated": False,
                "confidence": 0.0
            }
            source = "fallback_unknown"

        # 6. 处理异常截图保留逻辑
        evidence_path = ""
        if ai_result.get('is_deviated') and ai_result.get('confidence', 0.0) > 0.8:
            # 明确分心，将内存中的图写入硬盘留存证据
            save_dir = "anomaly_screenshots"
            os.makedirs(save_dir, exist_ok=True)
            safe_time = timestamp.replace(":", "-").replace(".", "-")
            evidence_path = os.path.join(save_dir, f"distraction_{safe_time}.jpg")
            pil_image.save(evidence_path, quality=80)
            logger.info(f"📸 捕获偏离目标行为！证据已保存至: {evidence_path}")

        logger.info(f"🤖 AI 分析完成 ({source}): {ai_result['summary']} (偏离: {ai_result.get('is_deviated')})")

        # 7. 写入存储模块
        self._log_to_storage(frame_data, ai_result, source)

    def _log_to_storage(self, frame_data, result_dict, source):
        """将最终分析结果整合并送入 StorageManager"""
        event_data = {
            "timestamp": frame_data['timestamp'],
            "app_name": frame_data['app'],
            "window_title": frame_data['title'],
            "category": result_dict.get('category', 'unknown'),
            "ai_summary": result_dict.get('summary', ''),
            "is_deviated": result_dict.get('is_deviated', False),
            "confidence": result_dict.get('confidence', 1.0),
            "model_used": source,
            "evidence_image_path": result_dict.get('evidence_image_path', '')
        }
        self.storage.log_event(event_data)