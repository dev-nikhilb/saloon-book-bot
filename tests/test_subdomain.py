"""Tests for subdomain extraction logic.

The middleware relies on settings.base_domain. We mock it to test parsing.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _extract_slug(host: str, base_domain: str = "bookbot.com") -> str | None:
    """Replicate the subdomain extraction logic from SubdomainMiddleware for unit testing."""
    host_clean = host.split(":")[0]  # strip port
    base = base_domain.split(":")[0]

    if host_clean.endswith(f".{base}") and host_clean != base:
        return host_clean.replace(f".{base}", "")
    return None


class TestSubdomainExtraction:
    def test_extracts_slug_from_subdomain(self):
        assert _extract_slug("stylestudio.bookbot.com") == "stylestudio"

    def test_returns_none_for_bare_domain(self):
        assert _extract_slug("bookbot.com") is None

    def test_returns_none_for_localhost(self):
        assert _extract_slug("localhost") is None

    def test_returns_none_for_localhost_with_port(self):
        assert _extract_slug("localhost:8000", base_domain="localhost:8000") is None

    def test_handles_port_in_host(self):
        assert _extract_slug("stylestudio.bookbot.com:8080") == "stylestudio"

    def test_multi_level_subdomain(self):
        # "branch1.stylestudio.bookbot.com" => "branch1.stylestudio"
        result = _extract_slug("branch1.stylestudio.bookbot.com")
        assert result == "branch1.stylestudio"

    def test_empty_host(self):
        assert _extract_slug("") is None

    def test_only_dot(self):
        assert _extract_slug(".") is None

    def test_subdomain_with_hyphen(self):
        assert _extract_slug("my-salon.bookbot.com") == "my-salon"

    def test_subdomain_with_numbers(self):
        assert _extract_slug("salon123.bookbot.com") == "salon123"

    def test_www_subdomain(self):
        assert _extract_slug("www.bookbot.com") == "www"

    def test_different_base_domain(self):
        assert _extract_slug("shop.example.io", base_domain="example.io") == "shop"

    def test_no_match_different_domain(self):
        assert _extract_slug("stylestudio.otherdomain.com") is None


class TestSubdomainMiddlewareIntegration:
    """Test the actual middleware class with mocked request/response."""

    @pytest.mark.asyncio
    async def test_middleware_sets_slug(self):
        from app.subdomain import SubdomainMiddleware

        middleware = SubdomainMiddleware(app=MagicMock())

        request = MagicMock()
        request.headers = {"host": "salon.bookbot.com"}
        request.state = MagicMock()

        call_next = AsyncMock(return_value=MagicMock())

        with patch("app.subdomain.settings") as mock_settings:
            mock_settings.base_domain = "bookbot.com"
            await middleware.dispatch(request, call_next)

        assert request.state.tenant_slug == "salon"

    @pytest.mark.asyncio
    async def test_middleware_sets_none_for_bare_domain(self):
        from app.subdomain import SubdomainMiddleware

        middleware = SubdomainMiddleware(app=MagicMock())

        request = MagicMock()
        request.headers = {"host": "bookbot.com"}
        request.state = MagicMock()

        call_next = AsyncMock(return_value=MagicMock())

        with patch("app.subdomain.settings") as mock_settings:
            mock_settings.base_domain = "bookbot.com"
            await middleware.dispatch(request, call_next)

        assert request.state.tenant_slug is None
