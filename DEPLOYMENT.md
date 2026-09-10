# Railway

1. Завантаж проект у GitHub repository.
2. Railway → New Project → Deploy from GitHub.
3. Додай Variables з `.env.example`.
4. Для SQLite додай Railway Volume з mount path `/app/data`.
5. Використовуй одну replica при Telegram long polling.


## Рекомендовані Variables

- `MAX_ARTICLE_CHARS=3500`
- `MAX_AI_CANDIDATES_PER_CYCLE=1`
- `MAX_COMPLETION_TOKENS=400`
- `AI_MAX_RETRIES=2`
- `HISTORY_RETENTION_DAYS=30`
- `MODERATION_ALLOWED_USER_IDS=<твой Telegram numeric user ID>`

Остання змінна особливо важлива, якщо чат модерації — це група: тоді публікувати або відхиляти новини зможуть лише вказані Telegram user ID.
