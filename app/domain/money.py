"""Single source of truth for the price disclaimer text (PROJECT.md §2.9).

The bot compares the tracked price against the threshold using the public
price returned by a marketplace API, which excludes personal discounts
(wallet balances, marketplace cards, loyalty points, etc.). This module
holds the two approved wordings of the disclaimer that must accompany any
price shown to the user, so both the bot layer (`app/bot/**`,
`app/handlers/**`) and `NotificationService` read the same text instead of
keeping independent copies that could drift apart.

Money-to-text conversion and price formatting are added by PAB-068.
"""

from typing import Final

PRICE_DISCLAIMER_SHORT: Final[str] = "Цена без учёта персональных скидок"

PRICE_DISCLAIMER_FULL: Final[str] = (
    "Бот показывает цену без учёта персональных скидок: кошелька или карты "
    "маркетплейса, баллов и т. п. На сайте цена для вас может быть ниже."
)
