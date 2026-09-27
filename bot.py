import os
import sqlite3
import logging
from datetime import datetime, date, timedelta, time as dt_time
from io import BytesIO

import matplotlib
matplotlib.use("Agg")  # без графічного дисплея — потрібно для сервера
import matplotlib.pyplot as plt

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, LabeledPrice
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    PreCheckoutQueryHandler,
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
EDITEX_DAY_ACTION, EDITEX_DAY_NAME = range(14, 16)
BODYWEIGHT_INPUT = 16
LOG_NOTE = 17
LOG_DAY = 18

# ---------- Підписи кнопок нижнього меню (українською) ----------
BTN_LOG = "📝 Записати підхід"
BTN_TODAY = "📅 Сьогодні"
BTN_PLAN = "📋 Мій план"
BTN_HISTORY = "📈 Прогрес"
BTN_RECORDS = "🏆 Рекорди"
BTN_WEEKLY = "📊 Тижневий звіт"
BTN_BODYWEIGHT = "⚖️ Моя вага"
BTN_EXPORT = "📤 Експорт CSV"
BTN_NEWDAY = "🆕 Новий день"
BTN_ADDEX = "➕ Додати вправу"
BTN_EDITEX = "🛠 Керувати вправами"
BTN_DONATE = "☕ Подякувати автору"

MENU_LABELS = [
    BTN_LOG, BTN_TODAY, BTN_PLAN, BTN_HISTORY, BTN_RECORDS, BTN_WEEKLY,
    BTN_BODYWEIGHT, BTN_EXPORT, BTN_NEWDAY, BTN_ADDEX, BTN_EDITEX, BTN_DONATE,
]
MENU_BUTTON_FILTER = filters.Text(MENU_LABELS)

MAIN_MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        [BTN_LOG, BTN_TODAY],
        [BTN_PLAN, BTN_HISTORY, BTN_RECORDS],
        [BTN_WEEKLY, BTN_BODYWEIGHT],
        [BTN_NEWDAY, BTN_ADDEX, BTN_EDITEX],
        [BTN_EXPORT, BTN_DONATE],
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


PLATE_STEP = 2.5  # практичний крок диска в залі (кг)


def round_to_plate(value, step=PLATE_STEP):
    """Округлює до найближчого практичного кроку (напр. 2.5 кг) — щоб не було 13.2, 27.4 і т.п."""
    return round(round(value / step) * step, 2)


def fmt_kg(value):
    """Прибирає зайве .0 при виводі: 60.0 -> '60', 62.5 -> '62.5'."""
    value = round(float(value), 2)
    if value == int(value):
        return str(int(value))
    return f"{value:g}"


def estimate_1rm(weight, reps):
    """Формула Еплі: 1ПМ = вага × (1 + повтори/30), округлено до цілого кг."""
    return round(weight * (1 + reps / 30))


def periodization_suggestions(one_rm):
    return [(label, round_to_plate(one_rm * pct), reps) for label, pct, reps in PERIODIZATION_ZONES]


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

        CREATE TABLE IF NOT EXISTS bot_users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS body_weight (
            user_id INTEGER NOT NULL,
            log_date TEXT NOT NULL,
            weight REAL NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, log_date)
        );

        CREATE TABLE IF NOT EXISTS workout_notes (
            user_id INTEGER NOT NULL,
            log_date TEXT NOT NULL,
            note TEXT NOT NULL,
            UNIQUE(user_id, log_date)
        );
        """
    )
    conn.commit()
    try:
        conn.execute("ALTER TABLE bot_users ADD COLUMN last_reminder_sent TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # колонка вже існує
    conn.close()


def track_user(user_id, username):
    now = datetime.now().isoformat()
    conn = get_conn()
    conn.execute(
        "INSERT INTO bot_users (user_id, username, first_seen, last_seen) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET last_seen = excluded.last_seen, username = excluded.username",
        (user_id, username, now, now),
    )
    conn.commit()
    conn.close()


def get_user_stats():
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) as c FROM bot_users").fetchone()["c"]
    week_ago = (date.today() - timedelta(days=6)).isoformat()
    active_7d = conn.execute(
        "SELECT COUNT(DISTINCT user_id) as c FROM logs WHERE log_date >= ?", (week_ago,)
    ).fetchone()["c"]
    total_workouts = conn.execute("SELECT COUNT(DISTINCT user_id || log_date) as c FROM logs").fetchone()["c"]
    conn.close()
    return {"total_users": total, "active_7d": active_7d, "total_workouts": total_workouts}


def get_all_user_ids():
    conn = get_conn()
    rows = conn.execute("SELECT user_id FROM bot_users").fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def get_users_needing_reminder(threshold_days=3):
    """user_id тих, хто не тренувався threshold_days+ днів і кому давно не нагадували."""
    conn = get_conn()
    rows = conn.execute("SELECT user_id, last_reminder_sent FROM bot_users").fetchall()
    conn.close()

    today = date.today()
    result = []
    for r in rows:
        conn2 = get_conn()
        last_log = conn2.execute(
            "SELECT MAX(log_date) as d FROM logs WHERE user_id = ?", (r["user_id"],)
        ).fetchone()
        conn2.close()
        if not last_log["d"]:
            continue  # ще жодного разу не тренувався — не спамимо
        last_date = date.fromisoformat(last_log["d"])
        gap = (today - last_date).days
        if gap < threshold_days:
            continue
        if r["last_reminder_sent"]:
            last_reminded = date.fromisoformat(r["last_reminder_sent"])
            if (today - last_reminded).days < threshold_days:
                continue  # вже нагадували нещодавно
        result.append(r["user_id"])
    return result


def mark_reminder_sent(user_id):
    conn = get_conn()
    conn.execute(
        "UPDATE bot_users SET last_reminder_sent = ? WHERE user_id = ?",
        (date.today().isoformat(), user_id),
    )
    conn.commit()
    conn.close()


def add_or_update_bodyweight(user_id, weight):
    log_date = date.today().isoformat()
    conn = get_conn()
    conn.execute(
        "INSERT INTO body_weight (user_id, log_date, weight, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id, log_date) DO UPDATE SET weight = excluded.weight",
        (user_id, log_date, weight, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def get_last_bodyweight(user_id, exclude_today=False):
    conn = get_conn()
    if exclude_today:
        today_str = date.today().isoformat()
        row = conn.execute(
            "SELECT * FROM body_weight WHERE user_id = ? AND log_date != ? ORDER BY log_date DESC LIMIT 1",
            (user_id, today_str),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM body_weight WHERE user_id = ? ORDER BY log_date DESC LIMIT 1", (user_id,)
        ).fetchone()
    conn.close()
    return row


def get_bodyweight_history(user_id, limit=30):
    conn = get_conn()
    rows = conn.execute(
        "SELECT log_date, weight FROM body_weight WHERE user_id = ? ORDER BY log_date DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return list(reversed([(r["log_date"], r["weight"]) for r in rows]))


def set_workout_note(user_id, log_date, note):
    conn = get_conn()
    conn.execute(
        "INSERT INTO workout_notes (user_id, log_date, note) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, log_date) DO UPDATE SET note = excluded.note",
        (user_id, log_date, note),
    )
    conn.commit()
    conn.close()


def get_workout_note(user_id, log_date):
    conn = get_conn()
    row = conn.execute(
        "SELECT note FROM workout_notes WHERE user_id = ? AND log_date = ?", (user_id, log_date)
    ).fetchone()
    conn.close()
    return row["note"] if row else None


def build_csv_export(user_id):
    import csv
    import io

    conn = get_conn()
    rows = conn.execute(
        "SELECT log_date, exercise_name, set_number, weight, reps FROM logs "
        "WHERE user_id = ? ORDER BY log_date, exercise_name, set_number",
        (user_id,),
    ).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Дата", "Вправа", "Підхід", "Вага (кг)", "Повтори"])
    for r in rows:
        writer.writerow([r["log_date"], r["exercise_name"], r["set_number"], r["weight"], r["reps"]])
    return output.getvalue()


def generate_achievement_card(title, main_stat, subtitle):
    fig, ax = plt.subplots(figsize=(6, 6))
    fig.patch.set_facecolor("#1e1e2e")
    ax.set_facecolor("#1e1e2e")
    ax.axis("off")
    ax.text(0.5, 0.72, title, ha="center", fontsize=22, color="white", weight="bold", transform=ax.transAxes)
    ax.text(0.5, 0.48, main_stat, ha="center", fontsize=38, color="#4CAF50", weight="bold", transform=ax.transAxes)
    ax.text(0.5, 0.30, subtitle, ha="center", fontsize=14, color="#cccccc", transform=ax.transAxes)
    ax.text(0.5, 0.06, "TrainingProg 💪", ha="center", fontsize=10, color="#888888", transform=ax.transAxes)

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf


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


def get_previous_session_best(user_id, exercise_name, exclude_date):
    """Найкращий підхід з попереднього (не сьогоднішнього) тренування цієї вправи."""
    sessions = best_set_per_session(user_id, exercise_name, limit=5)
    for d, w, r in sessions:
        if d != exclude_date:
            return d, w, r
    return None


def suggest_next_target(user_id, exercise_name):
    """Пропозиція цілі на підхід: вага з минулого разу + невеликий приріст."""
    prev = get_previous_session_best(user_id, exercise_name, exclude_date=date.today().isoformat())
    if not prev:
        return None
    prev_date, prev_w, prev_r = prev
    suggested_w = round_to_plate(prev_w + PLATE_STEP)
    return {"prev_date": prev_date, "prev_w": prev_w, "prev_r": prev_r, "suggested_w": suggested_w}


def ukr_days_word(n):
    if 11 <= n % 100 <= 14:
        return "днів"
    last = n % 10
    if last == 1:
        return "день"
    if 2 <= last <= 4:
        return "дні"
    return "днів"


def get_streak(user_id):
    """К-ть послідовних днів поспіль із записаними підходами (стрік)."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT log_date FROM logs WHERE user_id = ? ORDER BY log_date DESC", (user_id,)
    ).fetchall()
    conn.close()
    if not rows:
        return 0

    dates = [date.fromisoformat(r["log_date"]) for r in rows]
    today = date.today()
    if dates[0] not in (today, today - timedelta(days=1)):
        return 0  # останнє тренування було більше дня тому — стрік перервано

    streak = 1
    for i in range(1, len(dates)):
        if dates[i - 1] - dates[i] == timedelta(days=1):
            streak += 1
        else:
            break
    return streak


def rename_day(day_id, new_name):
    conn = get_conn()
    conn.execute("UPDATE days SET name = ? WHERE id = ?", (new_name, day_id))
    conn.commit()
    conn.close()


def get_period_logs(user_id, start_date, end_date):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM logs WHERE user_id = ? AND log_date >= ? AND log_date <= ? ORDER BY log_date",
        (user_id, start_date.isoformat(), end_date.isoformat()),
    ).fetchall()
    conn.close()
    return rows


def build_weekly_report(user_id):
    """Звіт за останні 7 днів: тренування, тоннаж, найбільший прогрес по 1ПМ порівняно з попередніми 7 днями."""
    today = date.today()
    period_start = today - timedelta(days=6)
    prev_start = today - timedelta(days=13)
    prev_end = today - timedelta(days=7)

    rows = get_period_logs(user_id, period_start, today)
    if not rows:
        return None

    distinct_days = len({r["log_date"] for r in rows})
    total_sets = len(rows)
    total_tonnage = sum(r["weight"] * r["reps"] for r in rows)

    best_this = {}
    for r in rows:
        rm = estimate_1rm(r["weight"], r["reps"])
        if r["exercise_name"] not in best_this or rm > best_this[r["exercise_name"]]:
            best_this[r["exercise_name"]] = rm

    prev_rows = get_period_logs(user_id, prev_start, prev_end)
    best_prev = {}
    for r in prev_rows:
        rm = estimate_1rm(r["weight"], r["reps"])
        if r["exercise_name"] not in best_prev or rm > best_prev[r["exercise_name"]]:
            best_prev[r["exercise_name"]] = rm

    improvements = []
    for ex, rm in best_this.items():
        prev_rm = best_prev.get(ex)
        if prev_rm and rm > prev_rm:
            improvements.append((ex, rm - prev_rm, prev_rm, rm))
    improvements.sort(key=lambda x: x[1], reverse=True)

    return {
        "period_start": period_start,
        "period_end": today,
        "days": distinct_days,
        "sets": total_sets,
        "tonnage": total_tonnage,
        "improvements": improvements[:3],
    }


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
        "для легкого/середнього/важкого тренування\n"
        f"• {BTN_WEEKLY} — підсумок за останні 7 днів: тренування, тоннаж, прогрес\n"
        f"• {BTN_DONATE} — якщо бот сподобався і хочеш підтримати автора\n\n"
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
            lines.append(f"   Підхід {s['set_number']}: {fmt_kg(s['weight'])} кг x {s['reps']} повт.")
        lines.append("")
    streak = get_streak(user_id)
    if streak >= 2:
        lines.append(f"🔥 Стрік: {streak} {ukr_days_word(streak)} поспіль!")
    note = get_workout_note(user_id, date.today().isoformat())
    if note:
        lines.append(f"\n📝 Нотатка: {note}")
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


# ---------- /editex (перейменувати/видалити день або вправу) ----------
def build_day_list_keyboard(days):
    keyboard = [[InlineKeyboardButton(d["name"], callback_data=f"eday_{d['id']}")] for d in days]
    return InlineKeyboardMarkup(keyboard)


def build_day_action_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🛠 Керувати вправами", callback_data="dayaction_exercises")],
            [InlineKeyboardButton("✏️ Перейменувати день", callback_data="dayaction_rename")],
            [InlineKeyboardButton("🗑 Видалити день", callback_data="dayaction_delete")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="editex_back_to_daylist")],
        ]
    )


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
    day = get_day_by_id(day_id)
    day_name = day["name"] if day else ""
    await query.edit_message_text(f"День: «{day_name}»\nЩо зробити?", reply_markup=build_day_action_keyboard())
    return EDITEX_DAY_ACTION


async def editex_back_to_daylist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    days = get_days(user_id)
    await query.edit_message_text("З яким днем працюємо?", reply_markup=build_day_list_keyboard(days))
    return EDITEX_DAY


async def editex_back_to_dayaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    day = get_day_by_id(context.user_data.get("editex_day_id"))
    day_name = day["name"] if day else ""
    await query.edit_message_text(f"День: «{day_name}»\nЩо зробити?", reply_markup=build_day_action_keyboard())
    return EDITEX_DAY_ACTION


async def editex_day_action_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    day_id = context.user_data.get("editex_day_id")

    if query.data == "dayaction_exercises":
        exs = get_exercises_for_day(day_id)
        if not exs:
            await query.edit_message_text(f"У цьому дні ще немає вправ. Додай через {BTN_ADDEX}")
            return ConversationHandler.END
        await query.edit_message_text("Яку вправу редагувати?", reply_markup=build_exercise_list_keyboard(exs))
        return EDITEX_EXERCISE

    elif query.data == "dayaction_rename":
        day = get_day_by_id(day_id)
        day_name = day["name"] if day else ""
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Назад", callback_data="editex_back_to_dayaction")]]
        )
        await query.edit_message_text(
            f"Поточна назва дня: «{day_name}»\nВведи нову назву:", reply_markup=keyboard
        )
        return EDITEX_DAY_NAME

    else:  # dayaction_delete
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


async def editex_day_name_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text.strip()
    day_id = context.user_data.get("editex_day_id")
    rename_day(day_id, new_name)
    await update.message.reply_text(
        f"День перейменовано на «{new_name}» ✅\nПеревір: {BTN_PLAN}", reply_markup=MAIN_MENU_KEYBOARD
    )
    return ConversationHandler.END


async def editex_delday_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "delday_yes":
        day_id = context.user_data.get("editex_day_id")
        delete_day(day_id)
        await query.edit_message_text("День видалено ✅")
        return ConversationHandler.END
    else:
        return await editex_back_to_dayaction(update, context)


async def editex_back_to_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # "Назад" зі списку вправ дня — повертає до меню дій над днем
    return await editex_back_to_dayaction(update, context)


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
    days = get_days(user_id)
    if not days:
        await update.message.reply_text(f"Спочатку створи день тренування: {BTN_NEWDAY}")
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton(d["name"], callback_data=f"logday_{d['id']}")] for d in days]
    await update.message.reply_text(
        "З яким днем працюємо?", reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return LOG_DAY


async def log_day_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    day_id = int(query.data.split("_", 1)[1])
    context.user_data["log_day_id"] = day_id
    return await show_log_exercise_list(query, context)


async def show_log_exercise_list(query, context):
    day_id = context.user_data.get("log_day_id")
    exs = get_exercises_for_day(day_id)
    if not exs:
        await query.edit_message_text(f"У цьому дні ще немає вправ. Додай через {BTN_ADDEX}")
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton(e["name"], callback_data=f"logexid_{e['id']}")] for e in exs]
    keyboard.append([InlineKeyboardButton("⬅️ Змінити день", callback_data="back_to_logday")])
    await query.edit_message_text("Яку вправу записуємо?", reply_markup=InlineKeyboardMarkup(keyboard))
    return LOG_EXERCISE


async def back_to_logday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    days = get_days(user_id)
    keyboard = [[InlineKeyboardButton(d["name"], callback_data=f"logday_{d['id']}")] for d in days]
    await query.edit_message_text("З яким днем працюємо?", reply_markup=InlineKeyboardMarkup(keyboard))
    return LOG_DAY


def weight_prompt_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_exercise")]])


def reps_prompt_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_weight")]])


def build_weight_prompt_text(user_id, ex_name):
    text = f"Вправа: {ex_name}\n"
    target = suggest_next_target(user_id, ex_name)
    if target:
        text += (
            f"Минулого разу ({target['prev_date']}): {fmt_kg(target['prev_w'])} кг x {target['prev_r']} повт.\n"
            f"🎯 Спробуй: {fmt_kg(target['suggested_w'])} кг x {target['prev_r']} повт.\n"
        )
    text += "Введи вагу (кг), наприклад: 60"
    return text


async def log_exercise_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ex_id = int(query.data.split("_", 1)[1])
    ex = get_exercise_by_id(ex_id)
    if not ex:
        await query.edit_message_text(f"Вправу не знайдено, спробуй {BTN_LOG} ще раз.")
        return ConversationHandler.END
    ex_name = ex["name"]
    context.user_data["log_exercise"] = ex_name
    user_id = update.effective_user.id
    await query.edit_message_text(
        build_weight_prompt_text(user_id, ex_name),
        reply_markup=weight_prompt_keyboard(),
    )
    return LOG_WEIGHT


async def log_custom_exercise_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Дозволяє записати підхід по вправі, якої немає в плані цього дня — вводиш назву вручну.
    ex_name = update.message.text.strip()
    context.user_data["log_exercise"] = ex_name
    user_id = update.effective_user.id
    await update.message.reply_text(
        build_weight_prompt_text(user_id, ex_name),
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
                f"{fmt_kg(log_row['weight'])} кг x {log_row['reps']} повт. ✅"
            )
        else:
            text = "Гаразд, залишаємо як було."
        weight = context.user_data.get("log_weight")
        await query.edit_message_text(text, reply_markup=log_more_keyboard(weight))
        return LOG_MORE

    return await show_log_exercise_list(query, context)


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


def log_more_keyboard(weight=None):
    # Якщо вже знаємо вагу поточного підходу — показуємо швидкі кнопки,
    # щоб записати наступний підхід (зазвичай їх 3-4 на вправу) в 1 тап,
    # без повторного набору ваги й повторів.
    buttons = []
    if weight is not None:
        buttons.append(
            [InlineKeyboardButton("🔂 Такий самий підхід", callback_data="more_repeat")]
        )
        buttons.append(
            [InlineKeyboardButton(f"🔁 Ще підхід ({fmt_kg(weight)} кг)", callback_data="more_sameweight")]
        )
    buttons.append([InlineKeyboardButton("➕ Інша вага цієї ж вправи", callback_data="more_same")])
    buttons.append([InlineKeyboardButton("✏️ Виправити цей підхід", callback_data="more_edit")])
    buttons.append([InlineKeyboardButton("🔁 Інша вправа", callback_data="more_other")])
    buttons.append([InlineKeyboardButton("📝 Додати нотатку до тренування", callback_data="more_note")])
    buttons.append([InlineKeyboardButton("✅ Завершити", callback_data="more_done")])
    return InlineKeyboardMarkup(buttons)


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
        context.user_data["log_reps"] = reps
        confirm_text = f"Виправлено: {ex_name}, підхід {set_number} — {fmt_kg(weight)} кг x {reps} повт. ✅"
    else:
        prev_best = get_max_weight_log(user_id, ex_name)
        is_new_record = (not prev_best) or weight > prev_best["weight"] or (
            weight == prev_best["weight"] and reps > prev_best["reps"]
        )
        today_str = date.today().isoformat()
        prev_session = get_previous_session_best(user_id, ex_name, exclude_date=today_str)

        set_number, log_id = add_log(user_id, ex_name, weight, reps)
        context.user_data["log_last_id"] = log_id
        context.user_data["log_last_set_number"] = set_number
        context.user_data["log_reps"] = reps
        confirm_text = f"Записано: {ex_name}, підхід {set_number} — {fmt_kg(weight)} кг x {reps} повт. ✅"

        if is_new_record:
            confirm_text += f"\n🎉 Новий рекорд ваги для цієї вправи! (1ПМ ≈ {fmt_kg(estimate_1rm(weight, reps))} кг)"
        elif prev_session:
            prev_date, prev_w, prev_r = prev_session
            curr_1rm = estimate_1rm(weight, reps)
            prev_1rm = estimate_1rm(prev_w, prev_r)
            if curr_1rm > prev_1rm:
                confirm_text += (
                    f"\n📈 Це більше, ніж минулого разу! ({fmt_kg(prev_w)} кг x {prev_r} → "
                    f"{fmt_kg(weight)} кг x {reps})"
                )

    await update.message.reply_text(confirm_text, reply_markup=log_more_keyboard(weight))
    return LOG_MORE


async def log_more_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data

    if choice == "more_repeat":
        # Записує підхід з тією ж вагою й тими ж повторами, що й попередній — в 1 тап,
        # без набору цифр. Зручно для прямих підходів (напр. 4x8 з однаковою вагою).
        context.user_data["editing_last"] = False
        user_id = update.effective_user.id
        ex_name = context.user_data["log_exercise"]
        weight = context.user_data.get("log_weight")
        reps = context.user_data.get("log_reps")
        if weight is None or reps is None:
            await query.answer("Немає попереднього підходу для повтору", show_alert=True)
            return LOG_MORE

        prev_best = get_max_weight_log(user_id, ex_name)
        is_new_record = (not prev_best) or weight > prev_best["weight"] or (
            weight == prev_best["weight"] and reps > prev_best["reps"]
        )
        set_number, log_id = add_log(user_id, ex_name, weight, reps)
        context.user_data["log_last_id"] = log_id
        context.user_data["log_last_set_number"] = set_number
        confirm_text = f"Записано: {ex_name}, підхід {set_number} — {fmt_kg(weight)} кг x {reps} повт. ✅"
        if is_new_record:
            confirm_text += f"\n🎉 Новий рекорд ваги для цієї вправи! (1ПМ ≈ {fmt_kg(estimate_1rm(weight, reps))} кг)"
        await query.edit_message_text(confirm_text, reply_markup=log_more_keyboard(weight))
        return LOG_MORE
    elif choice == "more_sameweight":
        # Пропускає введення ваги — одразу питає повтори для того ж підходу.
        context.user_data["editing_last"] = False
        ex_name = context.user_data["log_exercise"]
        weight = context.user_data.get("log_weight")
        await query.edit_message_text(
            f"Вправа: {ex_name}\nВага: {fmt_kg(weight)} кг (та ж)\nСкільки повторів?",
            reply_markup=reps_prompt_keyboard(),
        )
        return LOG_REPS
    elif choice == "more_same":
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
        return await show_log_exercise_list(query, context)
    elif choice == "more_note":
        user_id = update.effective_user.id
        existing = get_workout_note(user_id, date.today().isoformat())
        text = "Введи нотатку до сьогоднішнього тренування (наприклад: «боліло плече», «було важко»):"
        if existing:
            text += f"\n\nПоточна нотатка: «{existing}»"
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_logmore")]]
        )
        await query.edit_message_text(text, reply_markup=keyboard)
        return LOG_NOTE
    else:
        user_id = update.effective_user.id
        streak = get_streak(user_id)
        streak_line = f"\n🔥 Стрік: {streak} {ukr_days_word(streak)} поспіль!" if streak >= 2 else ""
        await query.edit_message_text(
            f"Тренування записано. Гарного відновлення! 💪{streak_line}\nПодивитись: {BTN_TODAY}"
        )
        return ConversationHandler.END


async def log_note_saved(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    note = update.message.text.strip()
    set_workout_note(user_id, date.today().isoformat(), note)
    weight = context.user_data.get("log_weight")
    await update.message.reply_text("Нотатку збережено ✅", reply_markup=log_more_keyboard(weight))
    return LOG_MORE


async def back_to_logmore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    weight = context.user_data.get("log_weight")
    await query.edit_message_text("Що далі?", reply_markup=log_more_keyboard(weight))
    return LOG_MORE


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
            lines.append(f"{d}: {fmt_kg(w)} кг x {r} повт.")
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
    await send_record(query, update.effective_user.id, ex_name, context)


async def send_record(query, user_id, ex_name, context):
    row = get_max_weight_log(user_id, ex_name)
    if not row:
        await query.edit_message_text(f"Записів по вправі «{ex_name}» ще немає.")
        return

    one_rm = estimate_1rm(row["weight"], row["reps"])
    zones = periodization_suggestions(one_rm)

    lines = [
        f"🏆 Рекорд: {ex_name}",
        f"{fmt_kg(row['weight'])} кг x {row['reps']} повт. ({row['log_date']})",
        f"💪 Розрахунковий 1ПМ (формула Еплі): {fmt_kg(one_rm)} кг",
        "",
        "🎯 Рекомендовані ваги для періодизації навантажень (% від 1ПМ, за NSCA):",
    ]
    for label, w, reps in zones:
        lines.append(f"{label}: {fmt_kg(w)} кг x {reps} повт.")

    context.user_data["pr_card"] = {
        "ex_name": ex_name,
        "weight": row["weight"],
        "reps": row["reps"],
        "one_rm": one_rm,
    }
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🖼 Картка для шерингу", callback_data="share_pr_card")]]
    )
    await query.edit_message_text("\n".join(lines), reply_markup=keyboard)


async def share_pr_card_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = context.user_data.get("pr_card")
    if not data:
        await query.message.reply_text("Немає даних для картки.")
        return
    buf = generate_achievement_card(
        f"🏆 {data['ex_name']}",
        f"{fmt_kg(data['weight'])} кг x {data['reps']}",
        f"1ПМ ≈ {fmt_kg(data['one_rm'])} кг",
    )
    await context.bot.send_photo(
        chat_id=query.message.chat_id, photo=buf, caption="Поділись своїм прогресом! 💪"
    )


# ---------- ☕ Подякувати автору (Telegram Stars) ----------
DONATE_AMOUNTS = [20, 50, 100, 250]


async def donate_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton(f"⭐ {amount}", callback_data=f"donate_{amount}") for amount in DONATE_AMOUNTS]
    ]
    await update.message.reply_text(
        "Дякую, що хочеш підтримати розробку бота! 🙏\nОбери суму у Зірках Telegram:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def donate_amount_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    amount = int(query.data.split("_", 1)[1])
    await context.bot.send_invoice(
        chat_id=query.message.chat_id,
        title="Подяка автору бота",
        description="Невелика підтримка розробки та підтримки TrainingProg ☕",
        payload=f"donate_{amount}",
        provider_token="",  # порожній рядок — оплата в Telegram Stars
        currency="XTR",
        prices=[LabeledPrice("Подяка", amount)],
    )


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount = update.message.successful_payment.total_amount
    await update.message.reply_text(
        f"Дякую за підтримку — {amount} ⭐! Дуже приємно 🙏", reply_markup=MAIN_MENU_KEYBOARD
    )


# ---------- ⚖️ Моя вага (вага тіла) ----------
async def bodyweight_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    last = get_last_bodyweight(user_id)
    text = "Введи свою поточну вагу (кг):"
    if last:
        text += f"\n(Останній запис: {fmt_kg(last['weight'])} кг, {last['log_date']})"
    await update.message.reply_text(text)
    return BODYWEIGHT_INPUT


async def bodyweight_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        weight = float(update.message.text.replace(",", ".").strip())
    except ValueError:
        await update.message.reply_text("Це не схоже на число. Введи вагу ще раз, наприклад: 82.5")
        return BODYWEIGHT_INPUT

    user_id = update.effective_user.id
    prev = get_last_bodyweight(user_id, exclude_today=True)
    add_or_update_bodyweight(user_id, weight)

    text = f"Записано: {fmt_kg(weight)} кг ✅"
    if prev:
        diff = round(weight - prev["weight"], 2)
        if diff != 0:
            sign = "+" if diff > 0 else ""
            text += f"\n({sign}{fmt_kg(diff)} кг від {prev['log_date']})"

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📊 Показати графік ваги", callback_data="show_bw_chart")]]
    )
    await update.message.reply_text(text, reply_markup=keyboard)
    return ConversationHandler.END


async def show_bodyweight_chart_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    series = get_bodyweight_history(user_id, limit=30)
    if len(series) < 2:
        await query.message.reply_text("Замало записів для графіка — потрібно хоча б 2.")
        return

    dates = [d for d, w in series]
    weights = [w for d, w in series]
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.plot(dates, weights, marker="o", color="#2196F3", linewidth=2)
    ax.set_title("Вага тіла")
    ax.set_ylabel("Вага, кг")
    ax.grid(True, alpha=0.3)
    ax.tick_params(axis="x", rotation=45)
    fig.tight_layout()
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    buf.seek(0)

    await context.bot.send_photo(chat_id=query.message.chat_id, photo=buf, caption="📊 Динаміка ваги тіла")


# ---------- 📤 Експорт CSV ----------
async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    csv_text = build_csv_export(user_id)
    if not csv_text.strip() or csv_text.count("\n") <= 1:
        await update.message.reply_text(
            "Ще немає даних для експорту.", reply_markup=MAIN_MENU_KEYBOARD
        )
        return

    buf = BytesIO(csv_text.encode("utf-8-sig"))  # utf-8-sig, щоб Excel коректно показував українські літери
    buf.name = "workout_history.csv"
    await update.message.reply_document(
        document=buf, filename="workout_history.csv", caption="Твоя історія тренувань 📤"
    )


# ---------- 📢 Розсилка (лише для власника) ----------
async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = os.environ.get("ADMIN_ID")
    if not admin_id or str(update.effective_user.id) != str(admin_id):
        return

    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Використання: /broadcast текст повідомлення для всіх користувачів")
        return

    user_ids = get_all_user_ids()
    sent = 0
    for uid in user_ids:
        try:
            await context.bot.send_message(chat_id=uid, text=text)
            sent += 1
        except Exception:
            pass  # користувач заблокував бота або видалив чат — пропускаємо

    await update.message.reply_text(f"Розіслано {sent}/{len(user_ids)} користувачам.")


# ---------- 🔔 Нагадування, якщо давно не тренувався ----------
async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    user_ids = get_users_needing_reminder(threshold_days=3)
    for uid in user_ids:
        try:
            await context.bot.send_message(
                chat_id=uid,
                text="Занудьгував за тобою 😄 Давно не було нових підходів — ще тренуєшся?",
            )
            mark_reminder_sent(uid)
        except Exception:
            pass  # користувач заблокував бота — пропускаємо


# ---------- 📊 Тижневий звіт ----------
async def weekly_report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    report = build_weekly_report(user_id)
    if not report:
        await update.message.reply_text(
            "За останні 7 днів ще немає записаних тренувань.", reply_markup=MAIN_MENU_KEYBOARD
        )
        return

    lines = [
        f"📊 Тижневий звіт ({report['period_start']} – {report['period_end']})",
        "",
        f"🏋️ Тренувань: {report['days']}",
        f"🔢 Підходів записано: {report['sets']}",
        f"⚖️ Загальний тоннаж: {fmt_kg(report['tonnage'])} кг",
    ]

    if report["improvements"]:
        lines.append("")
        lines.append("📈 Найбільший прогрес (1ПМ) порівняно з попереднім тижнем:")
        for ex, diff, prev_rm, rm in report["improvements"]:
            lines.append(f"{ex}: {fmt_kg(prev_rm)} → {fmt_kg(rm)} кг (+{fmt_kg(diff)} кг)")

    streak = get_streak(user_id)
    if streak >= 2:
        lines.append("")
        lines.append(f"🔥 Стрік: {streak} {ukr_days_word(streak)} поспіль!")

    context.user_data["weekly_card"] = {
        "days": report["days"],
        "tonnage": report["tonnage"],
    }
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🖼 Картка для шерингу", callback_data="share_weekly_card")]]
    )
    await update.message.reply_text("\n".join(lines), reply_markup=keyboard)


async def share_weekly_card_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = context.user_data.get("weekly_card")
    if not data:
        await query.message.reply_text("Немає даних для картки.")
        return
    buf = generate_achievement_card(
        "📊 Тижневий звіт",
        f"{fmt_kg(data['tonnage'])} кг",
        f"загального тоннажу за {data['days']} тренувань",
    )
    await context.bot.send_photo(
        chat_id=query.message.chat_id, photo=buf, caption="Поділись своїм тижнем! 💪"
    )


# ---------- Відстеження користувачів + статистика (лише для власника) ----------
async def track_user_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user:
        track_user(user.id, user.username)


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = os.environ.get("ADMIN_ID")
    if not admin_id or str(update.effective_user.id) != str(admin_id):
        return  # тихо ігноруємо для всіх, крім власника

    stats = get_user_stats()
    text = (
        "📊 Статистика бота\n\n"
        f"👥 Всього користувачів: {stats['total_users']}\n"
        f"🏃 Активних за останні 7 днів: {stats['active_7d']}\n"
        f"📝 Всього тренувань (людино-днів) записано: {stats['total_workouts']}"
    )
    await update.message.reply_text(text)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "Не знайдено TELEGRAM_BOT_TOKEN. Встанови змінну середовища з токеном від @BotFather."
        )

    init_db()
    app = Application.builder().token(token).build()

    # Відстежує кожного унікального користувача у фоні (group=-1 — не заважає іншим хендлерам)
    app.add_handler(MessageHandler(filters.ALL, track_user_handler), group=-1)
    app.add_handler(CallbackQueryHandler(track_user_handler), group=-1)

    app.add_handler(CommandHandler("stats", stats_cmd))

    app.add_handler(CommandHandler("start", start))

    app.add_handler(CommandHandler("donate", donate_start))
    app.add_handler(MessageHandler(filters.Text([BTN_DONATE]), donate_start))
    app.add_handler(CallbackQueryHandler(donate_amount_chosen, pattern=r"^donate_\d+$"))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
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
                CallbackQueryHandler(editex_day_chosen, pattern=r"^eday_"),
            ],
            EDITEX_DAY_ACTION: [
                CallbackQueryHandler(editex_back_to_daylist, pattern=r"^editex_back_to_daylist$"),
                CallbackQueryHandler(editex_day_action_chosen, pattern=r"^dayaction_"),
            ],
            EDITEX_DAY_NAME: [
                CallbackQueryHandler(editex_back_to_dayaction, pattern=r"^editex_back_to_dayaction$"),
                MessageHandler(free_text_filter, editex_day_name_chosen),
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
            LOG_DAY: [
                CallbackQueryHandler(log_day_chosen, pattern=r"^logday_"),
            ],
            LOG_EXERCISE: [
                CallbackQueryHandler(back_to_logday, pattern=r"^back_to_logday$"),
                CallbackQueryHandler(log_exercise_chosen, pattern=r"^logexid_"),
                MessageHandler(free_text_filter, log_custom_exercise_text),
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
            LOG_NOTE: [
                CallbackQueryHandler(back_to_logmore, pattern=r"^back_to_logmore$"),
                MessageHandler(free_text_filter, log_note_saved),
            ],
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

    app.add_handler(CommandHandler("weekly", weekly_report_cmd))
    app.add_handler(MessageHandler(filters.Text([BTN_WEEKLY]), weekly_report_cmd))
    app.add_handler(CallbackQueryHandler(share_pr_card_callback, pattern=r"^share_pr_card$"))
    app.add_handler(CallbackQueryHandler(share_weekly_card_callback, pattern=r"^share_weekly_card$"))

    bodyweight_conv = ConversationHandler(
        entry_points=[
            CommandHandler("bodyweight", bodyweight_start),
            MessageHandler(filters.Text([BTN_BODYWEIGHT]), bodyweight_start),
        ],
        states={
            BODYWEIGHT_INPUT: [MessageHandler(free_text_filter, bodyweight_save)],
            ConversationHandler.TIMEOUT: [timeout_handler],
        },
        fallbacks=[CommandHandler("cancel", cancel), other_commands_fallback, menu_button_fallback],
        conversation_timeout=CONVERSATION_TIMEOUT,
    )
    app.add_handler(bodyweight_conv)
    app.add_handler(CallbackQueryHandler(show_bodyweight_chart_callback, pattern=r"^show_bw_chart$"))

    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(MessageHandler(filters.Text([BTN_EXPORT]), export_cmd))

    app.add_handler(CommandHandler("broadcast", broadcast_cmd))

    if app.job_queue:
        app.job_queue.run_daily(reminder_job, time=dt_time(hour=9, minute=0))

    logger.info("Бот запущено...")
    app.run_polling()


if __name__ == "__main__":
    main()
