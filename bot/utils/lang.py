"""Language utility — resolves user language from database."""

from sqlalchemy import select

from bot.i18n import DEFAULT_LANGUAGE, get_text
from bot.models.database import async_session
from bot.models.user_settings import UserSettings


async def get_user_lang(user_id: int) -> str:
    """Get the language code for a user from the database."""
    async with async_session() as session:
        result = await session.execute(
            select(UserSettings.language).where(UserSettings.user_id == user_id)
        )
        lang = result.scalar_one_or_none()
    return lang if lang else DEFAULT_LANGUAGE


async def set_user_lang(user_id: int, language: str) -> None:
    """Set (upsert) the language preference for a user."""
    async with async_session() as session:
        result = await session.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
        settings = result.scalar_one_or_none()

        if settings:
            settings.language = language
        else:
            settings = UserSettings(user_id=user_id, language=language)
            session.add(settings)

        await session.commit()


async def t(key: str, user_id: int, **kwargs) -> str:
    """Translate a key for a specific user (fetches their lang from DB)."""
    lang = await get_user_lang(user_id)
    return get_text(key, lang=lang, **kwargs)
