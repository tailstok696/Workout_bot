import os
import sqlite3
import logging
from datetime import datetime, date

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "workout.db")

# ---------- Стани для розмов (ConversationHandler) ----------
NEWDAY_NAME = 1
ADDEX_DAY, ADDEX_NAME = range(2, 4)
LOG_EXERCISE, LOG_WEIGHT, LOG_REPS, LOG_MORE = range(4, 8)


# ---------- Робота з базою даних ----------
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS days (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            position INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS exercises (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (day_id) REFERENCES days (id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            exercise_name TEXT NOT NULL,
            log_date TEXT NOT NULL,
            set_number INTEGER NOT NULL,
            weight REAL NOT NULL,
            reps INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


# ---------- Допоміжні функції ----------
def get_days(user_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM days WHERE user_id = ? ORDER BY position, id", (user_id,)
    ).fetchall()
    conn.close()
    return rows


def get_exercises_for_day(day_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM exercises WHERE day_id = ? ORDER BY position, id", (day_id,)
    ).fetchall()
    conn.close()
    return rows


def get_all_exercise_names(user_id):
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT DISTINCT e.name FROM exercises e
        JOIN days d ON e.day_id = d.id
        WHERE d.user_id = ?
        ORDER BY e.name
        """,
        (user_id,),
    ).fetchall()
    conn.close()
    return [r["name"] for r in rows]


def next_set_number(user_id, exercise_name, log_date):
    conn = get_conn()
    row = conn.execute(
        "SELECT COALESCE(MAX(set_number), 0) as m FROM logs "
        "WHERE user_id = ? AND exercise_name = ? AND log_date = ?",
        (user_id, exercise_name, log_date),
    ).fetchone()
    conn.close()
    return row["m"] + 1


def add_log(user_id, exercise_name, weight, reps):
    log_date = date.today().isoformat()
    set_number = next_set_number(user_id, exercise_name, log_date)
    conn = get_conn()
    conn.execute(
        "INSERT INTO logs (user_id, exercise_name, log_date, set_number, weight, reps, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, exercise_name, log_date, set_number, weight, reps, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    return set_number


def best_set_per_session(user_id, exercise_name, limit=10):
    """Повертає для кожної дати найкращий підхід (найбільша вага, потім повтори)."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT log_date, weight, reps FROM logs "
        "WHERE user_id = ? AND exercise_name = ? "
        "ORDER BY log_date DESC, weight DESC, reps DESC",
        (user_id, exercise_name),
    ).fetchall()
    conn.close()

    sessions = {}
    order = []
    for r in rows:
        d = r["log_date"]
        if d not in sessions:
            sessions[d] = (r["weight"], r["reps"])
            order.append(d)
        else:
            w, rp = sessions[d]
            if r["weight"] > w or (r["weight"] == w and r["reps"] > rp):
                sessions[d] = (r["weight"], r["reps"])
    return [(d, sessions[d][0], sessions[d][1]) for d in order[:limit]]


def today_logs(user_id):
    log_date = date.today().isoformat()
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM logs WHERE user_id = ? AND log_date = ? ORDER BY exercise_name, set_number",
        (user_id, log_date),
    ).fetchall()
    conn.close()
    return rows


# ---------- Команди ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Привіт! Я бот для тренувань 💪\n\n"
        "Що я вмію:\n"
        "• /newday — додати день тренування (наприклад «Ноги», «Спина+біцепс»)\n"
        "• /addex — додати вправу до дня\n"
        "• /plan — показати весь план тренувань\n"
        "• /log — записати підхід (вага x повтори) під час тренування\n"
        "• /today — що вже записано сьогодні\n"
        "• /history — прогрес по конкретній вправі\n\n"
        "Почни з /newday, щоб створити перший день плану!"
    )
    await update.message.reply_text(text)


async def plan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    days = get_days(user_id)
    if not days:
        await update.message.reply_text(
            "У тебе ще немає плану. Створи день командою /newday"
        )
        return

    lines = ["📋 Твій план тренувань:\n"]
    for d in days:
        lines.append(f"🗓 {d['name']}")
        exs = get_exercises_for_day(d["id"])
        if not exs:
            lines.append("   (вправ поки немає — /addex)")
        for i, e in enumerate(exs, 1):
            lines.append(f"   {i}. {e['name']}")
        lines.append("")
    await update.message.reply_text("\n".join(lines))


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = today_logs(user_id)
    if not rows:
        await update.message.reply_text("Сьогодні ще немає записаних підходів. Використай /log")
        return
    by_ex = {}
    for r in rows:
        by_ex.setdefault(r["exercise_name"], []).append(r)
    lines = ["📅 Сьогоднішнє тренування:\n"]
    for ex, sets in by_ex.items():
        lines.append(f"💪 {ex}")
        for s in sets:
            lines.append(f"   Підхід {s['set_number']}: {s['weight']} кг x {s['reps']} повт.")
        lines.append("")
    await update.message.reply_text("\n".join(lines))


# ---------- /newday ----------
async def newday_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Як назвати новий день тренування? (наприклад: Ноги, Груди+трицепс)\n"
        "Або /cancel щоб скасувати."
    )
    return NEWDAY_NAME


async def newday_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    user_id = update.effective_user.id
    conn = get_conn()
    pos = conn.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 as p FROM days WHERE user_id = ?", (user_id,)
    ).fetchone()["p"]
    conn.execute(
        "INSERT INTO days (user_id, name, position) VALUES (?, ?, ?)", (user_id, name, pos)
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(
        f"Додано день «{name}» ✅\nТепер додай вправи командою /addex"
    )
    return ConversationHandler.END


# ---------- /addex ----------
async def addex_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    days = get_days(user_id)
    if not days:
        await update.message.reply_text("Спочатку створи день тренування: /newday")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton(d["name"], callback_data=f"day_{d['id']}")] for d in days
    ]
    await update.message.reply_text(
        "До якого дня додати вправу?", reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ADDEX_DAY


async def addex_day_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    day_id = int(query.data.split("_")[1])
    context.user_data["addex_day_id"] = day_id
    await query.edit_message_text("Введи назву вправи (наприклад: Жим лежачи)")
    return ADDEX_NAME


async def addex_name_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    day_id = context.user_data.get("addex_day_id")
    conn = get_conn()
    pos = conn.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 as p FROM exercises WHERE day_id = ?", (day_id,)
    ).fetchone()["p"]
    conn.execute(
        "INSERT INTO exercises (day_id, name, position) VALUES (?, ?, ?)", (day_id, name, pos)
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(f"Додано вправу «{name}» ✅\nМожеш додати ще: /addex")
    return ConversationHandler.END


# ---------- /log ----------
async def log_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    names = get_all_exercise_names(user_id)
    if not names:
        await update.message.reply_text(
            "У тебе ще немає вправ у плані. Додай через /newday і /addex, "
            "або просто напиши назву вправи зараз:"
        )
        return LOG_EXERCISE

    keyboard = [
        [InlineKeyboardButton(n, callback_data=f"ex_{n}")] for n in names
    ]
    await update.message.reply_text(
        "Яку вправу записуємо?", reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return LOG_EXERCISE


async def log_exercise_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        ex_name = query.data.split("_", 1)[1]
        context.user_data["log_exercise"] = ex_name
        await query.edit_message_text(f"Вправа: {ex_name}\nВведи вагу (кг), наприклад: 60")
    else:
        ex_name = update.message.text.strip()
        context.user_data["log_exercise"] = ex_name
        await update.message.reply_text(f"Вправа: {ex_name}\nВведи вагу (кг), наприклад: 60")
    return LOG_WEIGHT


async def log_weight_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        weight = float(update.message.text.replace(",", ".").strip())
    except ValueError:
        await update.message.reply_text("Це не схоже на число. Введи вагу ще раз, наприклад: 60")
        return LOG_WEIGHT
    context.user_data["log_weight"] = weight
    await update.message.reply_text("Скільки повторів?")
    return LOG_REPS


async def log_reps_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        reps = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Введи кількість повторів цілим числом, наприклад: 10")
        return LOG_REPS

    user_id = update.effective_user.id
    ex_name = context.user_data["log_exercise"]
    weight = context.user_data["log_weight"]
    set_number = add_log(user_id, ex_name, weight, reps)

    keyboard = [
        [InlineKeyboardButton("➕ Ще підхід цієї ж вправи", callback_data="more_same")],
        [InlineKeyboardButton("🔁 Інша вправа", callback_data="more_other")],
        [InlineKeyboardButton("✅ Завершити", callback_data="more_done")],
    ]
    await update.message.reply_text(
        f"Записано: {ex_name}, підхід {set_number} — {weight} кг x {reps} повт. ✅",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return LOG_MORE


async def log_more_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data

    if choice == "more_same":
        ex_name = context.user_data["log_exercise"]
        await query.edit_message_text(f"Вправа: {ex_name}\nВведи вагу (кг):")
        return LOG_WEIGHT
    elif choice == "more_other":
        user_id = update.effective_user.id
        names = get_all_exercise_names(user_id)
        keyboard = [[InlineKeyboardButton(n, callback_data=f"ex_{n}")] for n in names]
        await query.edit_message_text(
            "Яку вправу записуємо?", reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return LOG_EXERCISE
    else:
        await query.edit_message_text("Тренування записано. Гарного відновлення! 💪\nПодивитись: /today")
        return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Скасовано.")
    return ConversationHandler.END


# ---------- /history ----------
async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if not args:
        names = get_all_exercise_names(user_id)
        if not names:
            await update.message.reply_text("У тебе ще немає жодного запису.")
            return
        keyboard = [[InlineKeyboardButton(n, callback_data=f"hist_{n}")] for n in names]
        await update.message.reply_text(
            "Прогрес по якій вправі показати?", reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    ex_name = " ".join(args)
    await send_history(update.message, user_id, ex_name)


async def history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ex_name = query.data.split("_", 1)[1]
    await send_history(query.message, update.effective_user.id, ex_name, edit=query)


async def send_history(message, user_id, ex_name, edit=None):
    sessions = best_set_per_session(user_id, ex_name, limit=10)
    if not sessions:
        text = f"Записів по вправі «{ex_name}» ще немає."
    else:
        lines = [f"📈 Прогрес: {ex_name} (найкращий підхід за тренування)\n"]
        for d, w, r in sessions:
            lines.append(f"{d}: {w} кг x {r} повт.")
        text = "\n".join(lines)

    if edit:
        await edit.edit_message_text(text)
    else:
        await message.reply_text(text)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "Не знайдено TELEGRAM_BOT_TOKEN. Встанови змінну середовища з токеном від @BotFather."
        )

    init_db()
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("plan", plan_cmd))
    app.add_handler(CommandHandler("today", today_cmd))

    newday_conv = ConversationHandler(
        entry_points=[CommandHandler("newday", newday_start)],
        states={NEWDAY_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, newday_save)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(newday_conv)

    addex_conv = ConversationHandler(
        entry_points=[CommandHandler("addex", addex_start)],
        states={
            ADDEX_DAY: [CallbackQueryHandler(addex_day_chosen, pattern=r"^day_")],
            ADDEX_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, addex_name_chosen)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(addex_conv)

    log_conv = ConversationHandler(
        entry_points=[CommandHandler("log", log_start)],
        states={
            LOG_EXERCISE: [
                CallbackQueryHandler(log_exercise_chosen, pattern=r"^ex_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, log_exercise_chosen),
            ],
            LOG_WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, log_weight_chosen)],
            LOG_REPS: [MessageHandler(filters.TEXT & ~filters.COMMAND, log_reps_chosen)],
            LOG_MORE: [CallbackQueryHandler(log_more_chosen, pattern=r"^more_")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(log_conv)

    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CallbackQueryHandler(history_callback, pattern=r"^hist_"))

    logger.info("Бот запущено...")
    app.run_polling()


if __name__ == "__main__":
    main()
