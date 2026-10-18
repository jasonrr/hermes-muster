from __future__ import annotations

from pathlib import Path


def register(ctx):
    from .muster import cli, gateway, hold

    root = Path(__file__).resolve().parent
    ctx.register_cli_command(
        name="muster",
        help="GitHub label → coding agent in a herdr pane, reporting to a kanban card",
        setup_fn=cli.setup,
        handler_fn=lambda args: cli.main(args, ctx),
    )
    # Hermes serves the registered file whole (frontmatter included), so the skills are the prompt files themselves.
    for name, prompt in (("escalation", "escalation"), ("run", "dispatch")):
        ctx.register_skill(name, root / "prompts" / f"{prompt}.md")
    hold.CTX = ctx  # ctx.llm: the duplicate check before an automatic approval launches
    if hasattr(ctx, "register_auxiliary_task"):
        try:
            ctx.register_auxiliary_task(
                hold.TASK, display_name="muster duplicate check",
                description="Judges whether a new error issue reports the same incident as a run already under way; "
                            "a cheap model is enough")
        except Exception as error:  # noqa: BLE001 - never stop the plugin loading: the check then fails open
            print(f"muster: auxiliary task {hold.TASK} not registered: {error}")
    gateway.setup(ctx)  # the configured channel's surfaces; config loads lazily, when Hermes connects the adapter
    if hasattr(ctx, "register_hook"):
        ctx.register_hook("pre_gateway_dispatch", gateway.on_dispatch)
