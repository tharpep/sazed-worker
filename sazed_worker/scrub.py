"""Known secret values, removed from everything this process sends to Sazed."""

from .config import CLAUDE_TOKEN, WORKER_KEY

extra_secrets: set[str] = set()  # per-job values to scrub too (e.g. a clone token)


def scrub(text: str) -> str:
    """Remove known secret values from anything leaving this process."""
    for secret in (WORKER_KEY, CLAUDE_TOKEN, *extra_secrets):
        if secret and len(secret) >= 12:
            text = text.replace(secret, "[redacted]")
    return text
