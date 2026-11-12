"""UTF-8-safe bounded projections. Never modifies authoritative text."""
from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Callable

from kernelcat_executor.contracts.models import Json, canonical


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


def fit_observation_page(value: object, limit: int,
                         envelope: Callable[[object], object]) -> Json | None:
    """Project an existing page without advancing past its model-visible prefix.

    This never changes the authoritative page or its source hash. Unicode text
    pages use code-point cursors; artifact pages use byte cursors, including
    base64 pages. Charge the complete rendered observation, not just raw text.
    None means this is not one of these page contracts. A recognized page whose
    metadata prevents progress raises ValueError instead of fabricating a cursor.
    """
    if (not isinstance(value, dict) or not isinstance(value.get("text"), str)
            or type(value.get("offset")) is not int or value["offset"] < 0
            or "next_offset" not in value
            or (value["next_offset"] is not None and type(value["next_offset"]) is not int)
            or type(value.get("truncated")) is not bool
            or not isinstance(value.get("sha256"), str) or len(value["sha256"]) != 64
            or any(char not in "0123456789abcdef" for char in value["sha256"])):
        return None
    offset, text = value["offset"], value["text"]
    content: str | bytes = text
    byte_page = "encoding" in value
    if byte_page:
        if (not isinstance(value["encoding"], str) or value["encoding"] not in {"utf-8", "base64"}
                or type(value.get("size_bytes")) is not int
                or type(value.get("next_offset")) is not int
                or type(value.get("eof")) is not bool
                or not isinstance(value.get("read"), dict)
                or not isinstance(value["read"].get("tool"), str)
                or not isinstance(value["read"].get("arguments"), dict)):
            return None
        try:
            data = text.encode("utf-8") if value["encoding"] == "utf-8" else base64.b64decode(text, validate=True)
        except (ValueError, binascii.Error):
            return None
        end = offset + len(data)
        if (not end <= value["size_bytes"] or value["next_offset"] != end
                or value["eof"] != (end == value["size_bytes"])
                or value["truncated"] != (end < value["size_bytes"])
                or (not data and not value["eof"])):
            return None
        continuation = value.get("next_read")
        if "next_read" not in value or (continuation is None) != value["eof"]:
            return None
        if continuation is not None and (not isinstance(continuation, dict)
                or continuation.get("tool") != value["read"]["tool"]
                or not isinstance(continuation.get("arguments"), dict)
                or type(continuation["arguments"].get("offset")) is not int
                or continuation["arguments"]["offset"] != end):
            return None
        if continuation is not None:
            original = {key: item for key, item in value["read"]["arguments"].items()
                        if key not in {"offset", "limit"}}
            following = {key: item for key, item in continuation["arguments"].items()
                         if key not in {"offset", "limit"}}
            requested_limit = continuation["arguments"].get("limit")
            if (canonical(original) != canonical(following)
                    or (requested_limit is not None
                        and (type(requested_limit) is not int or requested_limit < 1))):
                return None
        if value["encoding"] == "base64":
            content = data
    else:
        end = offset + len(text)
        if (type(value.get("total_chars")) is not int
                or type(value.get("total_bytes")) is not int
                or not end <= value["total_chars"]
                or len(text.encode("utf-8")) > value["total_bytes"]
                or value["next_offset"] != (end if end < value["total_chars"] else None)
                or value["truncated"] != (end < value["total_chars"])
                or (not text and end < value["total_chars"])):
            return None

    def page(count: int) -> Json:
        prefix = content[:count]
        rendered = base64.b64encode(prefix).decode("ascii") if isinstance(prefix, bytes) else prefix
        result = {**value, "text": rendered}
        if byte_page:
            consumed = len(prefix) if isinstance(prefix, bytes) else len(prefix.encode("utf-8"))
            selected_end = offset + consumed
            eof = selected_end == value["size_bytes"]
            next_read = None
            if not eof:
                prior = value.get("next_read") or value["read"]
                next_read = {**prior, "arguments": {**prior["arguments"], "offset": selected_end}}
            result.update(next_offset=selected_end, eof=eof, truncated=not eof, next_read=next_read)
        else:
            selected_end = offset + count
            more = selected_end < value["total_chars"]
            result.update(next_offset=selected_end if more else None, truncated=more)
        return result

    def fits(candidate: Json) -> bool:
        return len(canonical(envelope(candidate)).encode("utf-8")) <= limit

    if fits(value):
        return dict(value)
    if not fits(page(0)):
        raise ValueError("page metadata exceeds observation budget")
    low, high = 0, len(content)
    while low < high:
        middle = (low + high + 1) // 2
        if fits(page(middle)):
            low = middle
        else:
            high = middle - 1
    if low == 0 and content:
        raise ValueError("page metadata and one character exceed observation budget")
    return page(low)
