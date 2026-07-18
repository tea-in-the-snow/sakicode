"""Configuration: resolve API key, base URL and model from flags, env and .env."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


@dataclass
class Config:
    api_key: str | None
    base_url: str
    model: str


def load_config(model: str | None = None, base_url: str | None = None) -> Config:
    """CLI flags win over env vars, which win over ./.env, which wins over defaults."""
    load_dotenv(Path.cwd() / ".env")  # does not override existing env vars
    return Config(
        api_key=os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY"),
        base_url=base_url or os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL,
        model=model or DEFAULT_MODEL,
    )
