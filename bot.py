import os
import asyncio
import random
import logging
from typing import List

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    ChatPermissions,
    CallbackQuery,
    ChatMemberUpdated,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties

import db


# --- ЛОГИРОВАНИЕ ---
logger = logging.getLogger("captcha_bot")

# --- Загрузка .env ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден в .env")


router = Router()


def generate_captcha() -> tuple[str, str, List[str]]:

    a = random.randint(1, 9)
    b = random.randint(1, 9)
    correct = a + b
    question = f"{a} + {b} = ?"
    answers = {str(correct)}
    while len(answers) < 4:
        fake = random.randint(2, 18)
        answers.add(str(fake))
    options = list(answers)
    random.shuffle(options)
    return question, str(correct), options


def build_captcha_keyboard(chat_id: int, user_id: int, options: List[str]):
    builder = InlineKeyboardBuilder()
    for answer in options:
        builder.button(
            text=answer,
            callback_data=f"captcha:{chat_id}:{user_id}:{answer}",
        )
    builder.adjust(2)
    return builder.as_markup()


async def kick_after_timeout(bot: Bot, chat_id: int, user_id: int, timeout: int = 60):

    await asyncio.sleep(timeout)

    row = await db.get_captcha(chat_id, user_id)
    if row is None:
        logger.debug(
            "Timeout check: no captcha row (chat_id=%s, user_id=%s) — skip",
            chat_id,
            user_id,
        )
        return
    if row["status"] != "pending":
        logger.debug(
            "Timeout check: captcha status is %s (chat_id=%s, user_id=%s) — skip",
            row["status"],
            chat_id,
            user_id,
        )
        return

    # пробуем кикнуть
    try:
        await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
        await db.update_status(chat_id, user_id, "kicked")

        # mention для сообщения
        mention = str(user_id)
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            mention = member.user.mention_html()
        except Exception:
            pass

        await bot.send_message(
            chat_id,
            f"{mention} не прошёл капчу за 60 секунд и был удалён из чата.",
        )

        logger.info(
            "User kicked after timeout: chat_id=%s, user_id=%s",
            chat_id,
            user_id,
        )
        return
    except Exception as e:
        logger.exception(
            "Failed to ban user for not solving captcha: chat_id=%s, user_id=%s",
            chat_id,
            user_id,
        )
        # если не смогли кикнуть — уведомляем в чат, чтобы ты видел причину
        try:
            await bot.send_message(
                chat_id,
                f"Не смог удалить пользователя с ID <code>{user_id}</code> после таймаута капчи.\n"
                f"Причина от Telegram: <code>{e}</code>\n"
                f"Чаще всего это значит, что пользователь — создатель/админ чата или у бота нет прав банить.",
            )
        except Exception:
            logger.exception(
                "Failed to send 'cannot kick' message: chat_id=%s, user_id=%s",
                chat_id,
                user_id,
            )


async def start_captcha_flow(
    bot: Bot,
    chat_id: int,
    chat_title: str | None,
    chat_type: str,
    user,
):

    user_id = user.id
    question, correct_answer, options = generate_captcha()

    await db.save_captcha(chat_id, user_id, question, correct_answer, status="pending")
    logger.debug(
        "Captcha saved: chat_id=%s, user_id=%s, question=%s, answer=%s",
        chat_id,
        user_id,
        question,
        correct_answer,
    )

    can_restrict = chat_type == "supergroup"

    # запрещаем писать в чат (если это супергруппа)
    if can_restrict:
        try:
            await bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=ChatPermissions(
                    can_send_messages=False,
                    can_send_media_messages=False,
                    can_send_polls=False,
                    can_send_other_messages=False,
                    can_add_web_page_previews=False,
                    can_change_info=False,
                    can_invite_users=False,
                    can_pin_messages=False,
                ),
            )
            logger.info(
                "User restricted (waiting for captcha): chat_id=%s (%s), user_id=%s",
                chat_id,
                chat_title,
                user_id,
            )
        except Exception:
            logger.exception(
                "Failed to restrict user: chat_id=%s, user_id=%s",
                chat_id,
                user_id,
            )
    else:
        # обычная группа — Telegram НЕ даёт restrictChatMember
        logger.warning(
            "Chat is basic 'group', cannot restrict members. "
            "For full captcha protection convert it to 'supergroup'. "
            "chat_id=%s (%s)",
            chat_id,
            chat_title,
        )

    # отправляем капчу
    kb = build_captcha_keyboard(chat_id, user_id, options)
    text = (
        f"Добро пожаловать, {user.mention_html()}!\n"
        f"Пожалуйста, реши капчу за 60 секунд, иначе ты будешь удалён.\n\n"
        f"<b>Вопрос:</b> {question}"
    )

    try:
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
        logger.info(
            "Captcha message sent: chat_id=%s (%s), user_id=%s",
            chat_id,
            chat_title,
            user_id,
        )
    except Exception:
        logger.exception(
            "Failed to send captcha message: chat_id=%s, user_id=%s",
            chat_id,
            user_id,
        )

    # запускаем таймер на кик
    asyncio.create_task(kick_after_timeout(bot, chat_id, user_id, timeout=60))


@router.message(CommandStart())
async def cmd_start(message: Message):
    logger.info(
        "/start from user_id=%s in chat_id=%s (type=%s)",
        message.from_user.id if message.from_user else None,
        message.chat.id,
        message.chat.type,
    )
    await message.answer(
        "Привет! Я бот-капча для групп.\n\n"
        "Лучше всего я работаю в супергруппах (там я могу ограничивать сообщения до прохождения капчи).\n"
        "Добавь меня в группу, выдай права:\n"
        "• удалять сообщения\n"
        "• ограничивать участников\n"
        "• банить участников\n\n"
        "После этого я буду выдавать капчу всем, кто ещё её не прошёл."
    )


@router.my_chat_member()
async def on_my_chat_member(update: ChatMemberUpdated):

    old_status = update.old_chat_member.status
    new_status = update.new_chat_member.status
    chat = update.chat

    logger.info(
        "MyChatMember update: chat_id=%s, title=%s, old_status=%s, new_status=%s, type=%s",
        chat.id,
        getattr(chat, "title", None),
        old_status,
        new_status,
        chat.type,
    )


@router.message(F.new_chat_members)
async def on_user_join(message: Message, bot: Bot):

    chat = message.chat
    chat_id = chat.id

    for user in message.new_chat_members:
        # игнорируем ботов
        if user.is_bot:
            logger.info(
                "Bot joined chat, skip captcha: chat_id=%s (%s), bot_id=%s, username=%s",
                chat_id,
                getattr(chat, "title", None),
                user.id,
                user.username,
            )
            continue

        logger.info(
            "New member joined: chat_id=%s (%s), type=%s, user_id=%s, username=%s, full_name=%s",
            chat_id,
            getattr(chat, "title", None),
            chat.type,
            user.id,
            user.username,
            user.full_name,
        )

        await start_captcha_flow(
            bot,
            chat_id,
            getattr(chat, "title", None),
            chat.type,
            user,
        )


@router.message(
    F.chat.type.in_({"group", "supergroup"}) & ~F.new_chat_members
)
async def on_any_message(message: Message, bot: Bot):

    chat = message.chat
    chat_id = chat.id
    chat_title = getattr(chat, "title", None)
    chat_type = chat.type

    user = message.from_user
    if user is None:
        return

    if user.is_bot:
        return

    user_id = user.id

    row = await db.get_captcha(chat_id, user_id)

    # Уже прошёл капчу
    if row is not None and row["status"] == "solved":
        return

    # Пытается писать без капчи/с незакрытой капчей → пытаемся удалить сообщение
    try:
        await message.delete()
        logger.info(
            "Deleted message from user without solved captcha: chat_id=%s (%s), user_id=%s",
            chat_id,
            chat_title,
            user_id,
        )
    except Exception:
        logger.exception(
            "Failed to delete message from user without solved captcha: chat_id=%s, user_id=%s",
            chat_id,
            user_id,
        )

    # Если капча уже pending — просто не спамим ещё одной
    if row is not None and row["status"] == "pending":
        logger.info(
            "User tried to write while captcha pending: chat_id=%s (%s), user_id=%s",
            chat_id,
            chat_title,
            user_id,
        )
        return

    # Капчи ещё не было → создаём и запускаем
    logger.info(
        "Starting captcha flow because user wrote without captcha: chat_id=%s (%s), type=%s, user_id=%s",
        chat_id,
        chat_title,
        chat_type,
        user_id,
    )
    await start_captcha_flow(bot, chat_id, chat_title, chat_type, user)


@router.callback_query(F.data.startswith("captcha:"))
async def on_captcha_answer(callback: CallbackQuery, bot: Bot):
    if not callback.data:
        await callback.answer("Некорректные данные.", show_alert=True)
        return

    try:
        _, chat_id_str, user_id_str, answer = callback.data.split(":", maxsplit=3)
    except ValueError:
        await callback.answer("Некорректные данные.", show_alert=True)
        return

    chat_id = int(chat_id_str)
    target_user_id = int(user_id_str)
    from_user_id = callback.from_user.id

    logger.info(
        "Captcha button pressed: chat_id=%s, target_user_id=%s, from_user_id=%s, answer=%s",
        chat_id,
        target_user_id,
        from_user_id,
        answer,
    )

    # нажимать кнопку может только тот, для кого капча
    if from_user_id != target_user_id:
        await callback.answer("Эта капча не для тебя.", show_alert=True)
        logger.warning(
            "User tried to answer foreign captcha: chat_id=%s, target_user_id=%s, from_user_id=%s",
            chat_id,
            target_user_id,
            from_user_id,
        )
        return

    row = await db.get_captcha(chat_id, target_user_id)
    if row is None or row["status"] != "pending":
        await callback.answer("Капча уже неактивна.", show_alert=False)
        logger.info(
            "Captcha not active: chat_id=%s, user_id=%s",
            chat_id,
            target_user_id,
        )
        return

    correct_answer = row["answer"]

    if answer != correct_answer:
        await callback.answer("Неверно, попробуй ещё раз.", show_alert=True)
        logger.info(
            "Wrong captcha answer: chat_id=%s, user_id=%s, answer=%s, correct=%s",
            chat_id,
            target_user_id,
            answer,
            correct_answer,
        )
        return

    # всё ок — снимаем ограничения (только в супергруппе)
    try:
        chat = await bot.get_chat(chat_id)
        if chat.type == "supergroup":
            await bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=target_user_id,
                permissions=ChatPermissions(
                    can_send_messages=True,
                    can_send_media_messages=True,
                    can_send_polls=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True,
                    can_change_info=False,
                    can_invite_users=True,
                    can_pin_messages=False,
                ),
            )
            logger.info(
                "User un-restricted (captcha solved): chat_id=%s, user_id=%s",
                chat_id,
                target_user_id,
            )
        else:
            logger.info(
                "Captcha solved in basic group (no restrict logic): chat_id=%s, user_id=%s",
                chat_id,
                target_user_id,
            )
    except Exception:
        logger.exception(
            "Failed to unrestrict user after captcha: chat_id=%s, user_id=%s",
            chat_id,
            target_user_id,
        )

    await db.update_status(chat_id, target_user_id, "solved")

    await callback.answer("Капча пройдена, добро пожаловать в чат!", show_alert=True)

    # убираем кнопки у сообщения (если можем)
    try:
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.exception(
            "Failed to remove inline keyboard after captcha solved: chat_id=%s, user_id=%s",
            chat_id,
            target_user_id,
        )


async def main():
    # базовая настройка логов: в консоль + формат
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    logger.info("Starting captcha bot...")

    await db.init_db()
    logger.info("Database initialized.")

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode="HTML"),
    )

    # информация о боте
    me = await bot.get_me()
    logger.info(
        "Bot started as: id=%s, username=@%s, name=%s",
        me.id,
        me.username,
        me.full_name,
    )

    dp = Dispatcher()
    dp.include_router(router)

    logger.info("Start polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
