import os, time, sqlite3, asyncio, httpx, base64, re, zipfile
from io import BytesIO
from dotenv import load_dotenv
from telegram import Update, InputFile
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from gtts import gTTS
import speech_recognition as sr
import googleapiclient.discovery
from flask import Flask
import threading

# ================== ENV ==================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
SERP_API_KEY = os.getenv("SERP_API_KEY")
# ================== ADMIN ==================
ADMIN_IDS = list(
    map(int, os.getenv("ADMIN_IDS", "").split(","))
)

def is_admin(uid):
    return int(uid) in ADMIN_IDS



# ================== MULTI API KEYS ==================
OPENROUTER_KEYS = [
    os.getenv("OPENROUTER_API_1"),
    os.getenv("OPENROUTER_API_2"),
    os.getenv("OPENROUTER_API_3"),
    os.getenv("OPENROUTER_API_4"),
    os.getenv("OPENROUTER_API_5"),
    os.getenv("OPENROUTER_API_6"),
    os.getenv("OPENROUTER_API_7"),
    os.getenv("OPENROUTER_API_8")
]


ELEVEN_KEYS = [
    os.getenv("ELEVEN_API_1"),
    os.getenv("ELEVEN_API_2"),
    os.getenv("ELEVEN_API_3")
]
ELEVEN_KEYS = [k for k in ELEVEN_KEYS if k]

ELEVEN_VOICES = {
    "priya": "EXAVITQu4vr4xnSDxMaL",
    "rose": "VR6AewLTigWG4xSOukaG"
}

async def eleven_tts(text, voice_name="priya"):
    if not ELEVEN_KEYS:
        print("❌ No ElevenLabs keys found")
        return None

    voice_id = ELEVEN_VOICES.get(voice_name, ELEVEN_VOICES["priya"])
    clean = re.sub(r"```.*?```", "Code attached.", text, flags=re.DOTALL)[:800]

    for api_key in ELEVEN_KEYS:
        try:
            headers = {
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg"
            }

            payload = {
                "text": clean,
                "model_id": "eleven_monolingual_v1",
                "voice_settings": {
                    "stability": 0.35,
                    "similarity_boost": 0.75
                }
            }

            async with httpx.AsyncClient(timeout=40) as client:
                r = await client.post(
                    f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                    headers=headers,
                    json=payload
                )

            print("🎤 ElevenLabs status:", r.status_code)

            if r.status_code == 200:
                bio = BytesIO(r.content)
                bio.seek(0)
                print("🎤 ElevenLabs voice used")
                return bio
            else:
                print("❌ ElevenLabs error:", r.status_code, r.text[:200])

        except Exception as e:
            print("🔥 ElevenLabs exception:", e)

    return None

OPENROUTER_KEYS = [k for k in OPENROUTER_KEYS if k]

if not OPENROUTER_KEYS:
    print("❌ No OpenRouter API keys found!")
    exit(1)


MODEL_NAME = "openai/gpt-4o-mini"

SYSTEM_PROMPT = """You are Priya — a cute, friendly Indian girl AI best-friend.
Use Hinglish, polite, supportive, emojis .
If the user asks for code, provide it clearly.
If the user asks for videos, discuss the YouTube links provided in the context.
No adult, violent, illegal content.
"""

# ================== DB ==================
conn = sqlite3.connect("memory.db", check_same_thread=False)
cur = conn.cursor()

BOT_UPDATING = False

cur.execute("""CREATE TABLE IF NOT EXISTS memory(
id INTEGER PRIMARY KEY,user_id TEXT,role TEXT,content TEXT,ts INTEGER)""")
cur.execute("""CREATE TABLE IF NOT EXISTS profile(
user_id TEXT PRIMARY KEY,name TEXT,voice_mode INTEGER DEFAULT 0)""")

conn.commit()

def ensure_voice_columns():
    try:
        cur.execute("ALTER TABLE profile ADD COLUMN voice_engine TEXT DEFAULT 'gtts'")
    except:
        pass
    try:
        cur.execute("ALTER TABLE profile ADD COLUMN voice_name TEXT DEFAULT ''")
    except:
        pass
    conn.commit()

ensure_voice_columns()

cur.execute("""
CREATE TABLE IF NOT EXISTS bans(
    user_id TEXT PRIMARY KEY,
    reason TEXT,
    ts INTEGER
)
""")
conn.commit()


conn.commit()

# ================== MEMORY ==================
def save_msg(uid, role, content):
    cur.execute("INSERT INTO memory(user_id,role,content,ts) VALUES(?,?,?,?)",
                (str(uid), role, content, int(time.time())))
    cur.execute("""DELETE FROM memory WHERE user_id=? AND id NOT IN
        (SELECT id FROM memory WHERE user_id=? ORDER BY id DESC LIMIT 20)""",
        (str(uid), str(uid)))
    conn.commit()

def load_memory(uid, limit=20):
    cur.execute("SELECT role,content FROM memory WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (str(uid), limit))
    return [{"role": r, "content": c} for r, c in cur.fetchall()[::-1]]

def get_profile(uid):
    cur.execute("SELECT name,voice_mode FROM profile WHERE user_id=?", (str(uid),))
    row = cur.fetchone()
    if row:
        return {"name": row[0] or "", "voice_mode": int(row[1] or 0)}
    cur.execute("INSERT INTO profile(user_id) VALUES(?)", (str(uid),))
    conn.commit()
    return {"name": "", "voice_mode": 0}

def set_voice_mode(uid, val):
    cur.execute("UPDATE profile SET voice_mode=? WHERE user_id=?",
                (int(val), str(uid)))
    conn.commit()

def set_voice(uid, engine="gtts", name=""):
    cur.execute(
        "UPDATE profile SET voice_engine=?, voice_name=? WHERE user_id=?",
        (engine, name, str(uid))
    )
    conn.commit()

def get_voice(uid):
    cur.execute(
        "SELECT voice_engine, voice_name FROM profile WHERE user_id=?",
        (str(uid),)
    )
    r = cur.fetchone()
    return {
        "engine": r[0] if r else "gtts",
        "name": r[1] if r else ""
    }

def is_banned(uid):
    cur.execute("SELECT 1 FROM bans WHERE user_id=?", (str(uid),))
    return cur.fetchone() is not None

# ================== YOUTUBE SEARCH ==================
def search_youtube(query, max_results=3):
    try:
        yt = googleapiclient.discovery.build(
            "youtube", "v3", developerKey=YOUTUBE_API_KEY)
        req = yt.search().list(
            part="snippet",
            q=query,
            type="video",
            maxResults=max_results
        )
        res = req.execute()

        ctx = "\n\n[SYSTEM: YouTube Results]\n"
        for i in res.get("items", []):
            title = i["snippet"]["title"]
            channel = i["snippet"]["channelTitle"]
            vid = i["id"]["videoId"]
            ctx += f"- {title}\n  📺 {channel}\n  🔗 https://youtu.be/{vid}\n\n"
        return ctx
    except Exception as e:
        print("YT Error:", e)
        return ""

# ================== SERP WEB SEARCH ==================
async def search_web_serp(query, max_results=5):
    url = "https://serpapi.com/search.json"
    params = {
        "engine": "google",
        "q": query,
        "api_key": SERP_API_KEY,
        "num": max_results
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, params=params)
            data = r.json()

        results = data.get("organic_results", [])
        if not results:
            return ""

        ctx = "\n\n[SYSTEM: Live Web Search]\n"
        for r in results[:max_results]:
            ctx += f"- {r.get('title','')}\n  {r.get('snippet','')}\n  🔗 {r.get('link','')}\n\n"
        return ctx
    except Exception as e:
        print("SERP Error:", e)
        return ""

# ================== ADVANCED ZIP UTILS ==================
def create_code_zip(text):
    """
    Extracts multiple code blocks with language
    and creates multi-file project ZIP
    """

    pattern = r"```(\w+)?\n(.*?)```"
    matches = re.findall(pattern, text, re.DOTALL)

    if not matches:
        return None

    EXT_MAP = {
        "python": "py",
        "py": "py",
        "html": "html",
        "css": "css",
        "javascript": "js",
        "js": "js",
        "json": "json",
        "text": "txt",
        "txt": "txt",
        "md": "md",
        "bash": "sh",
        "env": "env"
    }

    bio = BytesIO()
    file_count = 0

    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
        for idx, (lang, code) in enumerate(matches, start=1):
            if not code.strip():
                continue

            ext = EXT_MAP.get(lang.lower() if lang else "", "txt")

            # smart file naming
            filename = f"file_{idx}.{ext}"

            # project-like folders
            if ext == "py":
                filename = f"src/{filename}"
            elif ext in ["html", "css", "js"]:
                filename = f"web/{filename}"
            elif ext == "json":
                filename = f"config/{filename}"
            elif ext in ["md", "txt"]:
                filename = f"docs/{filename}"

            zf.writestr(filename, code.strip())
            file_count += 1

    if file_count == 0:
        return None

    bio.seek(0)
    return bio

# ================== OPENROUTER (MULTI API FAILOVER) ==================
async def ask_openrouter(messages):
    for api_key in OPENROUTER_KEYS:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": MODEL_NAME,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 1000
        }

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    json=payload
                )

            if r.status_code == 200:
                data = r.json()
                if data.get("choices"):
                    print("✅ OpenRouter key used:", api_key[:8], "****")
                    return data["choices"][0]["message"]["content"]

            else:
                print("⚠️ API failed → switching key", r.status_code)

        except Exception as e:
            print("🔥 API crash → switching key", e)

    return "🥺 Bestie free AI limits khatam ho gaye… thoda baad try karo 💔"


# ================== IMAGE GENERATION (POLLINATIONS) ==================
async def generate_image_pollinations(prompt: str):
    try:
        prompt = re.sub(r"[^\w\s]", "", prompt).strip()
        if not prompt:
            return None

        url = f"https://image.pollinations.ai/prompt/{prompt}"
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.get(url)

        if r.status_code == 200:
            bio = BytesIO(r.content)
            bio.seek(0)
            return bio
        else:
            print("Image gen failed:", r.status_code)
            return None

    except Exception as e:
        print("Image gen error:", e)
        return None

# ================== IMAGE TO TEXT ==================
async def image_to_text(image_bytes):
    b64 = base64.b64encode(image_bytes).decode()
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "Describe this image clearly and in detail."},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{b64}"
                }
            }
        ]
    }]
    return await ask_openrouter(messages)

def text_to_speech_bytes(text):
    text = re.sub(r"```.*?```", "Code attached.", text, flags=re.DOTALL)
    tts = gTTS(text=text[:500], lang="en", tld="co.in")
    bio = BytesIO()
    tts.write_to_fp(bio)
    bio.seek(0)
    return bio

def voice_to_text(file_bytes):
    r = sr.Recognizer()
    with BytesIO(file_bytes) as f:
        with sr.AudioFile(f) as src:
            audio = r.record(src)
    try:
        return r.recognize_google(audio)
    except:
        return "Voice clear nahi aaia 😅"

async def safe_action(bot, chat_id, action=ChatAction.TYPING):
    try:
        await bot.send_chat_action(chat_id, action)
    except:
        pass

async def download_file(bot, file_id):
    file = await bot.get_file(file_id)
    return bytes(await file.download_as_bytearray())

# ================== SEND REPLY ==================
async def send_reply(update, text):
    uid = update.effective_user.id

    # ✅ TEXT ALWAYS SEND (ONLY ONCE)
    await update.message.reply_text(text)

    profile = get_profile(uid)
    if profile["voice_mode"] == 0:
        return  # 🔕 voice OFF → sirf text

    # ================== ZIP ==================
    zipf = create_code_zip(text)
    if zipf:
        await update.message.reply_document(
            document=InputFile(zipf, "code.zip"),
            caption="📁 Code ZIP ready bestie 💻"
        )

    # ================== VOICE ==================
    v = get_voice(uid)  # returns dict: {"engine": "gtts"/"eleven", "name": "Priya"}
    bio = None

    if v["engine"] == "eleven" and v["name"]:
        try:
            # ElevenLabs async call
            bio_mp3 = await eleven_tts(text, v["name"])
            if bio_mp3:
                # Convert MP3 → OGG for Telegram
                from pydub import AudioSegment
                bio_mp3.seek(0)
                audio = AudioSegment.from_file(bio_mp3, format="mp3")
                bio_ogg = BytesIO()
                audio.export(bio_ogg, format="ogg")
                bio_ogg.seek(0)
                bio = bio_ogg
        except Exception as e:
            print("ElevenLabs conversion fail:", e)
            bio = None  # fallback gTTS

    # If ElevenLabs failed or not selected, fallback to gTTS
    if not bio:
        bio = text_to_speech_bytes(text)

    # Send voice reply
    if bio:
        await update.message.reply_voice(
            voice=InputFile(bio, "reply.ogg")
        )

# ================= ADMIN COMMANDS =================

async def admin_menu(update, ctx):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        "👑 *ADMIN MENU*\n\n"
        "/banuser <id>\n"
        "/unbanuser <id>\n"
        "/all_send <msg/photo/video>\n"
        "/user_send <id> <msg/photo/video>\n"
        "/update\n"
        "/updateoff\n",

        parse_mode="Markdown"
    )


async def ban_user(update, ctx):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /banuser <user_id>")
        return

    uid = ctx.args[0]
    cur.execute(
        "INSERT OR REPLACE INTO bans(user_id,reason,ts) VALUES(?,?,?)",
        (uid, "Admin ban", int(time.time()))
    )
    conn.commit()

    await update.message.reply_text(f"🚫 User {uid} banned successfully")


async def unban_user(update, ctx):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /unbanuser <user_id>")
        return

    uid = ctx.args[0]
    cur.execute("DELETE FROM bans WHERE user_id=?", (uid,))
    conn.commit()

    await update.message.reply_text(f"❤️ User {uid} unbanned – ab free ho")


async def all_send(update, ctx):
    if not is_admin(update.effective_user.id):
        return

    msg = update.message
    reply = msg.reply_to_message

    # 🔹 TEXT FIX
    parts = msg.text.split(" ", 1)
    broadcast_text = parts[1] if len(parts) > 1 else None

    cur.execute("SELECT DISTINCT user_id FROM memory")
    users = [u[0] for u in cur.fetchall()]

    for uid in users:
        try:
            if reply:
                if reply.photo:
                    await ctx.bot.send_photo(uid, reply.photo[-1].file_id, caption=reply.caption)
                elif reply.video:
                    await ctx.bot.send_video(uid, reply.video.file_id, caption=reply.caption)
                elif reply.audio:
                    await ctx.bot.send_audio(uid, reply.audio.file_id, caption=reply.caption)
                elif reply.document:
                    await ctx.bot.send_document(uid, reply.document.file_id, caption=reply.caption)
                elif reply.text:
                    await ctx.bot.send_message(uid, reply.text)

            elif broadcast_text:
                await ctx.bot.send_message(uid, broadcast_text)

            await asyncio.sleep(1)

        except Exception as e:
            print("Broadcast error:", e)

    await msg.reply_text("✅ Broadcast done")


async def user_send(update, ctx):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /user_send <id> <message>")
        return

    uid = ctx.args[0]
    msg_text = " ".join(ctx.args[1:]) if len(ctx.args) > 1 else None
    msg = update.message

    try:
        # Text
        if msg_text:
            await ctx.bot.send_message(uid, msg_text)

        # Photo
        if msg.photo:
            await ctx.bot.send_photo(uid, msg.photo[-1].file_id, caption=msg.caption)

        # Video
        if msg.video:
            await ctx.bot.send_video(uid, msg.video.file_id, caption=msg.caption)

        # Audio / Music
        if msg.audio:
            await ctx.bot.send_audio(uid, msg.audio.file_id, caption=msg.caption)

        # Voice note
        if msg.voice:
            await ctx.bot.send_voice(uid, msg.voice.file_id, caption=msg.caption)

        # Document / File
        if msg.document:
            await ctx.bot.send_document(uid, msg.document.file_id, caption=msg.caption)

        await update.message.reply_text("✅ Message sent to user")

    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}")


async def update_bot(update, ctx):
    global BOT_UPDATING
    if not is_admin(update.effective_user.id):
        return

    BOT_UPDATING = True
    await update.message.reply_text(
        "🔄 Bot update ho raha hai...\n"
        "Thoda wait karein bestie 💖"
    )

async def update_off(update, ctx):
    global BOT_UPDATING
    if not is_admin(update.effective_user.id):
        return

    BOT_UPDATING = False
    await update.message.reply_text("✅ Update complete! Bot live again 🚀")

# ================== HANDLERS ==================

# -------- TEXT HANDLER --------
async def handle_text(update, ctx):
    uid = update.effective_user.id

    # 1️⃣ BOT UPDATING check
    if BOT_UPDATING:
        await update.message.reply_text(
            "⚙️ Bot abhi update ho raha hai...\nThodi der baad aana bestie 💖"
        )
        return

    # 2️⃣ BAN check
    if is_banned(uid):
        await update.message.reply_text(
            "🚫 Aap ban ho chuke ho.\nAdmin se contact karein 🙏 @MANDAL4482"
        )
        return

    # 3️⃣ User text save
    text = update.message.text
    save_msg(uid, "user", text)
    await safe_action(ctx.bot, update.effective_chat.id)

    # 4️⃣ Context collection
    yt_ctx = ""
    web_ctx = ""

    if any(k in text.lower() for k in ["youtube", "video", "youtub", "youtube link", "youtub link", "youtuber" ]):
        yt_ctx = search_youtube(text)

    if any(k in text.lower() for k in ["latest", "news", "price", "what is", "who is", "define", "search", "gold price", "dimond price", "bitcoin price"]):
        web_ctx = await search_web_serp(text)

    # 5️⃣ Compose messages
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += load_memory(uid)

    if yt_ctx:
        messages.append({"role": "system", "content": yt_ctx})
    if web_ctx:
        messages.append({"role": "system", "content": web_ctx})

    # 6️⃣ Ask AI
    reply = await ask_openrouter(messages)
    save_msg(uid, "assistant", reply)
    await send_reply(update, reply)


# -------- VOICE HANDLER --------
async def handle_voice(update, ctx):
    uid = update.effective_user.id

    # 1️⃣ BOT UPDATING check
    if BOT_UPDATING:
        await update.message.reply_text(
            "⚙️ Bot abhi update ho raha hai...\nThodi der baad aana bestie 🛠️💖"
        )
        return

    # 2️⃣ BAN check
    if is_banned(uid):
        await update.message.reply_text(
            "🚫 Aap ban ho chuke ho.\nVoice allowed nahi ❌ admin ko bolo @MANDAL4482"
        )
        return

    # 3️⃣ Download & convert voice
    await safe_action(ctx.bot, update.effective_chat.id)
    file_bytes = await download_file(ctx.bot, update.message.voice.file_id)
    text = voice_to_text(file_bytes)
    save_msg(uid, "user", text)

    # 4️⃣ AI reply
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + load_memory(uid)
    reply = await ask_openrouter(messages)
    save_msg(uid, "assistant", reply)
    await send_reply(update, reply)


# -------- PHOTO HANDLER --------
async def handle_photo(update, ctx):
    uid = update.effective_user.id

    # 1️⃣ BOT UPDATING check
    if BOT_UPDATING:
        await update.message.reply_text(
            "⚙️ Bot abhi update ho raha hai...\nThodi der baad aana bestie 🛠️💖"
        )
        return

    # 2️⃣ BAN check
    if is_banned(uid):
        await update.message.reply_text(
            "🚫 Aap ban ho chuke ho.\nPhoto send allowed nahi ❌admin ko bolo"
        )
        return

    # 3️⃣ Download photo
    await safe_action(ctx.bot, update.effective_chat.id)
    file_bytes = await download_file(ctx.bot, update.message.photo[-1].file_id)

    # 4️⃣ Convert image → text
    caption = await image_to_text(file_bytes)
    save_msg(uid, "user", f"[Image sent] {caption}")

    # 5️⃣ AI reply
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + load_memory(uid)
    reply = await ask_openrouter(messages)
    save_msg(uid, "assistant", reply)
    await send_reply(update, reply)


# -------- IMAGE COMMAND HANDLER (/image) --------
async def image_cmd(update, ctx):
    uid = update.effective_user.id

    # 1️⃣ BOT UPDATING check
    if BOT_UPDATING:
        await update.message.reply_text(
            "⚙️ Bot abhi update ho raha hai...\nThodi der baad aana bestie 💖"
        )
        return

    # 2️⃣ BAN check
    if is_banned(uid):
        await update.message.reply_text(
            "🚫 Aap ban ho chuke ho.\nImage command allowed nahi ❌"
        )
        return

    # 3️⃣ Check prompt
    if not ctx.args:
        await update.message.reply_text("🖼️ Usage:\n/image cute anime girl")
        return

    prompt = " ".join(ctx.args)
    await safe_action(ctx.bot, update.effective_chat.id, ChatAction.UPLOAD_PHOTO)

    # 4️⃣ Generate image
    img = await generate_image_pollinations(prompt)
    if not img:
        await update.message.reply_text("🥺 Image generate nahi ho payi")
        return

    # 5️⃣ Send photo
    await update.message.reply_photo(
        photo=InputFile(img, "image.jpg"),
        caption=f"🖼️ Generated by Priya\n✨ Prompt: {prompt}"
    ) 
# ================== COMMANDS ==================
async def start(update, ctx):
    await update.message.reply_text(
        "😎 Hi bestie! Main Priya hoon 🥰\n"
              "tumari ai friend"
    )

async def voice_on(update, ctx):
    set_voice_mode(update.effective_user.id, 1)
    await update.message.reply_text("🎤 Voice ON 🥰")

async def voice_off(update, ctx):
    set_voice_mode(update.effective_user.id, 0)
    await update.message.reply_text("🔕 Voice OFF 👍")

async def on_priya(update, ctx):
    uid = update.effective_user.id
    set_voice_mode(uid, 1)
    set_voice(uid, "eleven", "priya")
    await update.message.reply_text("🎧✨ Priya voice ON")

async def off_priya(update, ctx):
    uid = update.effective_user.id
    set_voice(uid, "gtts", "")
    await update.message.reply_text("🔕 Priya voice OFF")

async def on_rose(update, ctx):
    uid = update.effective_user.id
    set_voice_mode(uid, 1)
    set_voice(uid, "eleven", "rose")
    await update.message.reply_text("🎧🌹 Rose voice ON")

async def off_rose(update, ctx):
    uid = update.effective_user.id
    set_voice(uid, "gtts", "")
    await update.message.reply_text("🔕 Rose voice OFF")

# ================== MAIN ==================
def main():
    # 🔥 Keep bot awake (Render + UptimeRobot)
    threading.Thread(target=run_web).start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("voice", voice_on))
    app.add_handler(CommandHandler("voiceoff", voice_off))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CommandHandler("onpriya", on_priya))
    app.add_handler(CommandHandler("offpriya", off_priya))
    app.add_handler(CommandHandler("onrose", on_rose))
    app.add_handler(CommandHandler("offrose", off_rose))
    app.add_handler(CommandHandler("image", image_cmd))
    app.add_handler(CommandHandler("menu", admin_menu))
    app.add_handler(CommandHandler("banuser", ban_user))
    app.add_handler(CommandHandler("unbanuser", unban_user))
    app.add_handler(CommandHandler("all_send", all_send))
    app.add_handler(CommandHandler("user_send", user_send))
    app.add_handler(CommandHandler("update", update_bot))
    app.add_handler(CommandHandler("updateoff", update_off))

    print("🚀 PRIYA AI (YT + SERP + ZIP + VOICE) LIVE")
    app.run_polling()

if __name__ == "__main__":
    main()
