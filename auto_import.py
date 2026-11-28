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
