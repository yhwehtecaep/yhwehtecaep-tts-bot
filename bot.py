"""Telegram text-to-speech bot.

Send any text message and the bot replies with an MP3 audio file.
Uses edge-tts (free, no API key) for synthesis.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import os
import tempfile
from pathlib import Path

import edge_tts
from dotenv import load_dotenv
from edge_tts.exceptions import NoAudioReceived
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
    filters,
)

load_dotenv()

# ---------------------------------------------------------------- config ---

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DEFAULT_VOICE = os.getenv("DEFAULT_VOICE", "en-US-AriaNeural")
MAX_CHARS = int(os.getenv("MAX_CHARS", "10000"))
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
# Webhook mode (used on Render). Render sets RENDER_EXTERNAL_URL and PORT automatically.
# If no public URL is found, the bot falls back to long polling.
PORT = int(os.getenv("PORT", "8080"))
WEBHOOK_BASE_URL = (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL", "")).strip()
# Comma separated Telegram user IDs. Leave empty to allow everyone.
ALLOWED_USER_IDS = {
    int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x
}

VOICES = [
    ("US Female (Aria)", "en-US-AriaNeural"),
    ("US Male (Guy)", "en-US-GuyNeural"),
    ("UK Female (Sonia)", "en-GB-SoniaNeural"),
    ("UK Male (Ryan)", "en-GB-RyanNeural"),
    ("Nigerian Female (Ezinne)", "en-NG-EzinneNeural"),
    ("Nigerian Male (Abeo)", "en-NG-AbeoNeural"),
    ("Australian Female (Natasha)", "en-AU-NatashaNeural"),
    ("Indian Female (Neerja)", "en-IN-NeerjaNeural"),
    ("French (Denise)", "fr-FR-DeniseNeural"),
    ("Spanish (Elvira)", "es-ES-ElviraNeural"),
    ("German (Katja)", "de-DE-KatjaNeural"),
    ("Portuguese BR (Francisca)", "pt-BR-FranciscaNeural"),
]
RATES = [("Slow", "-25%"), ("Normal", "+0%"), ("Fast", "+25%"), ("Faster", "+50%")]

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("tts-bot")

_voice_names: dict[str, str] | None = None  # lowercase -> real short name


# --------------------------------------------------------------- helpers ---


def is_allowed(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    user = update.effective_user
    return user is not None and user.id in ALLOWED_USER_IDS


def restricted(handler):
    """Block users who are not in ALLOWED_USER_IDS (when the list is set)."""

    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_allowed(update):
            if update.callback_query:
                await update.callback_query.answer("This bot is private.", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text("Sorry, this bot is private.")
            return
        await handler(update, context)

    return wrapper


async def get_voice_names() -> dict[str, str]:
    global _voice_names
    if _voice_names is None:
        voices = await edge_tts.list_voices()
        _voice_names = {v["ShortName"].lower(): v["ShortName"] for v in voices}
    return _voice_names


def two_column(buttons: list[InlineKeyboardButton]) -> list[list[InlineKeyboardButton]]:
    return [buttons[i : i + 2] for i in range(0, len(buttons), 2)]


# -------------------------------------------------------------- commands ---

HELP_TEXT = (
    "Send me any text and I will send it back as an audio file.\n\n"
    "Commands:\n"
    "/voice - pick a voice from a menu\n"
    "/setvoice <name> - use any edge-tts voice, e.g. /setvoice en-US-AriaNeural\n"
    "/speed - change the speaking speed\n"
    "/id - show your Telegram user ID\n"
    "/help - show this message"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hi! " + HELP_TEXT)


@restricted
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your Telegram user ID is {update.effective_user.id}")


@restricted
async def voice_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buttons = [
        InlineKeyboardButton(label, callback_data=f"voice:{name}") for label, name in VOICES
    ]
    current = context.user_data.get("voice", DEFAULT_VOICE)
    await update.message.reply_text(
        f"Current voice: {current}\nChoose a voice:",
        reply_markup=InlineKeyboardMarkup(two_column(buttons)),
    )


@restricted
async def speed_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buttons = [InlineKeyboardButton(label, callback_data=f"rate:{value}") for label, value in RATES]
    current = context.user_data.get("rate", "+0%")
    await update.message.reply_text(
        f"Current speed: {current}\nChoose a speed:",
        reply_markup=InlineKeyboardMarkup(two_column(buttons)),
    )


@restricted
async def set_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: /setvoice en-US-AriaNeural\n"
            "Full voice list: https://gist.github.com/BettyJJ/17cbaa1de96235a7f5773b8690a20462"
        )
        return
    try:
        names = await get_voice_names()
    except Exception:
        log.exception("Could not fetch voice list")
        await update.message.reply_text("Could not check the voice list right now. Try again shortly.")
        return
    match = names.get(context.args[0].lower())
    if not match:
        await update.message.reply_text("I could not find that voice. Check the spelling and try again.")
        return
    context.user_data["voice"] = match
    await update.message.reply_text(f"Voice set to {match}.")


@restricted
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kind, _, value = (query.data or "").partition(":")
    if kind == "voice" and value in {v for _, v in VOICES}:
        context.user_data["voice"] = value
        await query.edit_message_text(f"Voice set to {value}.")
    elif kind == "rate" and value in {r for _, r in RATES}:
        context.user_data["rate"] = value
        await query.edit_message_text(f"Speed set to {value}.")


# ------------------------------------------------------------ main logic ---


@restricted
async def speak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    text = (message.text or "").strip()
    if not text:
        return
    if len(text) > MAX_CHARS:
        await message.reply_text(
            f"That message is {len(text)} characters. The limit is {MAX_CHARS}. Please send it in smaller parts."
        )
        return

    voice = context.user_data.get("voice", DEFAULT_VOICE)
    rate = context.user_data.get("rate", "+0%")
    await context.bot.send_chat_action(message.chat_id, ChatAction.UPLOAD_VOICE)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "speech.mp3"
        try:
            await edge_tts.Communicate(text, voice, rate=rate).save(str(path))
        except NoAudioReceived:
            await message.reply_text("I could not turn that into speech. Try sending some plain text.")
            return
        except Exception:
            log.exception("Synthesis failed")
            await message.reply_text("Something went wrong while creating the audio. Please try again.")
            return

        with path.open("rb") as f:
            await message.reply_audio(
                audio=f,
                filename="speech.mp3",
                title=text[:60],
                performer="Text to Speech",
            )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error", exc_info=context.error)


async def post_init(app: Application):
    await app.bot.set_my_commands(
        [
            BotCommand("voice", "Pick a voice"),
            BotCommand("speed", "Change speaking speed"),
            BotCommand("setvoice", "Use any edge-tts voice by name"),
            BotCommand("id", "Show your Telegram user ID"),
            BotCommand("help", "How to use this bot"),
        ]
    )


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is missing. Copy .env.example to .env and add your token.")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    persistence = PicklePersistence(filepath=DATA_DIR / "bot_data.pkl")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .persistence(persistence)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("id", my_id))
    app.add_handler(CommandHandler("voice", voice_menu))
    app.add_handler(CommandHandler("speed", speed_menu))
    app.add_handler(CommandHandler("setvoice", set_voice))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, speak))
    app.add_error_handler(on_error)

    if WEBHOOK_BASE_URL:
        # Telegram only accepts letters, digits, "_" and "-" in the secret token,
        # so derive a safe one from the bot token.
        secret = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()
        url = f"{WEBHOOK_BASE_URL.rstrip('/')}/telegram"
        log.info("Starting in webhook mode on port %s", PORT)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="telegram",
            webhook_url=url,
            secret_token=secret,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        log.info("Starting in polling mode. Press Ctrl+C to stop.")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
