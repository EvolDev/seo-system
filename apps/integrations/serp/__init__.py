"""Выдача Google через SERP API (E2-02, ADR-040). Подробности — client.py."""

from .client import search, spent_today
from .types import SerpBudgetExceeded, SerpError, SerpPage, SerpResult

__all__ = [
    "SerpBudgetExceeded",
    "SerpError",
    "SerpPage",
    "SerpResult",
    "search",
    "spent_today",
]
