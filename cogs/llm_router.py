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


def strip_roleplay_prefix(text: str) -> str:
    """去除 LLaMA 等開源模型在對話開頭產生的「xxx的我」、「*xxx的我*」等角色扮演狀態前綴。"""
    if not text:
        return ""
    # 支援兩類：
    # 1. 有括號/星號包裹的: *xxx的我*, [xxx的我], （xxx的我）, 【xxx的我】
    # 2. 無括號但後方接空白或標點符號的: xxx的我 , xxx的我：, xxx的我,
    pattern = r'^(?:(?:[*(\[（【]([^\n\r*()\[\]）】]{1,20}的我)[*)\x5d）】]\s*[:：、，,]?\s*)|(?:([^\s\n\r*()\[\]）】]{1,15}的我)(?:\s+|[:：、，,]\s*)))'
    cleaned = text.strip()
    for _ in range(3):
        new_cleaned = re.sub(pattern, '', cleaned, count=1).strip()
        if new_cleaned == cleaned:
            break
        cleaned = new_cleaned
    return cleaned


# 已停用或過期模型自動轉發對照表 (確保舊設定或資料庫殘留模型不致報 404/410)
DEPRECATED_MODEL_MAPPING = {
    "llama-3.3-70b-versatile": "meta/llama-3.2-11b-vision-instruct",
    "meta/llama-3.3-70b-instruct": "meta/llama-3.2-11b-vision-instruct",
    "gemini-2.5-flash-lite": "gemini-flash-latest",
    "gemini-2.0-flash": "gemini-flash-latest",
    "gemini-2.0-flash-exp:free": "gemini-flash-latest",
    "mistral-small-latest": "ministral-8b-latest",
    "gemma-4-31b-it": "gemini-flash-latest",
    "gemini:gemma-4-31b-it": "gemini-flash-latest",
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
                default_text_model="gemini-3.5-flash-lite",
                default_vision_model="gemini-flash-latest",
                fallback_models=["gemini-3.5-flash-lite", "gemini-2.5-flash", "gemini-2.0-flash-lite"],
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

    def _record_failure(
        self,
        prov: LLMProvider,
        model: str,
        exc: Exception,
        failover_history: List[str],
    ) -> str:
        """記錄單一提供商或模型之執行失敗，設定合理冷卻時間並記錄歷程。"""
        status_code = getattr(exc, 'status_code', None) or getattr(exc, 'code', None)
        if isinstance(exc, (NotFoundError, APIStatusError)):
            if status_code in (404, 410, 503):
                safe_log(f"[LLM Router] {prov.name} 模型 {model} 暫不可用 (HTTP {status_code})")
                err_str = f"HTTP {status_code}"
                failover_history.append(f"{prov.name}:{model} (HTTP {status_code})")
                return err_str
            elif status_code == 429:
                err_str = f"HTTP 429 Rate Limit ({exc})"
                failover_history.append(f"{prov.name}:{model} (429 觸發冷卻 60s)")
                prov.mark_cooldown(60.0, err_str)
                safe_log(f"[LLM Router] {prov.name} 遇到 429 速率限制，標記冷卻並切換...")
                return err_str
            else:
                err_str = f"連線或服務端錯誤 (HTTP {status_code}: {exc})"
                failover_history.append(f"{prov.name}:{model} (HTTP {status_code})")
                prov.mark_cooldown(45.0, err_str)
                safe_log(f"[LLM Router] {prov.name} 調用失敗: {exc}")
                return err_str
        elif isinstance(exc, RateLimitError):
            err_str = f"HTTP 429 Rate Limit ({exc})"
            failover_history.append(f"{prov.name}:{model} (429 觸發冷卻 60s)")
            prov.mark_cooldown(60.0, err_str)
            safe_log(f"[LLM Router] {prov.name} 遇到 429 速率限制，標記冷卻並切換...")
            return err_str
        elif isinstance(exc, (APITimeoutError, asyncio.TimeoutError)):
            err_str = f"請求逾時 (>35.0s: {exc})"
            failover_history.append(f"{prov.name}:{model} (逾時 35s)")
            prov.mark_cooldown(45.0, err_str)
            safe_log(f"[LLM Router] {prov.name} 模型 {model} 請求逾時 (>35s)...")
            return err_str
        elif isinstance(exc, (AuthenticationError, PermissionDeniedError)):
            err_str = f"金鑰無效或權限錯誤 ({exc})"
            failover_history.append(f"{prov.name}:{model} (金鑰失效)")
            prov.mark_cooldown(300.0, err_str)
            safe_log(f"[LLM Router] {prov.name} 認證失敗，請檢查金鑰配置...")
            return err_str
        else:
            err_str = f"連線或服務端錯誤 ({exc})"
            failover_history.append(f"{prov.name}:{model} (錯誤: {type(exc).__name__})")
            prov.mark_cooldown(45.0, err_str)
            safe_log(f"[LLM Router] {prov.name} 調用失敗: {exc}")
            return err_str

    async def _execute_single_target(
        self,
        prov: LLMProvider,
        model: str,
        formatted_messages: List[Dict[str, Any]],
        temperature: float,
        max_tokens: int,
        is_vision: bool,
        timeout_seconds: float = 35.0,
    ) -> Tuple[str, Dict[str, Any]]:
        """執行單一提供商模型的對話生成請求。"""
        client = prov.get_client()
        call_start = time.perf_counter()
        prov.total_calls += 1

        call_kwargs: Dict[str, Any] = {
            "model": model,
            "messages": formatted_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        # 若為 Gemini 且支援思考預算，嘗試禁用 thinking_budget 以免浪費 token 預算導致回覆被切斷
        if prov.id == "gemini":
            call_kwargs["extra_body"] = {"thinking_budget": 0}

        try:
            async with asyncio.timeout(timeout_seconds):
                response = await client.chat.completions.create(**call_kwargs)
        except Exception as call_exc:
            # 若帶 extra_body 被 API 拒絕 (400 Bad Request)，去除 extra_body 自動降級重試
            if "extra_body" in call_kwargs and (
                "400" in str(call_exc) or "extra_body" in str(call_exc) or "thinking" in str(call_exc)
            ):
                retry_kwargs = dict(call_kwargs)
                retry_kwargs.pop("extra_body", None)
                async with asyncio.timeout(timeout_seconds):
                    response = await client.chat.completions.create(**retry_kwargs)
            else:
                raise call_exc

        elapsed = time.perf_counter() - call_start
        choice = response.choices[0] if response.choices else None
        if not choice or not choice.message:
            raise ValueError(f"提供商 {prov.name} 未回傳有效 choices")

        raw_content = choice.message.content or ""
        clean_content = strip_roleplay_prefix(strip_thinking_tags(raw_content))

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
            "model": model,
            "thinking_time": elapsed,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "is_multimodal": is_vision,
        }
        return clean_content, stats

    async def generate_chat_completion(
        self,
        messages: List[Dict[str, Any]],
        *,
        system_instruction: Optional[str] = None,
        image_url: Optional[str] = None,
        preferred_provider_id: Optional[str] = None,
        preferred_model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 4000,
    ) -> Tuple[Optional[str], Optional[str], Dict[str, Any]]:
        """執行跨提供商智慧容錯對話請求。
        
        採用 15 秒預熱對沖並行備援機制 (Hedged Requests)：
        - 主模型發起後等待 15 秒，若尚未產出回覆，在背景預先啟動備援模型請求。
        - 若原模型在硬逾時 (35s) 內成功產出，優先輸出原模型回覆並取消備援請求。
        - 若原模型真的 timeout 或失敗，才無縫輸出備援模型的回覆。
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
        anti_roleplay_rule = (
            "\n\n【格式約束】\n"
            "- 請直接針對使用者訊息進行自然對話回答。\n"
            "- 絕對嚴禁在回覆開頭添加動作、心理狀態或狀態描述前綴（例如嚴禁出現「xxx的我」、「*xxx的我*」、「[xxx的我]」等標籤）。\n"
            "- 嚴禁主動覆誦、確認或列出自己的系統人設與身份設定。"
        )
        base_instruction = (system_instruction or "").strip()
        full_system_instruction = (base_instruction + anti_roleplay_rule).strip() if base_instruction else anti_roleplay_rule.strip()
        formatted_messages.append({"role": "system", "content": full_system_instruction})

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

        # 4. 構建候選目標列表：[(prov, model), ...]
        candidate_targets: List[Tuple[LLMProvider, str]] = []
        for prov in candidate_providers:
            base_model = prov.default_vision_model if (is_vision and prov.default_vision_model) else prov.default_text_model
            if preferred_model and preferred_provider_id == prov.id:
                base_model = preferred_model

            models_to_try = [base_model]
            for fb in prov.fallback_models:
                if fb not in models_to_try:
                    models_to_try.append(fb)

            for prov_model in models_to_try:
                candidate_targets.append((prov, prov_model))

        # 去除重複目標保持唯一
        unique_targets: List[Tuple[LLMProvider, str]] = []
        seen = set()
        for p, m in candidate_targets:
            key = (p.id, m)
            if key not in seen:
                seen.add(key)
                unique_targets.append((p, m))
        candidate_targets = unique_targets

        if not candidate_targets:
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
        idx = 0
        total_targets = len(candidate_targets)

        # 5. 執行 15 秒預熱對沖並行容錯嘗試
        while idx < total_targets:
            primary_prov, primary_model = candidate_targets[idx]

            # 若該提供商處於冷卻中，且後方仍有未冷卻之提供商，則跳過
            if primary_prov.is_cooling_down() and any(not p.is_cooling_down() for p, _ in candidate_targets[idx + 1:]):
                idx += 1
                continue

            primary_task = asyncio.create_task(
                self._execute_single_target(
                    primary_prov,
                    primary_model,
                    formatted_messages,
                    temperature,
                    max_tokens,
                    is_vision,
                    timeout_seconds=35.0,
                )
            )

            # 階段一：等待主模型 15 秒
            done, _ = await asyncio.wait([primary_task], timeout=15.0)

            if primary_task in done:
                # 主模型在 15 秒內完成或報錯
                exc = primary_task.exception()
                if exc is None:
                    clean_content, stats = primary_task.result()
                    stats["failover_history"] = failover_history
                    stats["thinking_time"] = time.perf_counter() - start_time
                    return clean_content, None, stats
                else:
                    # 主模型在 15 秒內即拋錯 (如 429、404、503 等)，立即嘗試下一候選
                    last_error_details = self._record_failure(primary_prov, primary_model, exc, failover_history)
                    idx += 1
                    continue

            # 階段二：主模型超過 15 秒尚未回覆！預先啟動備援模型進行對沖請求
            fallback_task = None
            fallback_idx = idx + 1
            fallback_prov = None
            fallback_model = None

            # 尋找下一個可用的備援模型目標
            while fallback_idx < total_targets:
                fb_p, fb_m = candidate_targets[fallback_idx]
                if not fb_p.is_cooling_down() or all(p.is_cooling_down() for p, _ in candidate_targets[fallback_idx:]):
                    fallback_prov, fallback_model = fb_p, fb_m
                    break
                fallback_idx += 1

            if fallback_prov and fallback_model:
                safe_log(
                    f"[LLM Router] 主模型 {primary_prov.name} ({primary_model}) 超過 15s 未回覆，"
                    f"預先發送請求至備援模型 {fallback_prov.name} ({fallback_model})..."
                )
                fallback_task = asyncio.create_task(
                    self._execute_single_target(
                        fallback_prov,
                        fallback_model,
                        formatted_messages,
                        temperature,
                        max_tokens,
                        is_vision,
                        timeout_seconds=35.0,
                    )
                )

            # 階段三：主模型繼續執行，直到達到硬逾時限制 (剩餘 20 秒，即總共 35 秒)
            done, _ = await asyncio.wait([primary_task], timeout=20.0)

            if primary_task in done:
                exc = primary_task.exception()
                if exc is None:
                    # 原模型在 timeout 前產出回覆，優先輸出原模型！
                    clean_content, stats = primary_task.result()
                    safe_log(f"[LLM Router] 原模型 {primary_model} 於逾時前及時產出回覆，優先輸出並取消備援請求")
                    if fallback_task and not fallback_task.done():
                        fallback_task.cancel()
                        try:
                            await fallback_task
                        except (asyncio.CancelledError, Exception):
                            pass
                    stats["failover_history"] = failover_history
                    stats["thinking_time"] = time.perf_counter() - start_time
                    return clean_content, None, stats
                else:
                    # 主模型在 15~35 秒之間失敗
                    last_error_details = self._record_failure(primary_prov, primary_model, exc, failover_history)
                    safe_log(f"[LLM Router] 主模型 {primary_model} 執行失敗: {exc}")
            else:
                # 原模型真的 timeout (>35.0s)！取消原模型
                primary_task.cancel()
                try:
                    await primary_task
                except (asyncio.CancelledError, Exception):
                    pass
                err_str = "請求逾時 (>35.0s)"
                failover_history.append(f"{primary_prov.name}:{primary_model} (逾時 35s，啟用對沖備援)")
                primary_prov.mark_cooldown(45.0, err_str)
                last_error_details = err_str
                safe_log(f"[LLM Router] 原模型 {primary_model} 真正逾時 (>35s)，輸出備援模型之回覆")

            # 階段四：原模型逾時或失敗，輸出備援模型的回覆
            if fallback_task:
                # 若備援模型仍在執行中，等待其完成 (此時備援已在背景跑了 20 秒，最多再等 15 秒)
                if not fallback_task.done():
                    safe_log(f"[LLM Router] 等待預先發送之備援模型 {fallback_model} 完成...")
                    done_fb, _ = await asyncio.wait([fallback_task], timeout=15.0)

                if fallback_task.done():
                    fb_exc = fallback_task.exception()
                    if fb_exc is None:
                        clean_content, stats = fallback_task.result()
                        stats["failover_history"] = failover_history
                        stats["thinking_time"] = time.perf_counter() - start_time
                        safe_log(f"[LLM Router] 成功採用備援模型 {fallback_model} 回覆 (總耗時: {stats['thinking_time']:.2f}s)")
                        return clean_content, None, stats
                    else:
                        last_error_details = self._record_failure(fallback_prov, fallback_model, fb_exc, failover_history)
                        safe_log(f"[LLM Router] 備援模型 {fallback_model} 亦執行失敗: {fb_exc}")
                else:
                    # 備援模型亦逾時
                    fallback_task.cancel()
                    try:
                        await fallback_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    err_str = f"備援模型 {fallback_model} 請求逾時"
                    failover_history.append(f"{fallback_prov.name}:{fallback_model} (逾時)")
                    fallback_prov.mark_cooldown(45.0, err_str)
                    last_error_details = err_str

                # 已嘗試過 primary 與 fallback，推進索引至 fallback_idx + 1
                idx = fallback_idx + 1
            else:
                idx += 1

        # 6. 若所有提供商與備援模型均嘗試失敗
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

