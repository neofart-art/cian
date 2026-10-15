"""Shared source failures. Importing a local source never initializes networking."""


class SourceError(Exception):
    pass


class SourceBlocked(SourceError):
    """Expected refusal/CAPTCHA state, not an application crash."""
