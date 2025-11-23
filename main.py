import os
import time
import math
import asyncio
import logging
import re
import shutil
import subprocess
from datetime import datetime
from aiohttp import web
from pyrogram import Client, filters, idle
from pyrogram.types import Message
from bs4 import BeautifulSoup
import yt_dlp
import motor.motor_asyncio
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser

# --- CONFIGURATION (Load from Env) ---
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
MONGO_URI = os.environ.get("MONGO_URI", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
PORT = int(os.environ.get("PORT", 8080)) # Render provides this port

# --- LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("PowerExtractor")

# --- DATABASE ---
if MONGO_URI:
    mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
    db = mongo_client["tele_extractor_bot"]
    logger.info("Connected to MongoDB")
else:
    db = None
    logger.warning("MongoDB URI not found. Running in memory-only mode.")

# --- CLIENT ---
app = Client("extractor_session", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- STATE ---
user_states = {}
queue = []
is_processing = False

class UserState:
    def __init__(self):
        self.step = "idle"
        self.file_path = None
        self.batch_name = None
        self.caption_prefix = None
        self.quality = None
        self.urls = []

# --- HELPERS ---

def human_readable_size(size, decimal_places=2):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            return f"{size:.{decimal_places}f} {unit}"
        size /= 1024.0
    return f"{size:.{decimal_places}f} PB"

async def progress_bar(current, total, status_msg, start_time, process_type="Uploading"):
    now = time.time()
    diff = now - start_time
    if diff < 1: return
    
    if total == 0: return

    percentage = current * 100 / total
    speed = current / diff
    eta = (total - current) / speed if speed > 0 else 0
    
    try:
        await status_msg.edit_text(
            f"**{process_type}**\n"
            f"📊 **Progress:** {percentage:.2f}%\n"
            f"💾 **Size:** {human_readable_size(current)} / {human_readable_size(total)}\n"
            f"🚀 **Speed:** {human_readable_size(speed)}/s\n"
            f"⏳ **ETA:** {round(eta)}s"
        )
    except Exception:
        pass

def get_thumbnail(video_path, output_path):
    try:
        subprocess.call([
            "ffmpeg", "-i", video_path, "-ss", "00:00:01", "-vframes", "1", output_path
        ])
        return output_path if os.path.exists(output_path) else None
    except:
        return None

def get_duration(video_path):
    try:
        metadata = extractMetadata(createParser(video_path))
        return metadata.get('duration').seconds if metadata and metadata.has('duration') else 0
    except:
        return 0

# --- CORE HANDLERS ---

@app.on_message(filters.command("start"))
async def start(client, message):
    await message.reply_text(
        f"👋 **Hello {message.from_user.first_name}!**\n\n"
        "I am a Power Extractor Bot (Web Service Mode).\n"
        "Send me an **HTML** or **TXT** file to start extracting content."
    )

@app.on_message(filters.document & filters.private)
async def handle_document(client, message: Message):
    if ADMIN_ID and message.from_user.id != ADMIN_ID:
        return await message.reply("🔒 Authorized personnel only.")

    file_name = message.document.file_name
    if not (file_name.endswith(".html") or file_name.endswith(".txt")):
        return await message.reply("❌ Invalid file type. Send .html or .txt")

    status = await message.reply("📥 Downloading file map...")
    file_path = await message.download()
    
    # Parse Links
    urls = []
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
            if file_name.endswith(".html"):
                soup = BeautifulSoup(content, "html.parser")
                for link in soup.find_all("a"):
                    href = link.get("href")
                    if href: 
                        name = link.get_text(strip=True) or "Untitled"
                        urls.append((name, href))
            else:
                # Basic TXT parsing (Line by line or regex)
                raw_urls = re.findall(r'(https?://\S+)', content)
                urls = [(f"Link {i+1}", u) for i, u in enumerate(raw_urls)]
    except Exception as e:
        return await status.edit(f"❌ Error parsing file: {e}")

    if not urls:
        os.remove(file_path)
        return await status.edit("❌ No links found.")

    # Init State
    user_states[message.from_user.id] = UserState()
    state = user_states[message.from_user.id]
    state.urls = urls
    state.file_path = file_path
    state.step = "ask_batch"
    
    await status.delete()
    await message.reply(
        f"✅ Found **{len(urls)}** links.\n\n"
        "**Step 1:** Enter a **Batch Name** (This will be the main title):"
    )

@app.on_message(filters.text & filters.private)
async def handle_inputs(client, message: Message):
    user_id = message.from_user.id
    if user_id not in user_states: return

    state = user_states[user_id]

    if state.step == "ask_batch":
        state.batch_name = message.text
        state.step = "ask_caption"
        await message.reply("✅ Name set.\n\n**Step 2:** Enter **Extra Caption** (or type 'skip' to ignore):")
        return

    if state.step == "ask_caption":
        state.caption_prefix = "" if message.text.lower() == "skip" else message.text
        state.step = "ask_quality"
        await message.reply(
            "✅ Caption set.\n\n**Step 3:** Enter **Max Quality** (e.g., 360, 480, 720, 1080).\n"
            "I will download the best available quality up to this limit."
        )
        return

    if state.step == "ask_quality":
        # Extract number from text
        q_match = re.search(r'\d+', message.text)
        state.quality = int(q_match.group(0)) if q_match else 720
        
        state.step = "processing"
        await message.reply(f"🚀 **Starting Extraction!**\nQuality Limit: {state.quality}p\nQueue Size: {len(state.urls)}")
        
        # Add to global queue and try to process
        global queue
        queue.append(user_id)
        if not is_processing:
            asyncio.create_task(process_queue(client))

# --- PROCESSOR ---

async def process_queue(client):
    global is_processing
    is_processing = True

    while queue:
        user_id = queue[0]
        state = user_states.get(user_id)
        if not state: 
            queue.pop(0)
            continue
            
        await process_user_batch(client, user_id, state)
        
        # Cleanup
        if state.file_path and os.path.exists(state.file_path):
            os.remove(state.file_path)
        del user_states[user_id]
        queue.pop(0)
    
    is_processing = False

async def process_user_batch(client, user_id, state):
    total = len(state.urls)
    
    for index, (link_name, link_url) in enumerate(state.urls):
        msg = await client.send_message(user_id, f"⬇️ **Downloading ({index+1}/{total})**\n`{link_name}`")
        
        # --- DOWNLOAD ---
        out_name = f"downloads/{user_id}_{index}_%(title)s.%(ext)s"
        ydl_opts = {
            'format': f"bestvideo[height<={state.quality}]+bestaudio/best[height<={state.quality}]/best",
            'outtmpl': out_name,
            'quiet': True,
            'noplaylist': True,
            'merge_output_format': 'mp4',
        }

        fpath = None
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(link_url, download=True)
                fpath = ydl.prepare_filename(info)
        except Exception as e:
            if ".pdf" in link_url or ".doc" in link_url:
                fpath = f"downloads/{user_id}_{index}_document.pdf"
                subprocess.call(['wget', '-O', fpath, link_url])
            else:
                await msg.edit(f"❌ Failed: {link_name}\n`{str(e)[:50]}`")
                continue

        if not fpath or not os.path.exists(fpath):
            await msg.edit(f"❌ Download Failed: {link_name}")
            continue

        # --- SPLIT CHECK (2GB Limit) ---
        file_size = os.path.getsize(fpath)
        limit = 2000 * 1024 * 1024 
        
        upload_files = [] 

        if file_size > limit:
            await msg.edit(f"✂️ **Splitting File** ({human_readable_size(file_size)})...")
            split_dir = f"downloads/split_{user_id}_{index}"
            os.makedirs(split_dir, exist_ok=True)
            
            if fpath.endswith((".mp4", ".mkv", ".webm")):
                output_pattern = f"{split_dir}/part_%03d.mp4"
                subprocess.call([
                    "ffmpeg", "-i", fpath, "-c", "copy", "-map", "0",
                    "-f", "segment", "-segment_time", "1800", "-reset_timestamps", "1", output_pattern
                ])
            else:
                subprocess.call(f"split -b 1900M '{fpath}' '{split_dir}/part_'", shell=True)
            
            for r, d, f in os.walk(split_dir):
                for file in sorted(f):
                    upload_files.append(os.path.join(r, file))
        else:
            upload_files.append(fpath)

        # --- UPLOAD ---
        for i, up_path in enumerate(upload_files):
            part_txt = f" [Part {i+1}/{len(upload_files)}]" if len(upload_files) > 1 else ""
            caption = (
                f"**{state.batch_name}**\n"
                f"📝 {link_name}{part_txt}\n"
                f"{state.caption_prefix}"
            )

            await msg.edit(f"⬆️ **Uploading**{part_txt}...")
            start_time = time.time()
            
            try:
                if up_path.endswith((".mp4", ".mkv")):
                    thumb = get_thumbnail(up_path, f"{up_path}.jpg")
                    duration = get_duration(up_path)
                    await client.send_video(
                        user_id,
                        video=up_path,
                        caption=caption,
                        thumb=thumb,
                        duration=duration,
                        supports_streaming=True,
                        progress=progress_bar,
                        progress_args=(msg, start_time, "Uploading Video")
                    )
                    if thumb: os.remove(thumb)
                else:
                    await client.send_document(
                        user_id,
                        document=up_path,
                        caption=caption,
                        progress=progress_bar,
                        progress_args=(msg, start_time, "Uploading File")
                    )
            except Exception as e:
                logger.error(f"Upload failed: {e}")
                await client.send_message(user_id, f"❌ Upload Error: {str(e)}")
            
            if up_path != fpath: 
                os.remove(up_path)

        if os.path.exists(fpath): os.remove(fpath)
        split_d = f"downloads/split_{user_id}_{index}"
        if os.path.exists(split_d): shutil.rmtree(split_d)
        
        await msg.delete()

    await client.send_message(user_id, "✅ **Batch Extraction Completed Successfully!**")

# --- SERVER ---
async def web_server():
    async def handle(request):
        return web.Response(text="Bot is Running!")

    app_web = web.Application()
    app_web.router.add_get("/", handle)
    return app_web

async def start_services():
    print("🤖 Initializing Bot...")
    await app.start()
    print("✅ Bot Started")
    
    print(f"🌍 Starting Web Server on port {PORT}...")
    runner = web.AppRunner(await web_server())
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print("✅ Web Server Running")
    
    await idle()
    await app.stop()

if __name__ == "__main__":
    if not os.path.exists("downloads"):
        os.makedirs("downloads")
    
    # Run loop
    loop = asyncio.get_event_loop()
    loop.run_until_complete(start_services())
      
