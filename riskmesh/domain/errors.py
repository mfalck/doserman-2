"""Domain errors shared by all tiers. The API maps them to HTTP status codes."""


class DomainError(Exception):
    """Base class for errors raised by the business layer."""


class ValidationError(DomainError, ValueError):
    """Input is invalid (HTTP 422)."""


class NotFoundError(DomainError, LookupError):
    """A referenced entity does not exist (HTTP 404)."""


class ConflictError(DomainError):
    """The change conflicts with existing data, e.g. a duplicate name (HTTP 409)."""


class AuthenticationError(DomainError):
    """Not logged in, wrong credentials or expired token (HTTP 401)."""


class PermissionDenied(DomainError):
    """Logged in, but the role does not allow this (HTTP 403)."""
