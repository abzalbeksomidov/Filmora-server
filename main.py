import os
import re
import json
import time
import asyncio
from datetime import datetime
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
    ReplyKeyboardMarkup,
    KeyboardButton,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 6248171499
BAZA_CHANNEL_ID = -1003958730565
BACKUP_CHANNEL_ID = int(os.getenv("BACKUP_CHANNEL_ID", "-1005905529010"))
PUBLIC_CHANNEL = os.getenv("PUBLIC_CHANNEL", "@filmora_kinolar")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AQ.Ab8RN6KZy4OI5vqX9R2q3eW40diEKxb3b1ZM-UY1bgf6R_Y4cg")
MINI_APP_URL = "https://abzalbeksomidov.github.io/Filmora-app/"
BOT_USERNAME = "Filmorabot_bot"

AUTO_POST_INTERVAL = 17280

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

DB = {
    "movies": {},
    "channels": [{"id": "@filmora_kinolar", "title": "Filmora Kinolar", "url": "https://t.me/filmora_kinolar", "active": True}],
    "premium_users": {},
    "users": {},
    "auto_queue": [],
    "settings": {
        "card": "Karta kiritilmagan (/panel)",
        "price": "15 000 so‘m (1 oy)",
        "sub_enabled": True,
        "auto_enabled": True,
        "last_auto_post": 0,
    },
}
DB_MSG_ID = None
PENDING_NAME_PROMPTS = {}


def ensure_user(uid: int, name: str = "Foydalanuvchi"):
    suid = str(uid)
    if suid not in DB["users"]:
        DB["users"][suid] = {"name": name, "favs": [], "subs": [], "history": [], "progress": {}, "refs": [], "referred_by": None}
    elif name and name != "Foydalanuvchi":
        DB["users"][suid]["name"] = name
    return DB["users"][suid]


async def load_db():
    global DB, DB_MSG_ID
    try:
        chat = await bot.get_chat(BAZA_CHANNEL_ID)
        if chat.pinned_message and chat.pinned_message.text and chat.pinned_message.text.startswith("FILMORA_DB:"):
            loaded = json.loads(chat.pinned_message.text.replace("FILMORA_DB:", "", 1).strip())
            if isinstance(loaded.get("users"), list):
                loaded["users"] = {str(u): {"name": "Foydalanuvchi", "favs": [], "subs": [], "history": [], "progress": {}, "refs": [], "referred_by": None} for u in loaded["users"]}
            DB.update(loaded)
            DB.setdefault("auto_queue", [])
            DB.setdefault("settings", {})
            DB["settings"].setdefault("auto_enabled", True)
            DB["settings"].setdefault("last_auto_post", 0)
            DB_MSG_ID = chat.pinned_message.message_id
            return
    except Exception as e:
        print("DB load error:", e)
    await save_db()


async def save_db():
    global DB_MSG_ID
    payload = "FILMORA_DB:\n" + json.dumps(DB, ensure_ascii=False)
    try:
        if DB_MSG_ID:
            await bot.edit_message_text(chat_id=BAZA_CHANNEL_ID, message_id=DB_MSG_ID, text=payload)
        else:
            msg = await bot.send_message(chat_id=BAZA_CHANNEL_ID, text=payload)
            DB_MSG_ID = msg.message_id
            await bot.pin_chat_message(chat_id=BAZA_CHANNEL_ID, message_id=DB_MSG_ID, disable_notification=True)
    except Exception as e:
        print("DB save error:", e)


def get_next_movie_code() -> str:
    numeric_codes = [int(k) for k in DB["movies"].keys() if str(k).isdigit()]
    return str(max(numeric_codes) + 1) if numeric_codes else "1"


def normalize_genre_str(raw: str) -> str:
    parts = [p.strip().capitalize() for p in re.split(r"[,/|]", raw or "") if p.strip()]
    seen = []
    for p in parts:
        if p not in seen:
            seen.append(p)
    return ", ".join(seen) if seen else "Drama"


def clean_video_hint(raw: str) -> str:
    if not raw:
        return ""
    txt = re.sub(r"@\w+|https?://\S+|t\.me/\S+", " ", raw)
    txt = re.sub(r"\.(mp4|mkv|avi|mov|webm|flv|ts)$", "", txt, flags=re.I)
    txt = re.sub(r"\b(1080p|720p|480p|360p|4k|uhd|hd|bdrip|web-dl|webrip|hdtv|camrip|x264|x265|hevc|aac|mp3|uzbek|uzb|tarjima|kino|film|premyera|full)\b", " ", txt, flags=re.I)
    txt = re.sub(r"[_\.\[\]\(\)\{\}\|]+", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    if re.fullmatch(r"(vid|img|mov|video|file|document)?[\s\-_]*\d{4,}", txt, flags=re.I):
        return ""
    return txt


async def generate_movie_meta_with_ai(raw_text: str) -> dict:
    clean_hint = clean_video_hint(raw_text)
    fallback = {
        "title": clean_hint[:60] if clean_hint else "Yangi Premyera Kino",
        "search_en": clean_hint[:60] if clean_hint else "action movie",
        "search_alt": clean_hint[:60] if clean_hint else "",
        "year": "2025",
        "genre": "Jangari, Drama",
        "country": "AQSH",
        "type": "movie",
        "description": "Yuqori sifatli va qiziqarli syujetga ega ajoyib film. Hoziroq tomosha qiling!",
    }
    if not GEMINI_API_KEY or not clean_hint:
        return fallback

    prompt = f"""Sen professional kino ensiklopediyasi ekspertisan. Quyidagi fayl nomi yoki kanal izohidan bu AYNAN QAYSI KINO yoki MULTFILM ekanini 100% aniqlikda top.
Fayl/izoh matni: "{raw_text}"
Tozalangan nom: "{clean_hint}"

Qoidalar:
1. Agar matnda o'zbekcha yoki ruscha tarjima nomi yozilgan bo'lsa (masalan: "Qasoskorlar final", "Forsaj 10", "Uyda yolg'iz", "O'rgimchak odam"), kinoning o'zbek tilidagi eng to'g'ri rasmiy nomini "title" ga, asl inglizcha nomini esa "search_en" ga yoz!
2. Chiqarilgan aniq yilini ("year"), 1-2 ta asosiy janrini ("genre"), davlatini ("country") to'g'ri top.
3. Faqat quyidagi JSON formatda javob ber (boshqa hech qanday matn yoki markdown yozma):
{{
  "title": "Kinoning o'zbekcha rasmiy nomi",
  "search_en": "Kinoning aniq inglizcha original nomi (poster topish uchun)",
  "search_alt": "Kinoning ruscha yoki asl tilidagi nomi",
  "year": "Chiqarilgan aniq yili (masalan: 2019)",
  "genre": "1 yoki 2 ta janr o'zbek tilida (masalan: Jangari, Fantastika)",
  "country": "Davlati o'zbek tilida (masalan: AQSH)",
  "type": "movie yoki cartoon",
  "description": "O'zbek tilida 2 jumlali juda qiziqarli va jozibador reklama tavsifi"
}}"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            resp = await client.post(url, json={"contents": [{"parts": [{"text": prompt}]}]})
            if resp.status_code == 200:
                data = resp.json()
                txt = data["candidates"][0]["content"]["parts"][0]["text"]
                m = re.search(r"\{.*\}", txt, re.S)
                if m:
                    parsed = json.loads(m.group(0))
                    fallback.update({k: str(v).strip() for k, v in parsed.items() if v})
                    fallback["genre"] = normalize_genre_str(fallback["genre"])
                    if fallback["type"] not in ("movie", "cartoon"):
                        fallback["type"] = "movie"
    except Exception as e:
        print("Gemini AI error:", e)
    return fallback
async def find_movie_poster(search_en: str, search_alt: str = "", year: str = "") -> str:
    default_poster = "https://images.unsplash.com/photo-1536440136628-849c177e76a1?w=600&q=80"
    queries = [q.strip() for q in [search_en, search_alt] if q and q.strip()]
    if not queries:
        return default_poster

    headers = {"User-Agent": "FilmoraProBot/2.0 (https://t.me/Filmorabot_bot)"}
    async with httpx.AsyncClient(timeout=12, headers=headers, follow_redirects=True) as client:
        for q in queries:
            try:
                r = await client.get(
                    "https://itunes.apple.com/search",
                    params={"term": f"{q} {year}".strip(), "media": "movie", "limit": 3},
                )
                if r.status_code == 200:
                    results = r.json().get("results", [])
                    if not results and year:
                        r2 = await client.get("https://itunes.apple.com/search", params={"term": q, "media": "movie", "limit": 3})
                        if r2.status_code == 200:
                            results = r2.json().get("results", [])
                    for item in results:
                        art = item.get("artworkUrl100")
                        if art:
                            return art.replace("100x100bb", "600x900bb")
            except Exception as e:
                print("iTunes poster error:", e)

        for q in queries:
            try:
                r = await client.get("https://api.tvmaze.com/singlesearch/shows", params={"q": q})
                if r.status_code == 200:
                    img = (r.json().get("image") or {}).get("original") or (r.json().get("image") or {}).get("medium")
                    if img:
                        return img
            except Exception as e:
                print("TVMaze poster error:", e)

        wiki_langs = [("en", f"{search_en} {year} film"), ("ru", f"{search_alt or search_en} фильм"), ("uz", f"{search_alt or search_en} film")]
        for lang, wq in wiki_langs:
            if not wq.strip():
                continue
            try:
                sr = await client.get(
                    f"https://{lang}.wikipedia.org/w/api.php",
                    params={"action": "query", "list": "search", "srsearch": wq.strip(), "format": "json", "srlimit": 1},
                )
                if sr.status_code == 200:
                    hits = sr.json().get("query", {}).get("search", [])
                    if hits:
                        page_title = hits[0]["title"]
                        pr = await client.get(
                            f"https://{lang}.wikipedia.org/w/api.php",
                            params={"action": "query", "titles": page_title, "prop": "pageimages", "pithumbsize": 800, "format": "json"},
                        )
                        if pr.status_code == 200:
                            pages = pr.json().get("query", {}).get("pages", {})
                            for pdata in pages.values():
                                thumb = (pdata.get("thumbnail") or {}).get("source")
                                if thumb and not thumb.lower().endswith(".svg.png"):
                                    return thumb
            except Exception as e:
                print(f"Wiki ({lang}) poster error:", e)

    return default_poster


async def publish_one_from_queue() -> tuple[bool, str]:
    if not DB.get("auto_queue"):
        return False, "Navbat bo‘sh"

    item = DB["auto_queue"].pop(0)
    try:
        copied = await bot.copy_message(
            chat_id=BAZA_CHANNEL_ID,
            from_chat_id=BACKUP_CHANNEL_ID,
            message_id=item["msg_id"],
        )
    except Exception as e:
        await save_db()
        return False, f"Zaxira kanaldan nusxalashda xato: {e}"

    meta = await generate_movie_meta_with_ai(item.get("raw_text", ""))
    poster_url = await find_movie_poster(
        search_en=meta.get("search_en") or meta["title"],
        search_alt=meta.get("search_alt", ""),
        year=meta.get("year", ""),
    )
    code = get_next_movie_code()

    DB["movies"][code] = {
        "episodes": [copied.message_id],
        "title": meta["title"],
        "year": meta["year"],
        "genre": meta["genre"],
        "country": meta["country"],
        "poster": poster_url,
        "type": meta["type"],
        "premium": False,
        "views": 0,
        "ep_views": {"1": 0},
        "votes": [],
        "created_at": int(time.time()),
    }
    DB["settings"]["last_auto_post"] = int(time.time())
    await save_db()

    ad_caption = (
        f"🎬 <b>{meta['title']}</b> ({meta['year']})\n\n"
        f"🎭 <b>Janr:</b> {meta['genre']}\n"
        f"🌍 <b>Davlat:</b> {meta['country']}\n"
        f"🇺🇿 <b>Til:</b> O‘zbek tilida\n\n"
        f"📝 <i>{meta['description']}</i>\n\n"
        f"🔢 <b>Kino kodi:</b> <code>{code}</code>\n"
        f"🤖 <b>Bot:</b> @{BOT_USERNAME}"
    )
    ad_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Kinoni tomosha qilish", url=f"https://t.me/{BOT_USERNAME}?start=m_{code}")],
        [InlineKeyboardButton(text="🍿 FILMORA PRO Katalog", url=f"https://t.me/{BOT_USERNAME}")],
    ])
    try:
        await bot.send_photo(
            chat_id=PUBLIC_CHANNEL,
            photo=poster_url,
            caption=ad_caption,
            parse_mode="HTML",
            reply_markup=ad_kb,
        )
    except Exception as e:
        print("Public channel post error:", e)
        try:
            await bot.send_message(chat_id=PUBLIC_CHANNEL, text=ad_caption, parse_mode="HTML", reply_markup=ad_kb)
        except Exception:
            pass

    return True, f"✅ [{code}] <b>{meta['title']}</b> ({meta['year']}) joylandi va {PUBLIC_CHANNEL} ga chiqarildi!"


async def auto_publisher_worker():
    await asyncio.sleep(20)
    while True:
        try:
            if DB["settings"].get("auto_enabled", True) and DB.get("auto_queue"):
                last_ts = DB["settings"].get("last_auto_post", 0)
                if int(time.time()) - last_ts >= AUTO_POST_INTERVAL:
                    ok, info = await publish_one_from_queue()
                    if ok:
                        try:
                            await bot.send_message(ADMIN_ID, f"🤖 <b>Avto-kino hisoboti:</b>\n{info}\n📦 Navbatda qoldi: <b>{len(DB['auto_queue'])}</b> ta", parse_mode="HTML")
                        except Exception:
                            pass
        except Exception as e:
            print("Auto worker loop error:", e)
        await asyncio.sleep(300)


@dp.channel_post()
async def on_backup_channel_post(message: types.Message):
    if message.chat.id != BACKUP_CHANNEL_ID:
        return
    if not (message.video or message.document):
        return
    file_name = ""
    if message.video and message.video.file_name:
        file_name = message.video.file_name
    elif message.document and message.document.file_name:
        file_name = message.document.file_name

    combined_raw = " | ".join(p for p in [message.caption or "", file_name] if p.strip())
    cleaned = clean_video_hint(combined_raw)

    if any(q.get("msg_id") == message.message_id for q in DB["auto_queue"]):
        return

    DB["auto_queue"].append({
        "msg_id": message.message_id,
        "raw_text": combined_raw,
        "added_at": int(time.time()),
    })
    await save_db()

    if not cleaned:
        try:
            ask_msg = await bot.send_message(
                ADMIN_ID,
                f"⚠️ Zaxira kanalga <b>nomsiz video</b> tashlandi (ID: <code>{message.message_id}</code>).\n"
                f"100% aniq chiqishi uchun <b>shu xabarga Reply (Javob) qilib kino nomini yozing</b> (masalan: <code>Qasoskorlar 4 2019</code>):",
                parse_mode="HTML",
            )
            PENDING_NAME_PROMPTS[ask_msg.message_id] = message.message_id
        except Exception:
            pass
def is_premium(user_id: int) -> bool:
    return user_id == ADMIN_ID or DB["premium_users"].get(str(user_id), 0) > int(time.time())


async def get_unsubscribed_channels(user_id: int):
    if user_id == ADMIN_ID or not DB["settings"].get("sub_enabled", True):
        return []
    missing = []
    for ch in DB["channels"]:
        if not ch.get("active", True):
            continue
        try:
            member = await bot.get_chat_member(chat_id=ch["id"], user_id=user_id)
            if member.status not in ["member", "administrator", "creator"]:
                missing.append(ch)
        except Exception:
            pass
    return missing


def sub_keyboard(missing_channels):
    rows = [[InlineKeyboardButton(text=f"📢 {ch['title']}", url=ch["url"])] for ch in missing_channels]
    rows.append([InlineKeyboardButton(text="✅ Obunani tekshirish", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_reply_keyboard(user_id: int):
    kb = [
        [KeyboardButton(text="🎬 FILMORA PRO (Katalog)")],
        [KeyboardButton(text="🔎 Kino kodi"), KeyboardButton(text="💎 VIP / Premium")],
        [KeyboardButton(text="📝 Kino zakaz"), KeyboardButton(text="👤 Profilim")],
    ]
    if user_id == ADMIN_ID:
        kb.append([KeyboardButton(text="⚙️ Admin Panel")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


def get_rating_str(m: dict) -> str:
    votes = m.get("votes", [])
    return f"{round(sum(votes) / len(votes), 1)}/5 ({len(votes)} ta ovoz)" if votes else "hali baho yo‘q"


def movie_inline_kb(code: str, m: dict, ep_idx: int = 1):
    episodes = m.get("episodes", [])
    rows = []
    if m.get("type") == "series" and len(episodes) > 1:
        nav = []
        if ep_idx > 1:
            nav.append(InlineKeyboardButton(text=f"⬅️ {ep_idx - 1}-qism", callback_data=f"ep:{code}:{ep_idx - 1}"))
        if ep_idx < len(episodes):
            nav.append(InlineKeyboardButton(text=f"{ep_idx + 1}-qism ➡️", callback_data=f"ep:{code}:{ep_idx + 1}"))
        if nav:
            rows.append(nav)
        rows.append([InlineKeyboardButton(text="📑 Barcha qismlar", callback_data=f"alleps:{code}"), InlineKeyboardButton(text="🔔 Kuzatish", callback_data=f"subser:{code}")])
    rows.append([InlineKeyboardButton(text="❤️ Saralanganlarga", callback_data=f"fav:{code}"), InlineKeyboardButton(text="📤 Ulashish", switch_inline_query=f"🎬 {m['title']} (Kod: {code})")])
    rows.append([InlineKeyboardButton(text=f"{i}⭐", callback_data=f"rate:{code}:{i}") for i in range(1, 6)])
    rows.append([InlineKeyboardButton(text="🎬 O‘xshash kinolar", web_app=WebAppInfo(url=MINI_APP_URL))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_content_to_user(user_id: int, code: str, ep_idx: int = 1):
    m = DB["movies"].get(code)
    if not m:
        return
    u = ensure_user(user_id)
    episodes = m.get("episodes") or ([m["msg_id"]] if "msg_id" in m else [])
    if not episodes:
        return
    ep_idx = max(1, min(ep_idx, len(episodes)))
    m["views"] = m.get("views", 0) + 1
    ep_views = m.setdefault("ep_views", {})
    ep_views[str(ep_idx)] = ep_views.get(str(ep_idx), 0) + 1
    if m.get("type") == "series":
        u["progress"][code] = ep_idx
        title_line = f"🎬 <b>{m['title']}</b>\n📺 <b>{ep_idx}-qism</b> ({len(episodes)} ta qismdan)"
    else:
        title_line = f"🎬 <b>{m['title']}</b> ({m.get('year', '')})"
    u["history"] = [h for h in u["history"] if h.get("code") != code]
    u["history"].insert(0, {"code": code, "ep": ep_idx, "date": datetime.now().strftime("%d-%b")})
    u["history"] = u["history"][:30]
    await save_db()
    caption = (
        f"{title_line}\n\n🎭 Janr: {m.get('genre', 'Drama')}\n🌍 Davlat: {m.get('country', 'AQSH')}\n"
        f"🇺🇿 Til: O‘zbek tilida\n⭐ Reyting: {get_rating_str(m)}\n👁 Ko‘rishlar: {m['views']}\n\n"
        f"🎬 <b>FILMORA | PRO</b> BILAN ZAVQ OLING\n🔢 Kod: <code>{code}</code> • @{BOT_USERNAME}"
    )
    await bot.copy_message(user_id, BAZA_CHANNEL_ID, episodes[ep_idx - 1], caption=caption, parse_mode="HTML", reply_markup=movie_inline_kb(code, m, ep_idx))


class AdminStates(StatesGroup):
    add_kind = State()
    add_code = State()
    add_title = State()
    add_year = State()
    add_genre = State()
    add_country = State()
    add_poster = State()
    add_premium = State()
    add_video = State()
    ep_code = State()
    ep_video = State()
    edit_code = State()
    edit_new_title = State()
    del_code = State()
    ch_title = State()
    ch_id = State()
    ch_url = State()
    set_card = State()
    set_price = State()
    give_prem_id = State()
    broadcast_text = State()
    wait_receipt = State()
    wait_order = State()


def admin_panel_kb():
    sub_state = "🟢 Yoqilgan" if DB["settings"].get("sub_enabled", True) else "🔴 O‘chirilgan"
    q_len = len(DB.get("auto_queue", []))
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🤖 Avto-kino (Navbat: {q_len})", callback_data="adm_auto_menu")],
            [InlineKeyboardButton(text="🎬 Kino / Multfilm joylash", callback_data="add_kind:movie")],
            [InlineKeyboardButton(text="📺 Serial joylash (1-qism)", callback_data="add_kind:series"), InlineKeyboardButton(text="➕ Qism joylash", callback_data="adm_add_ep")],
            [InlineKeyboardButton(text="✏️ Tahrirlash", callback_data="adm_edit_movie"), InlineKeyboardButton(text="🗑 O‘chirish", callback_data="adm_del_movie")],
            [InlineKeyboardButton(text="📢 Majburiy obuna kanallari", callback_data="adm_channels")],
            [InlineKeyboardButton(text=f"🔒 Majburiy obuna: {sub_state}", callback_data="adm_toggle_sub")],
            [InlineKeyboardButton(text="💳 Karta raqami", callback_data="adm_set_card"), InlineKeyboardButton(text="💰 VIP narxi", callback_data="adm_set_price")],
            [InlineKeyboardButton(text="💎 Foydalanuvchiga VIP berish", callback_data="adm_give_prem")],
            [InlineKeyboardButton(text="📊 Statistika", callback_data="adm_stats"), InlineKeyboardButton(text="📣 Xabar tarqatish", callback_data="adm_broadcast")],
        ]
    )


@dp.callback_query(F.data == "adm_auto_menu")
async def adm_auto_menu(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    enabled = DB["settings"].get("auto_enabled", True)
    q_len = len(DB.get("auto_queue", []))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Hoziroq 1 ta kino chiqarish", callback_data="adm_auto_now")],
        [InlineKeyboardButton(text=f"Holat: {'🟢 Yoqilgan' if enabled else '🔴 O‘chirilgan'}", callback_data="adm_auto_toggle")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back_panel")],
    ])
    await call.message.edit_text(
        f"🤖 <b>Avtomatik Kino Tizimi (Kuniga 5 ta)</b>\n\n"
        f"📥 Zaxira kanal ID: <code>{BACKUP_CHANNEL_ID}</code>\n"
        f"📢 Reklama kanal: <b>{PUBLIC_CHANNEL}</b>\n"
        f"📦 Navbatdagi kinolar: <b>{q_len} ta</b>",
        parse_mode="HTML",
        reply_markup=kb,
    )


@dp.callback_query(F.data == "adm_auto_toggle")
async def adm_auto_toggle(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    DB["settings"]["auto_enabled"] = not DB["settings"].get("auto_enabled", True)
    await save_db()
    await adm_auto_menu(call)


@dp.callback_query(F.data == "adm_auto_now")
async def adm_auto_now(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer("⏳ AI kino nomi va posterini tayyorlamoqda...", show_alert=False)
    ok, info = await publish_one_from_queue()
    await call.message.answer(info, parse_mode="HTML")
    await adm_auto_menu(call)


@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    is_new = str(uid) not in DB["users"]
    u = ensure_user(uid, message.from_user.full_name)
    parts = message.text.split()
    deep_movie_code = None

    if len(parts) > 1:
        param = parts[1]
        if param.startswith("m_"):
            deep_movie_code = param.replace("m_", "", 1).strip()
        elif is_new and param.startswith("ref_"):
            ref_id = param.replace("ref_", "")
            if ref_id != str(uid) and ref_id in DB["users"]:
                u["referred_by"] = ref_id
                if uid not in DB["users"][ref_id]["refs"]:
                    DB["users"][ref_id]["refs"].append(uid)
                    if len(DB["users"][ref_id]["refs"]) >= 5:
                        DB["premium_users"][ref_id] = max(DB["premium_users"].get(ref_id, int(time.time())), int(time.time())) + 30 * 86400
                        try:
                            await bot.send_message(int(ref_id), "🎉 5 ta do‘stingizni taklif qilganingiz uchun sizga <b>30 kunlik VIP</b> berildi!", parse_mode="HTML")
                        except Exception:
                            pass
    await save_db()
    missing = await get_unsubscribed_channels(uid)
    if missing:
        await message.answer("🎬 Kinolarni ko‘rish uchun avval kanallarga obuna bo‘ling:", reply_markup=sub_keyboard(missing))
        return

    if deep_movie_code and deep_movie_code in DB["movies"]:
        movie = DB["movies"][deep_movie_code]
        if movie.get("premium") and not is_premium(uid):
            await premium_info(message, None)
            return
        await send_content_to_user(uid, deep_movie_code, 1)
        return

    await message.answer("👋 Xush kelibsiz! <b>FILMORA | PRO</b> katalogini oching yoki kino kodini yuboring:", parse_mode="HTML", reply_markup=main_reply_keyboard(uid))


@dp.callback_query(F.data == "check_sub")
async def check_sub_cb(call: types.CallbackQuery):
    if not await get_unsubscribed_channels(call.from_user.id):
        await call.message.delete()
        await call.message.answer("✅ Obuna tasdiqlandi!", reply_markup=main_reply_keyboard(call.from_user.id))
    else:
        await call.answer("❌ Hali barcha kanallarga obuna bo‘lmagansiz!", show_alert=True)


@dp.message(F.text == "🎬 FILMORA PRO (Katalog)")
async def open_catalog_btn(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎬 FILMORA PRO Katalogini ochish", web_app=WebAppInfo(url=MINI_APP_URL))
    ]])
    await message.answer("🍿 <b>FILMORA | PRO</b> katalogiga kirish uchun quyidagi tugmani bosing:", parse_mode="HTML", reply_markup=kb)


@dp.message(F.text == "🔎 Kino kodi")
async def ask_code(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("✍️ Kino yoki serial kodini yozib yuboring:")


@dp.message(F.text == "👤 Profilim")
async def my_profile(message: types.Message):
    uid = message.from_user.id
    u = ensure_user(uid, message.from_user.full_name)
    prem = "💎 VIP a’zo" if is_premium(uid) else "🆓 Oddiy a’zo"
    await message.answer(f"👤 <b>{u['name']}</b>\n⭐ Holat: <b>{prem}</b>\n👥 Takliflar: <b>{len(u.get('refs', []))}/5</b>\n🔗 Havola: <code>https://t.me/{BOT_USERNAME}?start=ref_{uid}</code>", parse_mode="HTML")


@dp.message(F.text == "📝 Kino zakaz")
async def order_movie_start(message: types.Message, state: FSMContext):
    await state.set_state(AdminStates.wait_order)
    await message.answer("📝 Qaysi kino yoki serialni qo‘shishimizni xohlaysiz? Nomi va yilini yozing:")


@dp.message(AdminStates.wait_order, F.text)
async def order_movie_receive(message: types.Message, state: FSMContext):
    await state.clear()
    await bot.send_message(ADMIN_ID, f"📝 <b>Yangi zakaz!</b>\n👤 {message.from_user.full_name} (<code>{message.from_user.id}</code>)\n🎬 <b>{message.text}</b>", parse_mode="HTML")
    await message.answer("✅ Buyurtmangiz adminga yuborildi!")


@dp.message(F.text == "💎 VIP / Premium")
async def premium_info(message: types.Message, state: FSMContext = None):
    if state:
        await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Sotib olish (Chek yuborish)", callback_data="buy_premium")]])
    await message.answer(f"💎 <b>FILMORA VIP</b>\n💰 Narxi: <b>{DB['settings']['price']}</b>\n💳 Karta:\n<code>{DB['settings']['card']}</code>\n\nTo‘lov qilgach chek rasmini yuboring:", parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data == "buy_premium")
async def buy_premium_cb(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.wait_receipt)
    await call.message.answer("📸 To‘lov cheki rasmini yuboring:")
    await call.answer()


@dp.message(AdminStates.wait_receipt, F.photo)
async def receive_receipt(message: types.Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Tasdiqlash (30 kun)", callback_data=f"approve_prem:{uid}"), InlineKeyboardButton(text="❌ Rad etish", callback_data=f"reject_prem:{uid}")]])
    await bot.send_photo(ADMIN_ID, message.photo[-1].file_id, caption=f"💳 <b>Yangi VIP chek!</b>\n👤 {message.from_user.full_name}\n🆔 <code>{uid}</code>", parse_mode="HTML", reply_markup=kb)
    await message.answer("✅ Chekingiz adminga yuborildi!")


@dp.callback_query(F.data.startswith("approve_prem:"))
async def approve_prem_cb(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    uid = int(call.data.split(":")[1])
    DB["premium_users"][str(uid)] = int(time.time()) + 30 * 86400
    await save_db()
    await call.message.edit_caption(caption=call.message.caption + "\n\n✅ <b>TASDIQLANDI</b>", parse_mode="HTML")
    try:
        await bot.send_message(uid, "🎉 Sizga <b>30 kunlik VIP</b> yoqildi!", parse_mode="HTML")
    except Exception:
        pass


@dp.callback_query(F.data.startswith("reject_prem:"))
async def reject_prem_cb(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    uid = int(call.data.split(":")[1])
    await call.message.edit_caption(caption=call.message.caption + "\n\n❌ <b>RAD ETILDI</b>", parse_mode="HTML")
    try:
        await bot.send_message(uid, "❌ To‘lov chekingiz tasdiqlanmadi.")
    except Exception:
        pass
@dp.callback_query(F.data.startswith("ep:"))
async def ep_nav_cb(call: types.CallbackQuery):
    _, code, ep_s = call.data.split(":")
    await send_content_to_user(call.from_user.id, code, int(ep_s))
    await call.answer()


@dp.callback_query(F.data.startswith("alleps:"))
async def all_eps_cb(call: types.CallbackQuery):
    code = call.data.split(":")[1]
    m = DB["movies"].get(code)
    if not m:
        return
    btns = [InlineKeyboardButton(text=f"{i}-qism", callback_data=f"ep:{code}:{i}") for i in range(1, len(m.get("episodes", [])) + 1)]
    await call.message.answer(f"📑 <b>{m['title']}</b> — qismni tanlang:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[btns[i:i + 4] for i in range(0, len(btns), 4)]))
    await call.answer()


@dp.callback_query(F.data.startswith("fav:"))
async def fav_toggle_cb(call: types.CallbackQuery):
    code = call.data.split(":")[1]
    u = ensure_user(call.from_user.id)
    if code in u["favs"]:
        u["favs"].remove(code)
        await save_db()
        await call.answer("💔 Saralanganlardan olib tashlandi")
    else:
        u["favs"].append(code)
        await save_db()
        await call.answer("❤️ Saralanganlarga qo‘shildi!")


@dp.callback_query(F.data.startswith("subser:"))
async def sub_series_cb(call: types.CallbackQuery):
    code = call.data.split(":")[1]
    u = ensure_user(call.from_user.id)
    if code in u["subs"]:
        u["subs"].remove(code)
        await save_db()
        await call.answer("🔕 Kuzatish o‘chirildi")
    else:
        u["subs"].append(code)
        await save_db()
        await call.answer("🔔 Yangi qism chiqsa xabar beramiz!")


@dp.callback_query(F.data.startswith("rate:"))
async def rate_movie_cb(call: types.CallbackQuery):
    _, code, score_s = call.data.split(":")
    m = DB["movies"].get(code)
    if not m:
        return
    m.setdefault("votes", []).append(int(score_s))
    await save_db()
    await call.answer(f"⭐ {score_s} baho berdingiz! O‘rtacha: {get_rating_str(m)}", show_alert=True)


@dp.message(Command("panel"))
@dp.message(F.text == "⚙️ Admin Panel")
async def open_admin_panel(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer("⚙️ <b>FILMORA PRO — Admin Panel</b>", parse_mode="HTML", reply_markup=admin_panel_kb())


@dp.callback_query(F.data.startswith("add_kind:"))
async def adm_add_kind(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    kind = call.data.split(":")[1]
    await state.update_data(kind=kind)
    await state.set_state(AdminStates.add_code)
    await call.message.answer(f"1️⃣ {'Serial' if kind == 'series' else 'Kino'} kodini kiriting (masalan: <code>1</code>):", parse_mode="HTML")
    await call.answer()


@dp.message(AdminStates.add_code)
async def adm_add_code(message: types.Message, state: FSMContext):
    await state.update_data(code=message.text.strip())
    await state.set_state(AdminStates.add_title)
    await message.answer("2️⃣ Nomini kiriting (masalan: <code>Umar Ibn Hattob</code>):", parse_mode="HTML")


@dp.message(AdminStates.add_title)
async def adm_add_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(AdminStates.add_year)
    await message.answer("3️⃣ Yilini kiriting (masalan: <code>2012</code>):", parse_mode="HTML")


@dp.message(AdminStates.add_year)
async def adm_add_year(message: types.Message, state: FSMContext):
    await state.update_data(year=message.text.strip())
    await state.set_state(AdminStates.add_genre)
    await message.answer("4️⃣ Janrini kiriting (masalan: <code>Tarixiy, Drama</code>):", parse_mode="HTML")


@dp.message(AdminStates.add_genre)
async def adm_add_genre(message: types.Message, state: FSMContext):
    await state.update_data(genre=normalize_genre_str(message.text.strip()))
    await state.set_state(AdminStates.add_country)
    await message.answer("5️⃣ Davlatini kiriting (masalan: <code>Qatar, BAA</code>):", parse_mode="HTML")


@dp.message(AdminStates.add_country)
async def adm_add_country(message: types.Message, state: FSMContext):
    await state.update_data(country=message.text.strip())
    await state.set_state(AdminStates.add_poster)
    await message.answer("6️⃣ Poster rasmini yuboring (galereyadan rasm yoki havola):")


@dp.message(AdminStates.add_poster)
async def adm_add_poster(message: types.Message, state: FSMContext):
    if message.photo:
        poster = f"https://filmora-server.onrender.com/api/poster/{message.photo[-1].file_id}"
    elif message.text and message.text.startswith("http"):
        poster = message.text.strip()
    else:
        poster = "https://images.unsplash.com/photo-1536440136628-849c177e76a1?w=500&q=80"
    await state.update_data(poster=poster)
    await state.set_state(AdminStates.add_premium)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎬 Oddiy Kino", callback_data="mcat:movie:free"), InlineKeyboardButton(text="🧸 Multfilm", callback_data="mcat:cartoon:free")], [InlineKeyboardButton(text="💎 VIP (Pullik)", callback_data="mcat:same:prem")]])
    await message.answer("7️⃣ Toifasini tanlang:", reply_markup=kb)


@dp.callback_query(AdminStates.add_premium, F.data.startswith("mcat:"))
async def adm_set_mcat(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    _, sub_type, tier = call.data.split(":")
    final_type = "series" if data["kind"] == "series" else ("cartoon" if sub_type == "cartoon" else "movie")
    await state.update_data(final_type=final_type, premium=(tier == "prem"))
    await state.set_state(AdminStates.add_video)
    await call.message.answer("8️⃣ Endi <b>1-qism videosini</b> yuboring:" if final_type == "series" else "8️⃣ Endi <b>videosini</b> yuboring:", parse_mode="HTML")
    await call.answer()


@dp.message(AdminStates.add_video, F.video | F.document)
async def adm_save_video(message: types.Message, state: FSMContext):
    data = await state.get_data()
    sent = await bot.copy_message(BAZA_CHANNEL_ID, message.chat.id, message.message_id)
    DB["movies"][data["code"]] = {
        "episodes": [sent.message_id], "title": data["title"], "year": data["year"], "genre": data["genre"],
        "country": data["country"], "poster": data["poster"], "type": data["final_type"], "premium": data["premium"],
        "views": 0, "ep_views": {"1": 0}, "votes": [], "created_at": int(time.time()),
    }
    await save_db()
    await state.clear()
    msg = f"✅ <b>{data['title']}</b> (Kod: <code>{data['code']}</code>) <b>1-qism</b> bilan joylandi!\nKeyingi qismlarni <b>➕ Qism joylash</b> orqali qo‘shasiz." if data["final_type"] == "series" else f"✅ <b>{data['title']}</b> (Kod: <code>{data['code']}</code>) joylandi!"
    await message.answer(msg, parse_mode="HTML", reply_markup=admin_panel_kb())


@dp.callback_query(F.data == "adm_add_ep")
async def adm_add_ep(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.ep_code)
    await call.message.answer("➕ Qaysi serialga qism qo‘shmoqchisiz? <b>Serial kodini</b> yuboring:", parse_mode="HTML")
    await call.answer()


@dp.message(AdminStates.ep_code)
async def adm_ep_code(message: types.Message, state: FSMContext):
    code = message.text.strip()
    if code not in DB["movies"]:
        await message.answer("❌ Bunday kodli serial topilmadi.")
        await state.clear()
        return
    await state.update_data(code=code)
    await state.set_state(AdminStates.ep_video)
    cur = len(DB["movies"][code].get("episodes", []))
    await message.answer(f"📺 <b>{DB['movies'][code]['title']}</b> (hozir {cur} ta qism bor).\nEndi <b>{cur + 1}-qism</b> videosini yuboring:", parse_mode="HTML")


@dp.message(AdminStates.ep_video, F.video | F.document)
async def adm_ep_video(message: types.Message, state: FSMContext):
    data = await state.get_data()
    code = data["code"]
    sent = await bot.copy_message(BAZA_CHANNEL_ID, message.chat.id, message.message_id)
    m = DB["movies"][code]
    m.setdefault("episodes", []).append(sent.message_id)
    m["type"] = "series"
    ep_num = len(m["episodes"])
    m.setdefault("ep_views", {})[str(ep_num)] = 0
    await save_db()
    await state.clear()
    await message.answer(f"✅ <b>{m['title']}</b> — <b>{ep_num}-qism</b> joylandi!", parse_mode="HTML", reply_markup=admin_panel_kb())
    for suid, udata in DB["users"].items():
        if code in udata.get("subs", []):
            try:
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"▶️ {ep_num}-qismni ko‘rish", callback_data=f"ep:{code}:{ep_num}")]])
                await bot.send_message(int(suid), f"🔔 Siz kuzatayotgan <b>{m['title']}</b> serialining <b>{ep_num}-qismi</b> chiqdi!", parse_mode="HTML", reply_markup=kb)
            except Exception:
                pass


@dp.callback_query(F.data == "adm_edit_movie")
async def adm_edit_movie(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.edit_code)
    await call.message.answer("✏️ Kodini yuboring:")
    await call.answer()


@dp.message(AdminStates.edit_code)
async def adm_edit_code_step(message: types.Message, state: FSMContext):
    code = message.text.strip()
    if code not in DB["movies"]:
        await message.answer("❌ Topilmadi.")
        await state.clear()
        return
    await state.update_data(code=code)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📝 Nomini o‘zgartirish", callback_data="ed_act:title")], [InlineKeyboardButton(text="🔄 Oddiy ↔ VIP", callback_data="ed_act:toggle_prem")]])
    await message.answer(f"🎬 <b>{DB['movies'][code]['title']}</b>", parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data.startswith("ed_act:"))
async def adm_edit_action(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    code, act = data["code"], call.data.split(":")[1]
    if act == "toggle_prem":
        DB["movies"][code]["premium"] = not DB["movies"][code].get("premium", False)
        await save_db()
        await state.clear()
        await call.message.answer("✅ Turi o‘zgartirildi!", reply_markup=admin_panel_kb())
    else:
        await state.set_state(AdminStates.edit_new_title)
        await call.message.answer("📝 Yangi nomni kiriting:")
    await call.answer()


@dp.message(AdminStates.edit_new_title)
async def adm_save_new_title(message: types.Message, state: FSMContext):
    data = await state.get_data()
    DB["movies"][data["code"]]["title"] = message.text.strip()
    await save_db()
    await state.clear()
    await message.answer("✅ Yangilandi!", reply_markup=admin_panel_kb())


@dp.callback_query(F.data == "adm_del_movie")
async def adm_del_movie(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.del_code)
    await call.message.answer("🗑 O‘chiriladigan kodini yuboring:")
    await call.answer()


@dp.message(AdminStates.del_code)
async def adm_del_movie_done(message: types.Message, state: FSMContext):
    code = message.text.strip()
    if code in DB["movies"]:
        title = DB["movies"].pop(code)["title"]
        await save_db()
        await message.answer(f"🗑 <b>{title}</b> o‘chirildi!", parse_mode="HTML", reply_markup=admin_panel_kb())
    else:
        await message.answer("❌ Topilmadi.")
    await state.clear()


@dp.callback_query(F.data == "adm_toggle_sub")
async def adm_toggle_sub(call: types.CallbackQuery):
    DB["settings"]["sub_enabled"] = not DB["settings"].get("sub_enabled", True)
    await save_db()
    await call.message.edit_reply_markup(reply_markup=admin_panel_kb())
    await call.answer("O‘zgartirildi!")


@dp.callback_query(F.data == "adm_channels")
async def adm_channels_menu(call: types.CallbackQuery):
    rows = [[InlineKeyboardButton(text=f"{'🟢' if ch.get('active', True) else '🔴'} {ch['title']}", callback_data=f"ch_tog:{i}"), InlineKeyboardButton(text="🗑", callback_data=f"ch_del:{i}")] for i, ch in enumerate(DB["channels"])]
    rows.append([InlineKeyboardButton(text="➕ Kanal qo‘shish", callback_data="ch_add")])
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back_panel")])
    await call.message.edit_text("📢 <b>Majburiy kanallar:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.startswith("ch_tog:"))
async def adm_ch_toggle(call: types.CallbackQuery):
    idx = int(call.data.split(":")[1])
    DB["channels"][idx]["active"] = not DB["channels"][idx].get("active", True)
    await save_db()
    await adm_channels_menu(call)


@dp.callback_query(F.data.startswith("ch_del:"))
async def adm_ch_del(call: types.CallbackQuery):
    DB["channels"].pop(int(call.data.split(":")[1]))
    await save_db()
    await adm_channels_menu(call)


@dp.callback_query(F.data == "ch_add")
async def adm_ch_add(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.ch_title)
    await call.message.answer("1️⃣ Kanal nomini kiriting:")
    await call.answer()


@dp.message(AdminStates.ch_title)
async def adm_ch_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(AdminStates.ch_id)
    await message.answer("2️⃣ Kanal @username yoki -100... ID sini kiriting:")


@dp.message(AdminStates.ch_id)
async def adm_ch_id(message: types.Message, state: FSMContext):
    val = message.text.strip()
    await state.update_data(id=int(val) if val.lstrip("-").isdigit() else val)
    await state.set_state(AdminStates.ch_url)
    await message.answer("3️⃣ Kanal havolasini (https://t.me/...) yuboring:")


@dp.message(AdminStates.ch_url)
async def adm_ch_url(message: types.Message, state: FSMContext):
    data = await state.get_data()
    DB["channels"].append({"id": data["id"], "title": data["title"], "url": message.text.strip(), "active": True})
    await save_db()
    await state.clear()
    await message.answer("✅ Kanal qo‘shildi!", reply_markup=admin_panel_kb())


@dp.callback_query(F.data == "back_panel")
async def back_panel_cb(call: types.CallbackQuery):
    await call.message.edit_text("⚙️ <b>FILMORA PRO — Admin Panel</b>", parse_mode="HTML", reply_markup=admin_panel_kb())


@dp.callback_query(F.data == "adm_set_card")
async def adm_set_card(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.set_card)
    await call.message.answer("💳 Karta raqami va egasini kiriting:")
    await call.answer()


@dp.message(AdminStates.set_card)
async def adm_save_card(message: types.Message, state: FSMContext):
    DB["settings"]["card"] = message.text.strip()
    await save_db()
    await state.clear()
    await message.answer("✅ Karta saqlandi!", reply_markup=admin_panel_kb())


@dp.callback_query(F.data == "adm_set_price")
async def adm_set_price(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.set_price)
    await call.message.answer("💰 VIP narxini kiriting:")
    await call.answer()


@dp.message(AdminStates.set_price)
async def adm_save_price(message: types.Message, state: FSMContext):
    DB["settings"]["price"] = message.text.strip()
    await save_db()
    await state.clear()
    await message.answer("✅ Narx saqlandi!", reply_markup=admin_panel_kb())


@dp.callback_query(F.data == "adm_give_prem")
async def adm_give_prem(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.give_prem_id)
    await call.message.answer("💎 Foydalanuvchi ID raqamini kiriting:")
    await call.answer()


@dp.message(AdminStates.give_prem_id)
async def adm_give_prem_done(message: types.Message, state: FSMContext):
    uid = message.text.strip()
    if uid.isdigit():
        DB["premium_users"][uid] = int(time.time()) + 30 * 86400
        await save_db()
        await message.answer(f"✅ <code>{uid}</code> ga VIP berildi!", parse_mode="HTML", reply_markup=admin_panel_kb())
    await state.clear()


@dp.callback_query(F.data == "adm_stats")
async def adm_stats(call: types.CallbackQuery):
    active_prem = sum(1 for exp in DB["premium_users"].values() if exp > int(time.time()))
    await call.message.answer(f"📊 Foydalanuvchilar: <b>{len(DB['users'])}</b>\n💎 VIP: <b>{active_prem}</b>\n🎬 Jami kontent: <b>{len(DB['movies'])}</b>", parse_mode="HTML")
    await call.answer()


@dp.callback_query(F.data == "adm_broadcast")
async def adm_broadcast(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.broadcast_text)
    await call.message.answer("📣 Tarqatiladigan xabarni yuboring:")
    await call.answer()


@dp.message(AdminStates.broadcast_text)
async def adm_broadcast_send(message: types.Message, state: FSMContext):
    await state.clear()
    sent = 0
    for suid in DB["users"]:
        try:
            await bot.copy_message(int(suid), message.chat.id, message.message_id)
            sent += 1
        except Exception:
            pass
    await message.answer(f"✅ {sent} ta foydalanuvchiga yuborildi!", reply_markup=admin_panel_kb())


@dp.message(F.text)
async def send_movie_by_code(message: types.Message):
    uid = message.from_user.id

    if uid == ADMIN_ID and message.reply_to_message:
        prompt_id = message.reply_to_message.message_id
        if prompt_id in PENDING_NAME_PROMPTS:
            target_msg_id = PENDING_NAME_PROMPTS.pop(prompt_id)
            for item in DB.get("auto_queue", []):
                if item.get("msg_id") == target_msg_id:
                    item["raw_text"] = message.text.strip()
                    await save_db()
                    await message.answer(f"✅ Zo‘r! Navbatdagi video nomi <b>{message.text.strip()}</b> deb belgilandi.", parse_mode="HTML")
                    return

    missing = await get_unsubscribed_channels(uid)
    if missing:
        await message.answer("🎬 Kinolarni ko‘rish uchun avval kanallarga obuna bo‘ling:", reply_markup=sub_keyboard(missing))
        return
    code = message.text.strip()
    movie = DB["movies"].get(code)
    if not movie:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎬 Katalogni ochish", web_app=WebAppInfo(url=MINI_APP_URL))]])
        await message.answer("❌ Bunday kodli kino topilmadi:", reply_markup=kb)
        return
    if movie.get("premium") and not is_premium(uid):
        await premium_info(message, None)
        return
    try:
        await send_content_to_user(uid, code, 1)
    except Exception:
        await message.answer("⚠️ Yuborishda xatolik.")


class WatchRequest(BaseModel):
    user_id: int
    code: str
    ep: int = 1


class ActionRequest(BaseModel):
    user_id: int
    code: str
    score: int = 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    await load_db()
    await bot.delete_webhook(drop_pending_updates=True)
    task = asyncio.create_task(dp.start_polling(bot))
    auto_task = asyncio.create_task(auto_publisher_worker())
    yield
    task.cancel()
    auto_task.cancel()


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/")
async def health():
    return {"status": "ok", "movies": len(DB["movies"]), "queue": len(DB.get("auto_queue", []))}


@app.get("/api/poster/{file_id}")
async def get_poster(file_id: str):
    f = await bot.get_file(file_id)
    return RedirectResponse(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{f.file_path}")


@app.get("/api/state")
async def api_state(user_id: int = 0, name: str = "Foydalanuvchi"):
    items = []
    for c, m in DB["movies"].items():
        votes = m.get("votes", [])
        avg = round(sum(votes) / len(votes), 1) if votes else 0
        eps = m.get("episodes", [])
        items.append({
            "code": c, "type": m.get("type", "movie"), "title": m["title"], "year": m.get("year", "2026"),
            "genre": m.get("genre", "Drama"), "country": m.get("country", "AQSH"), "poster": m.get("poster", ""),
            "premium": m.get("premium", False), "views": m.get("views", 0), "ep_views": m.get("ep_views", {}),
            "rating": avg, "vote_count": len(votes), "ep_count": len(eps), "created_at": m.get("created_at", 0),
        })
    u = ensure_user(user_id, name) if user_id else {"name": name, "favs": [], "subs": [], "history": [], "progress": {}, "refs": []}
    return {
        "items": items,
        "user": {
            "name": u["name"], "is_vip": is_premium(user_id) if user_id else False, "favs": u["favs"],
            "subs": u["subs"], "history": u["history"], "progress": u["progress"],
            "refs_count": len(u.get("refs", [])), "ref_link": f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}",
        },
    }


@app.post("/api/fav")
async def api_fav(req: ActionRequest):
    u = ensure_user(req.user_id)
    added = req.code not in u["favs"]
    if added:
        u["favs"].append(req.code)
    else:
        u["favs"].remove(req.code)
    await save_db()
    return {"ok": True, "added": added, "favs": u["favs"]}


@app.post("/api/sub")
async def api_sub(req: ActionRequest):
    u = ensure_user(req.user_id)
    added = req.code not in u["subs"]
    if added:
        u["subs"].append(req.code)
    else:
        u["subs"].remove(req.code)
    await save_db()
    return {"ok": True, "added": added, "subs": u["subs"]}


@app.post("/api/rate")
async def api_rate(req: ActionRequest):
    m = DB["movies"].get(req.code)
    if not m:
        raise HTTPException(status_code=404, detail="Topilmadi")
    m.setdefault("votes", []).append(req.score)
    await save_db()
    votes = m["votes"]
    return {"ok": True, "rating": round(sum(votes) / len(votes), 1), "vote_count": len(votes)}


@app.post("/api/watch")
async def api_watch(req: WatchRequest):
    missing = await get_unsubscribed_channels(req.user_id)
    if missing:
        await bot.send_message(req.user_id, "🎬 Avval kanallarga obuna bo‘ling:", reply_markup=sub_keyboard(missing))
        return {"ok": False, "error": "not_subscribed"}
    movie = DB["movies"].get(req.code)
    if not movie:
        raise HTTPException(status_code=404, detail="Topilmadi")
    if movie.get("premium") and not is_premium(req.user_id):
        await bot.send_message(req.user_id, f"💎 <b>{movie['title']}</b> — VIP kontent!", parse_mode="HTML")
        return {"ok": False, "error": "premium_required"}
    try:
        await send_content_to_user(req.user_id, req.code, req.ep)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
