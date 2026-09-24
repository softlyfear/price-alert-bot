"""Tests for app.domain.money: the two disclaimer wordings (AC1, PAB-067).

The independent source of truth for the expected text is ``PROJECT.md``
§2.9, quoted verbatim in the docstring below and in the asserted literals --
not a re-derivation from the module under test.

Approved wording (``PROJECT.md`` §2.9, quoted verbatim):
    Короткая -- рядом с каждой показанной ценой: «Цена без учёта
    персональных скидок».
    Полная -- в описании бота, /start и /help: «Бот показывает цену без
    учёта персональных скидок: кошелька или карты маркетплейса, баллов и
    т. п. На сайте цена для вас может быть ниже.»
"""

import subprocess
from pathlib import Path

from app.domain.money import PRICE_DISCLAIMER_FULL
from app.domain.money import PRICE_DISCLAIMER_SHORT

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_price_disclaimer_short_matches_approved_wording_verbatim() -> None:
    """Mutation target: any single-character edit of the literal below."""
    assert PRICE_DISCLAIMER_SHORT == "Цена без учёта персональных скидок"


def test_price_disclaimer_full_matches_approved_wording_verbatim() -> None:
    """Mutation target: any single-character edit of the literal below."""
    assert PRICE_DISCLAIMER_FULL == (
        "Бот показывает цену без учёта персональных скидок: кошелька или карты "
        "маркетплейса, баллов и т. п. На сайте цена для вас может быть ниже."
    )


def test_price_disclaimer_short_length_fits_telegram_short_description_limit() -> None:
    """Bot API's ``setMyShortDescription`` caps the field at 120 characters."""
    assert len(PRICE_DISCLAIMER_SHORT) <= 120


def test_price_disclaimer_wording_appears_only_in_the_money_module() -> None:
    """AC1: ``app/domain/money.py`` is the only file under ``app/`` naming
    the disclaimer wording -- every other user of it imports the constant
    instead of keeping an independent copy.

    Mutation target: a hardcoded copy of the disclaimer text pasted into
    any other file under ``app/`` instead of importing the constant.
    """
    result = subprocess.run(
        ["grep", "-rl", "--include=*.py", "персональных скидок", "app/"],
        capture_output=True,
        text=True,
        check=False,
        cwd=_REPO_ROOT,
    )

    matched_files = [
        line.strip() for line in result.stdout.splitlines() if line.strip()
    ]
    assert matched_files == ["app/domain/money.py"]
