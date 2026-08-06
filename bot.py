import logging
import os
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo

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

from db import (
    delete_checkin_session,
    delete_patient,
    get_all_patients,
    get_checkin_session,
    get_due_schedules,
    get_language,
    get_patient_by_phone,
    get_surgeries_due_for_day3,
    get_surgeries_due_for_day7,
    get_surgery,
    has_checkin_session,
    init_db,
    mark_day3_asked,
    mark_day3_sent,
    mark_day7_sent,
    mark_reminder_sent,
    mark_schedule_sent,
    save_checkin,
    save_checkin_session,
    schedule_followups,
    set_language,
    upsert_patient,
    upsert_surgery,
)
from i18n import TRANSLATIONS, t
from phone import normalize_phone
from sheets import fetch_upcoming_surgeries

KYIV_TZ = ZoneInfo("Europe/Kyiv")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
DOCTOR_CHAT_ID = int(os.environ["DOCTOR_CHAT_ID"])

BOOKING_TIME, BOOKING_CONTACT = range(2)

PATIENT_BY_MESSAGE: dict[int, int] = {}
PENDING_MESSAGE_TARGET: dict[int, int] = {}

LANGUAGE_BUTTONS = InlineKeyboardMarkup(
    [[InlineKeyboardButton("🇺🇦 Українська", callback_data="lang:uk"),
      InlineKeyboardButton("🇷🇺 Русский", callback_data="lang:ru")]]
)

ADMIN_MENU_PATIENTS = "📋 Пациенты"
ADMIN_MENU_MESSAGE = "✉️ Написать"
ADMIN_MENU_SYNC = "🔄 Синхронизировать таблицу"
ADMIN_MENU = ReplyKeyboardMarkup(
    [[ADMIN_MENU_PATIENTS, ADMIN_MENU_MESSAGE], [ADMIN_MENU_SYNC]], resize_keyboard=True
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
        return has_checkin_session(message.chat_id)


IN_CHECKIN = InCheckinFilter()


class DoctorPendingMessageFilter(filters.MessageFilter):
    def filter(self, message) -> bool:
        return message.chat_id == DOCTOR_CHAT_ID and DOCTOR_CHAT_ID in PENDING_MESSAGE_TARGET


DOCTOR_HAS_PENDING_MESSAGE = DoctorPendingMessageFilter()


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

    if chat.id == DOCTOR_CHAT_ID:
        await update.message.reply_text("Меню администратора:", reply_markup=ADMIN_MENU)
        return

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
    user = update.effective_user
    upsert_patient(update.effective_chat.id, user.full_name, user.username)
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

    user = update.effective_user
    upsert_patient(update.effective_chat.id, user.full_name, user.username, phone=normalize_phone(contact))

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
    user = update.effective_user
    upsert_patient(update.effective_chat.id, user.full_name, user.username)
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
    user = update.effective_user
    upsert_patient(update.effective_chat.id, user.full_name, user.username)
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


async def forget_patient(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != DOCTOR_CHAT_ID:
        return

    if not context.args:
        await update.message.reply_text("Формат: /forget <ID пациента>")
        return

    try:
        patient_chat_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID пациента должен быть числом.")
        return

    removed = delete_patient(patient_chat_id)
    if removed:
        await update.message.reply_text(f"Удалено из списка пациентов: id {patient_chat_id}.")
    else:
        await update.message.reply_text(f"Пациент с id {patient_chat_id} не найден в списке.")


async def register_patient(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != DOCTOR_CHAT_ID:
        return

    if len(context.args) < 2:
        await update.message.reply_text("Формат: /register <ID пациента> <Имя Фамилия>")
        return

    try:
        patient_chat_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID пациента должен быть числом.")
        return

    full_name = " ".join(context.args[1:])
    upsert_patient(patient_chat_id, full_name, None)
    await update.message.reply_text(f"Добавлено в список пациентов: {full_name} (id {patient_chat_id}).")


async def launch_checkin(bot, patient_chat_id: int, is_manual: bool = True) -> None:
    lang = get_language(patient_chat_id)
    save_checkin_session(patient_chat_id, {"step": 0, "answers": {}, "lang": lang, "multiselect_selected": set()})
    if is_manual:
        schedule_followups(patient_chat_id, datetime.now(KYIV_TZ).date())
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

    PENDING_MESSAGE_TARGET.pop(DOCTOR_CHAT_ID, None)
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


async def message_patients_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != DOCTOR_CHAT_ID:
        return

    PENDING_MESSAGE_TARGET.pop(DOCTOR_CHAT_ID, None)
    patients = get_all_patients()
    if not patients:
        await update.message.reply_text("Пациентов пока нет — никто ещё не писал боту.")
        return

    buttons = [
        [InlineKeyboardButton(full_name or username or str(chat_id), callback_data=f"msg:{chat_id}")]
        for chat_id, full_name, username in patients
    ]
    await update.message.reply_text(
        "Кому хотите написать?", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def select_message_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.message.chat.id != DOCTOR_CHAT_ID:
        await query.answer()
        return

    patient_chat_id = int(query.data.split(":")[1])
    PENDING_MESSAGE_TARGET[DOCTOR_CHAT_ID] = patient_chat_id
    await query.answer()
    await query.edit_message_text(f"Напишите текст сообщения следующим сообщением (получатель id {patient_chat_id}).")


async def send_pending_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    patient_chat_id = PENDING_MESSAGE_TARGET.pop(DOCTOR_CHAT_ID)
    lang = get_language(patient_chat_id)
    await context.bot.send_message(patient_chat_id, update.message.text)
    await update.message.reply_text(t("reply_sent", lang))


async def run_scheduled_checkins(context: ContextTypes.DEFAULT_TYPE) -> None:
    today = datetime.now(KYIV_TZ).date()
    for schedule_id, patient_chat_id in get_due_schedules(today):
        mark_schedule_sent(schedule_id)
        await launch_checkin(context.bot, patient_chat_id, is_manual=False)


RELEVANCE_LIMIT_DAYS = 10


async def sync_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != DOCTOR_CHAT_ID:
        return
    await update.message.reply_text("Синхронізую з таблицею...")
    await sync_surgeries_job(context)
    await update.message.reply_text("Готово.")


async def sync_surgeries_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    today = datetime.now(KYIV_TZ).date()

    try:
        surgeries = fetch_upcoming_surgeries(today)
    except Exception:
        logger.exception("Failed to sync surgeries spreadsheet")
    else:
        for item in surgeries:
            upsert_surgery(item["phone"], item["full_name"], item["surgery_date"])

    for surgery_id, phone, full_name, surgery_date, reminder_sent in get_surgeries_due_for_day3(
        today - timedelta(days=2)
    ):
        age_days = (today - surgery_date).days
        if age_days > RELEVANCE_LIMIT_DAYS:
            mark_day3_asked(surgery_id)
            mark_day7_sent(surgery_id)
            continue

        if get_patient_by_phone(phone) is None:
            if not reminder_sent:
                mark_reminder_sent(surgery_id)
                await context.bot.send_message(
                    DOCTOR_CHAT_ID,
                    f"⚠️ {full_name} (тел. {phone}, операція {surgery_date.strftime('%d.%m.%Y')}) "
                    "ще не підключена до бота — 3-й день вже настав. Підключіть, щоб можна було надіслати анкету.",
                )
            continue

        mark_day3_asked(surgery_id)
        buttons = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Так", callback_data=f"surg:{surgery_id}:yes"),
                InlineKeyboardButton("❌ Ні", callback_data=f"surg:{surgery_id}:no"),
            ]]
        )
        await context.bot.send_message(
            DOCTOR_CHAT_ID,
            f"📋 3-й день після операції — {full_name}\n"
            f"Операція: {surgery_date.strftime('%d.%m.%Y')}\n\n"
            "Надіслати анкету?",
            reply_markup=buttons,
        )

    for surgery_id, phone, full_name, surgery_date in get_surgeries_due_for_day7(today - timedelta(days=6)):
        age_days = (today - surgery_date).days
        if age_days > RELEVANCE_LIMIT_DAYS:
            mark_day7_sent(surgery_id)
            continue

        patient_chat_id = get_patient_by_phone(phone)
        if patient_chat_id is None:
            continue
        mark_day7_sent(surgery_id)
        await launch_checkin(context.bot, patient_chat_id, is_manual=False)


async def handle_surgery_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.message.chat.id != DOCTOR_CHAT_ID:
        await query.answer()
        return

    _, surgery_id_str, decision = query.data.split(":")
    surgery_id = int(surgery_id_str)
    await query.answer()

    surgery = get_surgery(surgery_id)
    if not surgery:
        await query.edit_message_text("Запис не знайдено (можливо, вже оброблено).")
        return

    _, phone, full_name, surgery_date = surgery

    if decision == "no":
        await query.edit_message_text(f"Пропущено: {full_name}.")
        return

    patient_chat_id = get_patient_by_phone(phone)
    if patient_chat_id is None:
        await query.edit_message_text(f"Пацієнта {full_name} не знайдено серед контактів бота.")
        return

    mark_day3_sent(surgery_id)
    await launch_checkin(context.bot, patient_chat_id, is_manual=False)
    await query.edit_message_text(f"✅ Надіслано: {full_name}.")


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
    state = get_checkin_session(patient_chat_id)
    state["step"] = next_question_index(state)
    lang = state["lang"]

    if state["step"] >= len(CHECKIN_QUESTIONS):
        await finish_checkin(bot, patient_chat_id, state)
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

    save_checkin_session(patient_chat_id, state)


async def finish_checkin(bot, patient_chat_id: int, state: dict) -> None:
    delete_checkin_session(patient_chat_id)
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
    state = get_checkin_session(patient_chat_id)
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
    save_checkin_session(patient_chat_id, state)
    await send_checkin_question(context.bot, patient_chat_id)


async def handle_scale_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    patient_chat_id = query.message.chat.id
    state = get_checkin_session(patient_chat_id)
    await query.answer()
    if not state:
        return

    question = CHECKIN_QUESTIONS[state["step"]]
    lang = state["lang"]
    value = int(query.data.split(":")[1])
    state["answers"][question["key"]] = value
    state["step"] += 1
    save_checkin_session(patient_chat_id, state)
    await query.edit_message_text(f"{t(question['q'], lang)}\n\n➡️ {value}", reply_markup=None)
    await send_checkin_question(context.bot, patient_chat_id)


async def handle_multiselect_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    patient_chat_id = query.message.chat.id
    state = get_checkin_session(patient_chat_id)
    await query.answer()
    if not state:
        return

    question = CHECKIN_QUESTIONS[state["step"]]
    lang = state["lang"]
    action = query.data.split(":")[1]

    if action == "done":
        selected = sorted(state.get("multiselect_selected", set()))
        state["answers"][question["key"]] = selected
        state["step"] += 1
        save_checkin_session(patient_chat_id, state)
        opts = option_texts(question, lang)
        chosen_text = ", ".join(opts[i] for i in selected) if selected else "-"
        await query.edit_message_text(f"{t(question['q'], lang)}\n\n➡️ {chosen_text}", reply_markup=None)
        await send_checkin_question(context.bot, patient_chat_id)
        return

    index = int(action)
    selected = state.setdefault("multiselect_selected", set())
    if index in selected:
        selected.discard(index)
    else:
        selected.add(index)
    save_checkin_session(patient_chat_id, state)
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
    application.add_handler(CommandHandler("forget", forget_patient))
    application.add_handler(CommandHandler("register", register_patient))
    application.add_handler(CommandHandler("checkin", start_checkin))
    application.add_handler(CommandHandler("patients", list_patients))
    application.add_handler(CommandHandler("message", message_patients_list))
    application.add_handler(CommandHandler("sync", sync_now))

    application.add_handler(CallbackQueryHandler(handle_language_choice, pattern="^lang:"))
    application.add_handler(CallbackQueryHandler(handle_scale_callback, pattern="^scale:"))
    application.add_handler(CallbackQueryHandler(handle_multiselect_callback, pattern="^ms:"))
    application.add_handler(CallbackQueryHandler(start_checkin_from_button, pattern="^checkin:"))
    application.add_handler(CallbackQueryHandler(handle_surgery_confirmation, pattern="^surg:"))
    application.add_handler(CallbackQueryHandler(select_message_target, pattern="^msg:"))

    application.add_handler(MessageHandler(IN_CHECKIN & filters.TEXT, handle_checkin_text))
    application.add_handler(
        MessageHandler(filters.Chat(DOCTOR_CHAT_ID) & filters.Text([ADMIN_MENU_PATIENTS]), list_patients)
    )
    application.add_handler(
        MessageHandler(filters.Chat(DOCTOR_CHAT_ID) & filters.Text([ADMIN_MENU_MESSAGE]), message_patients_list)
    )
    application.add_handler(
        MessageHandler(filters.Chat(DOCTOR_CHAT_ID) & filters.Text([ADMIN_MENU_SYNC]), sync_now)
    )
    application.add_handler(MessageHandler(DOCTOR_HAS_PENDING_MESSAGE & filters.TEXT, send_pending_message))
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

    application.job_queue.run_daily(run_scheduled_checkins, time=dt_time(hour=10, minute=0, tzinfo=KYIV_TZ))
    application.job_queue.run_daily(sync_surgeries_job, time=dt_time(hour=9, minute=45, tzinfo=KYIV_TZ))

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
