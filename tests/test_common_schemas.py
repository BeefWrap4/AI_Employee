from ai_employee.common_schemas.knowledge import (
    ChunkRecord,
    DocumentStatus,
    ParsedChunk,
    ParseRequest,
    ParseResponse,
)


def test_document_status_has_six_states() -> None:
    statuses = {s.value for s in DocumentStatus}
    assert statuses == {
        "uploaded",
        "parsing",
        "parse_failed",
        "ready",
        "published",
        "archived",
    }


def test_parsed_chunk_defaults() -> None:
    chunk = ParsedChunk(
        chunk_id="chunk_doc_001_001",
        chunk_no=1,
        content="RRC 建立失败时先检查告警。",
        section_path="接入侧",
    )
    assert chunk.page_no == 1
    assert chunk.embedding is None


def test_parse_request_serialization() -> None:
    req = ParseRequest(
        doc_id="doc_001",
        file_path="/tmp/doc_001.md",
        mime_type="text/markdown",
        metadata={"network_type": "5g"},
    )
    dumped = req.model_dump()
    assert dumped["doc_id"] == "doc_001"
    assert dumped["metadata"]["network_type"] == "5g"


def test_parse_response_includes_embeddings_and_model() -> None:
    resp = ParseResponse(
        doc_id="doc_001",
        chunks=[
            ParsedChunk(
                chunk_id="chunk_doc_001_001",
                chunk_no=1,
                content="x",
                section_path="root",
            )
        ],
        embeddings=[[0.1, 0.2]],
        embedding_model="stub",
    )
    assert resp.embedding_model == "stub"
    assert len(resp.embeddings) == len(resp.chunks)


def test_chunk_record_stores_embedding() -> None:
    rec = ChunkRecord(
        chunk_id="c1",
        doc_id="doc_001",
        chunk_no=1,
        content="x",
        section_path="root",
        embedding=[0.1, 0.2],
        embedding_model="stub",
    )
    assert rec.embedding == [0.1, 0.2]
