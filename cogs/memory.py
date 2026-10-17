from __future__ import annotations

import collections
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class ConversationTurn:
    role: str  # 'user' 或 'assistant'
    author_name: str
    content: str
    timestamp: float = 0.0


class ChannelConversationMemoryManager:
    """維護每個文字頻道或討論串 (Thread) 的最近 15 則滑動視窗對話記憶與專屬人設。"""

    def __init__(self, max_history: int = 15):
        self.max_history = max_history
        self._histories: Dict[int, collections.deque[ConversationTurn]] = {}
        self._personas: Dict[int, str] = {}  # 頻道專屬自訂人設覆蓋

    def _get_deque(self, channel_id: int) -> collections.deque[ConversationTurn]:
        if channel_id not in self._histories:
            self._histories[channel_id] = collections.deque(maxlen=self.max_history)
        return self._histories[channel_id]

    def add_user_message(self, channel_id: int, author_name: str, content: str) -> None:
        """紀錄發問者訊息，標註發言者名稱以利群聊上下文辨別。"""
        if not content.strip():
            return
        q = self._get_deque(channel_id)
        q.append(
            ConversationTurn(
                role="user",
                author_name=author_name.strip() or "User",
                content=content.strip(),
                timestamp=time.time(),
            )
        )

    def add_assistant_message(self, channel_id: int, content: str) -> None:
        """紀錄 AI 回覆訊息。"""
        if not content.strip():
            return
        q = self._get_deque(channel_id)
        q.append(
            ConversationTurn(
                role="assistant",
                author_name="AI",
                content=content.strip(),
                timestamp=time.time(),
            )
        )

    def get_context_messages(self, channel_id: int) -> List[Dict[str, str]]:
        """組裝提供給 LLM 的歷史上下文列表。群聊使用者訊息會格式化為 `[姓名]: 內容`。"""
        q = self._get_deque(channel_id)
        formatted: List[Dict[str, str]] = []
        for turn in q:
            if turn.role == "user":
                formatted.append({
                    "role": "user",
                    "content": f"[{turn.author_name}]: {turn.content}",
                })
            else:
                formatted.append({
                    "role": "assistant",
                    "content": turn.content,
                })
        return formatted

    def clear_channel(self, channel_id: int) -> int:
        """清除該頻道的對話歷史記憶，回傳被清除的訊息數量。"""
        if channel_id in self._histories:
            count = len(self._histories[channel_id])
            self._histories[channel_id].clear()
            return count
        return 0

    def set_channel_persona(self, channel_id: int, persona: str) -> None:
        """設定頻道專屬人設 (System Prompt)。"""
        if persona and persona.strip():
            self._personas[channel_id] = persona.strip()
        else:
            self._personas.pop(channel_id, None)

    def get_channel_persona(self, channel_id: int, fallback_default: str = "") -> str:
        """取得頻道專屬人設，若無則回退預設。"""
        return self._personas.get(channel_id) or fallback_default

    def reset_channel_persona(self, channel_id: int) -> bool:
        """重設頻道專屬人設回伺服器預設值。"""
        if channel_id in self._personas:
            del self._personas[channel_id]
            return True
        return False


# 全域單例對話記憶體實例
memory_manager = ChannelConversationMemoryManager(max_history=15)

