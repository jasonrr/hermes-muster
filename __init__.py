from __future__ import annotations

from pathlib import Path


def register(ctx):
    from .muster import cli, gateway

    root = Path(__file__).resolve().parent
    ctx.register_cli_command(
        name="muster",
        help="GitHub label → coding agent in a herdr pane, reporting to a kanban card",
        setup_fn=cli.setup,
        handler_fn=lambda args: cli.main(args, ctx),
    )
    for name in ("escalation", "run"):
        path = root / "skills" / name / "SKILL.md"
        if path.is_file():  # stat only; Task 7 creates the files
            ctx.register_skill(name, path)
    gateway.CTX = ctx  # config loads lazily, when Hermes connects the adapter or dispatches a message
    if hasattr(ctx, "register_telegram_handler"):
        ctx.register_telegram_handler(gateway.telegram_factory)
    if hasattr(ctx, "register_hook"):
        ctx.register_hook("pre_gateway_dispatch", gateway.on_dispatch)
