"""Web dashboard (login required). See ``app.create_app`` and docs/dashboard.md."""

from .app import Paths, create_app
from .auth import Auth, AuthError
from .jobs import JobRunner

__all__ = ["Paths", "create_app", "Auth", "AuthError", "JobRunner"]
