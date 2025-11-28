import os
import time
import aiosqlite
from dotenv import load_dotenv

# Загружаем переменные окружения из .env
load_dotenv()

DB_PATH = os.getenv("DB_PATH", "captcha_bot.sqlite3")


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS captchas (
                chat_id    INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                question   TEXT    NOT NULL,
                answer     TEXT    NOT NULL,
                created_at INTEGER NOT NULL,
                status     TEXT    NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            );
            """
        )
        await db.commit()


async def save_captcha(
    chat_id: int,
    user_id: int,
    question: str,
    answer: str,
    status: str = "pending",
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO captchas (chat_id, user_id, question, answer, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                question   = excluded.question,
                answer     = excluded.answer,
                created_at = excluded.created_at,
                status     = excluded.status;
            """,
            (chat_id, user_id, question, answer, int(time.time()), status),
        )
        await db.commit()


async def get_captcha(chat_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM captchas WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row


async def update_status(chat_id: int, user_id: int, status: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE captchas SET status = ? WHERE chat_id = ? AND user_id = ?",
            (status, chat_id, user_id),
        )
        await db.commit()


async def delete_captcha(chat_id: int, user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM captchas WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        await db.commit()
