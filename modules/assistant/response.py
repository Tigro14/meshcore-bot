"""One UTF-8-aware output boundary for assistant commands."""
from typing import Any

from ..models import MeshMessage


def split_reply(text: str, max_bytes: int, max_pages: int = 4) -> list[str]:
    """Bound airtime without cutting a UTF-8 code point; disclose truncation."""
    if max_bytes < 4 or max_pages < 1:
        raise ValueError("Invalid reply budget")
    remaining = text.strip()
    pages: list[str] = []
    while remaining and len(pages) < max_pages:
        data = remaining.encode("utf-8")
        if len(data) <= max_bytes:
            pages.append(remaining)
            break
        chunk = data[:max_bytes].decode("utf-8", errors="ignore")
        if len(pages) == max_pages - 1:
            pages.append(data[:max_bytes - 3].decode("utf-8", errors="ignore").rstrip() + "…")
            break
        boundary = max(chunk.rfind("\n"), chunk.rfind(" "))
        if boundary > 0:
            chunk = chunk[:boundary]
        pages.append(chunk)
        remaining = remaining[len(chunk):].lstrip()
    return pages or ["Aucune réponse disponible."]


async def send_answer(command: Any, message: MeshMessage, text: str, *, max_pages: int = 4) -> bool:
    pages = split_reply(text, command.get_max_message_length(message), max_pages)
    if len(pages) == 1:
        return await command.send_response(message, pages[0])
    return await command.send_response_chunked(message, pages)


def clarification_mention(message: MeshMessage, max_bytes: int) -> str:
    """Address channel clarifications using native MeshCore mention syntax."""
    if message.is_dm:
        return ''
    name = message.sender_id
    if not isinstance(name, str) or not name.strip() or name.strip().lower() == 'unknown':
        return ''
    # Do not let an untrusted sender forge additional mentions or format fields.
    if any(ord(c) < 32 or c in '@{}' for c in name):
        return ''
    depth = 0
    for char in name:
        if char == '[':
            depth += 1
        elif char == ']':
            depth -= 1
            if depth < 0:
                return ''
    if depth:
        return ''
    prefix = f'@[{name.strip()}] '
    return prefix if len(prefix.encode('utf-8')) <= max_bytes - 40 else ''
