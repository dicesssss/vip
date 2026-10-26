"""Assemble the aiogram Router."""
from __future__ import annotations

from aiogram import Router

from . import handlers_accounts, handlers_monitor, handlers_system
from .context import AppContext
from .middleware import AdminOnly


def build_router(ctx: AppContext) -> Router:
    router = Router(name="admin")
    gate = AdminOnly(ctx.cfg.admin_id)
    router.message.outer_middleware(gate)
    router.callback_query.outer_middleware(gate)
    handlers_system.register(router, ctx)
    handlers_accounts.register(router, ctx)
    handlers_monitor.register(router, ctx)
    return router
