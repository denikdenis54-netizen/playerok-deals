from __future__ import annotations

from .base import EscrowError, EscrowProvider, OrderInfo, ProviderNotImplemented
from .live import LiveProvider
from .mock import MockProvider

__all__ = [
    "EscrowError",
    "EscrowProvider",
    "OrderInfo",
    "ProviderNotImplemented",
    "MockProvider",
    "LiveProvider",
    "build_provider",
]


def build_provider(name: str, **kwargs) -> EscrowProvider:
    if name == "mock":
        return MockProvider(**kwargs)
    if name == "live":
        return LiveProvider(**kwargs)
    raise ValueError(f"Неизвестный ESCROW_PROVIDER: {name!r}")