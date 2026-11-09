"""API Collaborator — только чтение (E11-01, ADR-079)."""

from .client import CollaboratorClient, CollaboratorError, NoApiKey, has_key

__all__ = ["CollaboratorClient", "CollaboratorError", "NoApiKey", "has_key"]
