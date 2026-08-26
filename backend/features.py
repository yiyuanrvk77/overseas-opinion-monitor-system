from __future__ import annotations

import hashlib
import math
import re
import struct
from collections import Counter


DIMENSIONS = 256


def tokenize(text: str) -> list[str]:
    value = (text or "").lower()
    latin = re.findall(r"[a-z0-9_@.-]+", value)
    chinese = re.findall(r"[\u4e00-\u9fff]", value)
    bigrams = ["".join(chinese[index:index + 2]) for index in range(len(chinese) - 1)]
    return latin + chinese + bigrams


def feature_vector(text: str, dimensions: int = DIMENSIONS) -> list[float]:
    counts = Counter(tokenize(text))
    vector = [0.0] * dimensions
    for token, count in counts.items():
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        raw = int.from_bytes(digest, "little")
        index = raw % dimensions
        sign = -1.0 if raw & 1 else 1.0
        vector[index] += sign * (1.0 + math.log(count))
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


def pack_vector(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack_vector(blob: bytes, dimensions: int) -> list[float]:
    return list(struct.unpack(f"<{dimensions}f", blob))


def cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))
