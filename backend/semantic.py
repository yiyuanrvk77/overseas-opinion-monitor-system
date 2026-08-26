"""Multi-provider embedding engine for overseas-opinion monitoring.

Supports DashScope (Qwen), Zhipu, OpenAI and a deterministic offline feature
vector fallback so the system still works without any API key. Vectors are
stored as float32 BLOBs in SQLite (see knowledge_chunk.vector / dimensions).
"""
from __future__ import annotations

import hashlib
import logging
from typing import Iterable, Sequence

import requests

from .features import DIMENSIONS, cosine, feature_vector


LOGGER = logging.getLogger(__name__)


MODELS = {
    "qwen": {
        "name": "通义千问",
        "provider": "dashscope",
        "url": "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding",
        "model": "text-embedding-v3",
        "dimension": 1024,
        "env_key": "DASHSCOPE_API_KEY",
        "auth": "bearer",
    },
    "zhipu": {
        "name": "智谱AI",
        "provider": "zhipu",
        "url": "https://open.bigmodel.cn/api/paas/v4/embeddings",
        "model": "embedding-3",
        "dimension": 2048,
        "env_key": "ZHIPU_API_KEY",
        "auth": "bearer",
    },
    "openai": {
        "name": "OpenAI",
        "provider": "openai",
        "url": "https://api.openai.com/v1/embeddings",
        "model": "text-embedding-3-small",
        "dimension": 1536,
        "env_key": "OPENAI_API_KEY",
        "auth": "bearer",
    },
    "local": {
        "name": "本地离线向量",
        "provider": "local",
        "url": "",
        "model": "offline_feature_vector",
        "dimension": DIMENSIONS,
        "env_key": "",
        "auth": "none",
    },
}


# Maps a stored vector dimension back to the model that produced it.
DIMENSION_TO_MODEL = {model["dimension"]: key for key, model in MODELS.items()}


class SemanticEngine:
    """Resolve embeddings for one or more texts using the configured model."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], tuple[float, ...]] = {}

    @staticmethod
    def _api_key(model: str) -> str:
        spec = MODELS.get(model, MODELS["local"])
        return (spec.get("env_key") or "").strip() and os_getenv(spec["env_key"]).strip()

    @staticmethod
    def supported_models() -> list[dict]:
        result = []
        for key, spec in MODELS.items():
            entry = {"id": key}
            for field, value in spec.items():
                if field not in {"url", "env_key"}:
                    entry[field] = value
            result.append(entry)
        return result

    @staticmethod
    def model_for_dimension(dimension: int) -> str:
        return DIMENSION_TO_MODEL.get(int(dimension), "local")

    def local_embedding(self, text: str, dimension: int = DIMENSIONS) -> list[float]:
        vector = feature_vector(text, dimension)
        return [value / (sum(v * v for v in vector) ** 0.5 or 1.0) for value in vector]

    def _embed_one(self, text: str, model: str) -> list[float] | None:
        key = (model, text)
        if key in self._cache:
            return list(self._cache[key])
        value = self._embed_one_uncached(text, model)
        if value is not None:
            self._cache[key] = tuple(value)
        return value

    def _embed_one_uncached(self, text: str, model: str) -> list[float] | None:
        spec = MODELS.get(model) or MODELS["local"]
        if spec["provider"] == "local":
            return self.local_embedding(text, spec["dimension"])
        api_key = self._api_key(model)
        if not api_key:
            return None
        try:
            if spec["provider"] == "dashscope":
                payload = {"model": spec["model"], "input": {"texts": [text]}, "dimension": spec["dimension"]}
                response = requests.post(spec["url"], json=payload, headers={"Authorization": f"Bearer {api_key}"}, timeout=20)
                response.raise_for_status()
                embeddings = response.json()["output"]["embeddings"]
                values = embeddings[0]["embedding"]
            elif spec["provider"] == "zhipu":
                payload = {"model": spec["model"], "input": text}
                response = requests.post(spec["url"], json=payload, headers={"Authorization": f"Bearer {api_key}"}, timeout=20)
                response.raise_for_status()
                values = response.json()["data"][0]["embedding"]
            else:  # openai
                payload = {"model": spec["model"], "input": [text]}
                response = requests.post(spec["url"], json=payload, headers={"Authorization": f"Bearer {api_key}"}, timeout=20)
                response.raise_for_status()
                values = response.json()["data"][0]["embedding"]
            vector = [float(value) for value in values]
            if len(vector) != spec["dimension"]:
                raise ValueError(f"维度不匹配: {len(vector)} != {spec['dimension']}")
            norm = math_sqrt(sum(value * value for value in vector))
            return [value / norm for value in vector] if norm else vector
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            LOGGER.warning("embedding provider %s failed: %s", model, exc)
            return None

    def resolve_embeddings(
        self,
        texts: Iterable[str],
        model: str = "qwen",
        *,
        text_type: str = "document",
    ) -> tuple[list[list[float]] | None, dict]:
        model = model if model in MODELS else "local"
        spec = MODELS[model]
        vectors: list[list[float]] = []
        remote = spec["provider"] != "local"
        active = bool(remote and self._api_key(model))
        if not active:
            # Degrade every text to the offline vector so dimensions stay consistent.
            for text in texts:
                vectors.append(self.local_embedding(text, spec["dimension"]))
            active = False
        else:
            for text in texts:
                value = self._embed_one(text, model)
                if value is None:
                    # Degrade the whole batch if one call fails.
                    return None, {
                        "model": spec["model"],
                        "provider": spec["provider"],
                        "dimension": spec["dimension"],
                        "embedding_active": False,
                        "degraded": True,
                    }
                vectors.append(value)
        return vectors, {
            "model": spec["model"],
            "provider": spec["provider"],
            "dimension": spec["dimension"],
            "embedding_active": active,
            "degraded": not active,
        }

    @staticmethod
    def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
        return cosine(list(left), list(right))

    def embed_query(self, text: str, dimension: int) -> tuple[list[float] | None, bool, str, str]:
        """Return (vector, active, model_key, model_name) for a target dimension."""
        dimension = int(dimension)
        model = self.model_for_dimension(dimension)
        spec = MODELS.get(model, MODELS["local"])
        if spec["provider"] == "local":
            return self.local_embedding(text, dimension), True, "local", spec["model"]
        if not self._api_key(model):
            return None, False, "-", spec["model"]
        value = self._embed_one(text, model)
        if value is None or len(value) != dimension:
            return None, False, "-", spec["model"]
        return value, True, model, spec["model"]


def math_sqrt(value: float) -> float:
    return value ** 0.5


def os_getenv(key: str) -> str:
    import os

    return os.environ.get(key, "")


semantic_engine = SemanticEngine()
