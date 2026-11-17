"""Lavalink 雙節點設定讀取、最佳節點選擇演算法與退避重連工具。

本模組保持輕量且無狀態依賴，讓選點演算法（pick_best_node）與退避計算
（compute_reconnect_backoff）可直接透過純函式進行離線單元測試。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


@dataclass(frozen=True, slots=True)
class LavalinkNodeConfig:
    """單一公開 Lavalink v4 節點的連線設定（資料僅來自環境變數，不寫死密碼或主機）。"""

    identifier: str
    uri: str
    password: str
    slot: str
    from_legacy_env: bool = False


def _clean_env(env: Mapping[str, str], key: str) -> str:
    return str(env.get(key, "") or "").strip()


def load_lavalink_node_configs(
    env: Mapping[str, str] | None = None,
    *,
    log_warnings: bool = True,
) -> list[LavalinkNodeConfig]:
    """從環境變數載入雙 Lavalink 節點設定。

    讀取規則：
    1. Node 1：優先讀取 `LAVALINK_NODE1_URI` / `LAVALINK_NODE1_PASSWORD` / `LAVALINK_NODE1_IDENTIFIER`
       （未設定 URI 時，向下相容退回讀取既有的 `LAVALINK_URI` / `LAVALINK_PASSWORD`）。
    2. Node 2：讀取 `LAVALINK_NODE2_URI` / `LAVALINK_NODE2_PASSWORD` / `LAVALINK_NODE2_IDENTIFIER`。
    3. 任一節點設定缺失僅印出警告並略過該節點，絕不強制結束程式；
       只設定一個節點時降級為單節點模式正常運作。
    """
    source_env = os.environ if env is None else env
    configs: list[LavalinkNodeConfig] = []

    # --- Node 1（支援向下相容舊環境變數 LAVALINK_URI / LAVALINK_PASSWORD）---
    n1_uri = _clean_env(source_env, "LAVALINK_NODE1_URI").rstrip("/")
    n1_pw = _clean_env(source_env, "LAVALINK_NODE1_PASSWORD")
    n1_id = _clean_env(source_env, "LAVALINK_NODE1_IDENTIFIER") or "node1"
    n1_legacy = False

    if not n1_uri:
        legacy_uri = _clean_env(source_env, "LAVALINK_URI").rstrip("/")
        legacy_pw = _clean_env(source_env, "LAVALINK_PASSWORD")
        if legacy_uri:
            n1_uri = legacy_uri
            n1_pw = n1_pw or legacy_pw
            n1_legacy = True

    if n1_uri:
        if not n1_pw:
            if log_warnings:
                pw_var = "LAVALINK_PASSWORD" if n1_legacy else "LAVALINK_NODE1_PASSWORD"
                print(f"⚠️ 已設定 Node 1 位址但缺少 {pw_var}，略過 Node 1。")
        else:
            configs.append(
                LavalinkNodeConfig(
                    identifier=n1_id,
                    uri=n1_uri,
                    password=n1_pw,
                    slot="node1",
                    from_legacy_env=n1_legacy,
                )
            )

    # --- Node 2 ---
    n2_uri = _clean_env(source_env, "LAVALINK_NODE2_URI").rstrip("/")
    n2_pw = _clean_env(source_env, "LAVALINK_NODE2_PASSWORD")
    n2_id = _clean_env(source_env, "LAVALINK_NODE2_IDENTIFIER") or "node2"

    if n2_uri:
        if not n2_pw:
            if log_warnings:
                print("⚠️ 已設定 LAVALINK_NODE2_URI 但缺少 LAVALINK_NODE2_PASSWORD，略過 Node 2。")
        else:
            existing_ids = {c.identifier for c in configs}
            if n2_id in existing_ids:
                resolved_id = f"{n2_id}-2"
                if log_warnings:
                    print(
                        f"⚠️ LAVALINK_NODE2_IDENTIFIER（{n2_id}）與 Node 1 重複，"
                        f"已自動調整為 {resolved_id} 以避免連線池識別衝突。"
                    )
                n2_id = resolved_id
            configs.append(
                LavalinkNodeConfig(
                    identifier=n2_id,
                    uri=n2_uri,
                    password=n2_pw,
                    slot="node2",
                    from_legacy_env=False,
                )
            )

    if log_warnings:
        if not configs:
            print(
                "⚠️ 環境變數尚未設定任何 Lavalink 節點（LAVALINK_NODE1_URI / LAVALINK_NODE2_URI / LAVALINK_URI），"
                "已略過音樂節點連線，其他功能仍正常啟動。"
            )
        elif len(configs) == 1:
            print(
                f"⚠️ 目前僅設定單一 Lavalink 節點（{configs[0].identifier} -> {configs[0].uri}），"
                "已降級為單節點模式運作（設定第二組 LAVALINK_NODE2_* 後即可啟用雙節點容錯）。"
            )
        else:
            summary = "、".join(f"{c.identifier} ({c.uri})" for c in configs)
            print(f"ℹ️ 已載入雙 Lavalink 節點設定：{summary}")

    return configs


def _extract_status_name(status: Any, connected_flag: Any = None) -> str:
    """將 Enum / 字串 / 布林狀態正規化為大寫狀態字串。"""
    if status is not None:
        name = getattr(status, "name", None)
        if name:
            return str(name).strip().upper()
        return str(status).strip().upper()
    if connected_flag is True:
        return "CONNECTED"
    return "DISCONNECTED"


def _to_non_negative_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num < 0:
        return None
    return num


def _compute_load_score(state: Mapping[str, Any]) -> float | None:
    """計算單一節點的輔助負載分數（CPU / 記憶體）；資料均取不到時回傳 None。

    計算權重設計：
    - 若同時取得 CPU 與記憶體使用率，以 `0.7 * cpu + 0.3 * memory` 綜合評估
      （語音編解碼與串流轉發對 CPU 延遲較為敏感，故給予較高權重）。
    - 若僅取得 CPU 或僅取得記憶體，則單獨使用已取得之指標進行比較。
    - 兩者皆無法取得時回傳 None，讓選點演算法退回只比玩家數。
    """
    cpu_load = _to_non_negative_float(state.get("cpu_load"))
    if cpu_load is None:
        lavalink_load = _to_non_negative_float(state.get("lavalink_load"))
        system_load = _to_non_negative_float(state.get("system_load"))
        if lavalink_load is not None and system_load is not None:
            cpu_load = 0.7 * lavalink_load + 0.3 * system_load
        elif lavalink_load is not None:
            cpu_load = lavalink_load
        elif system_load is not None:
            cpu_load = system_load

    mem_ratio = _to_non_negative_float(state.get("memory_ratio"))
    if mem_ratio is None:
        mem_used = _to_non_negative_float(state.get("memory_used"))
        mem_cap = (
            _to_non_negative_float(state.get("memory_reservable"))
            or _to_non_negative_float(state.get("memory_allocated"))
        )
        if mem_used is not None and mem_cap is not None and mem_cap > 0:
            mem_ratio = min(1.0, max(0.0, mem_used / mem_cap))

    if cpu_load is not None and mem_ratio is not None:
        return 0.7 * cpu_load + 0.3 * mem_ratio
    if cpu_load is not None:
        return cpu_load
    if mem_ratio is not None:
        return mem_ratio
    return None


def pick_best_node(
    node_states: Mapping[str, Mapping[str, Any]],
    *,
    exclude: Iterable[str] = (),
) -> str | None:
    """純函式：從節點狀態字典中選出最適合承載新玩家的節點識別（identifier）。

    選擇規則（依序套用）：
    1. **狀態過濾**：只考慮狀態為 `CONNECTED` 且不在 `exclude` 排除清單中的節點；
       若沒有任何可用節點，回傳 `None`。
    2. **主要指標（玩家數優先）**：找出目前 `players`（玩家數）最低的候選節點群組；
       只要某節點玩家數嚴格少於另一節點，即優先勝出。
    3. **輔助指標（CPU／記憶體負載）**：當多個節點玩家數相同時，檢查是否皆能取得
       CPU 或記憶體負載分數；若皆有負載資料，挑出負載分數最低者。
    4. **無負載資料退回機制**：若相同玩家數的候選節點中缺少負載資料（例如剛連上尚未
       收到 `/v4/stats`），安全退回只比較玩家數（維持輸入順序取第一個）。
    """
    if not node_states:
        return None

    excluded_ids = {str(x) for x in exclude if x is not None}
    candidates: list[tuple[str, int, float | None]] = []

    for identifier, info in node_states.items():
        ident_str = str(identifier)
        if ident_str in excluded_ids or not isinstance(info, Mapping):
            continue

        status_name = _extract_status_name(info.get("status"), info.get("connected"))
        if status_name != "CONNECTED":
            continue

        raw_players = info.get("players")
        if raw_players is None:
            raw_players = info.get("playing")
        try:
            players = max(0, int(raw_players or 0))
        except (TypeError, ValueError):
            players = 0

        load_score = _compute_load_score(info)
        candidates.append((ident_str, players, load_score))

    if not candidates:
        return None

    # 先找出玩家數最少的一群候選節點
    min_players = min(item[1] for item in candidates)
    tied = [item for item in candidates if item[1] == min_players]

    if len(tied) == 1:
        return tied[0][0]

    # 玩家數相同時，若所有平手節點都有可取得的 CPU／記憶體負載資料，再比負載最低者；
    # 若有節點取不到負載資料，依規則退回只比玩家數（維持順序選第一位）。
    if all(item[2] is not None for item in tied):
        best = min(tied, key=lambda item: float(item[2]))  # type: ignore[arg-type]
        return best[0]

    return tied[0][0]


def extract_node_state(
    identifier: str,
    node: Any | None,
    *,
    cached_stats: Mapping[str, Any] | None = None,
    latency_ms: float | None = None,
    configured_uri: str | None = None,
) -> dict[str, Any]:
    """將實際 wavelink.Node（或離線佔位節點）與快取統計整理為標準節點狀態字典。"""
    if node is None:
        return {
            "identifier": identifier,
            "uri": configured_uri or "未設定",
            "status": "DISCONNECTED",
            "local_players": 0,
            "total_players": None,
            "players": 0,
            "cpu_load": None,
            "lavalink_load": None,
            "system_load": None,
            "cpu_cores": None,
            "memory_used": None,
            "memory_allocated": None,
            "memory_reservable": None,
            "memory_free": None,
            "memory_ratio": None,
            "latency_ms": latency_ms,
            "session_id": None,
        }

    status_name = _extract_status_name(getattr(node, "status", None))
    local_players = len(getattr(node, "players", None) or {})

    stats = cached_stats or {}
    total_players = stats.get("players")
    if total_players is None:
        total_players = getattr(node, "_total_player_count", None)
    if total_players is not None:
        try:
            total_players = max(0, int(total_players))
        except (TypeError, ValueError):
            total_players = None

    effective_players = max(local_players, total_players) if total_players is not None else local_players

    lavalink_load = _to_non_negative_float(stats.get("lavalink_load"))
    system_load = _to_non_negative_float(stats.get("system_load"))
    cpu_cores = stats.get("cpu_cores")

    mem_used = _to_non_negative_float(stats.get("memory_used"))
    mem_alloc = _to_non_negative_float(stats.get("memory_allocated"))
    mem_res = _to_non_negative_float(stats.get("memory_reservable"))
    mem_free = _to_non_negative_float(stats.get("memory_free"))

    state: dict[str, Any] = {
        "identifier": identifier,
        "uri": getattr(node, "uri", None) or configured_uri or "未知網址",
        "status": status_name,
        "local_players": local_players,
        "total_players": total_players,
        "players": effective_players,
        "lavalink_load": lavalink_load,
        "system_load": system_load,
        "cpu_cores": cpu_cores,
        "memory_used": mem_used,
        "memory_allocated": mem_alloc,
        "memory_reservable": mem_res,
        "memory_free": mem_free,
        "latency_ms": latency_ms,
        "session_id": getattr(node, "session_id", None),
    }
    if lavalink_load is not None and system_load is not None:
        state["cpu_load"] = 0.7 * lavalink_load + 0.3 * system_load
    elif lavalink_load is not None:
        state["cpu_load"] = lavalink_load
    elif system_load is not None:
        state["cpu_load"] = system_load
    else:
        state["cpu_load"] = None

    mem_cap = mem_res or mem_alloc
    if mem_used is not None and mem_cap is not None and mem_cap > 0:
        state["memory_ratio"] = min(1.0, max(0.0, mem_used / mem_cap))
    else:
        state["memory_ratio"] = None

    return state


def stats_payload_to_dict(stats_payload: Any) -> dict[str, Any]:
    """將 wavelink StatsEventPayload / StatsResponsePayload 轉成輕量快取字典。"""
    if stats_payload is None:
        return {}
    if isinstance(stats_payload, Mapping):
        return dict(stats_payload)

    cpu = getattr(stats_payload, "cpu", None)
    mem = getattr(stats_payload, "memory", None)
    return {
        "players": getattr(stats_payload, "players", None),
        "playing": getattr(stats_payload, "playing", None),
        "uptime": getattr(stats_payload, "uptime", None),
        "cpu_cores": getattr(cpu, "cores", None) if cpu is not None else None,
        "system_load": getattr(cpu, "system_load", None) if cpu is not None else None,
        "lavalink_load": getattr(cpu, "lavalink_load", None) if cpu is not None else None,
        "memory_used": getattr(mem, "used", None) if mem is not None else None,
        "memory_allocated": getattr(mem, "allocated", None) if mem is not None else None,
        "memory_reservable": getattr(mem, "reservable", None) if mem is not None else None,
        "memory_free": getattr(mem, "free", None) if mem is not None else None,
    }


def compute_reconnect_backoff(
    attempt: int,
    *,
    base: float = 5.0,
    cap: float = 60.0,
) -> float:
    """計算指數退避重連等待秒數（上限預設 60 秒）。

    attempt=0 -> 5s, attempt=1 -> 10s, attempt=2 -> 20s, attempt=3 -> 40s, attempt>=4 -> 60s。
    """
    safe_attempt = max(0, int(attempt))
    safe_base = max(1.0, float(base))
    safe_cap = max(safe_base, float(cap))
    return min(safe_cap, safe_base * (2 ** min(safe_attempt, 10)))
