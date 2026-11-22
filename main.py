import os
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

BOT_TOKEN = os.getenv("BOT_TOKEN")
BAZA_CHANNEL_ID = -1003958730565
MAIN_CHANNEL = "@filmora_kinolar"
MINI_APP_URL = "https://abzalbeksomidov.github.io/Filmora-app/"

# KINO KODI : BAZA KANALDAGI XABAR ID RAQAMI
MOVIES = {
    "1000": 2,
    "999": 2,
    "998": 2,
}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=MAIN_CHANNEL, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception:
        return True

def sub_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Kanalga obuna bo‘lish", url="https://t.me/filmora_kinolar")],
        [InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_sub")]
    ])

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    if not await is_subscribed(message.from_user.id):
        await message.answer("🎬 Kinolarni ko‘rish uchun avval asosiy kanalimizga obuna bo‘ling:", reply_markup=sub_keyboard())
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Kinolar katalogini ochish", web_app=WebAppInfo(url=MINI_APP_URL))]
    ])
    await message.answer(
        "👋 Assalomu alaykum! <b>Filmora Kinolar</b> botiga xush kelibsiz.\n\n"
        "✍️ Kino kodini yuboring yoki pastdagi tugma orqali katalogni oching:",
        parse_mode="HTML",
        reply_markup=kb
    )

@dp.callback_query(F.data == "check_sub")
async def check_sub_callback(call: types.CallbackQuery):
    if await is_subscribed(call.from_user.id):
        await call.message.delete()
        await call.message.answer("✅ Obuna tasdiqlandi! Endi kino kodini yuboring.")
    else:
        await call.answer("❌ Hali kanalga obuna bo‘lmagansiz!", show_alert=True)

@dp.message(F.text)
async def send_movie_by_code(message: types.Message):
    if not await is_subscribed(message.from_user.id):
        await message.answer("🎬 Kinolarni ko‘rish uchun avval kanalimizga obuna bo‘ling:", reply_markup=sub_keyboard())
        return
    code = message.text.strip()
    if code in MOVIES:
        try:
            await bot.copy_message(
                chat_id=message.from_user.id,
                from_chat_id=BAZA_CHANNEL_ID,
                message_id=MOVIES[code]
            )
        except Exception as e:
            await message.answer("⚠️ Kino yuborishda xatolik yuz berdi. Bot baza kanalda admin ekanligini tekshiring.")
    else:
        await message.answer("❌ Bunday kodli kino topilmadi. Katalogdan tekshirib ko‘ring.")

# MINI APP UCHUN API
class WatchRequest(BaseModel):
    user_id: int
    code: str

@asynccontextmanager
async def lifespan(app: FastAPI):
    await bot.delete_webhook(drop_pending_updates=True)
    task = asyncio.create_task(dp.start_polling(bot))
    yield
    task.cancel()

app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def health():
    return {"status": "Filmora server ishlamoqda"}

@app.post("/api/watch")
async def api_watch(req: WatchRequest):
    if not await is_subscribed(req.user_id):
        await bot.send_message(req.user_id, "🎬 Kinoni olish uchun avval kanalimizga obuna bo‘ling:", reply_markup=sub_keyboard())
        return {"ok": False, "error": "not_subscribed"}
    if req.code not in MOVIES:
        raise HTTPException(status_code=404, detail="Kino topilmadi")
    try:
        await bot.copy_message(
            chat_id=req.user_id,
            from_chat_id=BAZA_CHANNEL_ID,
            message_id=MOVIES[req.code]
        )
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
      
