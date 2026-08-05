import logging
import os
from datetime import datetime

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from db import get_all_patients, get_language, init_db, save_checkin, set_language, upsert_patient
from i18n import TRANSLATIONS, t

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
DOCTOR_CHAT_ID = int(os.environ["DOCTOR_CHAT_ID"])

BOOKING_TIME, BOOKING_CONTACT = range(2)

PATIENT_BY_MESSAGE: dict[int, int] = {}
CHECKINS: dict[int, dict] = {}

LANGUAGE_BUTTONS = InlineKeyboardMarkup(
    [[InlineKeyboardButton("🇺🇦 Українська", callback_data="lang:uk"),
      InlineKeyboardButton("🇷🇺 Русский", callback_data="lang:ru")]]
)

CHECKIN_QUESTIONS = [
    {"key": "general", "q": "q_general", "type": "choice", "opts": "opt_general"},
    {"key": "weakness", "q": "q_weakness", "type": "choice", "opts": "opt_weakness"},
    {"key": "pain_level", "q": "q_pain_level", "type": "scale"},
    {"key": "pain_location", "q": "q_pain_location", "type": "multiselect", "opts": "opt_pain_location"},
    {"key": "painkillers", "q": "q_painkillers", "type": "choice", "opts": "opt_painkillers"},
    {"key": "temp_high", "q": "q_temp_high", "type": "choice", "opts": "opt_temp_high"},
    {
        "key": "temp_detail",
        "q": "q_temp_detail",
        "type": "text",
        "condition": lambda a: a.get("temp_high") == 1,
    },
    {"key": "temp_max", "q": "q_temp_max", "type": "number"},
    {"key": "discharge", "q": "q_discharge", "type": "choice", "opts": "opt_discharge"},
    {"key": "sutures", "q": "q_sutures", "type": "multiselect", "opts": "opt_sutures"},
    {"key": "urination", "q": "q_urination", "type": "choice", "opts": "opt_urination"},
    {"key": "bowel", "q": "q_bowel", "type": "choice", "opts": "opt_bowel"},
    {"key": "bloating", "q": "q_bloating", "type": "choice", "opts": "opt_bloating"},
    {"key": "appetite", "q": "q_appetite", "type": "choice", "opts": "opt_appetite"},
    {"key": "nausea", "q": "q_nausea", "type": "choice", "opts": "opt_nausea"},
    {"key": "activity", "q": "q_activity", "type": "choice", "opts": "opt_activity"},
    {"key": "concerns", "q": "q_concerns", "type": "text"},
    {"key": "visited_doctor", "q": "q_visited_doctor", "type": "text"},
    {"key": "red_flags", "q": "q_red_flags", "type": "multiselect", "opts": "opt_red_flags"},
]


class InCheckinFilter(filters.MessageFilter):
    def filter(self, message) -> bool:
        return message.chat_id in CHECKINS


IN_CHECKIN = InCheckinFilter()


def patient_label(update: Update) -> str:
    user = update.effective_user
    username = f"@{user.username}" if user.username else "без username"
    return f"{user.full_name} ({username}, id {user.id})"


async def patient_label_by_id(bot, chat_id: int) -> str:
    chat = await bot.get_chat(chat_id)
    username = f"@{chat.username}" if chat.username else "без username"
    name = f"{chat.first_name or ''} {chat.last_name or ''}".strip() or "Пацієнт"
    return f"{name} ({username}, id {chat_id})"


def option_texts(question: dict, lang: str) -> list[str]:
    return TRANSLATIONS[question["opts"]][lang]


def build_main_menu(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[t("menu_book", lang)]], resize_keyboard=True)


def build_choice_keyboard(question: dict, lang: str) -> ReplyKeyboardMarkup:
    opts = option_texts(question, lang)
    return ReplyKeyboardMarkup([[o] for o in opts], resize_keyboard=True, one_time_keyboard=True)


def build_scale_keyboard() -> InlineKeyboardMarkup:
    row1 = [InlineKeyboardButton(str(n), callback_data=f"scale:{n}") for n in range(0, 6)]
    row2 = [InlineKeyboardButton(str(n), callback_data=f"scale:{n}") for n in range(6, 11)]
    return InlineKeyboardMarkup([row1, row2])


def build_multiselect_keyboard(question: dict, lang: str, selected: set) -> InlineKeyboardMarkup:
    opts = option_texts(question, lang)
    rows = []
    for i, label in enumerate(opts):
        mark = "✅ " if i in selected else "☐ "
        rows.append([InlineKeyboardButton(mark + label, callback_data=f"ms:{i}")])
    rows.append([InlineKeyboardButton(t("done", lang), callback_data="ms:done")])
    return InlineKeyboardMarkup(rows)


def evaluate_severity(answers: dict) -> str:
    if answers.get("discharge") == 3:
        return "urgent"
    if answers.get("temp_max", 0) >= 38:
        return "urgent"
    if answers.get("pain_level", 0) >= 8:
        return "urgent"
    if any(i != 0 for i in answers.get("sutures", [])):
        return "urgent"
    if answers.get("urination") in (2, 3):
        return "urgent"
    if answers.get("red_flags") and any(i != 4 for i in answers["red_flags"]):
        return "urgent"
    if answers.get("nausea") == 0:
        return "attention"
    return "routine"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    upsert_patient(chat.id, user.full_name, user.username)
    lang = get_language(chat.id)
    await update.message.reply_text(t("welcome", lang), reply_markup=build_main_menu(lang))
    await update.message.reply_text(t("choose_language", lang), reply_markup=LANGUAGE_BUTTONS)


async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = get_language(update.effective_chat.id)
    await update.message.reply_text(t("choose_language", lang), reply_markup=LANGUAGE_BUTTONS)


async def handle_language_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    lang = query.data.split(":")[1]
    set_language(query.message.chat.id, lang)
    await query.answer()
    await query.edit_message_text(t("language_set", lang))
    await context.bot.send_message(query.message.chat.id, t("welcome", lang), reply_markup=build_main_menu(lang))


async def book_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = get_language(update.effective_chat.id)
    await update.message.reply_text(t("ask_time", lang), reply_markup=ReplyKeyboardRemove())
    return BOOKING_TIME


async def book_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = get_language(update.effective_chat.id)
    context.user_data["booking_time"] = update.message.text
    await update.message.reply_text(t("ask_contact", lang))
    return BOOKING_CONTACT


async def book_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = get_language(update.effective_chat.id)
    contact = update.message.text
    desired_time = context.user_data.pop("booking_time", "-")

    sent = await context.bot.send_message(
        chat_id=DOCTOR_CHAT_ID,
        text=t("booking_notify_doctor", lang, patient=patient_label(update), time=desired_time, contact=contact),
    )
    PATIENT_BY_MESSAGE[sent.message_id] = update.effective_chat.id
    await update.message.reply_text(t("booking_confirm_patient", lang), reply_markup=build_main_menu(lang))
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = get_language(update.effective_chat.id)
    context.user_data.pop("booking_time", None)
    await update.message.reply_text(t("cancel", lang), reply_markup=build_main_menu(lang))
    return ConversationHandler.END


async def handle_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = get_language(update.effective_chat.id)
    message = update.message
    caption = t("doctor_file_caption", lang, patient=patient_label(update))

    sent = None
    if message.document:
        sent = await context.bot.send_document(DOCTOR_CHAT_ID, message.document.file_id, caption=caption)
    elif message.photo:
        sent = await context.bot.send_photo(DOCTOR_CHAT_ID, message.photo[-1].file_id, caption=caption)

    if sent:
        PATIENT_BY_MESSAGE[sent.message_id] = update.effective_chat.id
    await message.reply_text(t("file_received", lang))


async def escalate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = get_language(update.effective_chat.id)
    sent = await context.bot.send_message(
        chat_id=DOCTOR_CHAT_ID,
        text=t("doctor_escalate_template", lang, patient=patient_label(update), text=update.message.text),
    )
    PATIENT_BY_MESSAGE[sent.message_id] = update.effective_chat.id
    await update.message.reply_text(t("escalate_ack", lang))


async def doctor_reply_by_swipe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    replied = update.message.reply_to_message
    patient_chat_id = PATIENT_BY_MESSAGE.get(replied.message_id) if replied else None

    if patient_chat_id is None:
        await update.message.reply_text(t("reply_no_target", "ru"))
        return

    lang = get_language(patient_chat_id)
    await context.bot.send_message(chat_id=patient_chat_id, text=update.message.text)
    await update.message.reply_text(t("reply_sent", lang))


async def doctor_reply_by_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != DOCTOR_CHAT_ID:
        return

    if len(context.args) < 2:
        await update.message.reply_text("Формат: /reply <ID пациента> <текст>")
        return

    try:
        patient_chat_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID пациента должен быть числом. Формат: /reply <ID пациента> <текст>")
        return

    text = " ".join(context.args[1:])
    lang = get_language(patient_chat_id)
    await context.bot.send_message(chat_id=patient_chat_id, text=text)
    await update.message.reply_text(t("reply_sent", lang))


async def launch_checkin(bot, patient_chat_id: int) -> None:
    lang = get_language(patient_chat_id)
    CHECKINS[patient_chat_id] = {"step": 0, "answers": {}, "lang": lang}
    await bot.send_message(patient_chat_id, t("checkin_intro", lang))
    await send_checkin_question(bot, patient_chat_id)


async def start_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != DOCTOR_CHAT_ID:
        return

    if not context.args:
        await update.message.reply_text("Формат: /checkin <ID пациента> (или наберите /patients, чтобы выбрать из списка)")
        return

    try:
        patient_chat_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID пациента должен быть числом.")
        return

    await launch_checkin(context.bot, patient_chat_id)
    await update.message.reply_text(t("checkin_started_notice", "ru"))


async def list_patients(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != DOCTOR_CHAT_ID:
        return

    patients = get_all_patients()
    if not patients:
        await update.message.reply_text("Пациентов пока нет — никто ещё не писал боту.")
        return

    buttons = [
        [InlineKeyboardButton(full_name or username or str(chat_id), callback_data=f"checkin:{chat_id}")]
        for chat_id, full_name, username in patients
    ]
    await update.message.reply_text(
        "Выберите пациента, чтобы отправить ему опрос:", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def start_checkin_from_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.message.chat.id != DOCTOR_CHAT_ID:
        await query.answer()
        return

    patient_chat_id = int(query.data.split(":")[1])
    await query.answer()
    await query.edit_message_text(f"Опрос отправлен пациенту (id {patient_chat_id}).")
    await launch_checkin(context.bot, patient_chat_id)


def next_question_index(state: dict) -> int:
    step = state["step"]
    while step < len(CHECKIN_QUESTIONS):
        condition = CHECKIN_QUESTIONS[step].get("condition")
        if condition and not condition(state["answers"]):
            step += 1
            continue
        break
    return step


async def send_checkin_question(bot, patient_chat_id: int) -> None:
    state = CHECKINS[patient_chat_id]
    state["step"] = next_question_index(state)
    lang = state["lang"]

    if state["step"] >= len(CHECKIN_QUESTIONS):
        await finish_checkin(bot, patient_chat_id)
        return

    question = CHECKIN_QUESTIONS[state["step"]]
    text = t(question["q"], lang)

    if question["type"] == "choice":
        await bot.send_message(patient_chat_id, text, reply_markup=build_choice_keyboard(question, lang))
    elif question["type"] == "scale":
        await bot.send_message(patient_chat_id, text, reply_markup=build_scale_keyboard())
    elif question["type"] == "multiselect":
        state["multiselect_selected"] = set()
        await bot.send_message(patient_chat_id, text, reply_markup=build_multiselect_keyboard(question, lang, set()))
    else:
        await bot.send_message(patient_chat_id, text, reply_markup=ReplyKeyboardRemove())


async def finish_checkin(bot, patient_chat_id: int) -> None:
    state = CHECKINS.pop(patient_chat_id)
    lang = state["lang"]
    answers = state["answers"]
    severity = evaluate_severity(answers)

    if severity == "urgent":
        await bot.send_message(patient_chat_id, t("urgent_patient_message", lang))

    label = await patient_label_by_id(bot, patient_chat_id)
    lines = [
        t(
            "checkin_summary_header",
            lang,
            severity=t(f"severity_{severity}", lang),
            patient=label,
            date=datetime.now().strftime("%d.%m.%Y %H:%M"),
        )
    ]

    for question in CHECKIN_QUESTIONS:
        key = question["key"]
        if key not in answers:
            continue
        value = answers[key]
        q_label = t(question["q"], lang)

        if question["type"] == "choice":
            value_text = option_texts(question, lang)[value]
        elif question["type"] == "multiselect":
            opts = option_texts(question, lang)
            value_text = ", ".join(opts[i] for i in sorted(value)) if value else "-"
        else:
            value_text = str(value)

        lines.append(f"{q_label}: {value_text}")

    await bot.send_message(DOCTOR_CHAT_ID, "\n".join(lines))
    save_checkin(patient_chat_id, answers, severity)
    await bot.send_message(patient_chat_id, t("checkin_thanks", lang))


async def handle_checkin_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    patient_chat_id = update.effective_chat.id
    state = CHECKINS[patient_chat_id]
    question = CHECKIN_QUESTIONS[state["step"]]
    lang = state["lang"]
    text = update.message.text

    if question["type"] == "choice":
        opts = option_texts(question, lang)
        if text not in opts:
            await update.message.reply_text(t("choose_from_options", lang))
            return
        state["answers"][question["key"]] = opts.index(text)
    elif question["type"] == "number":
        try:
            value = float(text.replace(",", "."))
            if not (35 <= value <= 42):
                raise ValueError
        except ValueError:
            await update.message.reply_text(t("temp_invalid", lang))
            return
        state["answers"][question["key"]] = value
    else:
        state["answers"][question["key"]] = text

    state["step"] += 1
    await send_checkin_question(context.bot, patient_chat_id)


async def handle_scale_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    patient_chat_id = query.message.chat.id
    state = CHECKINS.get(patient_chat_id)
    await query.answer()
    if not state:
        return

    question = CHECKIN_QUESTIONS[state["step"]]
    value = int(query.data.split(":")[1])
    state["answers"][question["key"]] = value
    state["step"] += 1
    await query.edit_message_reply_markup(reply_markup=None)
    await send_checkin_question(context.bot, patient_chat_id)


async def handle_multiselect_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    patient_chat_id = query.message.chat.id
    state = CHECKINS.get(patient_chat_id)
    await query.answer()
    if not state:
        return

    question = CHECKIN_QUESTIONS[state["step"]]
    lang = state["lang"]
    action = query.data.split(":")[1]

    if action == "done":
        state["answers"][question["key"]] = sorted(state.get("multiselect_selected", set()))
        state["step"] += 1
        await query.edit_message_reply_markup(reply_markup=None)
        await send_checkin_question(context.bot, patient_chat_id)
        return

    index = int(action)
    selected = state.setdefault("multiselect_selected", set())
    if index in selected:
        selected.discard(index)
    else:
        selected.add(index)
    await query.edit_message_reply_markup(reply_markup=build_multiselect_keyboard(question, lang, selected))


def main() -> None:
    init_db()
    application = Application.builder().token(BOT_TOKEN).build()

    booking_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Text([t("menu_book", "uk"), t("menu_book", "ru")]), book_start)
        ],
        states={
            BOOKING_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, book_time)],
            BOOKING_CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, book_contact)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("language", language_command))
    application.add_handler(CommandHandler("reply", doctor_reply_by_command))
    application.add_handler(CommandHandler("checkin", start_checkin))
    application.add_handler(CommandHandler("patients", list_patients))

    application.add_handler(CallbackQueryHandler(handle_language_choice, pattern="^lang:"))
    application.add_handler(CallbackQueryHandler(handle_scale_callback, pattern="^scale:"))
    application.add_handler(CallbackQueryHandler(handle_multiselect_callback, pattern="^ms:"))
    application.add_handler(CallbackQueryHandler(start_checkin_from_button, pattern="^checkin:"))

    application.add_handler(MessageHandler(IN_CHECKIN & filters.TEXT, handle_checkin_text))
    application.add_handler(booking_conv)
    application.add_handler(
        MessageHandler(filters.Chat(DOCTOR_CHAT_ID) & filters.REPLY, doctor_reply_by_swipe)
    )
    application.add_handler(
        MessageHandler((filters.Document.ALL | filters.PHOTO) & ~filters.Chat(DOCTOR_CHAT_ID), handle_attachment)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Chat(DOCTOR_CHAT_ID), escalate)
    )

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
