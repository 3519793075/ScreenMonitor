import asyncio
import base64
import json
import os
from io import BytesIO

from google import genai
from loguru import logger
from openai import AsyncOpenAI


class AIRouter:
    def __init__(self, config, storage_manager):
        self.config = config
        self.storage = storage_manager
        self.goal = self.config.get("context", {}).get("current_goal", "未知目标")
        self.timeout_seconds = int(self.config.get("ai_models", {}).get("timeout_seconds", 20))
        self.max_retries = int(self.config.get("ai_models", {}).get("max_retries", 0))
        self.qwen_model_name = self._resolve_qwen_model_name()

        self.gemini_api_key = self.config.get("api_keys", {}).get("gemini", "")
        self.qwen_api_key = self.config.get("api_keys", {}).get("qwen", "") or os.getenv("DASHSCOPE_API_KEY", "")
        self.kimi_api_key = self.config.get("api_keys", {}).get("kimi", "")

        self.gemini_client = None
        self.gemini_model_name = self.config["ai_models"]["primary"]
        if self.gemini_api_key and self.gemini_api_key != "在这里填入你的_Gemini_API_Key":
            self.gemini_client = genai.Client(api_key=self.gemini_api_key)
        else:
            logger.warning("Gemini API key is missing; Gemini analysis is disabled.")

        self.qwen_client = None
        if self.qwen_api_key:
            self.qwen_client = AsyncOpenAI(
                api_key=self.qwen_api_key,
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            )
        else:
            logger.warning("Qwen API key is missing; Qwen fallback is disabled.")

        self.kimi_client = None
        if self.kimi_api_key and self.kimi_api_key != "在这里填入你的_Kimi_API_Key_如果不用可留空":
            self.kimi_client = AsyncOpenAI(
                api_key=self.kimi_api_key,
                base_url="https://api.moonshot.cn/v1",
            )
        else:
            logger.warning("Kimi API key is missing; Kimi fallback is disabled.")

    def _resolve_qwen_model_name(self):
        configured_name = self.config.get("ai_models", {}).get("qwen_fallback_model", "qwen-vl-plus")
        if str(configured_name).startswith("qwen-image"):
            logger.warning(
                "qwen-image-2.0-pro 属于图像生成模型，不适合当前的图像理解兜底；已自动改用 qwen-vl-plus。"
            )
            return "qwen-vl-plus"
        return configured_name

    def _local_rule_engine(self, app_name, window_title):
        """Fast local classification for obvious apps and titles."""
        app_lower = app_name.lower()
        title_lower = window_title.lower()

        study_apps = ["code.exe", "pycharm64.exe", "obsidian.exe"]
        entertainment_keywords = ["bilibili", "youtube", "爱奇艺", "steam", "游戏"]
        chat_apps = ["wechat.exe", "qq.exe", "feishu.exe"]

        if app_lower in study_apps:
            return {
                "summary": f"正在使用 {app_name} 学习/工作",
                "category": "study",
                "is_deviated": False,
                "confidence": 0.95,
            }

        if any(keyword in title_lower for keyword in entertainment_keywords):
            return {
                "summary": f"浏览娱乐内容: {window_title}",
                "category": "entertainment",
                "is_deviated": True,
                "confidence": 0.95,
            }

        if app_lower in chat_apps:
            return {
                "summary": "正在使用通讯软件",
                "category": "communication",
                "is_deviated": True,
                "confidence": 0.90,
            }

        return None

    def _image_to_base64(self, pil_image):
        """Convert an in-memory PIL image into base64."""
        buffered = BytesIO()
        image_format = self.config["capture"]["format"].upper()
        if image_format == "JPG":
            image_format = "JPEG"
        pil_image.save(
            buffered,
            format=image_format,
            quality=self.config["capture"]["quality"],
        )
        return base64.b64encode(buffered.getvalue()).decode("utf-8")

    def _build_prompt(self, app_name, window_title):
        return f"""
你是一个严格的自律监督助手。
当前用户的学习目标是：{self.goal}
当前前台软件：{app_name}
当前窗口标题：{window_title}

请结合截图内容和上述信息，判断用户正在做什么。
请直接输出合法 JSON，不要包含 Markdown 或多余说明。
JSON 字段要求：
- "summary": 1-2 句话描述当前行为。
- "category": 必须是 "study", "entertainment", "communication", "unknown" 之一。
- "is_deviated": true/false，表示是否偏离学习目标。
- "confidence": 0.0 到 1.0 之间的浮点数。
严禁输出 JSON 之外的任何前后缀、解释文字、标题或 Markdown 代码块。
"""

    async def _run_with_timeout(self, coro):
        return await asyncio.wait_for(coro, timeout=self.timeout_seconds)

    async def _call_gemini(self, pil_image, prompt):
        """Call Gemini with the current screenshot."""
        if not self.gemini_client:
            return None

        attempts = max(1, self.max_retries + 1)
        for attempt in range(1, attempts + 1):
            try:
                response = await self._run_with_timeout(
                    asyncio.to_thread(
                        self.gemini_client.models.generate_content,
                        model=self.gemini_model_name,
                        contents=[prompt, pil_image],
                    )
                )
                parsed = self._parse_json_response(getattr(response, "text", ""))
                if parsed:
                    return parsed

                logger.error(f"Gemini 返回内容异常，第 {attempt}/{attempts} 次未解析出有效 JSON")
            except asyncio.TimeoutError:
                logger.error(f"Gemini 调用超时，第 {attempt}/{attempts} 次超过 {self.timeout_seconds}s")
            except Exception as exc:
                logger.error(f"Gemini 调用异常，第 {attempt}/{attempts} 次: {exc}")

            if attempt < attempts:
                await asyncio.sleep(0.3)

        logger.warning("Gemini 已不可用，准备切换到 Qwen 兜底")
        return None

    async def _call_kimi_fallback(self, base64_image, prompt):
        """Call Kimi as the vision fallback."""
        if not self.kimi_client:
            logger.warning("Kimi fallback skipped because client is not initialized.")
            return None

        attempts = max(1, self.max_retries + 1)
        logger.info("触发 Kimi Vision 兜底分析")

        for attempt in range(1, attempts + 1):
            try:
                response = await self._run_with_timeout(
                    self.kimi_client.chat.completions.create(
                        model=self.config["ai_models"]["fallback"],
                        messages=[
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": prompt},
                                    {
                                        "type": "image_url",
                                        "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"},
                                    },
                                ],
                            }
                        ],
                        max_tokens=600,
                        temperature=1,
                    )
                )
                raw_content = ""
                response_preview = ""

                try:
                    choice = response.choices[0]
                    raw_content = getattr(choice.message, "content", "")
                except Exception:
                    raw_content = ""

                try:
                    if hasattr(response, "model_dump_json"):
                        response_preview = response.model_dump_json(indent=2)
                    else:
                        response_preview = repr(response)
                except Exception:
                    response_preview = repr(response)

                logger.info(f"Kimi 原始 content: {repr(raw_content)[:1000]}")
                logger.info(f"Kimi 原始响应预览: {response_preview[:2000]}")

                parsed = self._parse_json_response(raw_content)
                if parsed:
                    return parsed

                logger.error(f"Kimi 返回内容异常，第 {attempt}/{attempts} 次未解析出有效 JSON")
            except asyncio.TimeoutError:
                logger.error(f"Kimi 调用超时，第 {attempt}/{attempts} 次超过 {self.timeout_seconds}s")
            except Exception as exc:
                logger.error(f"Kimi 调用异常，第 {attempt}/{attempts} 次: {exc}")

            if attempt < attempts:
                await asyncio.sleep(0.3)

        return None

    async def _call_qwen_fallback(self, base64_image, prompt):
        """Call Qwen as the first vision fallback after Gemini."""
        if not self.qwen_client:
            logger.warning("Qwen fallback skipped because client is not initialized.")
            return None

        attempts = max(1, self.max_retries + 1)
        logger.info(f"触发 Qwen Vision 兜底分析，模型: {self.qwen_model_name}")

        for attempt in range(1, attempts + 1):
            try:
                response = await self._run_with_timeout(
                    self.qwen_client.chat.completions.create(
                        model=self.qwen_model_name,
                        messages=[
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": prompt},
                                    {
                                        "type": "image_url",
                                        "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"},
                                    },
                                ],
                            }
                        ],
                        max_tokens=600,
                    )
                )
                raw_content = ""
                response_preview = ""

                try:
                    choice = response.choices[0]
                    raw_content = getattr(choice.message, "content", "")
                except Exception:
                    raw_content = ""

                try:
                    if hasattr(response, "model_dump_json"):
                        response_preview = response.model_dump_json(indent=2)
                    else:
                        response_preview = repr(response)
                except Exception:
                    response_preview = repr(response)

                logger.info(f"Qwen 原始 content: {repr(raw_content)[:1000]}")
                logger.info(f"Qwen 原始响应预览: {response_preview[:2000]}")

                parsed = self._parse_json_response(raw_content)
                if parsed:
                    return parsed

                logger.error(f"Qwen 返回内容异常，第 {attempt}/{attempts} 次未解析出有效 JSON")
            except asyncio.TimeoutError:
                logger.error(f"Qwen 调用超时，第 {attempt}/{attempts} 次超过 {self.timeout_seconds}s")
            except Exception as exc:
                logger.error(f"Qwen 调用异常，第 {attempt}/{attempts} 次: {exc}")

            if attempt < attempts:
                await asyncio.sleep(0.3)

        return None

    @staticmethod
    def _parse_json_response(text):
        """Clean and parse model output as JSON."""
        try:
            text = (text or "").strip()
            if text.startswith("```json"):
                text = text[7:]
            if text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

            if not text:
                raise ValueError("empty response")

            try:
                return json.loads(text)
            except json.JSONDecodeError:
                start = text.find("{")
                end = text.rfind("}")
                if start != -1 and end != -1 and end > start:
                    return json.loads(text[start : end + 1])
                raise
        except Exception as exc:
            logger.error(f"解析 AI JSON 输出失败: {exc} | 原始输出: {text[:50]}...")
            return None

    async def analyze_frame(self, frame_data):
        """Main routing entry for a captured frame or pseudo event."""
        app_name = frame_data["app"]
        window_title = frame_data["title"]
        timestamp = frame_data["timestamp"]

        direct_result = frame_data.get("ai_direct_result")
        if direct_result:
            logger.info(f"系统直连判定: {direct_result.get('summary', '')}")
            self._log_to_storage(frame_data, direct_result, source="system")
            return

        local_result = self._local_rule_engine(app_name, window_title)
        if local_result:
            logger.info(f"本地规则命中: {local_result['summary']}")
            self._log_to_storage(frame_data, local_result, source="local")
            return

        prompt = self._build_prompt(app_name, window_title)
        pil_image = frame_data["image"]

        ai_result = await self._call_gemini(pil_image, prompt)
        source = "gemini"

        low_confidence_threshold = self.config["ai_models"]["low_confidence_threshold"]
        if not ai_result or ai_result.get("confidence", 0.0) < low_confidence_threshold:
            base64_img = self._image_to_base64(pil_image)
            if ai_result:
                logger.warning(
                    f"Gemini 结果置信度过低({ai_result.get('confidence', 0.0):.2f} < "
                    f"{low_confidence_threshold:.2f})，切换到 Qwen"
                )
            else:
                logger.warning("Gemini 未返回可用结果，立即切换到 Qwen")

            qwen_result = await self._call_qwen_fallback(base64_img, prompt)
            if qwen_result and qwen_result.get("confidence", 0.0) >= low_confidence_threshold:
                ai_result = qwen_result
                source = "qwen"
            else:
                if qwen_result:
                    logger.warning(
                        f"Qwen 结果置信度过低({qwen_result.get('confidence', 0.0):.2f} < "
                        f"{low_confidence_threshold:.2f})，切换到 Kimi"
                    )
                else:
                    logger.warning("Qwen 未返回可用结果，立即切换到 Kimi")

                kimi_result = await self._call_kimi_fallback(base64_img, prompt)
                if kimi_result:
                    ai_result = kimi_result
                    source = "kimi"

        if not ai_result:
            ai_result = {
                "summary": f"无法识别的内容 ({app_name})",
                "category": "unknown",
                "is_deviated": False,
                "confidence": 0.0,
            }
            source = "fallback_unknown"

        evidence_path = ""
        if pil_image and ai_result.get("is_deviated") and ai_result.get("confidence", 0.0) > 0.8:
            save_dir = "anomaly_screenshots"
            os.makedirs(save_dir, exist_ok=True)
            safe_time = timestamp.replace(":", "-").replace(".", "-")
            evidence_path = os.path.join(save_dir, f"distraction_{safe_time}.jpg")
            pil_image.save(evidence_path, quality=80)
            logger.info(f"捕获偏离行为，证据已保存到 {evidence_path}")

        if evidence_path:
            ai_result["evidence_image_path"] = evidence_path

        logger.info(f"AI 分析完成 ({source}): {ai_result['summary']} (偏离: {ai_result.get('is_deviated')})")
        self._log_to_storage(frame_data, ai_result, source)

    def _log_to_storage(self, frame_data, result_dict, source):
        """Normalize the result and pass it to StorageManager."""
        event_data = {
            "timestamp": frame_data["timestamp"],
            "app_name": frame_data["app"],
            "window_title": frame_data["title"],
            "category": result_dict.get("category", "unknown"),
            "ai_summary": result_dict.get("summary", ""),
            "is_deviated": result_dict.get("is_deviated", False),
            "confidence": result_dict.get("confidence", 1.0),
            "model_used": source,
            "evidence_image_path": result_dict.get("evidence_image_path", ""),
        }
        self.storage.log_event(event_data)
