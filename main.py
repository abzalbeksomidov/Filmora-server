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
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")
ADMIN_ID = 6248171499
BAZA_CHANNEL_ID = -1003958730565
BACKUP_CHANNEL_ID = -1005905529010
PUBLIC_CHANNEL = "@filmora_kinolar"
MINI_APP_URL = "https://abzalbeksomidov.github.io/Filmora-app/"
BOT_USERNAME = "Filmorabot_bot"
AUTO_INTERVAL = 17280  # Har 4.8 soatda (kuniga 5 ta kino)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

DB = {
    "movies": {},
    "queue": [],
    "next_auto_code": 1001,
    "channels": [{"id": "@filmora_kinolar", "title": "Filmora Kinolar", "url": "https://t.me/filmora_kinolar", "active": True}],
    "premium_users": {},
    "users": {},
    "settings": {
        "card": "Karta kiritilmagan (/panel)",
        "price": "15 000 so‘m (1 oy)",
        "sub_enabled": True,
        "auto_publish": True,
        "last_auto_time": 0,
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
            if "queue" not in loaded:
                loaded["queue"] = []
            if "next_auto_code" not in loaded:
                nums = [int(k) for k in loaded.get("movies", {}).keys() if str(k).isdigit()]
                loaded["next_auto_code"] = max(nums + [1000]) + 1
            loaded.setdefault("settings", {})
            loaded["settings"].setdefault("auto_publish", True)
            loaded["settings"].setdefault("last_auto_time", 0)
            DB.update(loaded)
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


# ================= ZAXIRA KANAL, GEMINI VA AVTO-REKLAMA =================
def extract_caption_details(raw_text: str) -> dict:
    info = {"title": "", "year": "", "genre": "", "country": ""}
    if not raw_text:
        return info
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
            if not clean_l or re.match(r'^(PREMYERA|Yili|Janr|Davlat|KinoPoisk|IMDb|O\'zbek|Til|Sifat|Kanal|Obuna)', clean_l, re.I):
                continue
            lines.append(clean_l)
        if lines:
            info["title"] = re.sub(r'[^\w\s\'\-\:\(\)]', '', lines[0]).strip()
    return info


def is_junk_filename(fname: str) -> bool:
    if not fname:
        return True
    base = os.path.splitext(fname)[0].strip()
    if len(base) <= 4:
        return True
    return bool(re.match(r'^(VID|MOV|IMG|VIDEO|FILE|DOC|MP4|HD|FULL|NEW|KINO|FILM)[\s_\-\d]*$', base, re.I))


async def fetch_itunes_poster(search_query: str) -> str:
    if not search_query:
        return ""
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get(
                "https://itunes.apple.com/search",
                params={"term": search_query, "media": "movie", "limit": 5},
            )
            results = r.json().get("results", [])
            if results and results[0].get("artworkUrl100"):
                return results[0]["artworkUrl100"].replace("100x100bb", "600x900bb")
    except Exception as e:
        print("iTunes error:", e)
    return ""


async def fetch_tmdb_poster(title: str, year: str = "") -> str:
    if not TMDB_API_KEY or not title:
        print("TMDB skipped: key or title empty")
        return ""
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            for lang in ("en-US", "ru-RU"):
                params = {"api_key": TMDB_API_KEY, "query": title, "language": lang}
                if year and year.isdigit():
                    params["year"] = year
                r = await client.get("https://api.themoviedb.org/3/search/movie", params=params)
                data = r.json()
                if data.get("status_message"):
                    print("TMDB API message:", data.get("status_message"))
                for item in data.get("results", []):
                    if item.get("poster_path"):
                        return f"https://image.tmdb.org/t/p/w500{item['poster_path']}"
                if "year" in params:
                    del params["year"]
                    r2 = await client.get("https://api.themoviedb.org/3/search/movie", params=params)
                    for item in r2.json().get("results", []):
                        if item.get("poster_path"):
                            return f"https://image.tmdb.org/t/p/w500{item['poster_path']}"
    except Exception as e:
        print("TMDB error:", e)
    return ""


async def fetch_wiki_poster(query: str) -> str:
    if not query:
        return ""
    try:
        async with httpx.AsyncClient(timeout=12, headers={"User-Agent": "FilmoraBot/1.0"}) as client:
            r = await client.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "generator": "search",
                    "gsrsearch": f"{query} film poster",
                    "gsrlimit": 3,
                    "prop": "pageimages",
                    "piprop": "thumbnail",
                    "pithumbsize": 600,
                    "format": "json",
                },
            )
            pages = r.json().get("query", {}).get("pages", {})
            for p in pages.values():
                thumb = p.get("thumbnail", {}).get("source")
                if thumb and thumb.startswith("http"):
                    return thumb
    except Exception as e:
        print("Wiki error:", e)
    return ""


async def identify_movie_with_gemini(raw_hint: str, parsed: dict) -> dict:
    title_uz = parsed.get("title") or raw_hint
    year = parsed.get("year") or "2024"
    genre = parsed.get("genre") or "Jangari, Drama"
    country = parsed.get("country") or "AQSH"
    desc = (
        f"{title_uz} ({year}) filmi keskin burilishlar va unutilmas sahnalari bilan "
        f"tomoshabinni ilk daqiqalardanoq o‘ziga tortadi. To‘liq versiyani botimiz orqali hoziroq ko‘ring!"
    )
    english_query = title_uz

    if GEMINI_API_KEY:
        prompt = (
            f"Quyidagi ma'lumot asosida filmni aniqla:\n"
            f"Matn: {raw_hint}\nAjratilgan nom: {title_uz}, Yil: {year}, Davlat: {country}, Janr: {genre}\n\n"
            f"Faqat quyidagi JSON formatda javob ber (boshqa hech narsa yozma):\n"
            f'{{"title_uz": "O\'zbekcha aniq nomi", "english_title": "Exact original or English movie title", '
            f'"year": "4 xonali yil", "genre": "Janri o\'zbekcha", "country": "Davlati o\'zbekcha", '
            f'"desc": "Kino haqida o\'zbek tilida 2 gaplik qiziqarli reklama tavsifi"}}'
        )
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
        )
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(url, json={"contents": [{"parts": [{"text": prompt}]}]})
                data = resp.json()
                if "candidates" in data and data["candidates"]:
                    text_out = data["candidates"][0]["content"]["parts"][0]["text"]
                    m = re.search(r"\{.*\}", text_out, re.S)
                    if m:
                        ai_data = json.loads(m.group(0))
                        title_uz = ai_data.get("title_uz") or title_uz
                        english_query = ai_data.get("english_title") or title_uz
                        year = str(ai_data.get("year") or year)
                        genre = ai_data.get("genre") or genre
                        country = ai_data.get("country") or country
                        desc = ai_data.get("desc") or desc
                else:
                    print("Gemini error response:", data)
        except Exception as e:
            print("Gemini exception:", e)

    print(f"Poster search -> raw={raw_hint!r}, title_uz={title_uz!r}, english={english_query!r}, year={year!r}")
    poster = (
        await fetch_tmdb_poster(english_query, year)
        or await fetch_tmdb_poster(title_uz, year)
        or await fetch_itunes_poster(f"{english_query} {year}")
        or await fetch_itunes_poster(english_query)
        or await fetch_wiki_poster(f"{english_query} {year}")
        or await fetch_itunes_poster(title_uz)
    )
    if not poster:
        print("Poster fallback used!")
        poster = "https://images.unsplash.com/photo-1536440136628-849c177e76a1?w=500&q=80"

    return {
        "title": title_uz,
        "year": year,
        "genre": genre,
        "country": country,
        "desc": desc,
        "poster": poster,
}
        
    


async def publish_next_queued_movie() -> tuple[bool, str]:
    if not DB.get("queue"):
        return False, "Navbatda kino yo‘q."

    item = DB["queue"].pop(0)
    while str(DB.get("next_auto_code", 1001)) in DB["movies"]:
        DB["next_auto_code"] = int(DB.get("next_auto_code", 1001)) + 1
    code = str(DB["next_auto_code"])
    DB["next_auto_code"] = int(code) + 1

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

    DB["movies"][code] = {
        "episodes": [copied.message_id],
        "title": item["title"],
        "year": item["year"],
        "genre": item["genre"],
        "country": item["country"],
        "poster": item["poster"],
        "type": "movie",
        "premium": False,
        "views": 0,
        "ep_views": {"1": 0},
        "votes": [],
        "created_at": int(time.time()),
    }
    DB["settings"]["last_auto_time"] = int(time.time())
    await save_db()

    ad_caption = (
        f"🔥 <b>PREMYERA | {item['title'].upper()} ({item['year']})</b>\n\n"
        f"🎭 <b>Janr:</b> {item['genre']}\n"
        f"🌍 <b>Davlat:</b> {item['country']}\n"
        f"🇺🇿 <b>Til:</b> O‘zbek tilida (HD)\n\n"
        f"📖 <i>{item['desc']}</i>\n\n"
        f"🔢 <b>Kino kodi:</b> <code>{code}</code>\n"
        f"🤖 <b>Bot orqali ko‘rish:</b> @{BOT_USERNAME}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="▶️ Kinoni tomosha qilish", url=f"https://t.me/{BOT_USERNAME}?start={code}")
    ]])
    try:
        await bot.send_photo(chat_id=PUBLIC_CHANNEL, photo=item["poster"], caption=ad_caption, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        return True, f"✅ Bazaga saqlandi (Kod: <code>{code}</code>), lekin kanalga post chiqishida xato: {e}"

    return True, f"✅ <b>{item['title']}</b> (Kod: <code>{code}</code>) baza va {PUBLIC_CHANNEL} kanaliga chiqarildi!"
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
    auto_state = "🟢 Yoqilgan" if DB["settings"].get("auto_publish", True) else "🔴 O‘chirilgan"
    q_len = len(DB.get("queue", []))
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🤖 Avto-navbat ({q_len} ta)", callback_data="adm_queue"), InlineKeyboardButton(text="▶️ Hoziroq chiqarish", callback_data="adm_pub_now")],
            [InlineKeyboardButton(text=f"⏱ Avto-taymer: {auto_state}", callback_data="adm_toggle_auto")],
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


@dp.channel_post(F.chat.id == BACKUP_CHANNEL_ID)
async def backup_channel_watcher(message: types.Message):
    media = message.video or message.document
    if not media:
        return
    raw_caption = message.caption or ""
    fname = getattr(media, "file_name", "") or ""
    parsed = extract_caption_details(raw_caption)

    if not parsed["title"] and is_junk_filename(fname):
        try:
            ask = await bot.send_message(
                ADMIN_ID,
                f"⚠️ <b>Zaxira kanalga nomsiz/shifrli kino tushdi!</b>\n"
                f"📁 Fayl: <code>{fname or 'Nomsiz'}</code>\n\n"
                f"Aniq poster va reklama posti yaratish uchun <b>shu xabarga Reply qilib kino nomini yozing</b> (masalan: <i>Daxshatli qo'rquv 2018</i>):",
                parse_mode="HTML",
            )
            PENDING_NAME_PROMPTS[ask.message_id] = message.message_id
        except Exception:
            pass
        return

    hint = raw_caption if raw_caption else os.path.splitext(fname)[0]
    info = await identify_movie_with_gemini(hint, parsed)
    DB.setdefault("queue", []).append({
        "backup_msg_id": message.message_id,
        "title": info["title"],
        "year": info["year"],
        "genre": info["genre"],
        "country": info["country"],
        "desc": info["desc"],
        "poster": info["poster"],
    })
    await save_db()
    try:
        await bot.send_message(
            ADMIN_ID,
            f"📥 <b>Avto-navbatga qo‘shildi!</b>\n\n"
            f"🎬 <b>Nomi:</b> {info['title']} ({info['year']})\n"
            f"🎭 <b>Janr:</b> {info['genre']}\n"
            f"⏳ <b>Navbatda jami:</b> {len(DB['queue'])} ta",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="▶️ Hoziroq kanalga chiqarish", callback_data="adm_pub_now")
            ]]),
        )
    except Exception:
        pass


@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    is_new = str(uid) not in DB["users"]
    u = ensure_user(uid, message.from_user.full_name)
    parts = message.text.split()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if is_new and arg.startswith("ref_"):
        ref_id = arg.replace("ref_", "")
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

    if arg and arg in DB["movies"]:
        movie = DB["movies"][arg]
        if movie.get("premium") and not is_premium(uid):
            await premium_info(message, None)
            return
        await send_content_to_user(uid, arg, 1)
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


@dp.callback_query(F.data == "adm_queue")
async def adm_queue_cb(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    q = DB.get("queue", [])
    if not q:
        await call.answer("Avto-navbat bo‘sh!", show_alert=True)
        return
    lines = [f"{i+1}. <b>{x['title']}</b> ({x['year']})" for i, x in enumerate(q[:15])]
    await call.message.answer("⏳ <b>Avto-navbatdagi kinolar:</b>\n\n" + "\n".join(lines), parse_mode="HTML")
    await call.answer()


@dp.callback_query(F.data == "adm_pub_now")
async def adm_pub_now_cb(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer("⏳ Kanalga reklama tayyorlanmoqda...", show_alert=False)
    ok, msg = await publish_next_queued_movie()
    await call.message.answer(msg, parse_mode="HTML")


@dp.callback_query(F.data == "adm_toggle_auto")
async def adm_toggle_auto_cb(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    DB["settings"]["auto_publish"] = not DB["settings"].get("auto_publish", True)
    await save_db()
    await call.message.edit_reply_markup(reply_markup=admin_panel_kb())
    await call.answer("Avto-taymer holati o‘zgardi!")


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
    await state.update_data(genre=message.text.strip())
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
    await call.message.answer(f"📊 Foydalanuvchilar: <b>{len(DB['users'])}</b>\n💎 VIP: <b>{active_prem}</b>\n🎬 Jami kontent: <b>{len(DB['movies'])}</b>\n⏳ Avto-navbat: <b>{len(DB.get('queue', []))}</b>", parse_mode="HTML")
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
    if (
        uid == ADMIN_ID
        and message.reply_to_message
        and message.reply_to_message.message_id in PENDING_NAME_PROMPTS
    ):
        backup_msg_id = PENDING_NAME_PROMPTS.pop(message.reply_to_message.message_id)
        parsed = extract_caption_details(message.text)
        info = await identify_movie_with_gemini(message.text, parsed)
        DB.setdefault("queue", []).append({
            "backup_msg_id": backup_msg_id,
            "title": info["title"],
            "year": info["year"],
            "genre": info["genre"],
            "country": info["country"],
            "desc": info["desc"],
            "poster": info["poster"],
        })
        await save_db()
        await message.reply(
            f"✅ <b>{info['title']} ({info['year']})</b> aniqlanib avto-navbatga qo‘shildi!",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="▶️ Hoziroq kanalga chiqarish", callback_data="adm_pub_now")
            ]]),
        )
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


async def auto_publisher_loop():
    while True:
        await asyncio.sleep(60)
        try:
            if DB["settings"].get("auto_publish", True) and DB.get("queue"):
                last_t = DB["settings"].get("last_auto_time", 0)
                if time.time() - last_t >= AUTO_INTERVAL:
                    ok, msg = await publish_next_queued_movie()
                    if ok:
                        await bot.send_message(ADMIN_ID, f"🤖 <b>Avto-reklama chiqarildi:</b>\n{msg}", parse_mode="HTML")
        except Exception as e:
            print("Auto publisher error:", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await load_db()
    await bot.delete_webhook(drop_pending_updates=True)
    task = asyncio.create_task(dp.start_polling(bot))
    auto_task = asyncio.create_task(auto_publisher_loop())
    yield
    task.cancel()
    auto_task.cancel()


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/")
async def health():
    return {"status": "ok", "movies": len(DB["movies"])}


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
        
