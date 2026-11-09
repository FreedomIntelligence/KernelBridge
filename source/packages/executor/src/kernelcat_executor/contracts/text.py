"""UTF-8-safe bounded projections. Never modifies authoritative text."""
from __future__ import annotations

import hashlib

from kernelcat_executor.contracts.models import Json


def text_page(text: str, offset: int = 0, limit: int = 8192,
              max_bytes: int = 16384) -> Json:
    """Offsets count Unicode code points, NOT bytes; a returned cursor always advances."""
    if type(offset) is not int or not 0 <= offset <= len(text):
        raise ValueError("offset outside text")
    if type(limit) is not int or not 1 <= limit <= 8192 or max_bytes < 4:
        raise ValueError("invalid page limit")
    content = text[offset:offset + limit].encode("utf-8")[:max_bytes].decode("utf-8", "ignore")
    end = offset + len(content)
    encoded = text.encode("utf-8")
    return {"text": content, "offset": offset, "next_offset": end if end < len(text) else None,
            "total_chars": len(text), "total_bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(), "truncated": end < len(text)}
