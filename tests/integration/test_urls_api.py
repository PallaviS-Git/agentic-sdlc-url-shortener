"""
Integration tests for the URL shortener API.

Uses the `http_client` fixture from conftest.py which wires a full FastAPI
ASGI stack to an in-memory SQLite database. No Docker or external services
required. Tests exercise real routes → service → repository → SQLite.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient


pytestmark = pytest.mark.integration


# ─── POST /shorten ────────────────────────────────────────────────────────────


class TestShortenEndpoint:
    async def test_returns_201_with_short_url(self, http_client: AsyncClient) -> None:
        response = await http_client.post(
            "/shorten", json={"url": "https://example.com/some/long/path"}
        )
        assert response.status_code == 201
        body = response.json()
        assert "short_url" in body
        assert "code" in body
        assert body["original_url"] == "https://example.com/some/long/path"
        assert body["short_url"].endswith(f"/{body['code']}")

    async def test_short_url_contains_base_url(self, http_client: AsyncClient) -> None:
        response = await http_client.post(
            "/shorten", json={"url": "https://example.com"}
        )
        assert response.status_code == 201
        assert response.json()["short_url"].startswith("http://testserver/")

    async def test_code_is_8_chars_alphanumeric(self, http_client: AsyncClient) -> None:
        response = await http_client.post(
            "/shorten", json={"url": "https://example.com"}
        )
        code = response.json()["code"]
        assert len(code) == 8
        assert code.isalnum()
        assert code == code.lower()

    async def test_two_requests_produce_different_codes(
        self, http_client: AsyncClient
    ) -> None:
        r1 = await http_client.post("/shorten", json={"url": "https://a.example.com"})
        r2 = await http_client.post("/shorten", json={"url": "https://b.example.com"})
        assert r1.json()["code"] != r2.json()["code"]

    async def test_same_url_produces_different_codes(
        self, http_client: AsyncClient
    ) -> None:
        url = "https://example.com/same"
        r1 = await http_client.post("/shorten", json={"url": url})
        r2 = await http_client.post("/shorten", json={"url": url})
        assert r1.status_code == r2.status_code == 201
        assert r1.json()["code"] != r2.json()["code"]

    async def test_expires_at_null_when_no_ttl(self, http_client: AsyncClient) -> None:
        response = await http_client.post(
            "/shorten", json={"url": "https://example.com"}
        )
        assert response.json()["expires_at"] is None

    async def test_expires_at_set_when_ttl_provided(
        self, http_client: AsyncClient
    ) -> None:
        response = await http_client.post(
            "/shorten", json={"url": "https://example.com", "expires_in_seconds": 3600}
        )
        assert response.status_code == 201
        assert response.json()["expires_at"] is not None

    async def test_rejects_non_http_url(self, http_client: AsyncClient) -> None:
        response = await http_client.post(
            "/shorten", json={"url": "ftp://example.com/file"}
        )
        assert response.status_code == 422

    async def test_rejects_plain_string(self, http_client: AsyncClient) -> None:
        response = await http_client.post("/shorten", json={"url": "not-a-url"})
        assert response.status_code == 422

    async def test_rejects_missing_url_field(self, http_client: AsyncClient) -> None:
        response = await http_client.post("/shorten", json={})
        assert response.status_code == 422

    async def test_rejects_url_over_2048_chars(self, http_client: AsyncClient) -> None:
        long_url = "https://example.com/" + "x" * 2030
        response = await http_client.post("/shorten", json={"url": long_url})
        assert response.status_code == 422


# ─── GET /{code} ──────────────────────────────────────────────────────────────


class TestRedirectEndpoint:
    async def test_redirects_to_original_url(self, http_client: AsyncClient) -> None:
        shorten_response = await http_client.post(
            "/shorten", json={"url": "https://redirect-target.example.com"}
        )
        code = shorten_response.json()["code"]

        redirect_response = await http_client.get(
            f"/{code}", follow_redirects=False
        )
        assert redirect_response.status_code == 302
        assert redirect_response.headers["location"] == "https://redirect-target.example.com"

    async def test_returns_404_for_unknown_code(self, http_client: AsyncClient) -> None:
        response = await http_client.get("/xxxxxxxx", follow_redirects=False)
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"

    async def test_returns_404_after_deactivation(
        self, http_client: AsyncClient
    ) -> None:
        shorten_response = await http_client.post(
            "/shorten", json={"url": "https://example.com/to-delete"}
        )
        code = shorten_response.json()["code"]

        await http_client.delete(f"/{code}")

        redirect_response = await http_client.get(f"/{code}", follow_redirects=False)
        assert redirect_response.status_code == 404


# ─── DELETE /{code} ───────────────────────────────────────────────────────────


class TestDeleteEndpoint:
    async def test_deactivates_and_returns_204(self, http_client: AsyncClient) -> None:
        shorten_response = await http_client.post(
            "/shorten", json={"url": "https://example.com/deleteable"}
        )
        code = shorten_response.json()["code"]

        delete_response = await http_client.delete(f"/{code}")
        assert delete_response.status_code == 204

    async def test_delete_unknown_code_returns_404(
        self, http_client: AsyncClient
    ) -> None:
        response = await http_client.delete("/doesnotexist")
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"

    async def test_double_delete_returns_404(self, http_client: AsyncClient) -> None:
        shorten_response = await http_client.post(
            "/shorten", json={"url": "https://example.com/double-delete"}
        )
        code = shorten_response.json()["code"]

        first = await http_client.delete(f"/{code}")
        assert first.status_code == 204

        second = await http_client.delete(f"/{code}")
        assert second.status_code == 404


# ─── GET /health ──────────────────────────────────────────────────────────────


class TestHealthEndpoint:
    async def test_health_returns_ok(self, http_client: AsyncClient) -> None:
        response = await http_client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
