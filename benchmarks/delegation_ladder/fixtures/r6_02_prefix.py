def extract_content(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Pull the assistant message content and the usage block out of a provider
    response body. Raises ValueError when the provider returned nothing usable."""
    choices = data.get("choices") or []
    if not choices:
        raise ValueError("API returned empty choices array")

    content: str = choices[0].get("message", {}).get("content") or ""
    usage = data.get("usage") or {}
    return content, usage
