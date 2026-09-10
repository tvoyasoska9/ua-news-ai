import asyncio
import logging
import signal

from app.config import get_settings
from app.database import Database
from app.pipeline import NewsPipeline
from app.telegram_bot import NewsBot
from app.health import start_health_server
from app.collector import close_telegram_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("ua-news-ai")

async def main():
    settings = get_settings()
    db = Database(settings.database_path)
    bot = NewsBot(settings, db)
    pipeline = NewsPipeline(settings, db, bot)
    await bot.start()
    health_server = await start_health_server(settings.health_port)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def stop():
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop)
        except NotImplementedError:
            pass

    task = asyncio.create_task(pipeline.run_forever())
    log.info("UA News AI started")
    try:
        await stop_event.wait()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        health_server.close()
        await health_server.wait_closed()
        await bot.stop()
        await close_telegram_client()
        db.close()

if __name__ == "__main__":
    asyncio.run(main())
