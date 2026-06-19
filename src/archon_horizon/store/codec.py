"""Pluggable on-disk encoding.

The store logic is format-agnostic: it deals in dicts and delegates bytes
to a :class:`Codec`. ``JsonCodec`` is the zero-dependency default;
``YamlCodec`` honors the roadmap's human-editable-YAML goal and imports
``yaml`` lazily so the package stays importable without PyYAML installed.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any


class Codec(ABC):
    extension: str

    @abstractmethod
    def dumps(self, data: Any) -> str: ...

    @abstractmethod
    def loads(self, text: str) -> Any: ...


class JsonCodec(Codec):
    extension = "json"

    def dumps(self, data: Any) -> str:
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    def loads(self, text: str) -> Any:
        return json.loads(text)


class YamlCodec(Codec):
    extension = "yaml"

    def __init__(self) -> None:
        import yaml  # noqa: F401  (fail fast at construction, not import time)

        self._yaml = yaml

    def dumps(self, data: Any) -> str:
        return self._yaml.safe_dump(data, sort_keys=False, allow_unicode=True)

    def loads(self, text: str) -> Any:
        return self._yaml.safe_load(text)
