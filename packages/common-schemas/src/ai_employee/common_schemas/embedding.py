"""Embedding providers shared across services.

Lives in common-schemas so both knowledge-api (query-side embedding) and
ingestion-worker (chunk-side embedding) use the identical provider and produce
dimensionally-compatible vectors. This avoids the query/chunk dim-mismatch that
would otherwise make cosine recall silently return zero.
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import Protocol

_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode"
_QWEN_DEFAULT_MODEL = "text-embedding-v3"
_QWEN_DEFAULT_DIM = 1024
_QWEN_MAX_BATCH = 10
_OPENAI_COMPAT_DEFAULT_DIM = 1024


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


class EmbeddingUnavailableError(RuntimeError):
    """远程 embedding provider 调用失败且重试用尽（检索降级候选）。

    cause 取值：
      - "network":  连接错误（DNS / refused / etc.）
      - "timeout":  请求超时
      - "4xx":       客户端错误（401/403/400），不可重试
      - "5xx":       服务端错误，重试耗尽
      - "dim_mismatch":  返回维度与构造 dim 不一致
    """

    def __init__(self, message: str, cause: str = "provider_error") -> None:
        super().__init__(message)
        self.cause = cause


class _RemoteEmbeddingMixin:
    """共享：批量分批 + 瞬时错误重试 + OpenAI-compatible 响应解析。"""

    def _embed_batches(
        self,
        texts: list[str],
        post_fn,
        url: str,
        headers: dict,
        model: str,
        max_batch: int,
        max_retries: int,
        timeout: float,
    ) -> list[list[float]]:
        if not texts:
            return []
        results: list[list[float]] = []
        for start in range(0, len(texts), max_batch):
            batch = texts[start : start + max_batch]
            vectors = self._post_with_retry(
                post_fn, url, headers, model, batch, max_retries, timeout
            )
            results.extend(vectors)
        return results

    def _post_with_retry(
        self, post_fn, url, headers, model, batch, max_retries, timeout
    ) -> list[list[float]]:
        import httpx

        last_status: int | None = None
        last_text: str = ""
        for attempt in range(max_retries + 1):
            try:
                resp = post_fn(
                    url,
                    headers=headers,
                    json={"model": model, "input": batch},
                    timeout=timeout,
                )
            except httpx.TimeoutException as exc:
                if attempt < max_retries:
                    time.sleep(0.2 * (2**attempt))
                    continue
                raise EmbeddingUnavailableError(
                    f"embedding api timeout: {exc}", cause="timeout"
                ) from exc
            except httpx.HTTPError as exc:
                raise EmbeddingUnavailableError(
                    f"embedding api network error: {exc}", cause="network"
                ) from exc
            if resp.status_code == 200:
                data = resp.json()
                return [item["embedding"] for item in data["data"]]
            last_status = resp.status_code
            last_text = getattr(resp, "text", "")
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < max_retries:
                    time.sleep(0.2 * (2**attempt))
                    continue
                # 重试耗尽
                raise EmbeddingUnavailableError(
                    f"embedding api returned {last_status}: {last_text[:200]}",
                    cause="5xx" if resp.status_code >= 500 else "5xx",
                )
            # 4xx（除 429）不可重试
            raise EmbeddingUnavailableError(
                f"embedding api returned {last_status}: {last_text[:200]}",
                cause="4xx",
            )
        # 不可达：网络错误兜底
        raise EmbeddingUnavailableError(
            f"embedding api unavailable (last status {last_status})",
            cause="network",
        )


class OpenAICompatEmbeddingProvider(_RemoteEmbeddingMixin):
    """OpenAI-compatible 远程 embedding：POST {base_url}/v1/embeddings。

    启动时 dim 由构造参数给定（默认 1024）；首次响应若返回不同维度则更新。
    """

    name = "openai_compat"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        dim: int = _OPENAI_COMPAT_DEFAULT_DIM,
        max_batch: int = 100,
        max_retries: int = 2,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._dim = dim
        self.max_batch = max_batch
        self.max_retries = max_retries
        self.timeout = timeout

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx

        url = f"{self.base_url}/v1/embeddings"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            vectors = self._embed_batches(
                texts,
                httpx.post,
                url,
                headers,
                self.model,
                self.max_batch,
                self.max_retries,
                self.timeout,
            )
        except EmbeddingUnavailableError:
            raise
        if self._dim is None and vectors:  # type: ignore[comparison-overlap]
            self._dim = len(vectors[0])
        return vectors


class QwenEmbeddingProvider(_RemoteEmbeddingMixin):
    """阿里云 DashScope（通义千问）OpenAI-compatible embedding。

    使用环境变量 QWEN_API_KEY 鉴权，默认 text-embedding-v3 / 1024 维。
    DashScope v3/v4 每请求最多 10 条输入，本 provider 内部自动分批。
    """

    name = "qwen"

    def __init__(
        self,
        api_key: str,
        model: str = _QWEN_DEFAULT_MODEL,
        dim: int = _QWEN_DEFAULT_DIM,
        base_url: str = _DASHSCOPE_BASE_URL,
        max_batch: int = _QWEN_MAX_BATCH,
        max_retries: int = 2,
        timeout: float = 10.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self._dim = dim
        self.base_url = base_url.rstrip("/")
        self.max_batch = max_batch
        self.max_retries = max_retries
        self.timeout = timeout

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx

        url = f"{self.base_url}/v1/embeddings"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        return self._embed_batches(
            texts,
            httpx.post,
            url,
            headers,
            self.model,
            self.max_batch,
            self.max_retries,
            self.timeout,
        )


def build_provider(
    provider_name: str | None = None,
) -> tuple[EmbeddingProvider, bool]:
    """根据环境构建 embedding provider。

    返回 (provider, degraded)：degraded=True 表示期望的远程 provider 因配置
    缺失而降级到 stub（供 /health 上报）。

    支持的 provider_name：
      - stub（默认）：零依赖确定性伪向量
      - qwen：DashScope OpenAI-compatible，读 QWEN_API_KEY；缺 key 降级 stub
      - openai_compat：通用 OpenAI-compatible，读 EMBEDDING_BASE_URL/API_KEY/MODEL
    """
    name = provider_name or os.getenv("EMBEDDING_PROVIDER", "stub")
    stub_dim = int(os.getenv("EMBEDDING_DIM", "8"))

    if name == "qwen":
        api_key = os.getenv("QWEN_API_KEY", "")
        if not api_key:
            return StubEmbeddingProvider(dim=stub_dim), True
        model = os.getenv("EMBEDDING_MODEL", _QWEN_DEFAULT_MODEL)
        dim = int(os.getenv("EMBEDDING_DIM", str(_QWEN_DEFAULT_DIM)))
        return QwenEmbeddingProvider(api_key=api_key, model=model, dim=dim), False

    if name == "openai_compat":
        base_url = os.getenv("EMBEDDING_BASE_URL", "")
        api_key = os.getenv("EMBEDDING_API_KEY", "")
        model = os.getenv("EMBEDDING_MODEL", "")
        if not (base_url and api_key and model):
            return StubEmbeddingProvider(dim=stub_dim), True
        dim = int(os.getenv("EMBEDDING_DIM", str(_OPENAI_COMPAT_DEFAULT_DIM)))
        return OpenAICompatEmbeddingProvider(base_url, api_key, model, dim=dim), False

    return StubEmbeddingProvider(dim=stub_dim), False


__all__ = [
    "EmbeddingProvider",
    "EmbeddingUnavailableError",
    "StubEmbeddingProvider",
    "OpenAICompatEmbeddingProvider",
    "QwenEmbeddingProvider",
    "build_provider",
]
