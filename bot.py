"""Telegram text-to-speech bot.

Send text, a photo of text, or a PDF and the bot replies with MP3 audio.
Uses edge-tts (free, no API key) for synthesis.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import logging
import os
import re
import tempfile
from pathlib import Path

import edge_tts
import pytesseract
from dotenv import load_dotenv
from edge_tts.exceptions import NoAudioReceived
from pdf2image import convert_from_path
from PIL import Image
from pypdf import PdfReader
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
# PDF settings
PDF_MAX_CHARS = int(os.getenv("PDF_MAX_CHARS", "50000"))  # max characters read from one PDF
PART_CHARS = int(os.getenv("PART_CHARS", "8000"))  # characters per audio file for PDFs
OCR_MAX_PAGES = int(os.getenv("OCR_MAX_PAGES", "10"))  # page cap when a PDF is scanned
TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # bots can only download files up to 20 MB
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
    "Send me any text and I will send it back as an audio file.\n"
    "Send me a photo of text and I will read out what it says.\n"
    "Send me a PDF and I will turn it into audio. Add a caption like 5-10 to read only those pages.\n\n"
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


async def synthesize_and_reply(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    title: str | None = None,
    filename: str = "speech.mp3",
) -> bool:
    """Turn text into speech and reply with the audio file. Returns True on success."""
    voice = context.user_data.get("voice", DEFAULT_VOICE)
    rate = context.user_data.get("rate", "+0%")
    await context.bot.send_chat_action(message.chat_id, ChatAction.UPLOAD_VOICE)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "speech.mp3"
        try:
            await edge_tts.Communicate(text, voice, rate=rate).save(str(path))
        except NoAudioReceived:
            await message.reply_text("I could not turn that into speech. Try sending some plain text.")
            return False
        except Exception:
            log.exception("Synthesis failed")
            await message.reply_text("Something went wrong while creating the audio. Please try again.")
            return False

        with path.open("rb") as f:
            await message.reply_audio(
                audio=f,
                filename=filename,
                title=title or text[:60],
                performer="Text to Speech",
                read_timeout=60,
                write_timeout=60,
            )
    return True


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
    await synthesize_and_reply(message, context, text)


@restricted
async def image_to_speech(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    photo_or_doc = message.photo[-1] if message.photo else message.document
    if photo_or_doc is None:
        return

    await context.bot.send_chat_action(message.chat_id, ChatAction.TYPING)

    with tempfile.TemporaryDirectory() as tmp:
        img_path = Path(tmp) / "image.jpg"
        try:
            tg_file = await context.bot.get_file(photo_or_doc.file_id)
            await tg_file.download_to_drive(str(img_path))
            text = pytesseract.image_to_string(Image.open(img_path)).strip()
        except Exception:
            log.exception("OCR failed")
            await message.reply_text("I could not read that image. Try a clearer photo of the text.")
            return

    if not text:
        await message.reply_text("I could not find any text in that image.")
        return

    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
        await message.reply_text(
            f"The text was longer than {MAX_CHARS} characters, so I trimmed it before reading it aloud."
        )

    await message.reply_text(f"Text I found:\n\n{text}")
    await synthesize_and_reply(message, context, text)


# ------------------------------------------------------------------ pdf ---


class PDFError(Exception):
    """A problem with a PDF that is safe to show to the user."""


def parse_page_range(caption: str | None) -> tuple[int, int] | None:
    """Read '5-10', '5 to 10' or '7' (optionally prefixed with 'pages') from a caption."""
    if not caption:
        return None
    m = re.search(r"(\d+)\s*(?:-|to)\s*(\d+)", caption, re.I)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return min(a, b), max(a, b)
    m = re.fullmatch(r"\s*(?:pages?\s*)?(\d+)\s*", caption, re.I)
    if m:
        n = int(m.group(1))
        return n, n
    return None


def clean_pdf_text(text: str) -> str:
    """Tidy extracted text so it sounds natural when read aloud."""
    text = text.replace("\x00", "")
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)  # re-join hyphenated words
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"\n{2,}", "\n\n", text)
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)  # single line breaks become spaces
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def split_text(text: str, limit: int) -> list[str]:
    """Split text into parts of at most `limit` characters, preferring paragraph and sentence breaks."""
    pieces: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        while len(para) > limit:
            cut = para.rfind(". ", 0, limit)
            cut = cut + 1 if cut > limit // 2 else limit
            pieces.append(para[:cut].strip())
            para = para[cut:].strip()
        if para:
            pieces.append(para)

    parts: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + len(piece) + 2 > limit:
            parts.append(current)
            current = piece
        else:
            current = f"{current}\n\n{piece}" if current else piece
    if current:
        parts.append(current)
    return parts


def extract_pdf_text(path: Path, page_range: tuple[int, int] | None):
    """Blocking. Returns (text, first_page, last_page, total_pages, used_ocr)."""
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted and reader.decrypt("") == 0:
            raise PDFError("That PDF is password protected, so I cannot read it.")
        total = len(reader.pages)
    except PDFError:
        raise
    except Exception as exc:
        raise PDFError("I could not open that PDF. The file may be damaged.") from exc

    if total == 0:
        raise PDFError("That PDF has no pages.")

    start, end = 1, total
    if page_range:
        if page_range[0] > total:
            raise PDFError(f"That PDF only has {total} pages.")
        start, end = page_range[0], min(page_range[1], total)

    try:
        chunks = [reader.pages[i].extract_text() or "" for i in range(start - 1, end)]
    except Exception as exc:
        raise PDFError("I could not read the text in that PDF.") from exc
    text = clean_pdf_text("\n\n".join(chunks))
    if len(text) >= 20:
        return text, start, end, total, False

    # Almost no text found, so this is probably a scanned PDF. Fall back to OCR.
    ocr_end = min(end, start + OCR_MAX_PAGES - 1)
    try:
        images = convert_from_path(str(path), dpi=200, first_page=start, last_page=ocr_end)
        text = clean_pdf_text("\n\n".join(pytesseract.image_to_string(img) for img in images))
    except Exception as exc:
        raise PDFError("That PDF looks scanned and I could not run OCR on it.") from exc
    return text, start, ocr_end, total, True


@restricted
async def pdf_to_speech(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    doc = message.document
    if doc is None:
        return
    if doc.file_size and doc.file_size > TELEGRAM_DOWNLOAD_LIMIT:
        await message.reply_text("That PDF is larger than 20 MB, which is the most Telegram lets bots download.")
        return

    page_range = parse_page_range(message.caption)
    await context.bot.send_chat_action(message.chat_id, ChatAction.TYPING)

    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / "input.pdf"
        try:
            tg_file = await context.bot.get_file(doc.file_id)
            await tg_file.download_to_drive(str(pdf_path))
            text, first, last, total, used_ocr = await asyncio.to_thread(
                extract_pdf_text, pdf_path, page_range
            )
        except PDFError as exc:
            await message.reply_text(str(exc))
            return
        except Exception:
            log.exception("PDF handling failed")
            await message.reply_text("Something went wrong while reading that PDF. Please try again.")
            return

    if not text:
        await message.reply_text("I could not find any text in that PDF.")
        return

    notes = [f"Read pages {first} to {last} of {total}."]
    if used_ocr:
        notes.append("The PDF looks scanned, so I used OCR and the text may contain mistakes.")
        if last < (page_range[1] if page_range else total) and last < total:
            notes.append(f"OCR is limited to {OCR_MAX_PAGES} pages at a time.")
    if len(text) > PDF_MAX_CHARS:
        text = text[:PDF_MAX_CHARS]
        notes.append(
            f"The text was longer than {PDF_MAX_CHARS} characters, so I trimmed it. "
            "Send the PDF again with a caption like 11-20 to read other pages."
        )

    parts = split_text(text, PART_CHARS)
    noun = "audio file" if len(parts) == 1 else "audio files"
    await message.reply_text(" ".join(notes) + f" Creating {len(parts)} {noun}...")

    name = Path(doc.file_name or "document").stem[:40] or "document"
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    for i, part in enumerate(parts, start=1):
        label = f"{name} (part {i} of {len(parts)})" if len(parts) > 1 else name
        filename = f"{stem}_part{i}.mp3" if len(parts) > 1 else f"{stem}.mp3"
        if not await synthesize_and_reply(message, context, part, title=label, filename=filename):
            break


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
        .concurrent_updates(True)
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
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, image_to_speech))
    app.add_handler(MessageHandler(filters.Document.PDF, pdf_to_speech))
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
