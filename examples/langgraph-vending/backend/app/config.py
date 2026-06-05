"""Configuration for the vending machine LangGraph agent."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


class Settings:
    # LLM
    bigmodel_api_key: str = os.getenv("BIGMODEL_API_KEY", "")
    bigmodel_base_url: str = os.getenv(
        "BIGMODEL_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"
    )
    bigmodel_model: str = os.getenv("BIGMODEL_MODEL", "glm-5.1")

    # Vending backend
    ucp_mock_url: str = os.getenv("UCP_MOCK_URL", "https://ucp-mock.fxp007.workers.dev")
    supply_chain_url: str = os.getenv(
        "SUPPLY_CHAIN_URL", "https://supply-chain-mock.fxp007.workers.dev"
    )
    ucp_client_secret: str = os.getenv("UCP_CLIENT_SECRET", "secret")

    # Skills directory (agentic-commerce-skills/skills)
    @property
    def skills_dir(self) -> Path:
        # Walk up to find the agentic-commerce-skills repo
        for parent in [BASE_DIR, *BASE_DIR.parents]:
            candidate = parent / "agentic-commerce-skills" / "skills"
            if candidate.exists():
                return candidate
        return BASE_DIR / "skills"


settings = Settings()
