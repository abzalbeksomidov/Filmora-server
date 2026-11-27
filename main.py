import asyncio
import json
import os
import re
import time
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
    FSInputFile,
)

# ================= ASOSIY SOZLAMALAR =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "")

BAZA_CHANNEL_ID = -1003958730565
BACKUP_CHANNEL_ID = -1005905529010
PUBLIC_CHANNEL = "@filmora_kinolar"
AUTO_INTERVAL = 17280  # 5 ta kino/kun (har 4.8 soatda)

DB_FILE = "db.json"
PENDING_NAME_PROMPTS = {}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
app = FastAPI()

DEFAULT_DB = {
    "movies": {},
    "queue": [],
    "next_code": 1001,
    "users": [],
    "premium_users": {},
    "favorites": {},
    "channels": [],
    "settings": {
        "sub_enabled": True,
        "auto_publish": True,
        "last_auto_time": 0,
    },
}

DB = json.loads(json.dumps(DEFAULT_DB))


def merge_db(loaded: dict):
    global DB
    for k, v in DEFAULT_DB.items():
        if k not in loaded:
            loaded[k] = json.loads(json.dumps(v))
    for sk, sv in DEFAULT_DB["settings"].items():
        if sk not in loaded["settings"]:
            loaded["settings"][sk] = sv
    DB = loaded


async def restore_db_from_telegram():
    """Server qayta yonganda Baza kanalidagi oxirgi qadalgan db.json ni tiklaydi"""
    try:
        chat = await bot.get_chat(BAZA_CHANNEL_ID)
        if chat.pinned_message and chat.pinned_message.document:
            doc = chat.pinned_message.document
            if doc.file_name == "filmora_db_backup.json":
                file_info = await bot.get_file(doc.file_id)
                await bot.download_file(file_info.file_path, DB_FILE)
                with open(DB_FILE, "r", encoding="utf-8") as f:
                    merge_db(json.load(f))
                print("✅ Baza Telegram Cloud'dan muvaffaqiyatli tiklandi!")
                return
    except Exception as e:
        print("Telegram backup tiklashda xato:", e)

    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                merge_db(json.load(f))
        except Exception:
            pass


async def save_db(sync_cloud: bool = True):
    """Bazani ham lokal faylga, ham Telegram Baza kanaliga saqlaydi"""
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(DB, f, ensure_ascii=False, indent=2)

    if sync_cloud:
        try:
            doc = FSInputFile(DB_FILE, filename="filmora_db_backup.json")
            msg = await bot.send_document(
                BAZA_CHANNEL_ID,
                document=doc,
                caption=f"🗄 <b>Filmora Baza Zaxirasi</b>\n🎬 Kinolar soni: {len(DB['movies'])} ta\n⏳ Navbatda: {len(DB['queue'])} ta",
                parse_mode="HTML",
            )
            await bot.pin_chat_message(BAZA_CHANNEL_ID, msg.message_id, disable_notification=True)
        except Exception as e:
            print("Cloud backup saqlashda xato:", e)
# ================= AQLLI MATN VA POSTER QIDIRUV =================
def extract_caption_details(raw_text: str) -> dict:
    """Forward qilingan matndan qo'shtirnoqdagi nom, yil, janr va davlatni ajratadi"""
    info = {"title": "", "year": "", "genre": "", "country": ""}
    if not raw_text:
        return info

    # Qo'shtirnoq ichidagi nomni topish: "Daxshatli qo'rquv" yoki «...»
    q_match = re.search(r'["«“]([^"»”\n]{2,80})["»”]', raw_text)
    if q_match:
        info["title"] = q_match.group(1).strip()

    y_match = re.search(r'(?:Yili|Year|Год)\s*[:\-]?\s*(19\d{2}|20\d{2})', raw_text, re.I)
    if not y_match:
        y_match = re.search(r'\b(19[5-9]\d|20[0-2]\d)\b', raw_text)
    if y_match:
        info["year"] = y_match.group(1)

    g_match = re.search(r'(?:Janr|Genre|Жанр)\s*[:\-]?\s*([^\n]+)', raw_text, re.I)
    if g_match:
        info["genre"] = g_match.group(1).strip()

    c_match = re.search(r'(?:Davlat|Country|Страна|Mamlakat)\s*[:\-]?\s*([^\n]+)', raw_text, re.I)
    if c_match:
        info["country"] = c_match.group(1).strip()

    if not info["title"]:
        lines = []
        for line in raw_text.splitlines():
            clean_l = re.sub(r'@\w+|https?://\S+|t\.me/\S+', '', line).strip()
            if not clean_l:
                continue
            if re.match(r'^(PREMYERA|Yili|Janr|Davlat|KinoPoisk|IMDb|O\'zbek|Til|Sifat|Kanal|Obuna)', clean_l, re.I):
                continue
            lines.append(clean_l)
        if lines:
            info["title"] = re.sub(r'[^\w\s\'\-\:\(\)]', '', lines[0]).strip()

    return info


def is_junk_filename(fname: str) -> bool:
    """Mkk.mp4, VID_1234.mp4 kabi shifrli fayl nomlarini aniqlaydi"""
    if not fname:
        return True
    base = os.path.splitext(fname)[0].strip()
    if len(base) <= 4:
        return True
    if re.match(r'^(VID|MOV|IMG|VIDEO|FILE|DOC|MP4|HD|FULL|NEW|KINO|FILM)[\s_\-\d]*$', base, re.I):
        return True
    return False


async def fetch_itunes_poster(search_query: str) -> str:
    """iTunes API orqali 600x900 sifatli asl film posterini topadi"""
    if not search_query:
        return ""
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get(
                "https://itunes.apple.com/search",
                params={"term": search_query, "media": "movie", "limit": 3},
            )
            data = r.json()
            results = data.get("results", [])
            if results:
                art = results[0].get("artworkUrl100", "")
                if art:
                    return art.replace("100x100bb", "600x900bb")
    except Exception:
        pass
    return ""


async def identify_movie_with_gemini(raw_hint: str, parsed: dict) -> dict:
    """Gemini yordamida kino nomini aniqlaydi, inglizchaga tarjima qilib poster topadi"""
    title_uz = parsed.get("title") or raw_hint
    year = parsed.get("year") or "2024"
    genre = parsed.get("genre") or "Jangari, Drama"
    country = parsed.get("country") or "AQSH"
    desc = "Yuqori sifatli va qiziqarli syujetga ega ajoyib film. Hoziroq tomosha qiling!"
    english_query = title_uz

    if GEMINI_API_KEY:
        prompt = (
            f"Quyidagi kino ma'lumotidan haqiqiy filmni aniqla:\n"
            f"Matn: {raw_hint}\n"
            f"Ajratilgan nom: {title_uz}, Yil: {year}, Davlat: {country}, Janr: {genre}\n\n"
            f"Faqat quyidagi JSON formatda javob ber (boshqa matn yozma):\n"
            f'{{"title_uz": "O\'zbekcha aniq nomi", "english_title": "Original English movie title for poster search", '
            f'"year": "Yili", "genre": "Janri o\'zbekcha", "country": "Davlati o\'zbekcha", '
            f'"desc": "O\'zbek tilida 2 gaplik qiziqarli tavsif"}}'
        )
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    url,
                    json={"contents": [{"parts": [{"text": prompt}]}]},
                )
                res_json = resp.json()
                text_out = res_json["candidates"][0]["content"]["parts"][0]["text"]
                m = re.search(r'\{.*\}', text_out, re.S)
                if m:
                    ai_data = json.loads(m.group(0))
                    title_uz = ai_data.get("title_uz") or title_uz
                    english_query = ai_data.get("english_title") or title_uz
                    year = str(ai_data.get("year") or year)
                    genre = ai_data.get("genre") or genre
                    country = ai_data.get("country") or country
                    desc = ai_data.get("desc") or desc
        except Exception as e:
            print("Gemini xato:", e)

    poster = await fetch_itunes_poster(f"{english_query} {year}")
    if not poster:
        poster = await fetch_itunes_poster(english_query)
    if not poster and english_query != title_uz:
        poster = await fetch_itunes_poster(title_uz)

    return {
        "title": title_uz,
        "year": year,
        "genre": genre,
        "country": country,
        "desc": desc,
        "poster": poster,
    }


async def publish_next_queued_movie() -> tuple[bool, str]:
    """Navbatdagi 1 ta kinoni Baza va @filmora_kinolar kanaliga chiqaradi"""
    if not DB["queue"]:
        return False, "Navbatda kino yo‘q."

    item = DB["queue"].pop(0)
    code = str(DB["next_code"])
    DB["next_code"] += 1

    try:
        copied = await bot.copy_message(
            chat_id=BAZA_CHANNEL_ID,
            from_chat_id=BACKUP_CHANNEL_ID,
            message_id=item["backup_msg_id"],
            caption=f"🎬 <b>{item['title']} ({item['year']})</b>\n🔢 Kod: <code>{code}</code>",
            parse_mode="HTML",
        )
    except Exception as e:
        DB["queue"].insert(0, item)
        return False, f"Zaxira kanaldan nusxalashda xato: {e}"

    movie_obj = {
        "code": code,
        "title": item["title"],
        "year": item["year"],
        "genre": item["genre"],
        "country": item["country"],
        "desc": item["desc"],
        "poster": item.get("poster", ""),
        "baza_msg_id": copied.message_id,
        "added_at": int(time.time()),
    }
    DB["movies"][code] = movie_obj
    DB["settings"]["last_auto_time"] = int(time.time())
    await save_db(sync_cloud=True)

    bot_me = await bot.get_me()
    caption_pub = (
        f"🎬 <b>{movie_obj['title']} ({movie_obj['year']})</b>\n\n"
        f"🎭 <b>Janr:</b> {movie_obj['genre']}\n"
        f"🌍 <b>Davlat:</b> {movie_obj['country']}\n"
        f"🇺🇿 <b>Til:</b> O‘zbek tilida\n\n"
        f"📝 <i>{movie_obj['desc']}</i>\n\n"
        f"🔢 <b>Kino kodi:</b> <code>{code}</code>\n"
        f"🤖 <b>Bot:</b> @{bot_me.username}"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="▶️ Kinoni tomosha qilish",
                    url=f"https://t.me/{bot_me.username}?start={code}",
                )
            ]
        ]
    )

    try:
        if movie_obj["poster"]:
            await bot.send_photo(
                chat_id=PUBLIC_CHANNEL,
                photo=movie_obj["poster"],
                caption=caption_pub,
                parse_mode="HTML",
                reply_markup=kb,
            )
        else:
            await bot.send_message(
                chat_id=PUBLIC_CHANNEL,
                text=caption_pub,
                parse_mode="HTML",
                reply_markup=kb,
            )
    except Exception as e:
        return True, f"Bazaga saqlandi (Kod: {code}), lekin kanalga post chiqishida xato: {e}"

    return True, f"✅ <b>{movie_obj['title']}</b> (Kod: <code>{code}</code>) kanalga chiqarildi!"
    # ================= BOT HANDLERLAR VA ADMIN PANEL =================
def is_premium(user_id: int) -> bool:
    return user_id == ADMIN_ID or DB["premium_users"].get(str(user_id), 0) > time.time()


async def get_unsubscribed_channels(user_id: int):
    if user_id == ADMIN_ID or not DB["settings"].get("sub_enabled", True):
        return []
    missing = []
    for ch in DB["channels"]:
        if not ch.get("active", True):
            continue
        try:
            member = await bot.get_chat_member(ch["id"], user_id)
            if member.status in ("left", "kicked"):
                missing.append(ch)
        except Exception:
            pass
    return missing


async def send_movie_to_user(user_id: int, code: str):
    m = DB["movies"].get(code)
    if not m:
        await bot.send_message(user_id, "❌ Bunday kodli kino topilmadi.")
        return

    missing = await get_unsubscribed_channels(user_id)
    if missing:
        btns = [[InlineKeyboardButton(text=f"➕ {c['title']}", url=c["url"])] for c in missing]
        btns.append([InlineKeyboardButton(text="✅ Tekshirish", callback_data=f"chk:{code}")])
        await bot.send_message(
            user_id,
            "🎬 Kinoni tomosha qilish uchun homiy kanallarga obuna bo‘ling:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=btns),
        )
        return

    caption = (
        f"🎬 <b>{m['title']} ({m['year']})</b>\n"
        f"🎭 Janr: {m['genre']} | 🌍 {m['country']}\n"
        f"🔢 Kod: <code>{code}</code>\n\n"
        f"📢 Kanalimiz: {PUBLIC_CHANNEL}"
    )
    favs = DB["favorites"].get(str(user_id), [])
    fav_text = "💔 Sevimlilardan olish" if code in favs else "❤️ Sevimlilarga qo‘shish"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=fav_text, callback_data=f"fav:{code}")]]
    )
    await bot.copy_message(
        chat_id=user_id,
        from_chat_id=BAZA_CHANNEL_ID,
        message_id=m["baza_msg_id"],
        caption=caption,
        parse_mode="HTML",
        reply_markup=kb,
    )


@dp.channel_post(F.chat.id == BACKUP_CHANNEL_ID)
async def backup_channel_watcher(message: types.Message):
    media = message.video or message.document
    if not media:
        return

    raw_caption = message.caption or ""
    fname = getattr(media, "file_name", "") or ""
    parsed = extract_caption_details(raw_caption)

    # Agar matn yo'q bo'lsa va fayl nomi Mkk.mp4 kabi tushunarsiz bo'lsa, adminga yozadi
    if not parsed["title"] and is_junk_filename(fname):
        if ADMIN_ID:
            try:
                ask = await bot.send_message(
                    ADMIN_ID,
                    f"⚠️ <b>Zaxira kanalga nomsiz/shifrli kino tushdi!</b>\n"
                    f"📁 Fayl: <code>{fname or 'Nomsiz'}</code>\n\n"
                    f"100% aniq poster va ma’lumot chiqishi uchun <b>shu xabarga Reply qilib kino nomini yozing</b> (masalan: <i>Daxshatli qo'rquv 2018</i>):",
                    parse_mode="HTML",
                )
                PENDING_NAME_PROMPTS[ask.message_id] = message.message_id
            except Exception:
                pass
        return

    hint = raw_caption if raw_caption else os.path.splitext(fname)[0]
    info = await identify_movie_with_gemini(hint, parsed)
    DB["queue"].append(
        {
            "backup_msg_id": message.message_id,
            "title": info["title"],
            "year": info["year"],
            "genre": info["genre"],
            "country": info["country"],
            "desc": info["desc"],
            "poster": info["poster"],
        }
    )
    await save_db(sync_cloud=True)

    if ADMIN_ID:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"📥 <b>Avto-navbatga qo‘shildi!</b>\n\n"
                f"🎬 <b>Nomi:</b> {info['title']} ({info['year']})\n"
                f"🎭 <b>Janr:</b> {info['genre']}\n"
                f"🖼 <b>Poster:</b> {'✅ Topildi' if info['poster'] else '⚠️ Topilmadi'}\n"
                f"⏳ <b>Navbatda jami:</b> {len(DB['queue'])} ta",
                parse_mode="HTML",
            )
        except Exception:
            pass


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    uid = message.from_user.id
    if uid not in DB["users"]:
        DB["users"].append(uid)
        await save_db(sync_cloud=False)

    parts = message.text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip() in DB["movies"]:
        await send_movie_to_user(uid, parts[1].strip())
        return

    rows = []
    if WEBAPP_URL:
        rows.append([InlineKeyboardButton(text="🎬 Filmora Katalog (Mini App)", web_app=WebAppInfo(url=WEBAPP_URL))])
    rows.append([InlineKeyboardButton(text="📢 Rasmiy kanal", url=f"https://t.me/{PUBLIC_CHANNEL.lstrip('@')}")])

    await message.answer(
        f"👋 Assalomu alaykum, <b>{message.from_user.first_name}</b>!\n\n"
        f"🎬 <b>Filmora</b> kino botiga xush kelibsiz!\n"
        f"🔢 Kino kodini yuboring (masalan: <code>1001</code>) yoki katalogni oching:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


def admin_panel_kb():
    auto_st = "🟢 Yoniq" if DB["settings"].get("auto_publish", True) else "🔴 O‘chiq"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🤖 Avto-kino (Navbat: {len(DB['queue'])})", callback_data="adm:queue")],
            [InlineKeyboardButton(text="▶️ Hoziroq 1 ta kino chiqarish", callback_data="adm:pub_now")],
            [InlineKeyboardButton(text=f"⏱ Avto-taymer: {auto_st}", callback_data="adm:toggle_auto")],
            [InlineKeyboardButton(text="☁️ Bazani Cloud'ga saqlash", callback_data="adm:backup")],
            [InlineKeyboardButton(text="📊 Statistika", callback_data="adm:stats")],
        ]
    )


@dp.message(Command("panel"))
async def cmd_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        f"⚙️ <b>Filmora Admin Panel</b>\n\n"
        f"🎬 Bazadagi kinolar: <b>{len(DB['movies'])} ta</b>\n"
        f"⏳ Zaxira navbatda: <b>{len(DB['queue'])} ta</b>\n"
        f"👥 Foydalanuvchilar: <b>{len(DB['users'])} ta</b>",
        parse_mode="HTML",
        reply_markup=admin_panel_kb(),
    )


@dp.callback_query(F.data.startswith("adm:"))
async def adm_callbacks(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    action = call.data.split(":")[1]

    if action == "pub_now":
        await call.answer("⏳ Kino chiqarilmoqda...", show_alert=False)
        ok, msg = await publish_next_queued_movie()
        await call.message.answer(msg, parse_mode="HTML")
    elif action == "queue":
        if not DB["queue"]:
            await call.answer("Navbat bo‘sh!", show_alert=True)
            return
        lines = [f"{i+1}. <b>{x['title']}</b> ({x['year']})" for i, x in enumerate(DB["queue"][:15])]
        await call.message.answer("⏳ <b>Navbatdagi kinolar:</b>\n\n" + "\n".join(lines), parse_mode="HTML")
        await call.answer()
    elif action == "toggle_auto":
        DB["settings"]["auto_publish"] = not DB["settings"].get("auto_publish", True)
        await save_db(sync_cloud=True)
        await call.message.edit_reply_markup(reply_markup=admin_panel_kb())
        await call.answer("Yangilandi!")
    elif action == "backup":
        await save_db(sync_cloud=True)
        await call.answer("✅ Baza Telegram Cloud'ga saqlandi!", show_alert=True)
    elif action == "stats":
        await call.answer(
            f"Kinolar: {len(DB['movies'])} | Navbat: {len(DB['queue'])} | Userlar: {len(DB['users'])}",
            show_alert=True,
        )


@dp.callback_query(F.data.startswith("chk:"))
async def check_sub_cb(call: types.CallbackQuery):
    code = call.data.split(":")[1]
    await call.answer()
    await send_movie_to_user(call.from_user.id, code)


@dp.callback_query(F.data.startswith("fav:"))
async def fav_cb(call: types.CallbackQuery):
    code = call.data.split(":")[1]
    uid = str(call.from_user.id)
    favs = DB["favorites"].setdefault(uid, [])
    if code in favs:
        favs.remove(code)
        await call.answer("Sevimlilardan olib tashlandi")
    else:
        favs.append(code)
        await call.answer("❤️ Sevimlilarga qo‘shildi!")
    await save_db(sync_cloud=False)


@dp.message()
async def general_message_handler(message: types.Message):
    # Admin nomsiz kinoga Reply qilib nom yozsa
    if (
        message.from_user.id == ADMIN_ID
        and message.reply_to_message
        and message.reply_to_message.message_id in PENDING_NAME_PROMPTS
        and message.text
    ):
        backup_msg_id = PENDING_NAME_PROMPTS.pop(message.reply_to_message.message_id)
        parsed = extract_caption_details(message.text)
        info = await identify_movie_with_gemini(message.text, parsed)
        DB["queue"].append(
            {
                "backup_msg_id": backup_msg_id,
                "title": info["title"],
                "year": info["year"],
                "genre": info["genre"],
                "country": info["country"],
                "desc": info["desc"],
                "poster": info["poster"],
            }
        )
        await save_db(sync_cloud=True)
        await message.reply(
            f"✅ <b>{info['title']} ({info['year']})</b> aniqlanib navbatga qo‘shildi!\n"
            f"🖼 Poster: {'✅ Topildi' if info['poster'] else '⚠️ Topilmadi'}",
            parse_mode="HTML",
        )
        return

    if message.text and message.text.strip().isdigit():
        await send_movie_to_user(message.from_user.id, message.text.strip())


# ================= MINI APP VA SERVER API =================
@app.get("/api/state")
async def api_state(user_id: int = 0):
    movies_list = list(DB["movies"].values())
    movies_list.sort(key=lambda x: int(x.get("code", 0)), reverse=True)
    return JSONResponse(
        {
            "movies": movies_list,
            "favorites": DB["favorites"].get(str(user_id), []),
            "is_premium": is_premium(user_id),
        }
    )


@app.get("/")
async def serve_webapp():
    if os.path.exists("index.html"):
        with open("index.html", "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h3>Filmora Server Ishlamoqda!</h3>")


async def auto_publisher_loop():
    while True:
        await asyncio.sleep(60)
        try:
            if DB["settings"].get("auto_publish", True) and DB["queue"]:
                last_t = DB["settings"].get("last_auto_time", 0)
                if time.time() - last_t >= AUTO_INTERVAL:
                    ok, msg = await publish_next_queued_movie()
                    if ok and ADMIN_ID:
                        await bot.send_message(ADMIN_ID, f"🤖 <b>Avto-nashr:</b>\n{msg}", parse_mode="HTML")
        except Exception as e:
            print("Auto publisher xato:", e)


@app.on_event("startup")
async def on_startup():
    await restore_db_from_telegram()
    asyncio.create_task(dp.start_polling(bot))
    asyncio.create_task(auto_publisher_loop())
            
