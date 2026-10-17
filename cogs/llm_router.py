from __future__ import annotations

import asyncio
import base64
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from dotenv import load_dotenv
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, '.env'))


def safe_log(msg: str) -> None:
    """安全日誌輸出函式，避免在 Windows cp950 終端因 Emoji 拋出 UnicodeEncodeError。"""
    try:
        print(msg)
    except UnicodeEncodeError:
        try:
            print(msg.encode('ascii', errors='replace').decode('ascii'))
        except Exception:
            pass


def strip_thinking_tags(text: str) -> str:
    """去除各類開源與專屬思考推理模型輸出的思考過程區塊 (<think>, <thought>, <thinking>, <reasoning> 等)。"""
    if not text:
        return ""

    cleaned = text

    # 1. 移除已閉合的各類 thinking 標籤 (支援 think, thought, thinking, reasoning 及其屬性)
    cleaned = re.sub(
        r'<(think|thought|thinking|reasoning)(?:\s[^>]*)?>[\s\S]*?</\1>',
        '',
        cleaned,
        flags=re.IGNORECASE,
    )

    # 2. 移除 markdown code block 格式的思考區塊 (如 ```thought ... ```)
    cleaned = re.sub(
        r'```(?:thought|thinking|reasoning)[\s\S]*?```',
        '',
        cleaned,
        flags=re.IGNORECASE,
    )

    # 3. 處理未閉合的思考標籤 (例如因 Token 限制或逾時在思考途中中斷)
    cleaned = re.sub(
        r'<(think|thought|thinking|reasoning)(?:\s[^>]*)?>[\s\S]*$',
        '',
        cleaned,
        flags=re.IGNORECASE,
    )

    # 4. 移除殘留的獨立閉合標籤
    cleaned = re.sub(
        r'</(think|thought|thinking|reasoning)>',
        '',
        cleaned,
        flags=re.IGNORECASE,
    )

    return cleaned.strip()


# 已停用或過期模型自動轉發對照表 (確保舊設定或資料庫殘留模型不致報 404/410)
DEPRECATED_MODEL_MAPPING = {
    "llama-3.3-70b-versatile": "meta/llama-3.2-11b-vision-instruct",
    "meta/llama-3.3-70b-instruct": "meta/llama-3.2-11b-vision-instruct",
    "gemini-2.5-flash-lite": "gemini-flash-latest",
    "gemini-2.0-flash": "gemini-flash-latest",
    "gemini-2.0-flash-exp:free": "gemini-flash-latest",
    "mistral-small-latest": "ministral-8b-latest",
    "kilo-auto/free": "gemini-flash-latest",
    "openrouter/auto": "auto",
}


@dataclass
class LLMProvider:
    id: str
    name: str
    base_url: str
    env_keys: List[str]
    default_text_model: str
    default_vision_model: Optional[str] = None
    fallback_models: List[str] = field(default_factory=list)
    supports_vision: bool = False
    is_zero_key: bool = False  # 是否支援無需 API Key 直接調用 (如未配置則略過)
    rpm_limit_desc: str = ""
    quota_desc: str = ""
    cooldown_until: float = 0.0
    last_error: Optional[str] = None
    last_used: float = 0.0
    total_calls: int = 0
    total_failures: int = 0
    client: Optional[AsyncOpenAI] = field(default=None, repr=False)

    def get_api_key(self) -> str:
        """從環境變數中取得目前可用的 API Key。"""
        for k in self.env_keys:
            val = os.getenv(k, "").strip()
            if val:
                return val
        if self.is_zero_key:
            return "zero-key-access"
        return ""

    def is_configured(self) -> bool:
        """檢查該提供商是否已在 .env 配置金鑰或為免金鑰提供商。"""
        if self.is_zero_key:
            return True
        return bool(self.get_api_key())

    def is_cooling_down(self) -> bool:
        """檢查是否處於限速 (429) 或故障冷卻倒數中。"""
        return time.time() < self.cooldown_until

    def mark_cooldown(self, seconds: float = 60.0, reason: str = "") -> None:
        """標記該提供商進入短期冷卻，避免後續請求重複撞牆。"""
        self.cooldown_until = time.time() + seconds
        self.last_error = reason
        self.total_failures += 1

    def clear_cooldown(self) -> None:
        """重設冷卻狀態。"""
        self.cooldown_until = 0.0
        self.last_error = None

    def get_client(self) -> AsyncOpenAI:
        """取得或建立 AsyncOpenAI 實例。"""
        api_key = self.get_api_key() or "dummy"
        if self.client is None:
            self.client = AsyncOpenAI(
                base_url=self.base_url,
                api_key=api_key,
                timeout=35.0,  # 寬裕 35 秒單次逾時限制
                max_retries=0,  # 停用 SDK 內部重試，改由 Router 即時切換下游提供商
            )
        return self.client


class MultiProviderLLMRouter:
    """多供應商免費 LLM 智慧路由器與自動容錯容災 (Failover) 閘道。

    文字請求鏈：Google AI Studio → NVIDIA NIM → Mistral AI
    視覺圖片鏈：Google AI Studio → NVIDIA NIM → Mistral AI
    """

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self.providers: Dict[str, LLMProvider] = {
            "gemini": LLMProvider(
                id="gemini",
                name="Google AI Studio (主要視覺/多模態)",
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                env_keys=["GEMINI_API_KEY", "GOOGLE_API_KEY"],
                default_text_model="gemini-flash-latest",
                default_vision_model="gemini-flash-latest",
                fallback_models=["gemma-4-31b-it", "gemini-2.0-flash-lite"],
                supports_vision=True,
                rpm_limit_desc="15 ~ 30 RPM",
                quota_desc="1,500 RPD (百萬超長 Context & 視覺分析)",
            ),
            "nvidia": LLMProvider(
                id="nvidia",
                name="NVIDIA NIM (大容量備援 #1)",
                base_url="https://integrate.api.nvidia.com/v1",
                env_keys=["NVIDIA_API_KEY", "NVIDIA_NIM_API_KEY"],
                default_text_model="meta/llama-3.2-11b-vision-instruct",
                default_vision_model="meta/llama-3.2-11b-vision-instruct",
                fallback_models=["meta/llama-3.2-90b-vision-instruct", "meta/llama2-70b"],
                supports_vision=True,
                rpm_limit_desc="40 RPM",
                quota_desc="10,000 RPD (圖文雙模態支援)",
            ),
            "mistral": LLMProvider(
                id="mistral",
                name="Mistral AI (突發與視覺備援 #2)",
                base_url="https://api.mistral.ai/v1",
                env_keys=["MISTRAL_API_KEY"],
                default_text_model="ministral-8b-latest",
                default_vision_model="ministral-8b-latest",
                fallback_models=["mistral-small-latest", "ministral-14b-latest"],
                supports_vision=True,
                rpm_limit_desc="60 RPM",
                quota_desc="500K TPM (支援圖文雙模態)",
            ),
        }

        # 預設優先容錯容災鏈
        self.text_chain: List[str] = ["gemini", "nvidia", "mistral"]
        self.vision_chain: List[str] = ["gemini", "nvidia", "mistral"]

    def reload_env_keys(self) -> None:
        """重新載入 .env 檔案並更新各提供商的 Client 連線狀態。"""
        load_dotenv(os.path.join(BASE_DIR, '.env'), override=True)
        for p in self.providers.values():
            p.client = None

    async def _get_http_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10.0))
        return self._session

    async def close(self) -> None:
        """關閉 HTTP 連線池。"""
        if self._session and not self._session.closed:
            await self._session.close()

    async def image_url_to_base64_data_uri(self, image_url: str) -> Optional[str]:
        """將 Discord 圖片附件 URL 轉換為 Base64 Data URI，確保跨提供商調用時圖片連結不因過期失效。"""
        try:
            session = await self._get_http_session()
            async with session.get(image_url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    content_type = resp.headers.get("Content-Type") or "image/png"
                    b64 = base64.b64encode(data).decode("utf-8")
                    return f"data:{content_type};base64,{b64}"
        except Exception as exc:
            print(f"⚠️ 圖片 Base64 轉換失敗 ({image_url}): {exc}")
        return None

    def get_providers_status(self) -> List[Dict[str, Any]]:
        """取得所有 AI 提供商當前的設定、在線與冷卻狀態。"""
        self.reload_env_keys()
        result = []
        now = time.time()
        for pid in ["gemini", "nvidia", "mistral"]:
            p = self.providers[pid]
            is_configured = p.is_configured()
            is_cooldown = now < p.cooldown_until
            remaining_cooldown = max(0, int(p.cooldown_until - now)) if is_cooldown else 0

            status_label = "正常在線"
            if not is_configured:
                status_label = "未配置金鑰"
            elif is_cooldown:
                status_label = f"冷卻中 ({remaining_cooldown}s)"

            result.append({
                "id": p.id,
                "name": p.name,
                "is_configured": is_configured,
                "is_zero_key": p.is_zero_key,
                "is_cooldown": is_cooldown,
                "cooldown_remaining": remaining_cooldown,
                "status_label": status_label,
                "rpm_limit": p.rpm_limit_desc,
                "quota": p.quota_desc,
                "supports_vision": p.supports_vision,
                "default_text_model": p.default_text_model,
                "default_vision_model": p.default_vision_model,
                "last_error": p.last_error,
                "total_calls": p.total_calls,
                "total_failures": p.total_failures,
            })
        return result

    def get_active_providers_for_chain(self, is_vision: bool = False, preferred_provider_id: Optional[str] = None) -> List[LLMProvider]:
        """取得符合條件且目前未在冷卻中的提供商鏈條，若有指定 preferred 則優先置頂。"""
        self.reload_env_keys()
        chain_ids = self.vision_chain if is_vision else self.text_chain
        active_list: List[LLMProvider] = []
        fallback_cooldown_list: List[LLMProvider] = []

        # 優先挑選有設定且未在冷卻中的提供商
        for pid in chain_ids:
            prov = self.providers.get(pid)
            if not prov or not prov.is_configured():
                continue
            if is_vision and not prov.supports_vision:
                continue

            if prov.is_cooling_down():
                fallback_cooldown_list.append(prov)
            else:
                active_list.append(prov)

        # 若使用者指定了特定提供商且可用，排在最前面
        if preferred_provider_id and preferred_provider_id in self.providers:
            p_obj = self.providers[preferred_provider_id]
            if p_obj in active_list:
                active_list.remove(p_obj)
                active_list.insert(0, p_obj)

        # 若所有正常提供商皆在冷卻，容許嘗試冷卻名單以防全部斷線
        return active_list if active_list else fallback_cooldown_list

    async def generate_chat_completion(
        self,
        messages: List[Dict[str, Any]],
        *,
        system_instruction: Optional[str] = None,
        image_url: Optional[str] = None,
        preferred_provider_id: Optional[str] = None,
        preferred_model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 3000,
    ) -> Tuple[Optional[str], Optional[str], Dict[str, Any]]:
        """執行跨提供商智慧容錯對話請求。

        回傳：(response_text, error_message, stats_meta)
        """
        start_time = time.perf_counter()
        is_vision = bool(image_url)

        # 0. 自動對照轉發過期或停用模型 (如舊設定或資料庫殘留)
        if preferred_model and preferred_model in DEPRECATED_MODEL_MAPPING:
            preferred_model = DEPRECATED_MODEL_MAPPING[preferred_model]

        # 1. 圖片處理：嘗試將 Discord URL 轉為 Base64 Data URI
        final_image_data_uri: Optional[str] = None
        if image_url:
            final_image_data_uri = await self.image_url_to_base64_data_uri(image_url) or image_url

        # 2. 構建請求訊息列表
        formatted_messages: List[Dict[str, Any]] = []
        if system_instruction and system_instruction.strip():
            formatted_messages.append({"role": "system", "content": system_instruction.strip()})

        for msg in messages:
            formatted_messages.append(msg)

        # 若有圖片且最後一條是使用者提問，轉換為多模態格式
        if final_image_data_uri and formatted_messages:
            last_msg = formatted_messages[-1]
            if last_msg.get("role") == "user":
                orig_text = str(last_msg.get("content") or "")
                last_msg["content"] = [
                    {"type": "text", "text": orig_text or "請分析這張圖片的內容。"},
                    {"type": "image_url", "image_url": {"url": final_image_data_uri}},
                ]

        # 3. 取得可用的候選提供商順序
        candidate_providers = self.get_active_providers_for_chain(
            is_vision=is_vision,
            preferred_provider_id=preferred_provider_id,
        )

        if not candidate_providers:
            return (
                None,
                "⚠️ **沒有可用的 AI 提供商**\n"
                "目前沒有配置任何有效的 API Key（例如 `GEMINI_API_KEY`、`NVIDIA_API_KEY`、`MISTRAL_API_KEY`），"
                "且備援通道目前無法連線。請至控制面板或主機 `.env` 配置至少一組免費金鑰！",
                {
                    "provider": "None",
                    "model": "None",
                    "thinking_time": time.perf_counter() - start_time,
                    "failover_history": [],
                    "total_tokens": 0,
                },
            )

        failover_history: List[str] = []
        last_error_details = ""

        # 4. 依序容錯嘗試
        for prov in candidate_providers:
            client = prov.get_client()
            attempt_start = time.perf_counter()
            prov.total_calls += 1

            # 決定優先嘗試之模型與備用模型清單
            base_model = prov.default_vision_model if (is_vision and prov.default_vision_model) else prov.default_text_model
            if preferred_model and preferred_provider_id == prov.id:
                base_model = preferred_model

            models_to_try = [base_model]
            for fb in prov.fallback_models:
                if fb not in models_to_try:
                    models_to_try.append(fb)

            provider_exhausted = False
            for model_attempt_idx, prov_model in enumerate(models_to_try):
                try:
                    call_kwargs: Dict[str, Any] = {
                        "model": prov_model,
                        "messages": formatted_messages,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                    }
                    # 若為 Gemini 且支援思考預算，嘗試禁用 thinking_budget 以免浪費 token 預算導致回覆被切斷
                    if prov.id == "gemini":
                        call_kwargs["extra_body"] = {"thinking_budget": 0}

                    # 寬裕 35.0 秒單次請求逾時（給予充分的生成時間，避免回覆到一半中斷）
                    try:
                        async with asyncio.timeout(35.0):
                            response = await client.chat.completions.create(**call_kwargs)
                    except Exception as call_exc:
                        # 若帶 extra_body 被 API 拒絕 (400 Bad Request)，去除 extra_body 自動降級重試
                        if "extra_body" in call_kwargs and ("400" in str(call_exc) or "extra_body" in str(call_exc) or "thinking" in str(call_exc)):
                            retry_kwargs = dict(call_kwargs)
                            retry_kwargs.pop("extra_body", None)
                            async with asyncio.timeout(35.0):
                                response = await client.chat.completions.create(**retry_kwargs)
                        else:
                            raise call_exc

                    elapsed = time.perf_counter() - start_time
                    choice = response.choices[0] if response.choices else None
                    if not choice or not choice.message:
                        raise ValueError(f"提供商 {prov.name} 未回傳有效 choices")

                    raw_content = choice.message.content or ""
                    clean_content = strip_thinking_tags(raw_content)

                    if not clean_content:
                        raise ValueError("AI 回傳之有效文字為空（可能在思考階段耗盡 Token）")

                    # 讀取 Token 統計
                    usage = getattr(response, "usage", None)
                    prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
                    completion_tokens = getattr(usage, "completion_tokens", 0) or 0
                    total_tokens = getattr(usage, "total_tokens", 0) or (prompt_tokens + completion_tokens)

                    if total_tokens <= 0:
                        total_tokens = max(1, len(clean_content) // 2 + 50)

                    prov.clear_cooldown()

                    stats = {
                        "provider_id": prov.id,
                        "provider_name": prov.name,
                        "model": prov_model,
                        "thinking_time": elapsed,
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": total_tokens,
                        "failover_history": failover_history,
                        "is_multimodal": is_vision,
                    }
                    return clean_content, None, stats

                except (NotFoundError, APIStatusError) as status_err:
                    status_code = getattr(status_err, 'status_code', None) or getattr(status_err, 'code', None)
                    # 遇 404 (Model Not Found)、410 (Model Gone/Retired) 或 503 時，若仍有備用模型，嘗試下一模型
                    if status_code in (404, 410, 503) and model_attempt_idx < len(models_to_try) - 1:
                        safe_log(f"[LLM Router] {prov.name} 模型 {prov_model} 暫不可用 (HTTP {status_code})，嘗試備用模型...")
                        continue

                    # 若為 429 速率限制
                    if status_code == 429:
                        err_str = f"HTTP 429 Rate Limit ({status_err})"
                        failover_history.append(f"{prov.name} (429 觸發冷卻 60s)")
                        prov.mark_cooldown(60.0, err_str)
                        last_error_details = err_str
                        safe_log(f"[LLM Router] {prov.name} 遇到 429 速率限制，標記冷卻並切換下一提供商...")
                        provider_exhausted = True
                        break

                    err_str = f"連線或服務端錯誤 (HTTP {status_code}: {status_err})"
                    failover_history.append(f"{prov.name} (HTTP {status_code})")
                    prov.mark_cooldown(45.0, err_str)
                    last_error_details = str(status_err)
                    safe_log(f"[LLM Router] {prov.name} 調用失敗: {status_err}")
                    provider_exhausted = True
                    break

                except RateLimitError as rle:
                    err_str = f"HTTP 429 Rate Limit ({rle})"
                    failover_history.append(f"{prov.name} (429 觸發冷卻 60s)")
                    prov.mark_cooldown(60.0, err_str)
                    last_error_details = err_str
                    safe_log(f"[LLM Router] {prov.name} 遇到 429 速率限制，標記冷卻並切換下一提供商...")
                    provider_exhausted = True
                    break

                except (APITimeoutError, asyncio.TimeoutError) as toe:
                    err_str = f"請求逾時 (>35.0s: {toe})"
                    failover_history.append(f"{prov.name} (逾時 35s)")
                    prov.mark_cooldown(45.0, err_str)
                    last_error_details = err_str
                    safe_log(f"[LLM Router] {prov.name} 請求逾時 (>35s)，切換下一提供商...")
                    provider_exhausted = True
                    break

                except (AuthenticationError, PermissionDeniedError) as auth_err:
                    err_str = f"金鑰無效或權限錯誤 ({auth_err})"
                    failover_history.append(f"{prov.name} (金鑰失效)")
                    prov.mark_cooldown(300.0, err_str)
                    last_error_details = err_str
                    safe_log(f"[LLM Router] {prov.name} 認證失敗，請檢查金鑰配置...")
                    provider_exhausted = True
                    break

                except Exception as gen_err:
                    err_str = f"連線或服務端錯誤 ({gen_err})"
                    failover_history.append(f"{prov.name} (錯誤: {type(gen_err).__name__})")
                    prov.mark_cooldown(45.0, err_str)
                    last_error_details = str(gen_err)
                    safe_log(f"[LLM Router] {prov.name} 調用失敗: {gen_err}")
                    provider_exhausted = True
                    break

            if provider_exhausted:
                continue

        # 5. 若所有提供商均嘗試失敗
        total_elapsed = time.perf_counter() - start_time
        is_timeout = (
            "逾時" in last_error_details
            or "Timeout" in last_error_details
            or "timeout" in last_error_details
            or any("逾時" in h for h in failover_history)
        )
        if is_timeout:
            err_msg = "請求逾時，請稍後再試"
        else:
            err_msg = "請求逾時，請稍後再試" if "401" in last_error_details or not last_error_details else "服務暫時無法回應，請稍後再試"
        return (
            None,
            err_msg,
            {
                "provider_id": "failed",
                "provider_name": "全部提供商斷線",
                "model": "failover_exhausted",
                "thinking_time": total_elapsed,
                "failover_history": failover_history,
                "total_tokens": 0,
            },
        )


# 全域單例路由器實例
router = MultiProviderLLMRouter()

