import asyncio
import logging
import random
import json
import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, BotCommand, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, CallbackQuery
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import asyncpg

# ====================================================================
# ⚙️ 1. ማዋቀሪያዎች (እነዚህን መረጃዎች ብቻ በስልክህ ማስታወሻ ላይ Edit አድርግ))
# ====================================================================
BOT_TOKEN = "8844814070:AAGzFfFwAkkLF5GWEqwHScu0DTReYVDI8Eo"            # 👈 ያገኘኸውን ረጅም የቦት ቶከን እዚህ አስገባ
ADMIN_USER_ID = 8922880748                   # 👈 ከሮዝ ቦት ያገኘኸውን ያንተን የቴሌግራም ID እዚህ ጻፍ
BINGO_GROUP_ID = -1005512455154             # 👈 ያንተ የHASUBINGO ጨዋታ ግሩፕ ID
ADMIN_FINANCE_LOG_CHANNEL = -1005512455154  # 👈 ያንተ የገንዘብ እንቅስቃሴ መከታተያ ቻናል ID
ADMIN_SUPPORT_CHAT_ID = -1005512455154      # 👈 ያንተ የደንበኞች እርዳታ (Support) ቶፒክ ID
MINI_APP_URL = "https://your-bingo-mini-app.com"

# አድሚን ዝርዝር ውስጥ ያንተን ID ማካተት
ADMIN_USER_IDS = [ADMIN_USER_ID]

app = FastAPI()
scheduler = AsyncIOScheduler()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
db_pool = None
active_ws_connections = []

# 🎲 የ3ቱ ክፍሎች ወቅታዊ ሁኔታ መቆጣጠሪያ ማዕከል
rooms_config = {
    "PLAY_10": {
        "ticket_price": 10.0,
        "win_condition": "DEFAULT_COMBINED", # 1 መስመር ወይም 4 ማዕዘን
        "prize_pool": 0.0,
        "registered_players": {}, # {user_id: {"name": "...", "cards": []}}
        "called_numbers": [],
        "serial_game_id": 1,
        "status": "REGISTRATION"
    },
    "SUPER_50": {
        "ticket_price": 50.0,
        "win_condition": "FULL_HOUSE",       # ሙሉ ዝግ (ጃክፖት)
        "prize_pool": 0.0,
        "registered_players": {},
        "called_numbers": [],
        "status": "REGISTRATION"             # ከጨዋታ መልስ ወዲያውኑ ክፍት የሚሆን
    },
    "CUSTOM_20": {
        "ticket_price": 20.0,
        "win_condition": "3_LINES",          # 3 መስመር የዘጋ ህግ
        "prize_pool": 0.0,
        "registered_players": {},
        "called_numbers": [],
        "serial_game_id": 1,
        "status": "REGISTRATION"
    }
}

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

class PaymentStates(StatesGroup): waiting_for_receipt = State()
class WithdrawStates(StatesGroup): waiting_for_amount = State(); waiting_for_method = State(); waiting_for_account = State()
class AdminStates(StatesGroup): waiting_for_new_rules = State()
class AdminPriceStates(StatesGroup): waiting_for_room1_price = State(); waiting_for_room2_price = State(); waiting_for_room3_price = State()
class AdminCommissionStates(StatesGroup): waiting_for_room1_commission = State(); waiting_for_room2_commission = State(); waiting_for_room3_commission = State()
class AdminFeeStates(StatesGroup): waiting_for_fixed_fee = State()

# ====================================================================
# 🗄️ ክፍል 1፦ የዌብሶኬት ስርጭት እና የአድሚን የገንዘብ መከታተያ (Audit Log)
# ====================================================================

async def broadcast_to_mini_app(data: dict):
    for ws in active_ws_connections:
        try: await ws.send_json(data)
        except Exception: pass

@app.websocket("/ws/bingo-live")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_ws_connections.append(websocket)
    try:
        while True: await websocket.receive_text()
    except WebSocketDisconnect: active_ws_connections.remove(websocket)

async def log_financial_activity(title: str, user_name: str, user_id: int, amount: float, details: str, new_balance: float):
    current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report_text = (
        f"📊 **የገንዘብ እንቅስቃሴ መከታተያ ሪፖርት**\n━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 **ዓይነት፦** {title}\n👤 **ተጫዋች፦** {user_name}\n🆔 **ID፦** `{user_id}`\n"
        f"💰 **መጠን፦** `{amount} ETB`\n💳 **ቀሪ ሂሳብ፦** `{new_balance} ETB`\n"
        f"ℹ️ **ዝርዝር፦** {details}\n📅 **ሰዓት፦** `{current_time}`\n━━━━━━━━━━━━━━━━━━━━"
    )
    try: await bot.send_message(chat_id=ADMIN_FINANCE_LOG_CHANNEL, text=report_text, parse_mode="Markdown")
    except Exception: pass

# ====================================================================
# 📐 ክፍል 2፦ 8ቱን የቢንጎ ህጎች እና ድርብ የዲፎልት ህግ ቼክ ማድረጊያ
# ====================================================================

def check_bingo_victory(matrix, called_set, rule):
    rows = matrix
    cols = [[matrix[r][c] for r in range(5)] for c in range(5)]
    diagonals = [[matrix[i][i] for i in range(5)], [matrix[i][4-i] for i in range(5)]]
    all_lines = rows + cols + diagonals

    completed_lines = sum(1 for line in all_lines if all(num == "❌" or num in called_set for num in line))
    corners = [matrix, matrix, matrix, matrix]
    corners_filled = all(num in called_set for num in corners)

    if rule == "DEFAULT_COMBINED": return (completed_lines >= 1) or corners_filled
    elif rule == "1_LINE": return completed_lines >= 1
    elif rule == "2_LINES": return completed_lines >= 2
    elif rule == "3_LINES": return completed_lines >= 3
    elif rule == "4_LINES": return completed_lines >= 4
    elif rule == "4_CORNERS": return corners_filled
    elif rule == "X_SHAPE": return all(all(num == "❌" or num in called_set for num in d) for d in diagonals)
    elif rule == "FULL_HOUSE": return all(all(num == "❌" or num in called_set for num in r) for r in rows)
    return False

# ====================================================================
# 🎁 ክፍል 3፦ ተከታታይ ጨዋታ የስትሪክ ቦነስ ሎጂክ (Streak Bonus Engine)
# ====================================================================

async def update_player_streaks_and_bonus(conn, current_game_id: int, registered_user_ids: list):
    await conn.execute(
        "UPDATE users SET current_streak = 0 WHERE last_played_game_id = $1 AND user_id NOT IN (SELECT unnest($2::bigint[]))",
        current_game_id - 1, registered_user_ids
    )
    for user_id in registered_user_ids:
        user_row = await conn.fetchrow("SELECT current_streak, last_played_game_id FROM users WHERE user_id = $1", user_id)
        if not user_row: continue
        
        new_streak = (user_row['current_streak'] + 1) if user_row['last_played_game_id'] == current_game_id - 1 else 1
        bonus = 10.0 if new_streak == 10 else (20.0 if new_streak == 15 else 0.0)

        if bonus > 0:
            await conn.execute("UPDATE users SET current_streak = $1, last_played_game_id = $2, balance = balance + $3 WHERE user_id = $4", new_streak, current_game_id, bonus, user_id)
            await conn.execute("INSERT INTO transaction_history (user_id, amount, type, status) VALUES ($1, $2, 'STREAK_BONUS', 'SUCCESSFUL')", user_id, bonus)
            try: await bot.send_message(chat_id=user_id, text=f"🔥 ስትሪክ {new_streak} ደረሱ! የ {bonus} ብር ቦነስ አካውንትዎ ላይ ተጨምሯል!")
            except Exception: pass
        else:
            await conn.execute("UPDATE users SET current_streak = $1, last_played_game_id = $2 WHERE user_id = $3", new_streak, current_game_id, user_id)

# ====================================================================
# 🔄 ክፍል 4፦ አውቶማቲክ የጨዋታ ዑደቶች (Play 10, Custom 20, Super 50)
# ====================================================================

async def run_standard_room_loop(room_type: str):
    room = rooms_config[room_type]
    while True:
        room["status"] = "REGISTRATION"
        room["called_numbers"].clear()
        
        # 1. ⏱️ የ 30 ሰከንድ የካርቴላ ሽያጭ (ምዝገባ)
        await broadcast_to_mini_app({"type": "STATUS_UPDATE", "room": room_type, "phase": "REGISTRATION", "timer": 30})
        await asyncio.sleep(30)

        if len(room["registered_players"]) < 2:
            await asyncio.sleep(5)
            continue

        async with db_pool.acquire() as conn:
            await update_player_streaks_and_bonus(conn, room["serial_game_id"], list(room["registered_players"].keys()))

        # 2. ⏱️ ወደ ጨዋታው ሜዳ ተሻግሮ ሰማያዊው ክብ ውስጥ የ 3 ሰከንድ መቁጠሪያ
        room["status"] = "COUNTDOWN"
        await broadcast_to_mini_app({"type": "COUNTDOWN_START", "room": room_type, "prize": room["prize_pool"] * 0.80, "player_count": len(room["registered_players"])})
        for count in range(3, 0, -1):
            await broadcast_to_mini_app({"type": "COUNTDOWN_TICK", "room": room_type, "value": count})
            await asyncio.sleep(1)

        # 3. የቀጥታ ጨዋታ መጀመር (Live Balls Roll)
        room["status"] = "LIVE"
        all_balls = list(range(1, 76))
        random.shuffle(all_balls)

        for ball in all_balls:
            if room["status"] == "GAME_OVER": break
            room["called_numbers"].append(ball)
            ball_idx = len(room["called_numbers"])
            
            async with db_pool.acquire() as conn:
                key_id = '1' if room_type=="PLAY_10" else '3'
                comm_rate = float(await conn.fetchval(f"SELECT value FROM bot_settings WHERE key = 'commission_room_{key_id}'"))
            visible_prize = room["prize_pool"] * (1.0 - comm_rate)

            await broadcast_to_mini_app({"type": "LIVE_BALL", "room": room_type, "number": ball, "ball_count": f"{ball_idx}/75", "prize": f"{visible_prize:.2f} ETB"})
            await asyncio.sleep(6)

        # 4. ⏱️ የ 15 ሰከንድ የአሸናፊ ማሳወቂያ ዲስፕሌይ ገጽ ብቻ
        await asyncio.sleep(15)
        room["serial_game_id"] += 1
        room["registered_players"].clear()

async def run_daily_super_bingo_sequence():
    room = rooms_config["SUPER_50"]
    if len(room["registered_players"]) < 2: return

    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎟️ አሁኑኑ ትኬት ግዛ!", web_app=WebAppInfo(url=MINI_APP_URL))]])