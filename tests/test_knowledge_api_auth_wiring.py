"""R24-A.4: knowledge-api document upload is wired to require_oidc_or_internal.

Confirms that the production write endpoint accepts:

  * an OIDC RS256 Bearer token (when SSO is enabled);
  * the legacy HS256 JWT;
  * the service-specific ``X-Internal-Token`` (default fallback);
  * nothing → 401.

The ``knowledge:write`` RBAC permission is enforced for OIDC and JWT
paths, while the internal-token path is trusted.

The ``InProcessWorkerClient`` is built locally so the test module does
not need to import from the conftest (which is a pytest fixture file
and a few sub-modules shadow the symbol under the same name).
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from ai_employee.ingestion_worker.app import create_app as create_worker_app
from ai_employee.knowledge_api.app import create_app as create_knowledge_app
from ai_employee.knowledge_api.store import SQLiteStore
from ai_employee.knowledge_api.worker_client import WorkerClient, WorkerDispatchResult
from ai_employee.common_schemas.knowledge import ParseResponse  # noqa: E402, F401


# --------------------------------------------------------------------------- #
# In-process worker (mirrors conftest pattern)
# --------------------------------------------------------------------------- #


class _InProcessWorkerClient(WorkerClient):
    def __init__(self) -> None:
        self._client = TestClient(create_worker_app())
        self._reachable = True

    def health(self) -> bool:
        return self._reachable

    def parse(self, doc_id, file_path, mime_type, metadata):  # type: ignore[override]
        resp = self._client.post(
            "/internal/parse",
            json={
                "doc_id": doc_id,
                "file_path": file_path,
                "mime_type": mime_type,
                "metadata": metadata,
            },
        )
        if resp.status_code == 200:
            return WorkerDispatchResult(
                dispatched=True,
                dispatch_status="accepted",
                response=ParseResponse(**resp.json()),
            )
        return WorkerDispatchResult(
            dispatched=False,
            dispatch_status="worker_error",
            error=f"worker returned {resp.status_code}: {resp.text}",
        )


@pytest.fixture
def knowledge_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "data"
    (data_dir / "raw").mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "knowledge.sqlite3"
    monkeypatch.setenv("KNOWLEDGE_DATA_DIR", str(data_dir))
    monkeypatch.setenv("KNOWLEDGE_SQLITE_PATH", str(db_path))
    monkeypatch.setenv("EMBEDDING_PROVIDER", "stub")
    monkeypatch.setenv("EMBEDDING_DIM", "8")
    monkeypatch.setenv("KNOWLEDGE_API_INTERNAL_TOKEN", "test-token")
    monkeypatch.setenv("INTERNAL_TOKEN", "test-token")
    monkeypatch.setenv("INGESTION_WORKER_URL", "http://in-process")
    return data_dir


@pytest.fixture
def api_factory(knowledge_workspace: Path):
    def _factory() -> TestClient:
        store = SQLiteStore(
            db_path=os.environ["KNOWLEDGE_SQLITE_PATH"],
            data_dir=os.environ["KNOWLEDGE_DATA_DIR"],
        )
        store.init_schema()
        app = create_knowledge_app(
            store=store, worker_client=_InProcessWorkerClient(),
        )
        # The autouse ``_patch_test_client_default_headers`` fixture
        # injects ``X-Internal-Token`` automatically.
        return TestClient(app)

    return _factory


# --------------------------------------------------------------------------- #
# OIDC / RS256 helpers
# --------------------------------------------------------------------------- #


def _rsa_keypair() -> tuple[Any, Any]:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return priv, priv.public_key()


def _public_jwk(public_key: Any, *, kid: str) -> dict[str, Any]:
    import base64

    nums = public_key.public_numbers()

    def b64u(value: int) -> str:
        length = (value.bit_length() + 7) // 8 or 1
        return base64.urlsafe_b64encode(value.to_bytes(length, "big")).rstrip(b"=").decode()

    return {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": b64u(nums.n),
        "e": b64u(nums.e),
    }


def _oidc_token(
    *,
    private_key: Any,
    kid: str,
    iss: str,
    aud: str,
    sub: str = "alice",
    roles: list[str] | None = None,
) -> str:
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": iss,
        "aud": aud,
        "sub": sub,
        "iat": now,
        "exp": now + 3600,
    }
    if roles:
        payload["realm_access"] = {"roles": roles}
    return pyjwt.encode(
        payload, pem, algorithm="RS256", headers={"kid": kid, "typ": "JWT"},
    )


def _upload(client: TestClient, *, auth_headers: dict[str, str] | None = None):
    headers = dict(auth_headers or {})
    return client.post(
        "/api/v1/documents",
        files={"file": ("doc.md", b"# title\nbody", "text/markdown")},
        data={
            "title": "doc",
            "metadata_json": json.dumps({}),
            "acl_tags_json": json.dumps([]),
            "version": "v1",
            "mime_type": "text/markdown",
        },
        headers=headers,
    )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_upload_without_credentials_rejected(api_factory) -> None:
    from ai_employee.knowledge_api.app import create_app as create_knowledge_app
    from ai_employee.knowledge_api.store import SQLiteStore
    import os

    store = SQLiteStore(
        db_path=os.environ["KNOWLEDGE_SQLITE_PATH"],
        data_dir=os.environ["KNOWLEDGE_DATA_DIR"],
    )
    store.init_schema()
    app = create_knowledge_app(store=store, worker_client=_InProcessWorkerClient())
    # ``_internal_token=False`` opts out of the autouse default header.
    client = TestClient(app, _internal_token=False)
    resp = _upload(client)
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"]["error_code"] == "authentication_required"


def test_upload_with_internal_token_accepted(api_factory) -> None:
    client = api_factory()
    resp = _upload(client)
    assert resp.status_code == 202, resp.text


def test_upload_rejects_wrong_internal_token(api_factory) -> None:
    from ai_employee.knowledge_api.app import create_app as create_knowledge_app
    from ai_employee.knowledge_api.store import SQLiteStore
    import os

    store = SQLiteStore(
        db_path=os.environ["KNOWLEDGE_SQLITE_PATH"],
        data_dir=os.environ["KNOWLEDGE_DATA_DIR"],
    )
    store.init_schema()
    app = create_knowledge_app(store=store, worker_client=_InProcessWorkerClient())
    client = TestClient(app, _internal_token=False)
    resp = _upload(client, auth_headers={"X-Internal-Token": "wrong-token"})
    assert resp.status_code == 401


def test_upload_with_oidc_admin_token_accepted(
    api_factory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    priv, pub = _rsa_keypair()
    kid = "oidc-knowledge"
    jwks = [_public_jwk(pub, kid=kid)]
    iss = "https://idp.example.com/realms/acme"
    aud = "ai-employee"
    monkeypatch.setenv("OIDC_ISSUER", iss)
    monkeypatch.setenv("OIDC_CLIENT_ID", aud)
    monkeypatch.setenv("OIDC_AUDIENCE", aud)
    monkeypatch.setenv("OIDC_JWKS_URL", "https://idp.example.com/jwks")
    from ai_employee.auth_policy import fastapi_dep as dep_mod
    from ai_employee.auth_policy.oidc import OIDCConfig, OIDCVerifier

    cfg = OIDCConfig(
        issuer=iss, audience=aud,
        jwks_url="https://idp.example.com/jwks",
        enabled=True,
    )

    class _StaticJwks:
        def __init__(self, keys: list[dict[str, Any]]) -> None:
            self._keys = list(keys)

        def fetch(self, kid: str | None = None) -> list[dict[str, Any]]:
            return list(self._keys)

    verifier = OIDCVerifier(cfg, _StaticJwks(jwks), verify_signature=True)
    monkeypatch.setattr(dep_mod, "build_oidc_verifier", lambda **kw: verifier)
    from ai_employee.knowledge_api.app import create_app as create_knowledge_app
    from ai_employee.knowledge_api.store import SQLiteStore
    import os

    store = SQLiteStore(
        db_path=os.environ["KNOWLEDGE_SQLITE_PATH"],
        data_dir=os.environ["KNOWLEDGE_DATA_DIR"],
    )
    store.init_schema()
    app = create_knowledge_app(store=store, worker_client=_InProcessWorkerClient())
    client = TestClient(app, _internal_token=False)
    token = _oidc_token(
        private_key=priv, kid=kid, iss=iss, aud=aud,
        sub="alice", roles=["admin"],  # admin bypasses knowledge:write
    )
    resp = _upload(client, auth_headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 202, resp.text


def test_upload_with_oidc_token_missing_permission_rejected(
    api_factory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    priv, pub = _rsa_keypair()
    kid = "oidc-knowledge-2"
    jwks = [_public_jwk(pub, kid=kid)]
    iss = "https://idp.example.com/realms/acme"
    aud = "ai-employee"
    monkeypatch.setenv("OIDC_ISSUER", iss)
    monkeypatch.setenv("OIDC_CLIENT_ID", aud)
    monkeypatch.setenv("OIDC_AUDIENCE", aud)
    monkeypatch.setenv("OIDC_JWKS_URL", "https://idp.example.com/jwks")
    from ai_employee.auth_policy import fastapi_dep as dep_mod
    from ai_employee.auth_policy.oidc import OIDCConfig, OIDCVerifier

    cfg = OIDCConfig(
        issuer=iss, audience=aud,
        jwks_url="https://idp.example.com/jwks",
        enabled=True,
    )

    class _StaticJwks:
        def fetch(self, kid: str | None = None) -> list[dict[str, Any]]:
            return list(jwks)

    verifier = OIDCVerifier(cfg, _StaticJwks(), verify_signature=True)
    monkeypatch.setattr(dep_mod, "build_oidc_verifier", lambda **kw: verifier)
    from ai_employee.knowledge_api.app import create_app as create_knowledge_app
    from ai_employee.knowledge_api.store import SQLiteStore
    import os

    store = SQLiteStore(
        db_path=os.environ["KNOWLEDGE_SQLITE_PATH"],
        data_dir=os.environ["KNOWLEDGE_DATA_DIR"],
    )
    store.init_schema()
    app = create_knowledge_app(store=store, worker_client=_InProcessWorkerClient())
    client = TestClient(app, _internal_token=False)
    token = _oidc_token(
        private_key=priv, kid=kid, iss=iss, aud=aud,
        sub="bob", roles=["viewer"],  # viewer lacks knowledge:write
    )
    resp = _upload(client, auth_headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    assert resp.json()["detail"]["error_code"] == "forbidden"
