"""Router assembly point.

``create_root_router`` is the single place that wires together every
feature router; ``app.bot.setup.create_dispatcher`` calls it instead of
including routers ad hoc in the dispatcher factory. The common router
(/start, /help, /cancel) is included first so it takes priority over
dialog handlers added by later tickets (Р8).
"""

from aiogram import Router

from app.handlers.common import create_router as create_common_router


def create_root_router() -> Router:
    """Assemble and return the root router with every feature router included."""
    root = Router(name="root")
    root.include_router(create_common_router())
    return root
