
import os
import sqlite3
import logging
from datetime import datetime, date
from io import BytesIO

import matplotlib
matplotlib.use("Agg")  # без графічного дисплея — потрібно для сервера
import matplotlib.pyplot as plt

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
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

DB_DIR = os.environ.get("DB_DIR", os.path.dirname(__file__))
DB_PATH = os.path.join(DB_DIR, "workout.db")

# ---------- Стани для розмов (ConversationHandler) ----------
NEWDAY_NAME = 1
ADDEX_DAY, ADDEX_NAME = range(2, 4)
LOG_EXERCISE, LOG_WEIGHT, LOG_REPS, LOG_MORE = range(4, 8)
EDITEX_DAY, EDITEX_EXERCISE, EDITEX_NAME, EDITEX_ACTION, EDITEX_CONFIRM_DELEX, EDITEX_CONFIRM_DELDAY = range(8, 14)

# ---------- Підписи кнопок нижнього меню (українською) ----------
BTN_LOG = "📝 Записати підхід"
BTN_TODAY = "📅 Сьогодні"
BTN_PLAN = "📋 Мій план"
BTN_HISTORY = "📈 Прогрес"
BTN_RECORDS = "🏆 Рекорди"
BTN_NEWDAY = "🆕 Новий день"
BTN_ADDEX = "➕ Додати вправу"
BTN_EDITEX = "🛠 Керувати вправами"

MENU_LABELS = [BTN_LOG, BTN_TODAY, BTN_PLAN, BTN_HISTORY, BTN_RECORDS, BTN_NEWDAY, BTN_ADDEX, BTN_EDITEX]
MENU_BUTTON_FILTER = filters.Text(MENU_LABELS)

MAIN_MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        [BTN_LOG, BTN_TODAY],
        [BTN_PLAN, BTN_HISTORY, BTN_RECORDS],
        [BTN_NEWDAY, BTN_ADDEX, BTN_EDITEX],
    ],
    resize_keyboard=True,
)

# ---------- Періодизація навантажень ----------
# Формула Еплі для оцінки одноповторного максимуму (1ПМ): 1ПМ = вага × (1 + повтори / 30)
# Відсотки від 1ПМ для легкого/середнього/важкого дня — за рекомендаціями NSCA
# (Essentials of Strength Training and Conditioning, таблиця % 1ПМ до к-ті повторів).
PERIODIZATION_ZONES = [
    ("🟢 Легкий день", 0.60, "12–15"),
    ("🟡 Середній день", 0.75, "8–10"),
    ("🔴 Важкий день", 0.85, "3–5"),
]


def estimate_1rm(weight, reps):
    """Формула Еплі: 1ПМ = вага × (1 + повтори/30)."""
    return round(weight * (1 + reps / 30), 1)


def periodization_suggestions(one_rm):
    return [(label, round(one_rm * pct, 1), reps) for label, pct, reps in PERIODIZATION_ZONES]


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
    cur = conn.execute(
        "INSERT INTO logs (user_id, exercise_name, log_date, set_number, weight, reps, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, exercise_name, log_date, set_number, weight, reps, datetime.now().isoformat()),
    )
    log_id = cur.lastrowid
    conn.commit()
    conn.close()
    return set_number, log_id


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


def rename_exercise(exercise_id, new_name):
    conn = get_conn()
    conn.execute("UPDATE exercises SET name = ? WHERE id = ?", (new_name, exercise_id))
    conn.commit()
    conn.close()


def get_exercise_by_id(exercise_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM exercises WHERE id = ?", (exercise_id,)).fetchone()
    conn.close()
    return row


def delete_exercise(exercise_id):
    conn = get_conn()
    conn.execute("DELETE FROM exercises WHERE id = ?", (exercise_id,))
    conn.commit()
    conn.close()


def get_day_by_id(day_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM days WHERE id = ?", (day_id,)).fetchone()
    conn.close()
    return row


def delete_day(day_id):
    conn = get_conn()
    conn.execute("DELETE FROM exercises WHERE day_id = ?", (day_id,))
    conn.execute("DELETE FROM days WHERE id = ?", (day_id,))
    conn.commit()
    conn.close()


def get_max_weight_log(user_id, exercise_name):
    """Найважчий колись записаний підхід (рекорд) по вправі."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM logs WHERE user_id = ? AND exercise_name = ? "
        "ORDER BY weight DESC, reps DESC, log_date DESC LIMIT 1",
        (user_id, exercise_name),
    ).fetchone()
    conn.close()
    return row


def get_progress_series(user_id, exercise_name, limit=30):
    """Хронологічний ряд найкращих підходів по датах для графіка (від старіших до новіших)."""
    sessions = best_set_per_session(user_id, exercise_name, limit=limit)
    return list(reversed(sessions))


def generate_progress_chart(user_id, exercise_name):
    series = get_progress_series(user_id, exercise_name, limit=30)
    if not series:
        return None
    dates = [s[0] for s in series]
    weights = [s[1] for s in series]

    fig, ax = plt.subplots(figsize=(7, 4))
    ax.plot(dates, weights, marker="o", color="#4CAF50", linewidth=2)
    ax.set_title(f"Прогрес: {exercise_name}")
    ax.set_ylabel("Вага, кг")
    ax.grid(True, alpha=0.3)
    ax.tick_params(axis="x", rotation=45)
    fig.tight_layout()

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf


def get_log_by_id(log_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM logs WHERE id = ?", (log_id,)).fetchone()
    conn.close()
    return row


def update_log(log_id, weight, reps):
    conn = get_conn()
    conn.execute("UPDATE logs SET weight = ?, reps = ? WHERE id = ?", (weight, reps, log_id))
    conn.commit()
    conn.close()


# ---------- Команди ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Привіт! Я бот для тренувань 💪\n\n"
        "Знизу з'явилось меню кнопок — можеш користуватись ним замість вводу команд.\n\n"
        "Що я вмію:\n"
        f"• {BTN_NEWDAY} — додати день тренування (наприклад «Ноги», «Спина+біцепс»)\n"
        f"• {BTN_ADDEX} — додати вправу до дня\n"
        f"• {BTN_EDITEX} — перейменувати або видалити вправу/день\n"
        f"• {BTN_PLAN} — показати весь план тренувань\n"
        f"• {BTN_LOG} — записати підхід (вага x повтори) під час тренування\n"
        f"• {BTN_TODAY} — що вже записано сьогодні\n"
        f"• {BTN_HISTORY} — прогрес по вправі (текстом або графіком)\n"
        f"• {BTN_RECORDS} — твій рекорд по вправі, розрахунковий 1ПМ і рекомендовані ваги "
        "для легкого/середнього/важкого тренування\n\n"
        f"Почни з «{BTN_NEWDAY}», щоб створити перший день плану!"
    )
    await update.message.reply_text(text, reply_markup=MAIN_MENU_KEYBOARD)


async def plan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    days = get_days(user_id)
    if not days:
        await update.message.reply_text(
            f"У тебе ще немає плану. Створи день командою «{BTN_NEWDAY}»",
            reply_markup=MAIN_MENU_KEYBOARD,
        )
        return

    lines = ["📋 Твій план тренувань:\n"]
    for d in days:
        lines.append(f"🗓 {d['name']}")
        exs = get_exercises_for_day(d["id"])
        if not exs:
            lines.append(f"   (вправ поки немає — {BTN_ADDEX})")
        for i, e in enumerate(exs, 1):
            lines.append(f"   {i}. {e['name']}")
        lines.append("")
    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_MENU_KEYBOARD)


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = today_logs(user_id)
    if not rows:
        await update.message.reply_text(
            f"Сьогодні ще немає записаних підходів. Використай «{BTN_LOG}»",
            reply_markup=MAIN_MENU_KEYBOARD,
        )
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
    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_MENU_KEYBOARD)


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
        f"Додано день «{name}» ✅\nТепер додай вправи командою «{BTN_ADDEX}»",
        reply_markup=MAIN_MENU_KEYBOARD,
    )
    return ConversationHandler.END


# ---------- /addex ----------
async def addex_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    days = get_days(user_id)
    if not days:
        await update.message.reply_text(f"Спочатку створи день тренування: {BTN_NEWDAY}")
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
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_addex_day")]])
    await query.edit_message_text(
        "Введи назву вправи (наприклад: Жим лежачи)", reply_markup=keyboard
    )
    return ADDEX_NAME


async def addex_back_to_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    days = get_days(user_id)
    keyboard = [[InlineKeyboardButton(d["name"], callback_data=f"day_{d['id']}")] for d in days]
    await query.edit_message_text(
        "До якого дня додати вправу?", reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ADDEX_DAY


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
    await update.message.reply_text(
        f"Додано вправу «{name}» ✅\nМожеш додати ще: {BTN_ADDEX}", reply_markup=MAIN_MENU_KEYBOARD
    )
    return ConversationHandler.END


# ---------- /editex (перейменувати або видалити вправу/день) ----------
def build_day_list_keyboard(days):
    keyboard = []
    for d in days:
        keyboard.append(
            [
                InlineKeyboardButton(d["name"], callback_data=f"eday_{d['id']}"),
                InlineKeyboardButton("🗑 Видалити день", callback_data=f"delday_{d['id']}"),
            ]
        )
    return InlineKeyboardMarkup(keyboard)


def build_exercise_list_keyboard(exs):
    keyboard = [[InlineKeyboardButton(e["name"], callback_data=f"eex_{e['id']}")] for e in exs]
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="editex_back_to_day")])
    return InlineKeyboardMarkup(keyboard)


async def editex_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    days = get_days(user_id)
    if not days:
        await update.message.reply_text(f"Спочатку створи день тренування: {BTN_NEWDAY}")
        return ConversationHandler.END

    await update.message.reply_text(
        "З яким днем працюємо?", reply_markup=build_day_list_keyboard(days)
    )
    return EDITEX_DAY


async def editex_day_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    day_id = int(query.data.split("_", 1)[1])
    context.user_data["editex_day_id"] = day_id
    exs = get_exercises_for_day(day_id)
    if not exs:
        await query.edit_message_text(f"У цьому дні ще немає вправ. Додай через {BTN_ADDEX}")
        return ConversationHandler.END

    await query.edit_message_text("Яку вправу редагувати?", reply_markup=build_exercise_list_keyboard(exs))
    return EDITEX_EXERCISE


async def editex_delday_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    day_id = int(query.data.split("_", 1)[1])
    context.user_data["editex_day_id"] = day_id
    day = get_day_by_id(day_id)
    exs = get_exercises_for_day(day_id)
    day_name = day["name"] if day else ""
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Так, видалити", callback_data="delday_yes")],
            [InlineKeyboardButton("❌ Скасувати", callback_data="delday_no")],
        ]
    )
    await query.edit_message_text(
        f"Точно видалити день «{day_name}» разом з {len(exs)} вправами?\n"
        "(Історія вже записаних підходів залишиться, видаляється лише план.)",
        reply_markup=keyboard,
    )
    return EDITEX_CONFIRM_DELDAY


async def editex_delday_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "delday_yes":
        day_id = context.user_data.get("editex_day_id")
        delete_day(day_id)
        await query.edit_message_text("День видалено ✅")
        return ConversationHandler.END
    else:
        user_id = update.effective_user.id
        days = get_days(user_id)
        await query.edit_message_text("Скасовано.\nЗ яким днем працюємо?", reply_markup=build_day_list_keyboard(days))
        return EDITEX_DAY


async def editex_back_to_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    days = get_days(user_id)
    await query.edit_message_text("З яким днем працюємо?", reply_markup=build_day_list_keyboard(days))
    return EDITEX_DAY


async def editex_exercise_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ex_id = int(query.data.split("_", 1)[1])
    context.user_data["editex_id"] = ex_id
    ex = get_exercise_by_id(ex_id)
    old_name = ex["name"] if ex else ""
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✏️ Перейменувати", callback_data="action_rename")],
            [InlineKeyboardButton("🗑 Видалити", callback_data="action_delete")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="editex_back_to_exercise")],
        ]
    )
    await query.edit_message_text(f"Вправа: «{old_name}»\nЩо зробити?", reply_markup=keyboard)
    return EDITEX_ACTION


async def editex_back_to_exercise(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    day_id = context.user_data.get("editex_day_id")
    exs = get_exercises_for_day(day_id)
    await query.edit_message_text("Яку вправу редагувати?", reply_markup=build_exercise_list_keyboard(exs))
    return EDITEX_EXERCISE


async def editex_action_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ex = get_exercise_by_id(context.user_data.get("editex_id"))
    old_name = ex["name"] if ex else ""

    if query.data == "action_rename":
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Назад", callback_data="editex_back_to_action")]]
        )
        await query.edit_message_text(
            f"Поточна назва: «{old_name}»\nВведи нову назву вправи:", reply_markup=keyboard
        )
        return EDITEX_NAME
    else:  # action_delete
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Так, видалити", callback_data="delex_yes")],
                [InlineKeyboardButton("❌ Скасувати", callback_data="delex_no")],
            ]
        )
        await query.edit_message_text(f"Точно видалити вправу «{old_name}»?", reply_markup=keyboard)
        return EDITEX_CONFIRM_DELEX


async def editex_back_to_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ex = get_exercise_by_id(context.user_data.get("editex_id"))
    old_name = ex["name"] if ex else ""
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✏️ Перейменувати", callback_data="action_rename")],
            [InlineKeyboardButton("🗑 Видалити", callback_data="action_delete")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="editex_back_to_exercise")],
        ]
    )
    await query.edit_message_text(f"Вправа: «{old_name}»\nЩо зробити?", reply_markup=keyboard)
    return EDITEX_ACTION


async def editex_delex_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "delex_yes":
        ex_id = context.user_data.get("editex_id")
        delete_exercise(ex_id)
        await query.edit_message_text("Вправу видалено ✅")
        return ConversationHandler.END
    else:
        return await editex_back_to_action(update, context)


async def editex_name_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text.strip()
    ex_id = context.user_data.get("editex_id")
    rename_exercise(ex_id, new_name)
    await update.message.reply_text(
        f"Вправу оновлено на «{new_name}» ✅\nПеревір: {BTN_PLAN}", reply_markup=MAIN_MENU_KEYBOARD
    )
    return ConversationHandler.END


# ---------- /log ----------
async def log_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    context.user_data["editing_last"] = False
    names = get_all_exercise_names(user_id)
    if not names:
        await update.message.reply_text(
            f"У тебе ще немає вправ у плані. Додай через {BTN_NEWDAY} і {BTN_ADDEX}, "
            "або просто напиши назву вправи зараз:"
        )
        return LOG_EXERCISE

    context.user_data["log_names_list"] = names
    keyboard = [
        [InlineKeyboardButton(n, callback_data=f"exi_{i}")] for i, n in enumerate(names)
    ]
    await update.message.reply_text(
        "Яку вправу записуємо?", reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return LOG_EXERCISE


def weight_prompt_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_exercise")]])


def reps_prompt_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_weight")]])


async def log_exercise_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        idx = int(query.data.split("_", 1)[1])
        names = context.user_data.get("log_names_list", [])
        if idx >= len(names):
            await query.edit_message_text(f"Список застарів, спробуй {BTN_LOG} ще раз.")
            return ConversationHandler.END
        ex_name = names[idx]
        context.user_data["log_exercise"] = ex_name
        await query.edit_message_text(
            f"Вправа: {ex_name}\nВведи вагу (кг), наприклад: 60",
            reply_markup=weight_prompt_keyboard(),
        )
    else:
        ex_name = update.message.text.strip()
        context.user_data["log_exercise"] = ex_name
        await update.message.reply_text(
            f"Вправа: {ex_name}\nВведи вагу (кг), наприклад: 60",
            reply_markup=weight_prompt_keyboard(),
        )
    return LOG_WEIGHT


async def back_to_exercise(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if context.user_data.get("editing_last"):
        # Під час виправлення вже записаного підходу "Назад" повертає до меню підходу,
        # а не до списку вправ (вправа для виправлення вже зафіксована).
        context.user_data["editing_last"] = False
        log_id = context.user_data.get("log_last_id")
        ex_name = context.user_data.get("log_exercise", "")
        set_number = context.user_data.get("log_last_set_number", "")
        log_row = get_log_by_id(log_id) if log_id else None
        if log_row:
            text = (
                f"Записано: {ex_name}, підхід {set_number} — "
                f"{log_row['weight']} кг x {log_row['reps']} повт. ✅"
            )
        else:
            text = "Гаразд, залишаємо як було."
        await query.edit_message_text(text, reply_markup=log_more_keyboard())
        return LOG_MORE

    user_id = update.effective_user.id
    names = get_all_exercise_names(user_id)
    context.user_data["log_names_list"] = names
    keyboard = [[InlineKeyboardButton(n, callback_data=f"exi_{i}")] for i, n in enumerate(names)]
    await query.edit_message_text(
        "Яку вправу записуємо?", reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return LOG_EXERCISE


async def log_weight_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        weight = float(update.message.text.replace(",", ".").strip())
    except ValueError:
        await update.message.reply_text(
            "Це не схоже на число. Введи вагу ще раз, наприклад: 60",
            reply_markup=weight_prompt_keyboard(),
        )
        return LOG_WEIGHT
    context.user_data["log_weight"] = weight
    await update.message.reply_text("Скільки повторів?", reply_markup=reps_prompt_keyboard())
    return LOG_REPS


async def back_to_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ex_name = context.user_data.get("log_exercise", "")
    await query.edit_message_text(
        f"Вправа: {ex_name}\nВведи вагу (кг), наприклад: 60",
        reply_markup=weight_prompt_keyboard(),
    )
    return LOG_WEIGHT


def log_more_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Ще підхід цієї ж вправи", callback_data="more_same")],
            [InlineKeyboardButton("✏️ Виправити цей підхід", callback_data="more_edit")],
            [InlineKeyboardButton("🔁 Інша вправа", callback_data="more_other")],
            [InlineKeyboardButton("✅ Завершити", callback_data="more_done")],
        ]
    )


async def log_reps_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        reps = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text(
            "Введи кількість повторів цілим числом, наприклад: 10",
            reply_markup=reps_prompt_keyboard(),
        )
        return LOG_REPS

    user_id = update.effective_user.id
    ex_name = context.user_data["log_exercise"]
    weight = context.user_data["log_weight"]

    if context.user_data.get("editing_last"):
        log_id = context.user_data.get("log_last_id")
        update_log(log_id, weight, reps)
        set_number = context.user_data.get("log_last_set_number", "")
        context.user_data["editing_last"] = False
        confirm_text = f"Виправлено: {ex_name}, підхід {set_number} — {weight} кг x {reps} повт. ✅"
    else:
        prev_best = get_max_weight_log(user_id, ex_name)
        is_new_record = (not prev_best) or weight > prev_best["weight"] or (
            weight == prev_best["weight"] and reps > prev_best["reps"]
        )
        set_number, log_id = add_log(user_id, ex_name, weight, reps)
        context.user_data["log_last_id"] = log_id
        context.user_data["log_last_set_number"] = set_number
        confirm_text = f"Записано: {ex_name}, підхід {set_number} — {weight} кг x {reps} повт. ✅"
        if is_new_record:
            confirm_text += f"\n🎉 Новий рекорд ваги для цієї вправи! (1ПМ ≈ {estimate_1rm(weight, reps)} кг)"

    await update.message.reply_text(confirm_text, reply_markup=log_more_keyboard())
    return LOG_MORE


async def log_more_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data

    if choice == "more_same":
        context.user_data["editing_last"] = False
        ex_name = context.user_data["log_exercise"]
        await query.edit_message_text(
            f"Вправа: {ex_name}\nВведи вагу (кг):", reply_markup=weight_prompt_keyboard()
        )
        return LOG_WEIGHT
    elif choice == "more_edit":
        log_id = context.user_data.get("log_last_id")
        if not log_id:
            await query.edit_message_text("Немає підходу для виправлення.")
            return ConversationHandler.END
        context.user_data["editing_last"] = True
        ex_name = context.user_data.get("log_exercise", "")
        await query.edit_message_text(
            f"Виправляємо: {ex_name}\nВведи нову вагу (кг):", reply_markup=weight_prompt_keyboard()
        )
        return LOG_WEIGHT
    elif choice == "more_other":
        context.user_data["editing_last"] = False
        user_id = update.effective_user.id
        names = get_all_exercise_names(user_id)
        context.user_data["log_names_list"] = names
        keyboard = [
            [InlineKeyboardButton(n, callback_data=f"exi_{i}")] for i, n in enumerate(names)
        ]
        await query.edit_message_text(
            "Яку вправу записуємо?", reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return LOG_EXERCISE
    else:
        await query.edit_message_text(f"Тренування записано. Гарного відновлення! 💪\nПодивитись: {BTN_TODAY}")
        return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Скасовано.", reply_markup=MAIN_MENU_KEYBOARD)
    return ConversationHandler.END


async def conversation_timed_out(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Викликається автоматично через CONVERSATION_TIMEOUT бездіяльності.
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "Попередня незавершена дія була автоматично скасована через бездіяльність. "
            "Можеш спробувати команду ще раз."
        )
    return ConversationHandler.END


async def restart_on_other_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Спрацьовує, якщо користувач надсилає іншу команду/кнопку посеред незавершеного сценарію.
    await update.message.reply_text(
        "Попередню незавершену дію скасовано. Спробуй ще раз.",
        reply_markup=MAIN_MENU_KEYBOARD,
    )
    return ConversationHandler.END


CONVERSATION_TIMEOUT = 300  # 5 хвилин бездіяльності — автоматичне скасування


# ---------- /history (текст + кнопка графіка) ----------
async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if not args:
        names = get_all_exercise_names(user_id)
        if not names:
            await update.message.reply_text(
                "У тебе ще немає жодного запису.", reply_markup=MAIN_MENU_KEYBOARD
            )
            return
        context.user_data["hist_names_list"] = names
        keyboard = [
            [InlineKeyboardButton(n, callback_data=f"histi_{i}")] for i, n in enumerate(names)
        ]
        await update.message.reply_text(
            "Прогрес по якій вправі показати?", reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    ex_name = " ".join(args)
    await send_history(update.message, user_id, ex_name, context)


async def history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split("_", 1)[1])
    names = context.user_data.get("hist_names_list", [])
    if idx >= len(names):
        await query.edit_message_text(f"Список застарів, спробуй {BTN_HISTORY} ще раз.")
        return
    ex_name = names[idx]
    await send_history(query.message, update.effective_user.id, ex_name, context, edit=query)


async def send_history(message, user_id, ex_name, context, edit=None):
    sessions = best_set_per_session(user_id, ex_name, limit=10)
    if not sessions:
        text = f"Записів по вправі «{ex_name}» ще немає."
        keyboard = None
    else:
        lines = [f"📈 Прогрес: {ex_name} (найкращий підхід за тренування)\n"]
        for d, w, r in sessions:
            lines.append(f"{d}: {w} кг x {r} повт.")
        text = "\n".join(lines)
        context.user_data["chart_exercise"] = ex_name
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("📊 Показати графіком", callback_data="show_chart")]]
        )

    if edit:
        await edit.edit_message_text(text, reply_markup=keyboard)
    else:
        await message.reply_text(text, reply_markup=keyboard if keyboard else MAIN_MENU_KEYBOARD)


async def show_chart_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ex_name = context.user_data.get("chart_exercise")
    user_id = update.effective_user.id
    if not ex_name:
        await query.message.reply_text("Немає даних для графіка.")
        return
    buf = generate_progress_chart(user_id, ex_name)
    if not buf:
        await query.message.reply_text("Недостатньо даних для графіка.")
        return
    await context.bot.send_photo(
        chat_id=query.message.chat_id, photo=buf, caption=f"📊 Прогрес: {ex_name}"
    )


# ---------- 🏆 Рекорди + періодизація навантажень ----------
async def records_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    names = get_all_exercise_names(user_id)
    if not names:
        await update.message.reply_text(
            "У тебе ще немає жодного запису.", reply_markup=MAIN_MENU_KEYBOARD
        )
        return
    context.user_data["pr_names_list"] = names
    keyboard = [[InlineKeyboardButton(n, callback_data=f"pri_{i}")] for i, n in enumerate(names)]
    await update.message.reply_text(
        "Рекорд по якій вправі показати?", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def records_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split("_", 1)[1])
    names = context.user_data.get("pr_names_list", [])
    if idx >= len(names):
        await query.edit_message_text(f"Список застарів, спробуй {BTN_RECORDS} ще раз.")
        return
    ex_name = names[idx]
    await send_record(query, update.effective_user.id, ex_name)


async def send_record(query, user_id, ex_name):
    row = get_max_weight_log(user_id, ex_name)
    if not row:
        await query.edit_message_text(f"Записів по вправі «{ex_name}» ще немає.")
        return

    one_rm = estimate_1rm(row["weight"], row["reps"])
    zones = periodization_suggestions(one_rm)

    lines = [
        f"🏆 Рекорд: {ex_name}",
        f"{row['weight']} кг x {row['reps']} повт. ({row['log_date']})",
        f"💪 Розрахунковий 1ПМ (формула Еплі): {one_rm} кг",
        "",
        "🎯 Рекомендовані ваги для періодизації навантажень (% від 1ПМ, за NSCA):",
    ]
    for label, w, reps in zones:
        lines.append(f"{label}: {w} кг x {reps} повт.")

    await query.edit_message_text("\n".join(lines))


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
    app.add_handler(MessageHandler(filters.Text([BTN_PLAN]), plan_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(MessageHandler(filters.Text([BTN_TODAY]), today_cmd))

    other_commands_fallback = MessageHandler(filters.COMMAND, restart_on_other_command)
    menu_button_fallback = MessageHandler(MENU_BUTTON_FILTER, restart_on_other_command)
    free_text_filter = filters.TEXT & ~filters.COMMAND & ~MENU_BUTTON_FILTER

    timeout_handler = MessageHandler(filters.ALL, conversation_timed_out)

    newday_conv = ConversationHandler(
        entry_points=[
            CommandHandler("newday", newday_start),
            MessageHandler(filters.Text([BTN_NEWDAY]), newday_start),
        ],
        states={
            NEWDAY_NAME: [MessageHandler(free_text_filter, newday_save)],
            ConversationHandler.TIMEOUT: [timeout_handler],
        },
        fallbacks=[CommandHandler("cancel", cancel), other_commands_fallback, menu_button_fallback],
        conversation_timeout=CONVERSATION_TIMEOUT,
    )
    app.add_handler(newday_conv)

    addex_conv = ConversationHandler(
        entry_points=[
            CommandHandler("addex", addex_start),
            MessageHandler(filters.Text([BTN_ADDEX]), addex_start),
        ],
        states={
            ADDEX_DAY: [CallbackQueryHandler(addex_day_chosen, pattern=r"^day_")],
            ADDEX_NAME: [
                CallbackQueryHandler(addex_back_to_day, pattern=r"^back_to_addex_day$"),
                MessageHandler(free_text_filter, addex_name_chosen),
            ],
            ConversationHandler.TIMEOUT: [timeout_handler],
        },
        fallbacks=[CommandHandler("cancel", cancel), other_commands_fallback, menu_button_fallback],
        conversation_timeout=CONVERSATION_TIMEOUT,
    )
    app.add_handler(addex_conv)

    editex_conv = ConversationHandler(
        entry_points=[
            CommandHandler("editex", editex_start),
            MessageHandler(filters.Text([BTN_EDITEX]), editex_start),
        ],
        states={
            EDITEX_DAY: [
                CallbackQueryHandler(editex_delday_ask, pattern=r"^delday_\d+$"),
                CallbackQueryHandler(editex_day_chosen, pattern=r"^eday_"),
            ],
            EDITEX_EXERCISE: [
                CallbackQueryHandler(editex_back_to_day, pattern=r"^editex_back_to_day$"),
                CallbackQueryHandler(editex_exercise_chosen, pattern=r"^eex_"),
            ],
            EDITEX_ACTION: [
                CallbackQueryHandler(editex_back_to_exercise, pattern=r"^editex_back_to_exercise$"),
                CallbackQueryHandler(editex_action_chosen, pattern=r"^action_"),
            ],
            EDITEX_NAME: [
                CallbackQueryHandler(editex_back_to_action, pattern=r"^editex_back_to_action$"),
                MessageHandler(free_text_filter, editex_name_chosen),
            ],
            EDITEX_CONFIRM_DELEX: [
                CallbackQueryHandler(editex_delex_confirmed, pattern=r"^delex_(yes|no)$"),
            ],
            EDITEX_CONFIRM_DELDAY: [
                CallbackQueryHandler(editex_delday_confirmed, pattern=r"^delday_(yes|no)$"),
            ],
            ConversationHandler.TIMEOUT: [timeout_handler],
        },
        fallbacks=[CommandHandler("cancel", cancel), other_commands_fallback, menu_button_fallback],
        conversation_timeout=CONVERSATION_TIMEOUT,
    )
    app.add_handler(editex_conv)

    log_conv = ConversationHandler(
        entry_points=[
            CommandHandler("log", log_start),
            MessageHandler(filters.Text([BTN_LOG]), log_start),
        ],
        states={
            LOG_EXERCISE: [
                CallbackQueryHandler(log_exercise_chosen, pattern=r"^exi_"),
                MessageHandler(free_text_filter, log_exercise_chosen),
            ],
            LOG_WEIGHT: [
                CallbackQueryHandler(back_to_exercise, pattern=r"^back_to_exercise$"),
                MessageHandler(free_text_filter, log_weight_chosen),
            ],
            LOG_REPS: [
                CallbackQueryHandler(back_to_weight, pattern=r"^back_to_weight$"),
                MessageHandler(free_text_filter, log_reps_chosen),
            ],
            LOG_MORE: [CallbackQueryHandler(log_more_chosen, pattern=r"^more_")],
            ConversationHandler.TIMEOUT: [timeout_handler],
        },
        fallbacks=[CommandHandler("cancel", cancel), other_commands_fallback, menu_button_fallback],
        conversation_timeout=CONVERSATION_TIMEOUT,
    )
    app.add_handler(log_conv)

    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(MessageHandler(filters.Text([BTN_HISTORY]), history_cmd))
    app.add_handler(CallbackQueryHandler(history_callback, pattern=r"^histi_"))
    app.add_handler(CallbackQueryHandler(show_chart_callback, pattern=r"^show_chart$"))

    app.add_handler(CommandHandler("records", records_cmd))
    app.add_handler(MessageHandler(filters.Text([BTN_RECORDS]), records_cmd))
    app.add_handler(CallbackQueryHandler(records_callback, pattern=r"^pri_"))

    logger.info("Бот запущено...")
    app.run_polling()


if __name__ == "__main__":
    main()


