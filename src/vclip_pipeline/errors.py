"""User-facing errors shared by every command."""


class VClipError(RuntimeError):
    """Raised when a command cannot complete safely."""
