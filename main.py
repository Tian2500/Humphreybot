import os
import asyncio
import discord
from discord.ext import commands
import yt_dlp

from flask import Flask
from threading import Thread

# ------------- CONFIG -------------

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = "!"

if TOKEN is None:
    raise RuntimeError("DISCORD_TOKEN env var not set!")

intents = discord.Intents.default()
intents.message_content = True  # important for reading commands

bot = commands.Bot(command_prefix=PREFIX, intents=intents)

# Per-guild queues: {guild_id: [song_dict, ...]}
music_queues = {}

# yt-dlp options
YTDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
    "extract_flat": False,
}

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTS)


# ------------- QUEUE HELPERS -------------

def get_guild_queue(guild_id: int):
    if guild_id not in music_queues:
        music_queues[guild_id] = []
    return music_queues[guild_id]


def add_to_queue(guild_id: int, song: dict):
    queue = get_guild_queue(guild_id)
    queue.append(song)


async def create_audio_source(url: str):
    """Create a discord audio source from a YouTube URL."""
    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(
            None, lambda: ytdl.extract_info(url, download=False)
        )
    except Exception as e:
        print(f"yt-dlp error in create_audio_source: {e}")
        return None

    if data is None:
        return None

    if "entries" in data:
        data = data["entries"][0]

    audio_url = data.get("url")
    if not audio_url:
        return None

    return discord.FFmpegPCMAudio(audio_url, **FFMPEG_OPTS)


async def play_next_in_queue(ctx: commands.Context):
    """Play the next song in the guild's queue."""
    voice_client = ctx.voice_client
    if not voice_client or not voice_client.is_connected():
        return

    queue = get_guild_queue(ctx.guild.id)
    if not queue:
        await ctx.send("Queue ended ✅")
        return

    song = queue.pop(0)
    url = song["url"]
    title = song["title"]

    source = await create_audio_source(url)

    if source is None:
        await ctx.send(f"Could not play **{title}** 😢")
        # Try next song
        await play_next_in_queue(ctx)
        return

    def after_playing(err):
        if err:
            print(f"Player error: {err}")
        fut = asyncio.run_coroutine_threadsafe(play_next_in_queue(ctx), bot.loop)
        try:
            fut.result()
        except Exception as e:
            print(f"Error in after_playing: {e}")

    voice_client.play(source, after=after_playing)
    asyncio.run_coroutine_threadsafe(
        ctx.send(f"🎶 Now playing: **{title}**"), bot.loop
    )


# ------------- BOT EVENTS -------------

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")


# ------------- COMMANDS -------------

@bot.command(name="play", aliases=["p"])
async def play(ctx: commands.Context, *, query: str):
    """Play a song from a YouTube URL or search term."""
    # Ensure user is in a voice channel
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("You need to be in a voice channel first!")
        return

    voice_channel = ctx.author.voice.channel

    # Connect or move to the voice channel
    if not ctx.voice_client:
        await voice_channel.connect()
    elif ctx.voice_client.channel != voice_channel:
        await ctx.voice_client.move_to(voice_channel)

    loop = asyncio.get_event_loop()

    # Determine if query is a URL or search term
    try:
        if query.startswith("http://") or query.startswith("https://"):
            # Direct URL
            data = await loop.run_in_executor(
                None, lambda: ytdl.extract_info(query, download=False)
            )
        else:
            # Search on YouTube
            search = f"ytsearch:{query}"
            res = await loop.run_in_executor(
                None, lambda: ytdl.extract_info(search, download=False)
            )
            data = res["entries"][0]  # first result

        if "entries" in data:
            data = data["entries"][0]

    except Exception as e:
        print(f"yt-dlp error in play command: {e}")
        await ctx.send("❌ Something went wrong while searching/downloading.")
        return

    url = data.get("webpage_url", None) or data.get("url")
    title = data.get("title", "Unknown title")

    if not url:
        await ctx.send("❌ Could not get a playable URL.")
        return

    # Add to queue
    add_to_queue(ctx.guild.id, {"url": url, "title": title})
    await ctx.send(f"➕ Added to queue: **{title}**")

    # If nothing is playing, start playback
    voice_client = ctx.voice_client
    if not voice_client.is_playing():
        await play_next_in_queue(ctx)


@bot.command(name="pause")
async def pause(ctx: commands.Context):
    """Pause the current song."""
    vc = ctx.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await ctx.send("⏸ Paused")
    else:
        await ctx.send("Nothing is playing right now.")


@bot.command(name="resume")
async def resume(ctx: commands.Context):
    """Resume a paused song."""
    vc = ctx.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await ctx.send("▶ Resumed")
    else:
        await ctx.send("Nothing is paused.")


@bot.command(name="skip", aliases=["s"])
async def skip(ctx: commands.Context):
    """Skip the current song."""
    vc = ctx.voice_client
    if vc and vc.is_playing():
        vc.stop()  # triggers after_playing callback
        await ctx.send("⏭ Skipped")
    else:
        await ctx.send("Nothing to skip.")


@bot.command(name="queue", aliases=["q"])
async def show_queue(ctx: commands.Context):
    """Show the current queue."""
    queue = get_guild_queue(ctx.guild.id)
    if not queue:
        await ctx.send("🧺 The queue is empty.")
        return

    lines = []
    for i, song in enumerate(queue, start=1):
        lines.append(f"{i}. {song['title']}")

    msg = "📜 **Current queue:**\n" + "\n".join(lines)
    await ctx.send(msg)


@bot.command(name="leave", aliases=["disconnect", "dc"])
async def leave(ctx: commands.Context):
    """Disconnect the bot from the voice channel."""
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("👋 Left the voice channel.")
    else:
        await ctx.send("I'm not in a voice channel.")


# ------------- KEEP-ALIVE WEB SERVER (for UptimeRobot) -------------

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive!", 200


def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


def run_bot():
    bot.run(TOKEN)


if __name__ == "__main__":
    # Run Flask in a separate thread, bot in main thread
    web_thread = Thread(target=run_web)
    web_thread.daemon = True
    web_thread.start()

    run_bot()
