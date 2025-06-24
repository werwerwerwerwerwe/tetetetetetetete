import os
import json
import time
import asyncio
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramForbiddenError
from aiosqlite import connect as aiosqlite_connect
from dotenv import load_dotenv

# Load env
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
SCENARIO_PATH = os.getenv("SCENARIO_JSON_PATH", "./scenario.json")
DB_PATH = os.getenv("DB_PATH", "./users.db")

logging.basicConfig(level=logging.INFO)

# Load scenario
with open(SCENARIO_PATH, encoding="utf-8") as f:
    SCENARIO = json.load(f)

bot = Bot(
    token=BOT_TOKEN, 
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

dp = Dispatcher(storage=MemoryStorage())

# --- DB helpers ---

async def init_db():
    async with aiosqlite_connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            username TEXT,
            started_at INTEGER,
            scenario_step INTEGER DEFAULT 0,
            last_sent_at INTEGER DEFAULT 0,
            repeat_substep INTEGER DEFAULT 0,
            repeat_last_sent_at INTEGER DEFAULT 0
        )""")
        await db.commit()

async def upsert_user(user: types.User):
    async with aiosqlite_connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO users (user_id, first_name, username, started_at, scenario_step, last_sent_at, repeat_substep, repeat_last_sent_at)
        VALUES (?, ?, ?, strftime('%s','now'), 0, 0, 0, 0)
        ON CONFLICT(user_id) DO NOTHING
        """, (user.id, user.first_name, user.username))
        await db.commit()

async def get_all_users():
    async with aiosqlite_connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, scenario_step, started_at, last_sent_at, repeat_substep, repeat_last_sent_at FROM users"
        ) as cursor:
            return await cursor.fetchall()

async def update_step(user_id, step, last_sent_at=None):
    query = "UPDATE users SET scenario_step=?"
    params = [step]
    if last_sent_at is not None:
        query += ", last_sent_at=?"
        params.append(last_sent_at)
    query += " WHERE user_id=?"
    params.append(user_id)
    async with aiosqlite_connect(DB_PATH) as db:
        await db.execute(query, tuple(params))
        await db.commit()

async def update_repeat_group(user_id, substep, repeat_last_sent_at):
    async with aiosqlite_connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET repeat_substep=?, repeat_last_sent_at=? WHERE user_id=?",
            (substep, repeat_last_sent_at, user_id)
        )
        await db.commit()

# --- Scenario helpers ---

def make_kb(buttons):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=btn["text"], url=btn["url"])] for btn in buttons
        ]
    )

async def send_message_with_photo_or_text(user_id, text, buttons, photo_url=None):
    kb = make_kb(buttons) if buttons else None
    try:
        if photo_url:
            await bot.send_photo(
                user_id,
                photo=photo_url,
                caption=text,
                reply_markup=kb,
                parse_mode=ParseMode.HTML
            )
        else:
            await bot.send_message(user_id, text, reply_markup=kb)
    except TelegramForbiddenError:
        logging.warning(f"[BLOCKED] User {user_id} has blocked the bot. Skipping.")
    except Exception as e:
        logging.exception(f"[ERROR] Failed to send message to {user_id}: {e}")

async def send_scenario_step(user_id, step):
    if step >= len(SCENARIO):
        logging.info(f"[SKIP] User {user_id}: step {step} >= len(SCENARIO)")
        return
    data = SCENARIO[step]
    now = int(time.time())
    # Если это repeat_group
    if "repeat_group" in data:
        # Получим repeat_substep и repeat_last_sent_at
        user = await get_user(user_id)
        substep = user['repeat_substep']
        repeat_last_sent_at = user['repeat_last_sent_at']
        repeat_steps = data["repeat_group"]
        current_repeat = repeat_steps[substep]
        delay = current_repeat["delay_minutes"] * 60
        if now >= repeat_last_sent_at + delay:
            logging.info(f"[REPEAT_GROUP] User {user_id}: substep {substep}, send: {current_repeat.get('text', '')[:40]}")
            try:
                await send_message_with_photo_or_text(
                    user_id,
                    current_repeat["text"],
                    current_repeat.get("buttons", []),
                    current_repeat.get("photo_url")
                )
                # Следующий substep по кругу
                next_substep = (substep + 1) % len(repeat_steps)
                await update_repeat_group(user_id, next_substep, now)
            except Exception as e: 
                print(e)
    else:
        text = data["text"]
        buttons = data.get("buttons", [])
        photo_url = data.get("photo_url")
        delay = data.get("delay_minutes", 0) * 60
        user = await get_user(user_id)
        last_sent_at = user['last_sent_at']
        started_at = user['started_at']
        # когда должен быть отправлен этот шаг
        total_delay = sum(
            s.get("delay_minutes", 0)
            for s in SCENARIO[:step+1]
            if "repeat_group" not in s
        ) * 60
        should_send_after = started_at + total_delay
        if now >= should_send_after:
            logging.info(f"[SEND] User {user_id}: step {step} — {text[:40]}")
            await send_message_with_photo_or_text(user_id, text, buttons, photo_url)
            await update_step(user_id, step + 1, now)

async def get_user(user_id):
    async with aiosqlite_connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, scenario_step, started_at, last_sent_at, repeat_substep, repeat_last_sent_at FROM users WHERE user_id=?",
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    'user_id': row[0],
                    'scenario_step': row[1],
                    'started_at': row[2],
                    'last_sent_at': row[3],
                    'repeat_substep': row[4],
                    'repeat_last_sent_at': row[5]
                }
            else:
                return None

# --- Handlers ---

@dp.message(CommandStart())
async def on_start(message: types.Message):
    await upsert_user(message.from_user)
    await send_scenario_step(message.from_user.id, 0)

# --- Background task ---

async def scenario_scheduler():
    await init_db()
    while True:
        users = await get_all_users()
        now = int(time.time())
        logging.info(f"[TICK] Checking {len(users)} users at {now}")
        for user in users:
            user_id, step, started_at, last_sent_at, repeat_substep, repeat_last_sent_at = user
            if step >= len(SCENARIO):
                continue
            data = SCENARIO[step]
            if "repeat_group" in data:
                # Вызываем repeat_group обработчик
                await send_scenario_step(user_id, step)
            else:
                await send_scenario_step(user_id, step)
        await asyncio.sleep(30)

# --- Entrypoint ---

async def main():
    asyncio.create_task(scenario_scheduler())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
