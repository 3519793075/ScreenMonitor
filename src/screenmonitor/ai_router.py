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
        self.provider_configs = self._build_provider_configs()
        self.provider_order = self._build_provider_order()
        self.provider_clients = self._initialize_provider_clients()

    def _resolve_qwen_model_name(self):
        configured_name = self.config.get("ai_models", {}).get("qwen_fallback_model", "qwen-vl-plus")
        if str(configured_name).startswith("qwen-image"):
            logger.warning(
                "qwen-image-2.0-pro 属于图像生成模型，不适合当前的图像理解兜底；已自动改用 qwen-vl-plus。"
            )
            return "qwen-vl-plus"
        return configured_name

    def _legacy_provider_configs(self):
        ai_models = self.config.get("ai_models", {})
        return {
            "gemini": {
                "type": "gemini",
                "enabled": ai_models.get("gemini_enabled", True) is not False,
                "model": ai_models.get("primary", "gemini-2.5-flash"),
                "api_key_ref": "gemini",
            },
            "qwen": {
                "type": "openai_compatible",
                "enabled": ai_models.get("qwen_enabled", True) is not False,
                "model": self._resolve_qwen_model_name(),
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "api_key_ref": "qwen",
                "api_key_env": "DASHSCOPE_API_KEY",
            },
            "kimi": {
                "type": "openai_compatible",
                "enabled": ai_models.get("kimi_enabled", True) is not False,
                "model": ai_models.get("fallback", "kimi-k2.5"),
                "base_url": "https://api.moonshot.cn/v1",
                "api_key_ref": "kimi",
                "temperature": 1,
            },
        }

    def _build_provider_configs(self):
        providers = self._legacy_provider_configs()
        configured_providers = self.config.get("ai_providers", {}) or {}
        for name, provider_config in configured_providers.items():
            merged = providers.get(name, {}) | (provider_config or {})
            providers[name] = merged
        return providers

    def _build_provider_order(self):
        configured_order = self.config.get("ai_models", {}).get("ai_provider_order", ["gemini", "qwen", "kimi"])
        if isinstance(configured_order, str):
            configured_order = [item.strip() for item in configured_order.split(",") if item.strip()]
        return [name for name in configured_order if name in self.provider_configs]

    def _get_api_key(self, provider_config):
        api_key = provider_config.get("api_key", "")
        if api_key:
            return api_key

        key_ref = provider_config.get("api_key_ref", "")
        if key_ref:
            api_key = self.config.get("api_keys", {}).get(key_ref, "")

        env_name = provider_config.get("api_key_env", "")
        if not api_key and env_name:
            api_key = os.getenv(env_name, "")

        return api_key

    def _initialize_provider_clients(self):
        clients = {}
        for name in self.provider_order:
            provider_config = self.provider_configs[name]
            if provider_config.get("enabled", True) is False:
                logger.warning(f"{name} provider is disabled by configuration.")
                continue

            api_key = self._get_api_key(provider_config)
            if not api_key:
                logger.warning(f"{name} provider API key is missing; provider is disabled.")
                continue

            provider_type = provider_config.get("type", "openai_compatible")
            if provider_type == "gemini":
                clients[name] = genai.Client(api_key=api_key)
            elif provider_type == "openai_compatible":
                clients[name] = AsyncOpenAI(
                    api_key=api_key,
                    base_url=provider_config["base_url"],
                )
            else:
                logger.warning(f"{name} provider type is unsupported: {provider_type}")

        return clients

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

    async def _call_gemini(self, provider_name, pil_image, prompt):
        """Call Gemini with the current screenshot."""
        client = self.provider_clients.get(provider_name)
        if not client:
            return None

        provider_config = self.provider_configs[provider_name]
        attempts = max(1, self.max_retries + 1)
        for attempt in range(1, attempts + 1):
            try:
                response = await self._run_with_timeout(
                    asyncio.to_thread(
                        client.models.generate_content,
                        model=provider_config["model"],
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

        logger.warning(f"{provider_name} provider is unavailable; trying next provider.")
        return None

    async def _call_openai_compatible(self, provider_name, base64_image, prompt):
        """Call an OpenAI-compatible vision provider."""
        client = self.provider_clients.get(provider_name)
        if not client:
            logger.warning(f"{provider_name} provider skipped because client is not initialized.")
            return None

        provider_config = self.provider_configs[provider_name]
        attempts = max(1, self.max_retries + 1)
        logger.info(f"Calling {provider_name} vision provider, model: {provider_config['model']}")

        for attempt in range(1, attempts + 1):
            try:
                create_kwargs = {
                    "model": provider_config["model"],
                    "messages": [
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
                    "max_tokens": int(provider_config.get("max_tokens", 600)),
                }
                if "temperature" in provider_config:
                    create_kwargs["temperature"] = provider_config["temperature"]

                response = await self._run_with_timeout(
                    client.chat.completions.create(**create_kwargs)
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

                logger.info(f"{provider_name} raw content: {repr(raw_content)[:1000]}")
                logger.info(f"{provider_name} response preview: {response_preview[:2000]}")

                parsed = self._parse_json_response(raw_content)
                if parsed:
                    return parsed

                logger.error(f"{provider_name} returned invalid content on attempt {attempt}/{attempts}.")
            except asyncio.TimeoutError:
                logger.error(f"{provider_name} timed out on attempt {attempt}/{attempts} after {self.timeout_seconds}s.")
            except Exception as exc:
                logger.error(f"{provider_name} call failed on attempt {attempt}/{attempts}: {exc}")

            if attempt < attempts:
                await asyncio.sleep(0.3)

        return None

    async def _call_provider(self, provider_name, pil_image, base64_image, prompt):
        provider_config = self.provider_configs[provider_name]
        provider_type = provider_config.get("type", "openai_compatible")
        if provider_type == "gemini":
            return await self._call_gemini(provider_name, pil_image, prompt)
        if provider_type == "openai_compatible":
            return await self._call_openai_compatible(provider_name, base64_image, prompt)

        logger.warning(f"{provider_name} provider type is unsupported: {provider_type}")
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
        base64_img = None
        ai_result = None
        source = "fallback_unknown"
        low_confidence_threshold = self.config["ai_models"]["low_confidence_threshold"]
        for provider_name in self.provider_order:
            if provider_name not in self.provider_clients:
                continue
            if self.provider_configs[provider_name].get("type", "openai_compatible") == "openai_compatible":
                base64_img = base64_img or self._image_to_base64(pil_image)

            provider_result = await self._call_provider(provider_name, pil_image, base64_img, prompt)
            if provider_result and provider_result.get("confidence", 0.0) >= low_confidence_threshold:
                ai_result = provider_result
                source = provider_name
                break

            if provider_result:
                logger.warning(
                    f"{provider_name} confidence too low "
                    f"({provider_result.get('confidence', 0.0):.2f} < {low_confidence_threshold:.2f}); trying next provider."
                )
            else:
                logger.warning(f"{provider_name} returned no usable result; trying next provider.")

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
