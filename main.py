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
ADMIN_ID = 6248171499
BAZA_CHANNEL_ID = -1003958730565
MINI_APP_URL = "https://abzalbeksomidov.github.io/Filmora-app/"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

DB = {
    "movies": {
        "1000": {"msg_id": 2, "title": "Test Kino (1000)", "year": "2026 • Jangari", "poster": "https://images.unsplash.com/photo-1536440136628-849c177e76a1?w=500&q=80", "type": "movie", "premium": False},
        "999": {"msg_id": 2, "title": "Jang ortidan jang", "year": "2025 • Jangari, Triller", "poster": "https://images.unsplash.com/photo-1509347528160-9a9e33742cdb?w=500&q=80", "type": "movie", "premium": False},
        "998": {"msg_id": 2, "title": "Muzyorar (Snowpiercer)", "year": "2013 • Fantastika", "poster": "https://images.unsplash.com/photo-1478720568477-152d9b164e26?w=500&q=80", "type": "movie", "premium": False},
    },
    "channels": [{"id": "@filmora_kinolar", "title": "Filmora Kinolar", "url": "https://t.me/filmora_kinolar", "active": True}],
    "premium_users": {},
    "users": [],
    "settings": {"card": "Karta kiritilmagan (/panel)", "price": "15 000 so‘m (1 oy)", "sub_enabled": True},
}
DB_MSG_ID = None


async def load_db():
    global DB, DB_MSG_ID
    try:
        chat = await bot.get_chat(BAZA_CHANNEL_ID)
        if chat.pinned_message and chat.pinned_message.text and chat.pinned_message.text.startswith("FILMORA_DB:"):
            DB.update(json.loads(chat.pinned_message.text.replace("FILMORA_DB:", "", 1).strip()))
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
            [InlineKeyboardButton(text="✏️ Kinoni tahrirlash", callback_data="adm_edit_movie"), InlineKeyboardButton(text="🗑 Kinoni o‘chirish", callback_data="adm_del_movie")],
            [InlineKeyboardButton(text="📢 Majburiy obuna kanallari", callback_data="adm_channels")],
            [InlineKeyboardButton(text=f"🔒 Majburiy obuna: {sub_state}", callback_data="adm_toggle_sub")],
            [InlineKeyboardButton(text="💳 Karta raqami", callback_data="adm_set_card"), InlineKeyboardButton(text="💰 Premium narxi", callback_data="adm_set_price")],
            [InlineKeyboardButton(text="💎 Foydalanuvchiga Premium berish", callback_data="adm_give_prem")],
            [InlineKeyboardButton(text="📊 Statistika", callback_data="adm_stats"), InlineKeyboardButton(text="📣 Xabar tarqatish", callback_data="adm_broadcast")],
        ]
    )


@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    if uid not in DB["users"]:
        DB["users"].append(uid)
        await save_db()
    missing = await get_unsubscribed_channels(uid)
    if missing:
        await message.answer("🎬 Kinolarni ko‘rish uchun avval kanallarga obuna bo‘ling:", reply_markup=sub_keyboard(missing))
        return
    await message.answer("👋 Assalomu alaykum! <b>Filmora Kinolar</b> botiga xush kelibsiz.\n\n✍️ Kino kodini yuboring yoki katalogni oching:", parse_mode="HTML", reply_markup=main_reply_keyboard(uid))


@dp.callback_query(F.data == "check_sub")
async def check_sub_cb(call: types.CallbackQuery):
    if not await get_unsubscribed_channels(call.from_user.id):
        await call.message.delete()
        await call.message.answer("✅ Obuna tasdiqlandi!", reply_markup=main_reply_keyboard(call.from_user.id))
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
    await message.answer(f"👤 <b>Profilingiz:</b>\n🆔 ID: <code>{uid}</code>\n⭐ Holat: <b>{prem}</b>", parse_mode="HTML")


@dp.message(F.text == "📝 Kino zakaz qilish")
async def order_movie_start(message: types.Message, state: FSMContext):
    await state.set_state(AdminStates.wait_order)
    await message.answer("📝 Qaysi kino yoki serialni qo‘shishimizni xohlaysiz? Nomi va yilini yozib yuboring:")


@dp.message(AdminStates.wait_order, F.text)
async def order_movie_receive(message: types.Message, state: FSMContext):
    await state.clear()
    await bot.send_message(ADMIN_ID, f"📝 <b>Yangi zakaz!</b>\n👤 {message.from_user.full_name} (<code>{message.from_user.id}</code>)\n🎬 <b>{message.text}</b>", parse_mode="HTML")
    await message.answer("✅ Buyurtmangiz adminga yuborildi!")


@dp.message(F.text == "💎 Premium")
async def premium_info(message: types.Message, state: FSMContext = None):
    if state:
        await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Sotib olish (Chek yuborish)", callback_data="buy_premium")]])
    await message.answer(f"💎 <b>Filmora Premium</b>\n💰 Narxi: <b>{DB['settings']['price']}</b>\n💳 Karta:\n<code>{DB['settings']['card']}</code>\n\nTo‘lov qilgach chek rasmini yuboring:", parse_mode="HTML", reply_markup=kb)


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
    await bot.send_photo(ADMIN_ID, message.photo[-1].file_id, caption=f"💳 <b>Yangi chek!</b>\n👤 {message.from_user.full_name}\n🆔 <code>{uid}</code>", parse_mode="HTML", reply_markup=kb)
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
        await bot.send_message(uid, "🎉 Sizga <b>30 kunlik Premium</b> yoqildi!", parse_mode="HTML")
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
@dp.message(Command("panel"))
@dp.message(F.text == "⚙️ Admin Panel")
async def open_admin_panel(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer("⚙️ <b>Filmora Admin Panel</b>", parse_mode="HTML", reply_markup=admin_panel_kb())


@dp.callback_query(F.data == "adm_add_movie")
async def adm_add_movie(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.add_code)
    await call.message.answer("1️⃣ Kino kodini kiriting (masalan: <code>991</code>):", parse_mode="HTML")
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
    await message.answer("3️⃣ Yili va janrini kiriting (masalan: <code>2025 • Jangari</code>):", parse_mode="HTML")


@dp.message(AdminStates.add_year)
async def adm_add_year(message: types.Message, state: FSMContext):
    await state.update_data(year=message.text.strip())
    await state.set_state(AdminStates.add_poster)
    await message.answer("4️⃣ Poster rasm havolasini yuboring yoki <code>-</code> yozing:", parse_mode="HTML")


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
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎬 Oddiy (Bepul)", callback_data="set_mtype:free"), InlineKeyboardButton(text="💎 Premium", callback_data="set_mtype:prem")]])
    await message.answer("5️⃣ Kino turini tanlang:", reply_markup=kb)
    


@dp.callback_query(AdminStates.add_premium, F.data.startswith("set_mtype:"))
async def adm_set_mtype(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(premium=(call.data.split(":")[1] == "prem"))
    await state.set_state(AdminStates.add_video)
    await call.message.answer("6️⃣ Endi <b>kino videosini</b> yuboring:", parse_mode="HTML")
    await call.answer()


@dp.message(AdminStates.add_video, F.video | F.document)
async def adm_save_video(message: types.Message, state: FSMContext):
    data = await state.get_data()
    sent = await bot.copy_message(BAZA_CHANNEL_ID, message.chat.id, message.message_id)
    DB["movies"][data["code"]] = {"msg_id": sent.message_id, "title": data["title"], "year": data["year"], "poster": data["poster"], "type": "movie", "premium": data["premium"]}
    await save_db()
    await state.clear()
    await message.answer(f"✅ <b>{data['title']}</b> (Kod: <code>{data['code']}</code>) qo‘shildi!", parse_mode="HTML", reply_markup=admin_panel_kb())


@dp.callback_query(F.data == "adm_edit_movie")
async def adm_edit_movie(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.edit_code)
    await call.message.answer("✏️ Kino kodini yuboring:")
    await call.answer()


@dp.message(AdminStates.edit_code)
async def adm_edit_code_step(message: types.Message, state: FSMContext):
    code = message.text.strip()
    if code not in DB["movies"]:
        await message.answer("❌ Topilmadi.")
        await state.clear()
        return
    await state.update_data(code=code)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📝 Nomini o‘zgartirish", callback_data="ed_act:title")], [InlineKeyboardButton(text="🔄 Oddiy ↔ Premium", callback_data="ed_act:toggle_prem")]])
    await message.answer(f"🎬 <b>{DB['movies'][code]['title']}</b>", parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data.startswith("ed_act:"))
async def adm_edit_action(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    code, act = data["code"], call.data.split(":")[1]
    if act == "toggle_prem":
        DB["movies"][code]["premium"] = not DB["movies"][code].get("premium", False)
        await save_db()
        await state.clear()
        await call.message.answer("✅ Kino turi o‘zgartirildi!", reply_markup=admin_panel_kb())
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
    await call.message.answer("🗑 O‘chiriladigan kino kodini yuboring:")
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
    await call.message.edit_text("⚙️ <b>Filmora Admin Panel</b>", parse_mode="HTML", reply_markup=admin_panel_kb())


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
    await call.message.answer("💰 Premium narxini kiriting:")
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
        await message.answer(f"✅ <code>{uid}</code> ga Premium berildi!", parse_mode="HTML", reply_markup=admin_panel_kb())
    await state.clear()


@dp.callback_query(F.data == "adm_stats")
async def adm_stats(call: types.CallbackQuery):
    active_prem = sum(1 for exp in DB["premium_users"].values() if exp > int(time.time()))
    await call.message.answer(f"📊 Foydalanuvchilar: <b>{len(DB['users'])}</b>\n💎 Premium: <b>{active_prem}</b>\n🎬 Kinolar: <b>{len(DB['movies'])}</b>", parse_mode="HTML")
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
    for uid in DB["users"]:
        try:
            await bot.copy_message(uid, message.chat.id, message.message_id)
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
    movie = DB["movies"].get(message.text.strip())
    if not movie:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎬 Katalogni ochish", web_app=WebAppInfo(url=MINI_APP_URL))]])
        await message.answer("❌ Bunday kodli kino topilmadi:", reply_markup=kb)
        return
    if movie.get("premium") and not is_premium(uid):
        await premium_info(message, None)
        return
    try:
        await bot.copy_message(uid, BAZA_CHANNEL_ID, movie["msg_id"])
    except Exception:
        await message.answer("⚠️ Kino yuborishda xatolik.")


class WatchRequest(BaseModel):
    user_id: int
    code: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    await load_db()
    await bot.delete_webhook(drop_pending_updates=True)
    task = asyncio.create_task(dp.start_polling(bot))
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/")
async def health():
    return {"status": "ok", "movies": len(DB["movies"])}


@app.get("/api/catalog")
async def api_catalog():
    return {"items": [{"id": c, "type": m.get("type", "movie"), "title": m["title"], "year": m.get("year", ""), "poster": m.get("poster", ""), "code": c, "premium": m.get("premium", False)} for c, m in DB["movies"].items()]}


@app.post("/api/watch")
async def api_watch(req: WatchRequest):
    missing = await get_unsubscribed_channels(req.user_id)
    if missing:
        await bot.send_message(req.user_id, "🎬 Avval kanallarga obuna bo‘ling:", reply_markup=sub_keyboard(missing))
        return {"ok": False, "error": "not_subscribed"}
    movie = DB["movies"].get(req.code)
    if not movie:
        raise HTTPException(status_code=404, detail="Kino topilmadi")
    if movie.get("premium") and not is_premium(req.user_id):
        await bot.send_message(req.user_id, f"💎 <b>{movie['title']}</b> — Premium kino!", parse_mode="HTML")
        return {"ok": False, "error": "premium_required"}
    try:
        await bot.copy_message(req.user_id, BAZA_CHANNEL_ID, movie["msg_id"])
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
