"""Settings loaded from /opt/fb-webhook/.env"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Facebook
    fb_app_id: str = ""
    fb_app_secret: str = ""
    fb_page_id: str = ""
    fb_page_token: str = ""
    fb_verify_token: str = "change-me"
    fb_graph_version: str = "v21.0"

    # LLM (works with OpenAI directly or any OpenAI-compatible gateway
    # such as OpenRouter, cx.ai, LiteLLM, vLLM, Ollama, etc. — point
    # `openai_base_url` at the gateway's `/v1` endpoint.)
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str = "https://api.openai.com/v1"

    # Local services
    redis_url: str = "redis://127.0.0.1:6379/0"
    database_url: str = "postgresql://fbwebhook:fbwebhook@127.0.0.1:5432/fbwebhook"

    # Behaviour
    auto_reply_enabled: bool = False
    human_review_queue: bool = True
    telegram_review_bot_token: str = ""
    telegram_review_chat_id: str = ""

    log_level: str = "INFO"


settings = Settings()
