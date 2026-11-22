import os
import json
import time
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
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
ADMIN_ID = 8751791526
BAZA_CHANNEL_ID = -1003958730565
MINI_APP_URL = "https://abzalbeksomidov.github.io/Filmora-app/"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

DB = {
    "movies": {
        "1000": {
            "msg_id": 2,
            "title": "Test Kino (1000)",
            "year": "2026 • Jangari, Drama",
            "poster": "https://images.unsplash.com/photo-1536440136628-849c177e76a1?w=500&q=80",
            "type": "movie",
            "premium": False,
        },
        "999": {
            "msg_id": 2,
            "title": "Jang ortidan jang",
            "year": "2025 • Jangari, Triller",
            "poster": "https://images.unsplash.com/photo-1509347528160-9a9e33742cdb?w=500&q=80",
            "type": "movie",
            "premium": False,
        },
        "998": {
            "msg_id": 2,
            "title": "Muzyorar (Snowpiercer)",
            "year": "2013 • Fantastika, Drama",
            "poster": "https://images.unsplash.com/photo-1478720568477-152d9b164e26?w=500&q=80",
            "type": "movie",
            "premium": False,
        },
    },
    "channels": [
        {"id": "@filmora_kinolar", "title": "Filmora Kinolar", "url": "https://t.me/filmora_kinolar", "active": True}
    ],
    "premium_users": {},
    "users": [],
    "settings": {
        "card": "Karta raqami hali kiritilmagan (/panel orqali kiriting)",
        "price": "15 000 so‘m (1 oy)",
        "sub_enabled": True,
    },
}
DB_MSG_ID = None


async def load_db():
    global DB, DB_MSG_ID
    try:
        chat = await bot.get_chat(BAZA_CHANNEL_ID)
        if chat.pinned_message and chat.pinned_message.text and chat.pinned_message.text.startswith("FILMORA_DB:"):
            raw = chat.pinned_message.text.replace("FILMORA_DB:", "", 1).strip()
            loaded = json.loads(raw)
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


def is_premium(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    exp = DB["premium_users"].get(str(user_id), 0)
    return exp > int(time.time())


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
        [KeyboardButton(text="🎬 Kinolar katalogi", web_app=WebAppInfo(url=MINI_APP_URL))],
        [KeyboardButton(text="🔎 Kino kodi"), KeyboardButton(text="💎 Premium")],
        [KeyboardButton(text="📝 Kino zakaz qilish"), KeyboardButton(text="👤 Profilim")],
        [KeyboardButton(text="📢 Kanalimiz")],
    ]
    if user_id == ADMIN_ID:
        kb.append([KeyboardButton(text="⚙️ Admin Panel")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


class AdminStates(StatesGroup):
    add_code = State()
    add_title = State()
    add_year = State()
    add_poster = State()
    add_premium = State()
    add_video = State()

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
            [InlineKeyboardButton(text="➕ Kino / Serial qo‘shish", callback_data="adm_add_movie")],
            [
                InlineKeyboardButton(text="✏️ Kinoni tahrirlash", callback_data="adm_edit_movie"),
                InlineKeyboardButton(text="🗑 Kinoni o‘chirish", callback_data="adm_del_movie"),
            ],
            [InlineKeyboardButton(text="📢 Majburiy obuna kanallari", callback_data="adm_channels")],
            [InlineKeyboardButton(text=f"🔒 Majburiy obuna: {sub_state}", callback_data="adm_toggle_sub")],
            [
                InlineKeyboardButton(text="💳 Karta raqami", callback_data="adm_set_card"),
                InlineKeyboardButton(text="💰 Premium narxi", callback_data="adm_set_price"),
            ],
            [InlineKeyboardButton(text="💎 Foydalanuvchiga Premium berish", callback_data="adm_give_prem")],
            [
                InlineKeyboardButton(text="📊 Statistika", callback_data="adm_stats"),
                InlineKeyboardButton(text="📣 Xabar tarqatish", callback_data="adm_broadcast"),
            ],
        ]
    )


# ==================== FOYDALANUVCHI QISMI ====================
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    if uid not in DB["users"]:
        DB["users"].append(uid)
        await save_db()

    missing = await get_unsubscribed_channels(uid)
    if missing:
        await message.answer(
            "🎬 Kinolarni ko‘rish uchun avval quyidagi kanallarga obuna bo‘ling:",
            reply_markup=sub_keyboard(missing),
        )
        return

    await message.answer(
        "👋 Assalomu alaykum! <b>Filmora Kinolar</b> botiga xush kelibsiz.\n\n"
        "✍️ Kino kodini yuboring yoki pastdagi tugmalar orqali katalogdan foydalaning:",
        parse_mode="HTML",
        reply_markup=main_reply_keyboard(uid),
    )


@dp.callback_query(F.data == "check_sub")
async def check_sub_cb(call: types.CallbackQuery):
    missing = await get_unsubscribed_channels(call.from_user.id)
    if not missing:
        await call.message.delete()
        await call.message.answer(
            "✅ Obuna tasdiqlandi! Endi kino kodini yuboring yoki katalogni oching:",
            reply_markup=main_reply_keyboard(call.from_user.id),
        )
    else:
        await call.answer("❌ Hali barcha kanallarga obuna bo‘lmagansiz!", show_alert=True)


@dp.message(F.text == "🔎 Kino kodi")
async def ask_code(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("✍️ Kinoning kodini yozib yuboring (masalan: <code>1000</code>):", parse_mode="HTML")


@dp.message(F.text == "📢 Kanalimiz")
async def our_channel(message: types.Message):
    await message.answer("📢 Asosiy kanalimiz: @filmora_kinolar")


@dp.message(F.text == "👤 Profilim")
async def my_profile(message: types.Message):
    uid = message.from_user.id
    prem = "💎 Faol (Premium)" if is_premium(uid) else "🆓 Oddiy tarif"
    await message.answer(
        f"👤 <b>Sizning profilingiz:</b>\n\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"⭐ Holat: <b>{prem}</b>",
        parse_mode="HTML",
    )


@dp.message(F.text == "📝 Kino zakaz qilish")
async def order_movie_start(message: types.Message, state: FSMContext):
    await state.set_state(AdminStates.wait_order)
    await message.answer(
        "📝 <b>Kino buyurtma berish:</b>\n\n"
        "Qaysi kino yoki serialni qo‘shishimizni xohlaysiz? Nomi va yilini yozib yuboring:",
        parse_mode="HTML",
    )


@dp.message(AdminStates.wait_order, F.text)
async def order_movie_receive(message: types.Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    username = f"@{message.from_user.username}" if message.from_user.username else "yo‘q"
    await bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"📝 <b>Yangi kino buyurtmasi!</b>\n\n"
            f"👤 Foydalanuvchi: {message.from_user.full_name} ({username})\n"
            f"🆔 ID: <code>{uid}</code>\n"
            f"🎬 Buyurtma: <b>{message.text}</b>"
        ),
        parse_mode="HTML",
    )
    await message.answer("✅ Buyurtmangiz adminga yuborildi! Tez orada kanalga va katalogga joylanadi.")


@dp.message(F.text == "💎 Premium")
async def premium_info(message: types.Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="💳 Sotib olish (Chek yuborish)", callback_data="buy_premium")]]
    )
    await message.answer(
        f"💎 <b>Filmora Premium obunasi</b>\n\n"
        f"✅ Barcha pullik va eksklyuziv kinolarga cheksiz kirish\n"
        f"💰 Narxi: <b>{DB['settings']['price']}</b>\n"
        f"💳 To‘lov uchun karta:\n<code>{DB['settings']['card']}</code>\n\n"
        f"To‘lovni amalga oshirgach, pastdagi tugmani bosib chek rasmini yuboring:",
        parse_mode="HTML",
        reply_markup=kb,
    )


@dp.callback_query(F.data == "buy_premium")
async def buy_premium_cb(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.wait_receipt)
    await call.message.answer("📸 To‘lov cheki rasmini (skrinshotini) shu yerga yuboring:")
    await call.answer()


@dp.message(AdminStates.wait_receipt, F.photo)
async def receive_receipt(message: types.Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Tasdiqlash (30 kun)", callback_data=f"approve_prem:{uid}"),
                InlineKeyboardButton(text="❌ Rad etish", callback_data=f"reject_prem:{uid}"),
            ]
        ]
    )
    await bot.send_photo(
        chat_id=ADMIN_ID,
        photo=message.photo[-1].file_id,
        caption=f"💳 <b>Yangi Premium to‘lov cheki!</b>\n👤 Foydalanuvchi: {message.from_user.full_name}\n🆔 ID: <code>{uid}</code>",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await message.answer("✅ Chekingiz adminga yuborildi! Tez orada tasdiqlanadi.")


@dp.callback_query(F.data.startswith("approve_prem:"))
async def approve_prem_cb(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    uid = int(call.data.split(":")[1])
    DB["premium_users"][str(uid)] = int(time.time()) + 30 * 86400
    await save_db()
    await call.message.edit_caption(caption=call.message.caption + "\n\n✅ <b>TASDIQLANDI (30 kun)</b>", parse_mode="HTML")
    try:
        await bot.send_message(uid, "🎉 Tabriklaymiz! Sizga <b>30 kunlik Premium obuna</b> faollashtirildi!", parse_mode="HTML")
    except Exception:
        pass


@dp.callback_query(F.data.startswith("reject_prem:"))
async def reject_prem_cb(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    uid = int(call.data.split(":")[1])
    await call.message.edit_caption(caption=call.message.caption + "\n\n❌ <b>RAD ETILDI</b>", parse_mode="HTML")
    try:
        await bot.send_message(uid, "❌ To‘lov chekingiz tasdiqlanmadi. Ma’lumotlarni tekshirib qayta urinib ko‘ring.")
    except Exception:
        pass


# ==================== ADMIN PANEL (/panel) ====================
@dp.message(Command("panel"))
@dp.message(F.text == "⚙️ Admin Panel")
async def open_admin_panel(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer("⚙️ <b>Filmora Admin Panel</b>\nKerakli bo‘limni tanlang:", parse_mode="HTML", reply_markup=admin_panel_kb())


@dp.callback_query(F.data == "adm_add_movie")
async def adm_add_movie(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.add_code)
    await call.message.answer("1️⃣ Kino kodini kiriting (masalan: <code>991</code> yoki <code>umar_1</code>):", parse_mode="HTML")
    await call.answer()


@dp.message(AdminStates.add_code)
async def adm_add_code(message: types.Message, state: FSMContext):
    await state.update_data(code=message.text.strip())
    await state.set_state(AdminStates.add_title)
    await message.answer("2️⃣ Kino nomini kiriting:")


@dp.message(AdminStates.add_title)
async def adm_add_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(AdminStates.add_year)
    await message.answer("3️⃣ Yili va janrini kiriting (masalan: <code>2025 • Jangari, Drama</code>):", parse_mode="HTML")


@dp.message(AdminStates.add_year)
async def adm_add_year(message: types.Message, state: FSMContext):
    await state.update_data(year=message.text.strip())
    await state.set_state(AdminStates.add_poster)
    await message.answer("4️⃣ Kino posteri (rasm) havolasini yuboring yoki shunchaki <code>-</code> yozing:")


@dp.message(AdminStates.add_poster)
async def adm_add_poster(message: types.Message, state: FSMContext):
    poster = message.text.strip() if message.text and message.text.startswith("http") else "https://images.unsplash.com/photo-1536440136628-849c177e76a1?w=500&q=80"
    await state.update_data(poster=poster)
    await state.set_state(AdminStates.add_premium)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🎬 Oddiy (Bepul)", callback_data="set_mtype:free"),
                InlineKeyboardButton(text="💎 Premium (Pullik)", callback_data="set_mtype:prem"),
            ]
        ]
    )
    await message.answer("5️⃣ Kino turini tanlang:", reply_markup=kb)


@dp.callback_query(AdminStates.add_premium, F.data.startswith("set_mtype:"))
async def adm_set_mtype(call: types.CallbackQuery, state: FSMContext):
    is_prem = call.data.split(":")[1] == "prem"
    await state.update_data(premium=is_prem)
    await state.set_state(AdminStates.add_video)
    await call.message.answer("6️⃣ Endi <b>kino videosini</b> (yoki faylini) shu yerga yuboring:", parse_mode="HTML")
    await call.answer()


@dp.message(AdminStates.add_video, F.video | F.document)
async def adm_save_video(message: types.Message, state: FSMContext):
    data = await state.get_data()
    sent = await bot.copy_message(chat_id=BAZA_CHANNEL_ID, from_chat_id=message.chat.id, message_id=message.message_id)
    DB["movies"][data["code"]] = {
        "msg_id": sent.message_id,
        "title": data["title"],
        "year": data["year"],
        "poster": data["poster"],
        "type": "movie",
        "premium": data["premium"],
    }
    await save_db()
    await state.clear()
    await message.answer(
        f"✅ <b>Kino muvaffaqiyatli qo‘shildi!</b>\n\n"
        f"🎬 Nomi: {data['title']}\n"
        f"🔢 Kodi: <code>{data['code']}</code>\n"
        f"💎 Turi: {'Premium' if data['premium'] else 'Oddiy (Bepul)'}",
        parse_mode="HTML",
        reply_markup=admin_panel_kb(),
    )


@dp.callback_query(F.data == "adm_edit_movie")
async def adm_edit_movie(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.edit_code)
    await call.message.answer("✏️ Tahrirlamoqchi bo‘lgan kino kodini yuboring:")
    await call.answer()


@dp.message(AdminStates.edit_code)
async def adm_edit_code_step(message: types.Message, state: FSMContext):
    code = message.text.strip()
    if code not in DB["movies"]:
        await message.answer("❌ Bunday kodli kino topilmadi.")
        await state.clear()
        return
    await state.update_data(code=code)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📝 Nomini o‘zgartirish", callback_data="ed_act:title")],
            [InlineKeyboardButton(text="🔄 Oddiy ↔ Premium almashtirish", callback_data="ed_act:toggle_prem")],
        ]
    )
    await message.answer(f"🎬 <b>{DB['movies'][code]['title']}</b>\nNimani o‘zgartiramiz?", parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data.startswith("ed_act:"))
async def adm_edit_action(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    code = data["code"]
    act = call.data.split(":")[1]
    if act == "toggle_prem":
        DB["movies"][code]["premium"] = not DB["movies"][code].get("premium", False)
        await save_db()
        await state.clear()
        st = "💎 Premium" if DB["movies"][code]["premium"] else "🎬 Oddiy (Bepul)"
        await call.message.answer(f"✅ Kino holati o‘zgartirildi: <b>{st}</b>", parse_mode="HTML", reply_markup=admin_panel_kb())
    elif act == "title":
        await state.set_state(AdminStates.edit_new_title)
        await call.message.answer("📝 Yangi nomni kiriting:")
    await call.answer()


@dp.message(AdminStates.edit_new_title)
async def adm_save_new_title(message: types.Message, state: FSMContext):
    data = await state.get_data()
    DB["movies"][data["code"]]["title"] = message.text.strip()
    await save_db()
    await state.clear()
    await message.answer("✅ Kino nomi yangilandi!", reply_markup=admin_panel_kb())


@dp.callback_query(F.data == "adm_del_movie")
async def adm_del_movie(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.del_code)
    await call.message.answer("🗑 O‘chirmoqchi bo‘lgan kino kodini yuboring:")
    await call.answer()


@dp.message(AdminStates.del_code)
async def adm_del_movie_done(message: types.Message, state: FSMContext):
    code = message.text.strip()
    if code in DB["movies"]:
        title = DB["movies"].pop(code)["title"]
        await save_db()
        await message.answer(f"🗑 <b>{title}</b> ({code}) bazadan o‘chirildi!", parse_mode="HTML", reply_markup=admin_panel_kb())
    else:
        await message.answer("❌ Bunday kodli kino topilmadi.")
    await state.clear()


@dp.callback_query(F.data == "adm_toggle_sub")
async def adm_toggle_sub(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    DB["settings"]["sub_enabled"] = not DB["settings"].get("sub_enabled", True)
    await save_db()
    await call.message.edit_reply_markup(reply_markup=admin_panel_kb())
    await call.answer("Majburiy obuna holati o‘zgartirildi!")


@dp.callback_query(F.data == "adm_channels")
async def adm_channels_menu(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    rows = []
    for idx, ch in enumerate(DB["channels"]):
        st = "🟢" if ch.get("active", True) else "🔴"
        rows.append(
            [
                InlineKeyboardButton(text=f"{st} {ch['title']}", callback_data=f"ch_tog
