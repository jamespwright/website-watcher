"""Tiny plugin registry so sources and domains self-register by name."""
from __future__ import annotations

import importlib
import pkgutil
from typing import Generic, TypeVar

T = TypeVar("T")


class Registry(Generic[T]):
    def __init__(self, kind: str, package: str):
        self.kind = kind
        self.package = package
        self._items: dict[str, type[T]] = {}
        self._loaded = False

    def register(self, cls: type[T]) -> type[T]:
        name = getattr(cls, "name", None)
        if not name:
            raise ValueError(f"{cls.__name__} needs a `name` attribute to register as a {self.kind}")
        self._items[name] = cls
        return cls

    def _load(self) -> None:
        # Import every module in the package so their @register decorators run.
        if self._loaded:
            return
        pkg = importlib.import_module(self.package)
        for mod in pkgutil.iter_modules(pkg.__path__):
            importlib.import_module(f"{self.package}.{mod.name}")
        self._loaded = True

    def get(self, name: str) -> type[T]:
        self._load()
        try:
            return self._items[name]
        except KeyError:
            known = ", ".join(sorted(self._items)) or "none"
            raise KeyError(f"Unknown {self.kind} '{name}'. Known: {known}") from None

    def all(self) -> dict[str, type[T]]:
        self._load()
        return dict(sorted(self._items.items()))
