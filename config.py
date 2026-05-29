"""
Central configuration loader.

Reads secrets from .env via pydantic-settings (Settings class),
and structured config from config.yaml (AppConfig class).

Usage:
    from config import settings, config
"""

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load .env into os.environ so that LLM client libraries (openai, anthropic, etc.)
# can read API keys directly from the environment, as they expect.
load_dotenv()


# ---------------------------------------------------------------------------
# config.yaml schema
# ---------------------------------------------------------------------------

class LabeledModelConfig(BaseModel):
    model: str
    label: str


class ModelsConfig(BaseModel):
    ingestion: str
    filter: str
    summary: str = "openai:gpt-5.4-mini"
    brainstorm: list[LabeledModelConfig]
    critic: list[LabeledModelConfig]
    proposal_writer: str


class ClientConfig(BaseModel):
    """Per-agent client selection: API mode vs local CLI (subscription)."""
    mode: str = "api"          # "api" or "local"
    local_model: str = ""      # used when mode == "local"


class ClientsConfig(BaseModel):
    ingestion: ClientConfig = ClientConfig()
    filter: ClientConfig = ClientConfig()
    summary: ClientConfig = ClientConfig()
    brainstorm: list[ClientConfig] = []
    critic: list[ClientConfig] = []
    proposal_writer: ClientConfig = ClientConfig()


class ThresholdsConfig(BaseModel):
    min_novelty_rating: float = 3.0
    min_viability_rating: float = 3.0


class ResearcherConfig(BaseModel):
    name: str
    profile_summary: str


class NotionConfig(BaseModel):
    database_name: str


class FilterConfig(BaseModel):
    use_full_text: bool = True


class BrainstormConfig(BaseModel):
    max_rounds: int = 3
    include_pdf: bool = True
    max_input_tokens: int = 400_000  # safety margin below 450K API limit


class AppConfig(BaseModel):
    models: ModelsConfig
    clients: ClientsConfig = ClientsConfig()
    thresholds: ThresholdsConfig
    researcher: ResearcherConfig
    notion: NotionConfig
    filter: FilterConfig = FilterConfig()
    brainstorm: BrainstormConfig = BrainstormConfig()


# ---------------------------------------------------------------------------
# .env schema
# ---------------------------------------------------------------------------

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Notion
    notion_api_token: str
    notion_database_id: str
    notion_directions_page_id: str
    notion_log_page_id: str

    # LLM providers (optional — only needed for the models you configure)
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    google_api_key: str = ""


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_app_config(path: Path | None = None) -> AppConfig:
    config_path = path or Path(__file__).parent / "config.yaml"
    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return AppConfig(**data)


# Module-level singletons — import these everywhere
settings = Settings()
config = load_app_config()
