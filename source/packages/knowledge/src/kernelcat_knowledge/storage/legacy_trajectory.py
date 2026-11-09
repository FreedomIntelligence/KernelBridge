"""会话轨迹落盘：把上传的 .md 写到 yaml 配置的 uploaded_dir。"""
from __future__ import annotations

from pathlib import Path


def save_trajectory(session_id: str, content: str, *, uploaded_dir: Path) -> dict:
    """把上传的会话轨迹写入 uploaded_dir/<session_id>.md。

    路径校验：
    - session_id 走 `Path(session_id).name` 防止 ../ 遍历。
    - uploaded_dir 在写入前 mkdir(parents=True, exist_ok=True)。
    """
    if not session_id:
        return {"status": "error", "message": "session_id is required"}
    if not content:
        return {"status": "error", "message": "content must not be empty"}

    safe_stem = Path(session_id).name
    if not safe_stem or safe_stem in (".", ".."):
        return {"status": "error", "message": "invalid session_id"}

    target_dir = Path(uploaded_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    raw_path = target_dir / f"{safe_stem}.md"
    raw_path.write_text(content, encoding="utf-8")

    return {"status": "ok", "path": str(raw_path)}
