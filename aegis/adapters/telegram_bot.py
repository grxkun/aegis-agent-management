"""
Telegram Approval Bot — Operator Interface for HITL Escalations.

When the HITLManager escalates a request, this adapter formats a rich
message with inline keyboard buttons and sends it to the designated
operator chat.  Callback queries from the [ APPROVE ] / [ REJECT ]
buttons are routed back to ``HITLManager.resolve()``.

Architecture:
    The bot runs in *webhook mode* alongside FastAPI rather than long-polling,
    so it shares the same async event loop and process.  Telegram pushes
    updates to ``/api/v1/telegram/webhook``, which FastAPI forwards to
    the bot's internal dispatcher.

Setup:
    1. Create a bot via @BotFather and get the token.
    2. Set ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_OPERATOR_CHAT_ID`` in ``.env``.
    3. Register the webhook URL with Telegram (done automatically on startup
       if ``TELEGRAM_WEBHOOK_URL`` is set).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

if TYPE_CHECKING:
    from aegis.core.hitl import EscalationRequest, HITLManager
    from aegis.core.kaizen import KaizenEngine
    from aegis.policy.optimizer import PolicyOptimizer

logger = logging.getLogger("aegis.telegram")


class TelegramApprovalBot:
    """
    Sends HITL escalation prompts and processes operator decisions.

    Parameters
    ----------
    token : str
        Telegram Bot API token.
    operator_chat_id : int | str
        Chat ID of the human operator who receives approval requests.
    hitl_manager : HITLManager
        Reference to resolve pending approvals when buttons are clicked.
    webhook_url : str | None
        If provided, the bot registers this URL with Telegram on startup.
    """

    def __init__(
        self,
        token: str,
        operator_chat_id: int | str,
        hitl_manager: HITLManager,
        webhook_url: Optional[str] = None,
        kaizen_engine: Optional["KaizenEngine"] = None,
        policy_optimizer: Optional["PolicyOptimizer"] = None,
    ) -> None:
        self._token = token
        self._chat_id = int(operator_chat_id)
        self._hitl = hitl_manager
        self._webhook_url = webhook_url
        self._kaizen = kaizen_engine
        self._optimizer = policy_optimizer

        self._app: Application = (
            Application.builder()
            .token(token)
            .build()
        )
        self._register_handlers()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def setup(self) -> None:
        """Initialise the bot and optionally register the webhook."""
        await self._app.initialize()
        await self._app.start()

        if self._webhook_url:
            await self._app.bot.set_webhook(url=self._webhook_url)
            logger.info("Telegram webhook registered: %s", self._webhook_url)
        else:
            logger.info("Telegram bot initialised (no webhook URL — manual mode)")

    async def shutdown(self) -> None:
        """Gracefully stop the bot."""
        await self._app.stop()
        await self._app.shutdown()

    async def process_update(self, payload: dict) -> None:
        """
        Feed a raw Telegram update (from the FastAPI webhook endpoint)
        into the bot's dispatcher.
        """
        update = Update.de_json(payload, self._app.bot)
        await self._app.process_update(update)

    # ------------------------------------------------------------------
    # Send escalation prompt
    # ------------------------------------------------------------------

    async def send_escalation(self, esc: "EscalationRequest") -> None:
        """
        Send a formatted approval request to the operator.

        Message format:
            Header with agent ID, intent, cost, and escalation reasons,
            followed by [ APPROVE ] and [ REJECT ] inline buttons.
        """
        reasons_text = "\n".join(f"  - {r}" for r in esc.escalation_reasons)

        text = (
            f"\U0001f6a8 *HITL Escalation Required*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"\U0001f916 *Agent ID:* `{esc.agent_id}`\n"
            f"\U0001f3af *Intent:* {esc.task_intent}\n"
            f"\U0001f4b0 *Requested Budget:* ${esc.estimated_cost_usd:.2f}\n"
            f"\U0001f4ca *Priority:* {esc.priority}/10\n"
            f"\U0001f9e0 *Confidence:* {esc.confidence:.0%}\n"
            f"\n"
            f"\u26a0\ufe0f *Reason for Escalation:*\n{reasons_text}\n"
            f"\n"
            f"\U0001f510 Approval ID: `{esc.approval_id}`"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "\u2705 APPROVE",
                    callback_data=f"hitl:approve:{esc.approval_id}",
                ),
                InlineKeyboardButton(
                    "\u274c REJECT",
                    callback_data=f"hitl:reject:{esc.approval_id}",
                ),
            ]
        ])

        await self._app.bot.send_message(
            chat_id=self._chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        logger.info(
            "Escalation prompt sent to chat=%s for approval_id=%s",
            self._chat_id, esc.approval_id,
        )

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Kaizen summary
    # ------------------------------------------------------------------

    async def send_kaizen_summary(self) -> None:
        """
        Send the 24-hour Kaizen digest to the operator with
        [ COMMIT CHANGES ] and [ ROLLBACK ] buttons.
        """
        if not self._kaizen:
            logger.warning("Kaizen engine not wired — skipping summary.")
            return

        summary = await self._kaizen.generate_kaizen_summary()
        text = self._kaizen.format_telegram_summary(summary)

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "\u2705 COMMIT CHANGES",
                    callback_data="kaizen:commit",
                ),
                InlineKeyboardButton(
                    "\U0001f504 ROLLBACK",
                    callback_data="kaizen:rollback",
                ),
            ]
        ])

        await self._app.bot.send_message(
            chat_id=self._chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        logger.info("Kaizen summary sent to operator chat=%s", self._chat_id)

    def _register_handlers(self) -> None:
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("pending", self._cmd_pending))
        self._app.add_handler(CommandHandler("kaizen", self._cmd_kaizen))
        self._app.add_handler(
            CallbackQueryHandler(self._on_approval_callback, pattern=r"^hitl:")
        )
        self._app.add_handler(
            CallbackQueryHandler(self._on_kaizen_callback, pattern=r"^kaizen:")
        )

    async def _cmd_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await update.message.reply_text(
            "\U0001f6e1\ufe0f *Aegis Approval Bot*\n\n"
            "I'll send you agent requests that need human approval.\n"
            "Commands:\n"
            "  /pending — List requests awaiting your decision\n"
            "  /kaizen — View the latest Kaizen summary",
            parse_mode="Markdown",
        )

    async def _cmd_pending(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List all pending escalations."""
        pending = await self._hitl.get_pending()
        if not pending:
            await update.message.reply_text("\u2705 No pending escalations.")
            return

        lines = []
        for p in pending:
            lines.append(
                f"- `{p.get('approval_id', '?')[:8]}...` | "
                f"{p.get('agent_id', '?')} | "
                f"${p.get('estimated_cost_usd', 0):.2f}"
            )
        await update.message.reply_text(
            f"\u23f3 *Pending Escalations ({len(pending)}):*\n" + "\n".join(lines),
            parse_mode="Markdown",
        )

    async def _on_approval_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle APPROVE / REJECT button clicks."""
        query = update.callback_query
        await query.answer()  # Acknowledge the button press immediately.

        data = query.data  # e.g. "hitl:approve:abc123def456"
        parts = data.split(":", 2)
        if len(parts) != 3:
            await query.edit_message_text("\u274c Malformed callback data.")
            return

        _, action, approval_id = parts
        approved = action == "approve"
        decided_by = query.from_user.username or str(query.from_user.id)

        resolved = await self._hitl.resolve(
            approval_id=approval_id,
            approved=approved,
            decided_by=decided_by,
        )

        if resolved:
            status_emoji = "\u2705" if approved else "\u274c"
            status_text = "APPROVED" if approved else "REJECTED"
            await query.edit_message_text(
                f"{status_emoji} *{status_text}* by @{decided_by}\n"
                f"\U0001f510 `{approval_id}`",
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(
                f"\u23f0 This escalation has already expired or been resolved.\n"
                f"\U0001f510 `{approval_id}`",
                parse_mode="Markdown",
            )

    async def _cmd_kaizen(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Send the Kaizen summary on demand."""
        if not self._kaizen:
            await update.message.reply_text("Kaizen engine is not configured.")
            return
        await self.send_kaizen_summary()

    async def _on_kaizen_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle COMMIT / ROLLBACK button clicks from Kaizen summaries."""
        query = update.callback_query
        await query.answer()

        data = query.data  # "kaizen:commit" or "kaizen:rollback"
        _, action = data.split(":", 1)
        decided_by = query.from_user.username or str(query.from_user.id)

        if not self._kaizen:
            await query.edit_message_text("\u274c Kaizen engine unavailable.")
            return

        if action == "commit":
            committed = await self._kaizen.commit_staged_changes(
                optimizer=self._optimizer
            )
            await query.edit_message_text(
                f"\u2705 *{len(committed)} changes committed* by @{decided_by}\n"
                f"Policy updated and evolution log written.",
                parse_mode="Markdown",
            )
        elif action == "rollback":
            count = await self._kaizen.rollback_staged_changes()
            await query.edit_message_text(
                f"\U0001f504 *{count} pending changes rolled back* by @{decided_by}\n"
                f"No policy changes applied.",
                parse_mode="Markdown",
            )
