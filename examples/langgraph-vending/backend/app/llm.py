"""BigModel GLM chat model (OpenAI-compatible endpoint)."""
from __future__ import annotations

from langchain_openai import ChatOpenAI

from .config import settings


def build_llm(streaming: bool = True) -> ChatOpenAI:
    if not settings.bigmodel_api_key:
        raise RuntimeError(
            "BIGMODEL_API_KEY is not set. Copy .env.example to .env and add your key."
        )
    return ChatOpenAI(
        model=settings.bigmodel_model,
        api_key=settings.bigmodel_api_key,
        base_url=settings.bigmodel_base_url,
        temperature=0.2,
        streaming=streaming,
        timeout=60,
        max_retries=2,
    )
