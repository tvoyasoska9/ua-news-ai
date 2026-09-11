import asyncio
import logging
import signal

from app.collector import close_telegram_client
from app.config import get_settings
from app.news_runner import NewsRunner
from app.moderation_bot import ModerationBot
from app.simple_database import SimpleDatabase

logging.basicConfig(level=logging.INFO,format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log=logging.getLogger("ua-news-ai")

async def main():
    settings=get_settings()
    db=SimpleDatabase(settings.database_path)
    bot=ModerationBot(settings,db)
    runner=NewsRunner(settings,db,bot)
    await bot.start()

    stop_event=asyncio.Event()
    loop=asyncio.get_running_loop()
    for sig in (signal.SIGINT,signal.SIGTERM):
        try: loop.add_signal_handler(sig,stop_event.set)
        except NotImplementedError: pass

    task=asyncio.create_task(runner.forever())
    log.info("UA News AI started")
    try:
        await stop_event.wait()
    finally:
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        await bot.stop()
        await close_telegram_client()
        db.close()

if __name__=="__main__":
    asyncio.run(main())
