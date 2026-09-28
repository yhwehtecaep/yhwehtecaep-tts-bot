# Telegram Text to Speech Bot

Send the bot any text message and it replies with an MP3 audio file you can listen to or download. No AI chat, no conversation, just text in and audio out.

Built with `python-telegram-bot` and `edge-tts` (free, no API key needed).

## Features

- Converts any text message to an MP3 file
- Per-user voice and speed settings that persist across restarts
- Menu of ready made voices, or use any edge-tts voice by name
- Optional allow list so only you can use it
- Docker support
- Ready to deploy on Render (webhook mode, `render.yaml` included)

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Clone this repo and enter the folder.
3. Create a virtual environment and install dependencies:

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

4. Copy the example environment file and add your token:

```bash
cp .env.example .env            # Windows: copy .env.example .env
```

5. Run the bot:

```bash
python bot.py
```

Open your bot in Telegram, send some text, and you will get an audio file back.

## Commands

| Command | What it does |
| --- | --- |
| `/voice` | Pick a voice from a menu |
| `/setvoice <name>` | Use any edge-tts voice, for example `/setvoice en-NG-EzinneNeural` |
| `/speed` | Choose slow, normal, fast or faster |
| `/id` | Show your Telegram user ID |
| `/help` | Show usage help |

## Configuration

All settings live in `.env`.

| Variable | Required | Description |
| --- | --- | --- |
| `BOT_TOKEN` | Yes | Token from BotFather |
| `DEFAULT_VOICE` | No | Default voice, `en-US-AriaNeural` if unset |
| `MAX_CHARS` | No | Max characters per message, default `10000` |
| `ALLOWED_USER_IDS` | No | Comma separated user IDs. Empty means everyone can use the bot |
| `DATA_DIR` | No | Folder for saved settings, default `data` |

To lock the bot to yourself, run it once, send `/id`, put the number in `ALLOWED_USER_IDS`, and restart.

To see every available voice, run:

```bash
edge-tts --list-voices
```

## Run with Docker

```bash
docker build -t tts-bot .
docker run -d --name tts-bot --env-file .env -v tts-bot-data:/app/data tts-bot
```

## Deploy on Render

The bot switches to webhook mode automatically when it runs on Render, so it works as a web service, including the free plan.

1. Push this repo to GitHub.
2. In Render, choose **New**, then **Blueprint**, and select your repo. Render reads `render.yaml`.
3. When prompted, enter `BOT_TOKEN`. Optionally enter `ALLOWED_USER_IDS` to keep the bot private.
4. Deploy. On startup the bot registers its webhook with Telegram using the `RENDER_EXTERNAL_URL` that Render provides.

You can also create a Web Service by hand: build command `pip install -r requirements.txt`, start command `python bot.py`, and add the same environment variables.

Things to know about the free plan:

- A free web service spins down after 15 minutes without traffic and takes about a minute to wake. The first message after a quiet period is delayed, and Telegram retries it, so it still gets answered.
- The filesystem is wiped on every spin down or redeploy, so saved voice and speed settings reset to the defaults.
- For an always-on bot, use the Starter web service ($7/month at the time of writing), or a Background Worker. A worker has no public URL, so the bot falls back to polling there automatically.

## Notes

- `edge-tts` uses Microsoft's online read aloud service through an unofficial client. It is great for personal projects, but it has no uptime guarantee. If you need something production grade, swap the synthesis call in `bot.py` for a paid provider such as ElevenLabs or Azure Speech.
- The bot uses long polling, so it needs no public URL or webhook. Any machine or VPS that can reach the internet will work.

## License

MIT
