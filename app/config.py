import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    moderation_chat_id: int
    publish_channel_id: int
    openai_api_key: str
    openai_model: str
    telegram_api_id: int
    telegram_api_hash: str
    telegram_session: str
    check_interval_seconds: int
    max_completion_tokens: int
    database_path: str


def get_settings():
    return Settings(
        telegram_bot_token=required("TELEGRAM_BOT_TOKEN"),
        moderation_chat_id=int(required("MODERATION_CHAT_ID")),
        publish_channel_id=int(required("PUBLISH_CHANNEL_ID")),
        openai_api_key=required("OPENAI_API_KEY"),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-5-mini").strip() or "gpt-5-mini",
        telegram_api_id=int(required("TELEGRAM_API_ID")),
        telegram_api_hash=required("TELEGRAM_API_HASH"),
        telegram_session=required("TELEGRAM_SESSION"),
        check_interval_seconds=max(10, int(os.getenv("CHECK_INTERVAL_SECONDS", "30"))),
        max_completion_tokens=max(1000, int(os.getenv("MAX_COMPLETION_TOKENS", "8000"))),
        database_path=os.getenv("DATABASE_PATH", "/app/data/news.db"),
    )
