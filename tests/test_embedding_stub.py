from ai_employee.ingestion_worker.embedding import (
    EmbeddingProvider,
    StubEmbeddingProvider,
)


def test_stub_provider_name_and_dim() -> None:
    provider = StubEmbeddingProvider(dim=8)
    assert provider.name == "stub"
    assert provider.dim == 8


def test_stub_provider_is_deterministic() -> None:
    provider = StubEmbeddingProvider(dim=8)
    a = provider.embed(["RRC 建立失败", "传输误码"])
    b = provider.embed(["RRC 建立失败", "传输误码"])
    assert a == b
    assert len(a) == 2
    assert all(len(vec) == 8 for vec in a)


def test_stub_provider_different_texts_different_vectors() -> None:
    provider = StubEmbeddingProvider(dim=8)
    vectors = provider.embed(["RRC 建立失败", "传输误码"])
    assert vectors[0] != vectors[1]


def test_stub_provider_empty_input_returns_empty() -> None:
    provider = StubEmbeddingProvider(dim=8)
    assert provider.embed([]) == []


def test_stub_provider_vector_components_in_range() -> None:
    provider = StubEmbeddingProvider(dim=8)
    vectors = provider.embed(["任意文本"])
    for value in vectors[0]:
        assert -1.0 <= value <= 1.0


def test_stub_provider_satisfies_protocol() -> None:
    provider: EmbeddingProvider = StubEmbeddingProvider(dim=8)
    assert provider.dim == 8
