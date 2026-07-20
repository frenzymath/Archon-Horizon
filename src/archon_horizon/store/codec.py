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
        # `safe_load` always uses PyYAML's pure-Python loader, which is several
        # times slower than the libyaml-backed one. Reading is hot — the
        # dashboard's state poll parses hundreds of small YAML files (tasks,
        # roadmap, inbox, run records) — so prefer `CSafeLoader`, which parses
        # the same safe subset and yields identical data. It exists only when
        # PyYAML was built against libyaml, hence the fallback.
        #
        # Only the loader is swapped: `CSafeDumper` formats its output slightly
        # differently, and these files are human-editable and diffed in the
        # ledger, so writes stay on the pure-Python dumper.
        self._loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

    def dumps(self, data: Any) -> str:
        return self._yaml.safe_dump(data, sort_keys=False, allow_unicode=True)

    def loads(self, text: str) -> Any:
        return self._yaml.load(text, Loader=self._loader)
