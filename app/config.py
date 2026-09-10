import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def bounded_int(name, default, minimum, maximum):
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def int_set(name):
    values = set()
    for part in os.getenv(name, "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            values.add(int(part))
        except ValueError:
            continue
    return frozenset(values)


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    moderation_chat_id: int
    publish_channel_id: int
    openai_api_key: str
    openai_model: str
    check_interval_minutes: int
    min_importance_to_send: int
    database_path: str
    health_port: int
    moderation_interval_seconds: int
    telegram_api_id: int
    telegram_api_hash: str
    telegram_session: str
    max_article_chars: int
    max_ai_candidates_per_cycle: int
    max_completion_tokens: int
    moderation_allowed_user_ids: frozenset[int]
    history_retention_days: int
    ai_max_retries: int


def get_settings():
    # Keep the monitor genuinely near-real-time, but allow a small amount of
    # configuration without letting an accidental Railway variable create a
    # huge backlog.
    check_interval = bounded_int("CHECK_INTERVAL_MINUTES", 1, 1, 2)
    moderation_interval = bounded_int("MODERATION_INTERVAL_SECONDS", 45, 20, 60)

    return Settings(
        telegram_bot_token=required("TELEGRAM_BOT_TOKEN"),
        moderation_chat_id=int(required("MODERATION_CHAT_ID")),
        publish_channel_id=int(required("PUBLISH_CHANNEL_ID")),
        openai_api_key=required("OPENAI_API_KEY"),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-5-mini").strip() or "gpt-5-mini",
        check_interval_minutes=check_interval,
        min_importance_to_send=bounded_int("MIN_IMPORTANCE_TO_SEND", 3, 1, 10),
        database_path=os.getenv("DATABASE_PATH", "/app/data/news.db"),
        health_port=int(os.getenv("HEALTH_PORT", "8080")),
        moderation_interval_seconds=moderation_interval,
        telegram_api_id=int(required("TELEGRAM_API_ID")),
        telegram_api_hash=required("TELEGRAM_API_HASH"),
        telegram_session=required("TELEGRAM_SESSION"),
        # A Telegram news post almost never needs 24,000 characters of source
        # material. This is the main API-cost guard.
        max_article_chars=bounded_int("MAX_ARTICLE_CHARS", 1800, 1200, 3000),
        # Hard guard against a source backlog/restart spending the whole API
        # balance in one polling cycle.
        max_ai_candidates_per_cycle=bounded_int("MAX_AI_CANDIDATES_PER_CYCLE", 1, 1, 2),
        # News posts are short; a hard output cap prevents runaway generations.
        max_completion_tokens=bounded_int("MAX_COMPLETION_TOKENS", 400, 250, 700),
        # Optional allow-list for moderation callbacks. When empty, the existing
        # private-chat behavior is preserved; when configured, only these users
        # can publish or reject.
        moderation_allowed_user_ids=int_set("MODERATION_ALLOWED_USER_IDS"),
        # Keep SQLite bounded on a long-running Railway deployment.
        history_retention_days=bounded_int("HISTORY_RETENTION_DAYS", 30, 7, 180),
        # Retries are only used for transient API failures.
        ai_max_retries=bounded_int("AI_MAX_RETRIES", 2, 0, 3),
    )
