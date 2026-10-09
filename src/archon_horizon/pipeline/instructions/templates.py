"""Inspectable Markdown prompts, rendered from the session's immutable bundle.

Only ``{{identifier}}`` tokens interpolate. JSON braces, shell variables and
Markdown are literal text, so authoring a prompt cannot accidentally execute
formatting expressions. Record values are substituted once, never re-rendered.
"""

from __future__ import annotations

import base64
import hashlib
import re

from .._resources import PIPELINE_ROOT
from ..errors import DomainError

_TOKEN = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")
_NAME = re.compile(r"[a-z0-9][a-z0-9/-]*")


def template(name: str, bundle: dict | None = None, **values: object) -> str:
    """Render one trusted template, retaining old catalogs without new fields.

    Catalogs predating Markdown templates have no ``prompt_index``. They use
    the packaged compatibility text for previously unpinned fragments; callers
    still prefer their original core/function/maintainer fields. New catalogs
    must contain the requested file and its correct digest: silently loading a
    newer installed prompt would break retained-session provenance.
    """
    if not _NAME.fullmatch(name) or ".." in name:
        raise ValueError("invalid prompt template name")
    path = "prompts/" + name + ".md"
    if bundle is not None and "prompt_index" in bundle:
        item = bundle.get("files", {}).get(path)
        if item is None:
            raise DomainError("prompt_unavailable", f"Pinned catalog has no {path}", 422)
        try:
            content = base64.b64decode(item["content_base64"], validate=True)
            if hashlib.sha256(content).hexdigest() != item["sha256"]:
                raise ValueError("digest mismatch")
            text = content.decode("utf-8").rstrip()
        except (KeyError, ValueError, UnicodeDecodeError) as error:
            raise DomainError("invalid_prompt_template", f"Invalid pinned prompt {path}", 422) from error
    else:
        text = (PIPELINE_ROOT / "instructions" / "templates" / (name + ".md")).read_text().rstrip()
    required = set(_TOKEN.findall(text))
    if required != set(values):
        raise ValueError(f"Prompt {name} fields differ: required={sorted(required)}, supplied={sorted(values)}")
    return _TOKEN.sub(lambda match: str(values[match[1]]), text)
