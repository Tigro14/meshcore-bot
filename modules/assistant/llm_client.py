"""Shared OpenAI-compatible HTTP transport for conversation and mesh queries."""
from typing import Any

import requests


def configured_reasoning_effort(config_reader: Any) -> str:
    """Return the optional OpenAI-compatible reasoning effort setting."""
    value = config_reader(
        "Llm_Command",
        "reasoning_effort",
        fallback="",
        value_type="str",
    )
    return str(value or "").strip()


def apply_reasoning_effort(payload: dict[str, Any], reasoning_effort: str) -> None:
    """Add the setting only when configured, preserving backend compatibility."""
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort


def post_chat(endpoint: str, payload: dict[str, Any], timeout: float) -> requests.Response:
    return requests.post(endpoint, json=payload, timeout=timeout)
