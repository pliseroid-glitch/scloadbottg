---
title: LoadIt Bot
emoji: 🎵
colorFrom: purple
colorTo: blue
sdk: docker
app_port: 7860
---

# LoadIt Bot

Telegram inline bot for downloading tracks from SoundCloud and fetching lyrics from Genius.

## Usage
- `@bot track name` — download from SoundCloud
- `@bot track name text` — get lyrics

## Environment Variables (set as HF Spaces secrets)
- `BOT_TOKEN` — Telegram bot token
- `STORAGE_CHAT_ID` — Private channel ID for audio storage
- `GENIUS_TOKEN` — Genius API access token
