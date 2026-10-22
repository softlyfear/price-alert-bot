"""Free text outside any dialog (PROJECT.md section 2.13, Р7).

Reacts only in the empty FSM state, so dialogs keep handling their own input.
Only private chats and whitelisted content types are answered; service
messages are skipped silently. Messages without a sender have no FSM context
(the state looks empty), so they are ignored. A recognised product reference
gets an "add this product?" offer; anything else gets a short hint with the
main menu. No service, database or marketplace is touched here, and user text
never reaches the log.
"""

from typing import assert_never

from aiogram import F
from aiogram import Router
from aiogram.enums import ChatType
from aiogram.enums import ContentType
from aiogram.filters import StateFilter
from aiogram.types import Message

from app.bot import texts
from app.bot.keyboards import add_article_keyboard
from app.bot.keyboards import add_ref_keyboard
from app.bot.keyboards import add_short_link_keyboard
from app.bot.keyboards import main_menu_keyboard
from app.domain.exceptions import MultipleProductRefsError
from app.domain.exceptions import ProductRefParseError
from app.domain.links import BareArticle
from app.domain.links import OzonShortLink
from app.domain.links import ProductRef
from app.domain.links import extract_product_ref
from app.handlers.add_product import MAX_TEXT_LENGTH

# User content worth a hint; service messages (pin, auto-delete timer,
# member joins and leaves, ...) are deliberately absent and stay silent.
REPLY_CONTENT_TYPES = frozenset(
    {
        ContentType.TEXT,
        ContentType.STICKER,
        ContentType.PHOTO,
        ContentType.VIDEO,
        ContentType.ANIMATION,
        ContentType.DOCUMENT,
        ContentType.AUDIO,
        ContentType.VOICE,
        ContentType.VIDEO_NOTE,
        ContentType.LOCATION,
        ContentType.VENUE,
        ContentType.CONTACT,
        ContentType.POLL,
        ContentType.DICE,
        ContentType.STORY,
        ContentType.GAME,
        ContentType.CHECKLIST,
        ContentType.PAID_MEDIA,
        ContentType.GIVEAWAY,
    }
)


def create_router() -> Router:
    """Build a fresh ``fallback`` router (see ``common.create_router``)."""
    router = Router(name="fallback")
    # Private chats only, and only whitelisted user content.
    router.message.filter(
        F.chat.type == ChatType.PRIVATE,
        F.content_type.in_(REPLY_CONTENT_TYPES),
    )

    @router.message(StateFilter(None), F.from_user, F.text)
    async def on_free_text(message: Message) -> None:
        """Offer to add a recognised product, else show the main menu."""
        try:
            ref = extract_product_ref((message.text or "")[:MAX_TEXT_LENGTH])
        except MultipleProductRefsError:
            await message.answer(
                texts.MULTIPLE_PRODUCT_REFS_TEXT, reply_markup=main_menu_keyboard()
            )
            return
        except ProductRefParseError:
            await message.answer(texts.FALLBACK_TEXT, reply_markup=main_menu_keyboard())
            return
        match ref:
            case BareArticle(article=article):
                await message.answer(
                    texts.add_article_offer_text(article),
                    reply_markup=add_article_keyboard(article),
                    parse_mode=None,
                )
            case OzonShortLink(code=code):
                await message.answer(
                    texts.add_short_link_offer_text(),
                    reply_markup=add_short_link_keyboard(code),
                    parse_mode=None,
                )
            case ProductRef():
                await message.answer(
                    texts.add_ref_offer_text(ref.marketplace, ref.article),
                    reply_markup=add_ref_keyboard(ref.marketplace, ref.article),
                    parse_mode=None,
                )
            case _:
                assert_never(ref)

    @router.message(StateFilter(None), F.from_user)
    async def on_other_message(message: Message) -> None:
        """Non-text message or unknown command: hint and the main menu."""
        await message.answer(texts.FALLBACK_TEXT, reply_markup=main_menu_keyboard())

    return router
