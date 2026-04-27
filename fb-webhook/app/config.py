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

    # Comment moderation. Each setting is a comma-separated list of labels
    # produced by `llm.classify_comment` (positive | neutral | question |
    # negative | spam_toxic). Defaults are conservative: only spam/toxic
    # gets auto-hidden, both negative and spam_toxic get forwarded to the
    # admin Telegram, and only spam_toxic gets skipped on auto-reply.
    comment_auto_hide_labels: str = "spam_toxic"
    comment_forward_labels: str = "negative,spam_toxic"
    comment_skip_reply_labels: str = "spam_toxic"
    # Minimum classifier confidence required before acting on
    # auto-hide/skip. Below this, the comment is treated as "neutral".
    comment_action_min_confidence: float = 0.6

    # Telegram admin bot — used for posting/managing the Page from chat.
    # If `telegram_admin_chat_id` is empty the bot will only respond to
    # /start and /whoami (so the owner can discover their own chat_id).
    telegram_bot_token: str = ""
    telegram_admin_chat_id: str = ""
    # Optional shared secret echoed back by Telegram in
    # `X-Telegram-Bot-Api-Secret-Token`. Acts as an authn check on the
    # public webhook so random POSTs cannot trigger commands.
    telegram_webhook_secret: str = ""

    # Optional: route admin notifications into a supergroup with topics
    # instead of the 1-1 chat. When set, `telegram_admin_group_id` wins
    # over `telegram_admin_chat_id` for outbound notify_admin() calls.
    # The per-topic thread IDs (numeric, see Telegram getForumTopic) tell
    # the bot which forum thread to post into for each notification kind.
    telegram_admin_group_id: str = ""
    telegram_admin_topic_customer: str = ""    # khách hỏi / chốt đơn / SĐT
    telegram_admin_topic_inventory: str = ""   # kho hàng (tham khảo)
    telegram_admin_topic_post: str = ""        # đăng bài (tham khảo)

    # Legacy review-bot fields (kept for backwards compat with older .env
    # files; the admin bot above replaces them).
    telegram_review_bot_token: str = ""
    telegram_review_chat_id: str = ""

    log_level: str = "INFO"


settings = Settings()
