import logging
import os

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
DOCTOR_CHAT_ID = int(os.environ["DOCTOR_CHAT_ID"])

BOOKING_TIME, BOOKING_CONTACT = range(2)

MAIN_MENU = ReplyKeyboardMarkup([["📅 Записаться на приём"]], resize_keyboard=True)


def patient_label(update: Update) -> str:
    user = update.effective_user
    username = f"@{user.username}" if user.username else "без username"
    return f"{user.full_name} ({username}, id {user.id})"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Здравствуйте, это АІ-помощник доктора Романа Чередниченко.\n\n"
        "— «📅 Записаться на приём» — оформить заявку на визит.\n"
        "— Просто напишите сообщение — вопрос передастся врачу.\n"
        "— Прикрепите файл или фото с результатами анализов — он тоже уйдёт врачу.",
        reply_markup=MAIN_MENU,
    )


async def book_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "На какую дату и время вам удобно записаться? Напишите в свободной форме.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return BOOKING_TIME


async def book_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["booking_time"] = update.message.text
    await update.message.reply_text("Оставьте номер телефона для подтверждения записи.")
    return BOOKING_CONTACT


async def book_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    contact = update.message.text
    desired_time = context.user_data.pop("booking_time", "не указано")

    await context.bot.send_message(
        chat_id=DOCTOR_CHAT_ID,
        text=(
            "🗓 Новая заявка на запись\n"
            f"Пациент: {patient_label(update)}\n"
            f"Желаемое время: {desired_time}\n"
            f"Контакт: {contact}"
        ),
    )
    await update.message.reply_text(
        "Заявка передана врачу, с вами свяжутся для подтверждения времени.",
        reply_markup=MAIN_MENU,
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("booking_time", None)
    await update.message.reply_text("Отменено.", reply_markup=MAIN_MENU)
    return ConversationHandler.END


async def handle_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    caption = f"📄 Файл от пациента: {patient_label(update)}"

    if message.document:
        await context.bot.send_document(DOCTOR_CHAT_ID, message.document.file_id, caption=caption)
    elif message.photo:
        await context.bot.send_photo(DOCTOR_CHAT_ID, message.photo[-1].file_id, caption=caption)

    await message.reply_text("Файл получен, врач ознакомится и прокомментирует результаты.")


async def escalate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_message(
        chat_id=DOCTOR_CHAT_ID,
        text=(
            "💬 Сообщение от пациента\n"
            f"От: {patient_label(update)}\n\n"
            f"{update.message.text}"
        ),
    )
    await update.message.reply_text("Ваше сообщение передано врачу, он ответит вам в ближайшее время.")


def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()

    booking_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📅 Записаться на приём$"), book_start)],
        states={
            BOOKING_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, book_time)],
            BOOKING_CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, book_contact)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(booking_conv)
    application.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_attachment))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, escalate))

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
