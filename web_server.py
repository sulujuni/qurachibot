"""Run the web dashboard independently or alongside the bot."""

import uvicorn
from bot.config import settings
from web.app import app

if __name__ == "__main__":
    uvicorn.run(
        "web.app:app",
        host=settings.WEB_HOST,
        port=settings.WEB_PORT,
        reload=True,
    )
