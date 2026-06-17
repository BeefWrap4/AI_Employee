from __future__ import annotations

import hashlib
from typing import Protocol


class EmbeddingProvider(Protocol):
    """embedding 提供方抽象。"""

    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class StubEmbeddingProvider:
    """零依赖伪 embedding：基于文本 hash 映射到 [-1, 1] 区间的固定维度向量。

    确定性：同一文本恒定产生同一向量，便于测试与离线回归。
    """

    name = "stub"

    def __init__(self, dim: int = 8) -> None:
        if dim <= 0:
            raise ValueError("dim must be positive")
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        results: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            values: list[float] = []
            for i in range(self.dim):
                lo = digest[(i * 2) % len(digest)]
                hi = digest[(i * 2 + 1) % len(digest)]
                raw = (lo << 8) | hi
                values.append((raw / 32768.0) - 1.0)
            results.append(values)
        return results


class OpenAICompatEmbeddingProvider:
    """OpenAI-compatible 远程 embedding：POST {base_url}/v1/embeddings。

    启动时探测不到 dim 时用首次响应推断。M1 仅做最小可用实现，
    失败由调用方捕获并降级到 stub。
    """

    name = "openai_compat"

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._dim: int | None = None

    @property
    def dim(self) -> int:
        if self._dim is None:
            raise RuntimeError("dim not probed; call embed first")
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx

        if not texts:
            return []
        resp = httpx.post(
            f"{self.base_url}/v1/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "input": texts},
            timeout=10.0,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"embedding api returned {resp.status_code}")
        data = resp.json()
        vectors = [item["embedding"] for item in data["data"]]
        if self._dim is None and vectors:
            self._dim = len(vectors[0])
        return vectors
