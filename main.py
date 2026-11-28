"""
FILMORA avtomatik import moduli.

Zaxira kanalga kino tashlansa:
  1) caption / fayl nomidan reklama va keraksiz so'zlar tozalanadi
  2) Claude kino nomi va yilini ajratadi
  3) TMDB dan janr, davlat, reyting, tavsif va VERTIKAL poster olinadi
  4) kino botning bazasiga qo'shiladi (mini app o'zi ko'radi)
  5) reklama kanalga poster + ma'lumot + "Ko'rish" tugmasi bilan post chiqadi

Kerakli environment o'zgaruvchilar (Render -> Environment):
  RESERVE_CHANNEL_ID   zaxira kanal ID si (masalan -1001234567890)
  AD_CHANNEL_ID        reklama kanal ID si yoki @username
  TMDB_API_KEY         themoviedb.org dan
  ANTHROPIC_API_KEY    console.anthropic.com dan (bo'lmasa oddiy regex ishlaydi)
"""
import os
import re
import json
import time
import html
import asyncio

import httpx
from aiogram import F, types
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

try:
    from anthropic import AsyncAnthropic
except Exception:  # anthropic o'rnatilmagan bo'lsa ham bot ishlayveradi
    AsyncAnthropic = None

RESERVE_ID = int(os.getenv("RESERVE_CHANNEL_ID", "0") or 0)
AD_CHANNEL = os.getenv("AD_CHANNEL_ID", "").strip()
TMDB_KEY = os.getenv("TMDB_API_KEY", "").strip()
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
AI_MODEL = "claude-haiku-5-5"
DELAY = 4  # postlar orasidagi kutish (soniya), Telegram limitidan oshmaslik uchun
DEFAULT_POSTER = "https://images.unsplash.com/photo-1536440136628-849c177e76a1?w=500&q=80"

GENRES_UZ = {
    28: "Jangari", 12: "Sarguzasht", 16: "Multfilm", 35: "Komediya", 80: "Kriminal",
    99: "Hujjatli", 18: "Drama", 10751: "Oilaviy", 14: "Fantastika", 36: "Tarixiy",
    27: "Qo‘rqinchli", 10402: "Musiqiy", 9648: "Detektiv", 10749: "Melodrama",
    878: "Ilmiy-fantastika", 10770: "Teleserial", 53: "Triller", 10752: "Harbiy", 37: "Vestern",
}
COUNTRIES_UZ = {
    "US": "AQSH", "GB": "Buyuk Britaniya", "RU": "Rossiya", "TR": "Turkiya", "IN": "Hindiston",
    "KR": "Janubiy Koreya", "JP": "Yaponiya", "CN": "Xitoy", "FR": "Fransiya", "DE": "Germaniya",
    "IT": "Italiya", "ES": "Ispaniya", "CA": "Kanada", "AU": "Avstraliya", "UZ": "O‘zbekiston",
    "KZ": "Qozog‘iston", "AE": "BAA", "QA": "Qatar", "EG": "Misr", "IR": "Eron", "HK": "Gonkong",
    "MX": "Meksika", "BR": "Braziliya", "SE": "Shvetsiya", "PL": "Polsha", "UA": "Ukraina",
}

# ---- main.py dan beriladi (setup orqali) ----
bot = None
DB = None
save_db = None
BAZA_ID = None
ADMIN_ID = None
BOT_USERNAME = None

queue: "asyncio.Queue" = asyncio.Queue()
stats = {"ok": 0, "fail": 0, "dup": 0}
_http = None
_ai = None


def setup(dp, bot_, db, save_db_, baza_id, admin_id, bot_username):
    global bot, DB, save_db, BAZA_ID, ADMIN_ID, BOT_USERNAME
    bot, DB, save_db, BAZA_ID, ADMIN_ID, BOT_USERNAME = bot_, db, save_db_, baza_id, admin_id, bot_username
    if not RESERVE_ID:
        print("auto_import: RESERVE_CHANNEL_ID berilmagan, modul o'chiq")
        return

    @dp.channel_post(F.chat.id == RESERVE_ID, F.video | F.document)
    async def on_reserve_post(message: types.Message):
        await queue.put(message)


def start():
    if RESERVE_ID:
        asyncio.create_task(worker())


# ------------------------------------------------------------------ tozalash
JUNK = re.compile(
    r"(@\w+|https?://\S+|t\.me/\S+|www\.\S+|\b(?:1080p|720p|480p|2160p|4k|hdrip|hd|webrip|web-dl|bluray|brrip|dvdrip|"
    r"x264|x265|hevc|aac|mkv|mp4|avi|uzbek|o'zbek|ozbek|tarjima|uzbekcha|kino|film|skachat|yuklab)\b)",
    re.I,
)


def regex_clean(raw: str) -> dict:
    text = re.sub(r"\.(mkv|mp4|avi|mov)$", "", raw.strip(), flags=re.I)
    text = text.replace(".", " ").replace("_", " ")
    year_m = re.search(r"\b(19[3-9]\d|20[0-3]\d)\b", text)
    year = int(year_m.group(1)) if year_m else None
    if year_m:
        text = text[: year_m.start()]
    text = JUNK.sub(" ", text)
    text = re.sub(r"[^\w\s:'’\-]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return {"title": text, "search_title": text, "year": year}


async def ai_parse(raw: str) -> dict:
    """Claude yordamida nom va yilni ajratadi. Xato bo'lsa regex ga qaytadi."""
    global _ai
    base = regex_clean(raw)
    if not (ANTHROPIC_KEY and AsyncAnthropic):
        return base
    try:
        _ai = _ai or AsyncAnthropic(api_key=ANTHROPIC_KEY)
        resp = await _ai.messages.create(
            model=AI_MODEL,
            max_tokens=200,
            system=(
                "Sen kino nomini aniqlovchi yordamchisan. Berilgan matn Telegram kanaldagi kino captioni yoki fayl nomi. "
                "Undagi reklama, @username, havola, sifat (1080p), tarjima/studiya nomlari kabi ortiqcha narsalarni tashla. "
                "Faqat JSON qaytar, boshqa hech narsa yozma: "
                '{"title": "matndagi toza nom", "search_title": "kinoning asl (ingliz) nomi, TMDB da qidirish uchun", "year": 2009 yoki null}. '
                "Agar nomni aniqlab bo'lmasa title ni bo'sh qoldir."
            ),
            messages=[{"role": "user", "content": raw[:800]}],
        )
        txt = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        txt = re.sub(r"```json|```", "", txt).strip()
        data = json.loads(txt)
        title = (data.get("title") or "").strip()
        if not title:
            return base
        year = data.get("year")
        return {
            "title": title,
            "search_title": (data.get("search_title") or title).strip(),
            "year": int(year) if str(year).isdigit() else base["year"],
        }
    except Exception as e:
        print("ai_parse xato:", e)
        return base


async def ai_short_uz(text: str, title: str) -> str:
    """Tavsifni 1-2 gapli o'zbekcha (lotin) qisqa matnga aylantiradi."""
    text = (text or "").strip()
    if not text:
        return ""
    if not (ANTHROPIC_KEY and AsyncAnthropic):
        return text[:220]
    try:
        global _ai
        _ai = _ai or AsyncAnthropic(api_key=ANTHROPIC_KEY)
        resp = await _ai.messages.create(
            model=AI_MODEL,
            max_tokens=250,
            system=(
                "Kino tavsifini o'zbek tilida (lotin yozuvida) 1-2 gapga, 200 belgidan oshmasdan qisqartir. "
                "Spoiler yozma. Faqat tayyor matnni qaytar."
            ),
            messages=[{"role": "user", "content": f"Kino: {title}\nTavsif: {text[:1200]}"}],
        )
        out = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        return out[:260] or text[:220]
    except Exception as e:
        print("ai_short_uz xato:", e)
        return text[:220]


# ---------------------------------------------------------------------- TMDB
async def tmdb(path: str, **params):
    global _http
    _http = _http or httpx.AsyncClient(timeout=20)
    params["api_key"] = TMDB_KEY
    r = await _http.get(f"https://api.themoviedb.org/3{path}", params=params)
    r.raise_for_status()
    return r.json()


async def tmdb_find(parsed: dict) -> dict | None:
    names = []
    for n in (parsed["search_title"], parsed["title"]):
        if n and n not in names:
            names.append(n)
    year = parsed.get("year")
    for name in names:
        for with_year in ([True, False] if year else [False]):
            params = {"query": name, "language": "en-US", "include_adult": "false"}
            if with_year:
                params["year"] = year
            res = (await tmdb("/search/movie", **params)).get("results", [])
            if res:
                best = res[0]
                if year:
                    for r in res:
                        ry = (r.get("release_date") or "")[:4]
                        if ry == str(year):
                            best = r
                            break
                return best
    return None


async def tmdb_details(movie_id: int) -> dict:
    d = await tmdb(f"/movie/{movie_id}", language="ru-RU")
    if not d.get("overview"):
        d_en = await tmdb(f"/movie/{movie_id}", language="en-US")
        d["overview"] = d_en.get("overview", "")
    return d


# ------------------------------------------------------------------ yordamchi
def next_code() -> str:
    nums = [int(c) for c in DB["movies"] if str(c).isdigit()]
    return str(max(nums) + 1 if nums else 1)


def is_duplicate(uid: str) -> bool:
    return any(m.get("uid") == uid for m in DB["movies"].values())


async def tg_retry(coro_factory):
    """Telegram 'flood wait' berganda kutib, qayta urinadi."""
    for _ in range(4):
        try:
            return await coro_factory()
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
    return await coro_factory()


async def notify_admin(text: str):
    try:
        await bot.send_message(ADMIN_ID, text, parse_mode="HTML")
    except Exception:
        pass


# -------------------------------------------------------------------- asosiy
async def process(message: types.Message):
    media = message.video or message.document
    uid = media.file_unique_id
    if is_duplicate(uid):
        stats["dup"] += 1
        return

    raw = (message.caption or "").strip()
    if not raw or len(re.sub(r"@\w+|https?://\S+|t\.me/\S+", "", raw).strip()) < 2:
        raw = getattr(media, "file_name", "") or raw
    parsed = await ai_parse(raw)
    if not parsed["title"]:
        stats["fail"] += 1
        await notify_admin(f"⚠️ Nomini aniqlab bo‘lmadi (zaxira kanal, xabar #{message.message_id}).\nMatn: <code>{html.escape(raw[:200])}</code>")
        return

    found = await tmdb_find(parsed) if TMDB_KEY else None
    if not found:
        stats["fail"] += 1
        await notify_admin(
            f"⚠️ TMDB da topilmadi: <b>{html.escape(parsed['title'])}</b> ({parsed.get('year') or '?'})\n"
            f"Zaxira kanal xabar #{message.message_id}. Qo‘lda joylang (/panel)."
        )
        return

    d = await tmdb_details(found["id"])
    title = parsed["title"]
    year = (d.get("release_date") or "")[:4] or str(parsed.get("year") or "")
    genre = ", ".join(GENRES_UZ.get(g["id"], g["name"]) for g in d.get("genres", [])[:3]) or "Drama"
    countries = [COUNTRIES_UZ.get(c["iso_3166_1"], c["name"]) for c in d.get("production_countries", [])[:2]]
    country = ", ".join(countries) or "AQSH"
    rating = round(d.get("vote_average") or 0, 1)
    poster = f"https://image.tmdb.org/t/p/w500{d['poster_path']}" if d.get("poster_path") else ""
    desc = await ai_short_uz(d.get("overview", ""), title)

    clean_caption = f"🎬 {title} ({year})" if year else f"🎬 {title}"

    # 1) botning baza kanaliga nusxa (toza caption bilan)
    sent = await tg_retry(lambda: bot.copy_message(BAZA_ID, message.chat.id, message.message_id, caption=clean_caption))

    # 2) bot bazasiga yozish (mini app shu bazadan oladi)
    code = next_code()
    DB["movies"][code] = {
        "episodes": [sent.message_id], "title": title, "year": year, "genre": genre, "country": country,
        "poster": poster or DEFAULT_POSTER, "type": "cartoon" if 16 in [g["id"] for g in d.get("genres", [])] else "movie",
        "premium": False, "views": 0, "ep_views": {"1": 0}, "votes": [], "created_at": int(time.time()),
        "uid": uid, "tmdb_id": found["id"], "tmdb_rating": rating,
    }
    await save_db()

    # 3) zaxira kanaldagi reklama yozuvlarini tozalash
    try:
        await tg_retry(lambda: bot.edit_message_caption(chat_id=message.chat.id, message_id=message.message_id, caption=clean_caption))
    except Exception as e:
        print("zaxira caption tozalanmadi:", e)

    # 4) reklama kanalga post
    if AD_CHANNEL:
        ad_text = (
            f"🎬 <b>{html.escape(title)}</b> ({year})\n\n"
            f"🎭 Janr: {html.escape(genre)}\n🌍 Davlat: {html.escape(country)}\n"
            f"⭐ Reyting: {rating}/10\n\n"
            + (f"📝 {html.escape(desc)}\n\n" if desc else "")
            + f"🔢 Kod: <code>{code}</code>"
        )[:1000]
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🎬 Ko‘rish", url=f"https://t.me/{BOT_USERNAME}?start=kino_{code}")
        ]])
        try:
            if poster:
                await tg_retry(lambda: bot.send_photo(AD_CHANNEL, poster, caption=ad_text, parse_mode="HTML", reply_markup=kb))
            else:
                await tg_retry(lambda: bot.send_message(AD_CHANNEL, ad_text, parse_mode="HTML", reply_markup=kb))
        except Exception as e:
            await notify_admin(f"⚠️ Reklama kanalga post chiqmadi ({html.escape(title)}): {html.escape(str(e))}")

    stats["ok"] += 1


async def worker():
    while True:
        message = await queue.get()
        try:
            await process(message)
        except Exception as e:
            stats["fail"] += 1
            print("auto_import xato:", e)
            await notify_admin(f"⚠️ Import xatosi (xabar #{message.message_id}): {html.escape(str(e)[:200])}")
        queue.task_done()
        if queue.empty():
            await notify_admin(
                f"✅ Navbat tugadi.\nQo‘shildi: <b>{stats['ok']}</b>\nTopilmadi/xato: <b>{stats['fail']}</b>\nTakror: <b>{stats['dup']}</b>"
            )
            stats.update(ok=0, fail=0, dup=0)
        await asyncio.sleep(DELAY)
import os
import io
import json
import time
import asyncio
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from aiogram.exceptions import TelegramBadRequest
import auto_import
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
    BufferedInputFile,
    InputMediaDocument,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 6248171499
BAZA_CHANNEL_ID = -1003958730565
MINI_APP_URL = "https://abzalbeksomidov.github.io/Filmora-app/"
BOT_USERNAME = "Filmorabot_bot"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

DB = {
    "movies": {},
    "channels": [{"id": "@filmora_kinolar", "title": "Filmora Kinolar", "url": "https://t.me/filmora_kinolar", "active": True}],
    "premium_users": {},
    "users": {},
    "settings": {"card": "Karta kiritilmagan (/panel)", "price": "15 000 so‘m (1 oy)", "sub_enabled": True},
}
DB_MSG_ID = None


def ensure_user(uid: int, name: str = "Foydalanuvchi"):
    suid = str(uid)
    if suid not in DB["users"]:
        DB["users"][suid] = {"name": name, "favs": [], "subs": [], "history": [], "progress": {}, "refs": [], "referred_by": None}
    elif name and name != "Foydalanuvchi":
        DB["users"][suid]["name"] = name
    return DB["users"][suid]


_save_lock = asyncio.Lock()


async def load_db():
    """Baza endi pin qilingan filmora_db.json FAYLDA saqlanadi (matn xabar 4096 belgidan oshib ketardi)."""
    global DB_MSG_ID
    last_err = None
    for _ in range(3):
        try:
            chat = await bot.get_chat(BAZA_CHANNEL_ID)
            pm = chat.pinned_message
            loaded = None
            if pm and pm.document and pm.document.file_name == "filmora_db.json":
                buf = io.BytesIO()
                await bot.download(pm.document.file_id, destination=buf)
                loaded = json.loads(buf.getvalue().decode("utf-8"))
                DB_MSG_ID = pm.message_id
            elif pm and pm.text and pm.text.startswith("FILMORA_DB:"):
                loaded = json.loads(pm.text.replace("FILMORA_DB:", "", 1).strip())
                DB_MSG_ID = None  # eski matnli baza: keyingi saqlashda faylga ko'chadi
            if loaded is not None:
                if isinstance(loaded.get("users"), list):
                    loaded["users"] = {str(u): {"name": "Foydalanuvchi", "favs": [], "subs": [], "history": [], "progress": {}, "refs": [], "referred_by": None} for u in loaded["users"]}
                DB.update(loaded)
                if DB_MSG_ID is None:
                    await save_db()
                return
            await save_db()  # baza umuman yo'q: birinchi marta yaratamiz
            return
        except Exception as e:
            last_err = e
            print("DB load error:", e)
            await asyncio.sleep(2)
    # bazani o'qib bo'lmadi: bo'sh baza bilan ustidan yozib yubormaslik uchun ishga tushmaymiz
    raise RuntimeError(f"DB o'qilmadi: {last_err}")


async def save_db():
    global DB_MSG_ID
    data = json.dumps(DB, ensure_ascii=False).encode("utf-8")
    async with _save_lock:
        doc = BufferedInputFile(data, filename="filmora_db.json")
        try:
            if DB_MSG_ID:
                try:
                    await bot.edit_message_media(chat_id=BAZA_CHANNEL_ID, message_id=DB_MSG_ID, media=InputMediaDocument(media=doc))
                    return
                except TelegramBadRequest as e:
                    if "not modified" in str(e):
                        return
                    print("DB edit error:", e)
            msg = await bot.send_document(BAZA_CHANNEL_ID, doc, disable_notification=True)
            DB_MSG_ID = msg.message_id
            await bot.pin_chat_message(chat_id=BAZA_CHANNEL_ID, message_id=DB_MSG_ID, disable_notification=True)
        except Exception as e:
            print("DB save error:", e)


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
    return InlineKeyboardMarkup(
        inline_keyboard=[
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


@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    is_new = str(uid) not in DB["users"]
    u = ensure_user(uid, message.from_user.full_name)
    parts = message.text.split()
    if is_new and len(parts) > 1 and parts[1].startswith("ref_"):
        ref_id = parts[1].replace("ref_", "")
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
    if len(parts) > 1 and parts[1].startswith("kino_"):
        code = parts[1][5:]
        m = DB["movies"].get(code)
        if m and (not m.get("premium") or is_premium(uid)):
            await message.answer("🍿 Marhamat!", reply_markup=main_reply_keyboard(uid))
            await send_content_to_user(uid, code, 1)
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


auto_import.setup(dp, bot, DB, save_db, BAZA_CHANNEL_ID, ADMIN_ID, BOT_USERNAME)


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
    auto_import.start()
    await bot.delete_webhook(drop_pending_updates=True)
    task = asyncio.create_task(dp.start_polling(bot))
    yield
    task.cancel()


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

    
