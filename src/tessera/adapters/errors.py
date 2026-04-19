"""Classified adapter errors per docs/system-design.md §Failure taxonomy.

The retry policy and user-visible surfaces documented in ``system-design.md``
branch on which class of error a model adapter reports. Surfacing a raw
``httpx.HTTPError`` at the retrieval boundary would collapse network flakes,
model-not-loaded states, and authentication failures into one opaque signal;
the retrieval pipeline needs to distinguish them to pick the right recovery
path (exponential backoff, ``ollama pull``, surface to the user, etc).
"""

from __future__ import annotations


class AdapterError(Exception):
    """Base class for adapter-level failures."""


class AdapterNetworkError(AdapterError):
    """Transport failure: timeout, connection refused, DNS error."""


class AdapterModelNotFoundError(AdapterError):
    """Provider returned a 404-equivalent for the requested model."""


class AdapterOOMError(AdapterError):
    """Provider reported a resource-exhaustion error."""


class AdapterAuthError(AdapterError):
    """Credentials were missing or rejected."""


class AdapterResponseError(AdapterError):
    """Response body failed schema validation (malformed, wrong shape, etc.)."""
