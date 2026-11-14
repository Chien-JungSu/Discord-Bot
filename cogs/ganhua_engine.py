from __future__ import annotations

import json
import math
import os
import random
import re
from collections import Counter
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    from sklearn.feature_extraction.text import TfidfVectorizer  # type: ignore[import-not-found]
    from sklearn.metrics.pairwise import cosine_similarity  # type: ignore[import-not-found]

    _HAS_SKLEARN = True
except ImportError:  # pragma: no cover - 極簡環境未安裝 scikit-learn 時自動啟用純 Python 備援
    TfidfVectorizer = None  # type: ignore[assignment]
    cosine_similarity = None  # type: ignore[assignment]
    _HAS_SKLEARN = False

COGS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(COGS_DIR)

# 過濾 Discord 使用者 (<@123> / <@!123>)、身分組 (<@&123>) 與頻道 (<#123> / <!#123>) Mention 標籤
MENTION_REGEX = re.compile(r"<(?:@!?|@&|!?#)\d+>")
WHITESPACE_REGEX = re.compile(r"\s+")

EMPTY_INPUT_REPLY = "你什麼都沒講是要我吐槽空氣嗎？請至少打幾個字吧！"

# 萬用萬金油幹話保底池（當 JSON 未提供或清空 fallback_memes 時的預設保底清單）
FALLBACK_MEMES: List[str] = [
    "我的建議是不要聽別人的建議，包括我現在說的這句。",
    "嗯嗯，對對，你說得都對，好棒棒，下次別再說了。",
    "世上無難事，只要肯放棄，馬上就沒事。",
    "關於這個問題，我建議先點一份大辣鹹酥雞冷靜思考，想不出來至少吃飽了。",
    "聽君一席話，如聽一席話，廢話含金量高達百分之百。",
    "沒關係，明天太陽一樣會從東邊升起，不會因為你的煩惱而改道。",
    "你的想法很大膽，但我的大腦記憶體只有 130 MiB，暫時無法解析這波操作。",
    "這件事告訴我們一個道理：凡事只要躺平，就沒有人能絆倒你。",
    "先別急著難過，至少今天的你比明天的你還要年輕一天。",
    "這題太深奧了，建議擲筊問神明，神明笑杯就當作沒發生過。",
    "你說得很有道理，所以我決定把這段話當成今天的背景音樂左耳進右耳出。",
    "遇事不決，量子力學；量子力學救不了你，那就去睡覺。",
    "別問了，問就是「都行、都可以、隨便、看你」。",
    "只要你尷尬的速度夠快，別人就來不及替你感到尷尬。",
    "我掐指一算，你現在最缺的不是答案，是一杯大冰奶幫你順暢一下思緒。",
]


def clean_input_text(raw_text: Optional[str]) -> str:
    """清理輸入文字：移除 Discord Mention 標籤與多餘空白。"""
    if not raw_text:
        return ""
    without_mentions = MENTION_REGEX.sub(" ", raw_text)
    return WHITESPACE_REGEX.sub(" ", without_mentions).strip()


def _resolve_data_path(data_path: str) -> str:
    """依序從目前目錄、專案根目錄與 cogs 目錄解析語料 JSON 路徑。"""
    if not data_path or os.path.isabs(data_path) or os.path.exists(data_path):
        return data_path

    rel_path = data_path[2:] if data_path.startswith(("./", ".\\")) else data_path
    for base_dir in (PROJECT_ROOT, COGS_DIR):
        candidate = os.path.normpath(os.path.join(base_dir, rel_path))
        if os.path.exists(candidate):
            return candidate
    return data_path


class _PurePythonCharTfidf:
    """純 Python 字元級 (1, 2)-gram TF-IDF 備援實作（零外部套件依賴）。"""

    def __init__(self, ngram_range: Tuple[int, int] = (1, 2)) -> None:
        self.min_n, self.max_n = ngram_range
        self.idf: Dict[str, float] = {}
        self.doc_vectors: List[Dict[str, float]] = []

    def _extract_ngrams(self, text: str) -> List[str]:
        normalized = text.lower()
        length = len(normalized)
        tokens: List[str] = []
        for n in range(self.min_n, self.max_n + 1):
            if length < n:
                continue
            for idx in range(length - n + 1):
                tokens.append(normalized[idx : idx + n])
        return tokens

    def _to_normalized_tfidf(self, tokens: Sequence[str]) -> Dict[str, float]:
        if not tokens:
            return {}
        counts = Counter(tokens)
        vec: Dict[str, float] = {}
        norm_sq = 0.0
        for term, tf in counts.items():
            idf_val = self.idf.get(term)
            if idf_val is None:
                continue
            weight = float(tf) * idf_val
            vec[term] = weight
            norm_sq += weight * weight
        if norm_sq <= 0.0:
            return {}
        inv_norm = 1.0 / math.sqrt(norm_sq)
        for term in vec:
            vec[term] *= inv_norm
        return vec

    def fit(self, corpus: Sequence[str]) -> None:
        doc_tokens = [self._extract_ngrams(doc) for doc in corpus]
        num_docs = len(doc_tokens)
        df: Counter[str] = Counter()
        for tokens in doc_tokens:
            for term in set(tokens):
                df[term] += 1

        self.idf = {
            term: math.log((1.0 + num_docs) / (1.0 + freq)) + 1.0
            for term, freq in df.items()
        }
        self.doc_vectors = [self._to_normalized_tfidf(tokens) for tokens in doc_tokens]

    def similarities(self, query: str) -> List[float]:
        query_vec = self._to_normalized_tfidf(self._extract_ngrams(query))
        if not query_vec:
            return [0.0] * len(self.doc_vectors)
        scores: List[float] = []
        for doc_vec in self.doc_vectors:
            dot = sum(weight * doc_vec.get(term, 0.0) for term, weight in query_vec.items())
            scores.append(dot)
        return scores


class LightweightGanhuaEngine:
    """極致輕量、零 AI 模型依賴的台灣幹話檢索引擎（TF-IDF + 萬用幹話保底池）。"""

    def __init__(self, data_path: str = "memes_data.json") -> None:
        self.data_path: str = data_path
        self.resolved_data_path: str = _resolve_data_path(data_path)
        self.categorized_memes: List[Tuple[List[str], List[str]]] = []
        self.fallback_memes: List[str] = list(FALLBACK_MEMES)
        self.corpus_keys: List[str] = []
        self.vectorizer: Optional[Any] = None
        self.tfidf_matrix: Optional[Any] = None
        self._fallback_tfidf: Optional[_PurePythonCharTfidf] = None

        self.reload()

    def reload(self) -> int:
        """重新讀取 JSON 語料庫並重建字元級 TF-IDF 矩陣，回傳載入的主題組數。"""
        self.resolved_data_path = _resolve_data_path(self.data_path)
        with open(self.resolved_data_path, "r", encoding="utf-8") as fp:
            payload: Dict[str, Any] = json.load(fp)

        raw_categories = payload.get("categorized_memes", [])
        parsed_categories: List[Tuple[List[str], List[str]]] = []
        for item in raw_categories:
            if not isinstance(item, dict):
                continue
            keywords = [str(kw).strip() for kw in item.get("keywords", []) if str(kw).strip()]
            replies = [str(rep).strip() for rep in item.get("replies", []) if str(rep).strip()]
            if keywords and replies:
                parsed_categories.append((keywords, replies))

        raw_fallbacks = payload.get("fallback_memes", [])
        parsed_fallbacks = [str(rep).strip() for rep in raw_fallbacks if str(rep).strip()]

        self.categorized_memes = parsed_categories
        self.fallback_memes = parsed_fallbacks or list(FALLBACK_MEMES)
        self.corpus_keys = [" ".join(keywords) for keywords, _ in self.categorized_memes]

        if not self.corpus_keys:
            self.vectorizer = None
            self.tfidf_matrix = None
            self._fallback_tfidf = None
            return 0

        if _HAS_SKLEARN and TfidfVectorizer is not None:
            self.vectorizer = TfidfVectorizer(analyzer="char", ngram_range=(1, 2))
            self.tfidf_matrix = self.vectorizer.fit_transform(self.corpus_keys)
            self._fallback_tfidf = None
        else:
            fallback_tfidf = _PurePythonCharTfidf(ngram_range=(1, 2))
            fallback_tfidf.fit(self.corpus_keys)
            self._fallback_tfidf = fallback_tfidf
            self.vectorizer = None
            self.tfidf_matrix = None

        return len(self.categorized_memes)

    def _compute_similarities(self, cleaned_input: str) -> List[float]:
        """計算輸入字串與所有主題關鍵字的字元級餘弦相似度。"""
        if not self.corpus_keys:
            return []

        if _HAS_SKLEARN and self.vectorizer is not None and self.tfidf_matrix is not None and cosine_similarity is not None:
            query_vec = self.vectorizer.transform([cleaned_input])
            sims = cosine_similarity(query_vec, self.tfidf_matrix).flatten()
            return [float(score) for score in sims]

        if self._fallback_tfidf is not None:
            return self._fallback_tfidf.similarities(cleaned_input)

        return []

    def reply(self, user_input: str, threshold: float = 0.30) -> str:
        """清理輸入後進行語意檢索；達門檻回傳對應主題幹話，否則由保底池隨機抽句。"""
        cleaned = clean_input_text(user_input)
        if not cleaned:
            return EMPTY_INPUT_REPLY

        similarities = self._compute_similarities(cleaned)
        if similarities:
            best_idx = max(range(len(similarities)), key=lambda idx: similarities[idx])
            if similarities[best_idx] >= threshold:
                candidates = self.categorized_memes[best_idx][1]
                return random.choice(candidates)

        return random.choice(self.fallback_memes)

    def random_reply(self) -> str:
        """從所有分類幹話與萬用保底池中隨機抽取一句幹話。"""
        pool: List[str] = []
        seen: set[str] = set()
        for _, replies in self.categorized_memes:
            for item in replies:
                if item not in seen:
                    seen.add(item)
                    pool.append(item)
        for item in self.fallback_memes:
            if item not in seen:
                seen.add(item)
                pool.append(item)

        if not pool:
            return random.choice(FALLBACK_MEMES)
        return random.choice(pool)


# 保留相容別名，避免舊有匯入中斷
GanhuaBotEngine = LightweightGanhuaEngine
