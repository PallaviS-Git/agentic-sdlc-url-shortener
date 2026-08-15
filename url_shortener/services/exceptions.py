"""
Domain exceptions for the URL shortener service.

These are HTTP-agnostic. The API layer (api/exceptions.py) maps them to
appropriate HTTP status codes and response bodies.
"""
from __future__ import annotations


class ShortCodeNotFoundError(Exception):
    """Raised when a short code does not exist, is inactive, or has expired."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Short code '{code}' not found, inactive, or expired")


class CodeGenerationError(Exception):
    """Raised when a unique short code cannot be generated after all retries."""
