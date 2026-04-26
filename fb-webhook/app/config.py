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

    # LLM
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

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
