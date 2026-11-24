# requirements.txt :
#   python-telegram-bot[job-queue]>=21
#   httpx
#   pillow
#   numpy      (simulation Monte Carlo)
#   cairosvg   (optionnel : sans lui, les logos sont remplacés par l'abréviation de l'équipe)
#
# Variables d'environnement :
#   TELEGRAM_TOKEN, ADMIN_PASSWORD, DATABASE_PATH (optionnel)
#   ALLOWED_USERS   (optionnel : IDs VIP, accès gratuit illimité)
#   ADMIN_CHAT_ID   (optionnel : ton ID Telegram : ventes, alertes API, rapport des picks)
#   SUPPORT_CONTACT (optionnel : ex. @tonpseudo, affiché dans /paysupport)
#
# Police optionnelle : mets "Inter-Bold.ttf" à côté du script (sinon police par défaut).

import asyncio
import difflib
import io
import logging
import math
import os
import re
import secrets
import sqlite3
import time
import unicodedata
from datetime import datetime, timedelta, date as ddate, time as dtime
from functools import lru_cache
from html import escape as esc
from zoneinfo import ZoneInfo

import httpx
from PIL import Image, ImageDraw, ImageFont
from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto,
                      LabeledPrice, ReplyKeyboardMarkup, Update)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler,
                          MessageHandler, PersistenceInput, PicklePersistence,
                          PreCheckoutQueryHandler, TypeHandler, filters)

try:
    import cairosvg
except Exception:
    cairosvg = None

# =====================================================
# CONFIGURATION
# =====================================================
TOKEN = os.environ["TELEGRAM_TOKEN"]
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
DB_PATH = os.environ.get("DATABASE_PATH", "/data/black_ice.db")
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_USERS", "").replace(" ", "").split(",") if x}
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "").strip()
SUPPORT_CONTACT = os.environ.get("SUPPORT_CONTACT", "").strip()
API_WEB = "https://api-web.nhle.com/v1"
LOGO_URL = "https://assets.nhle.com/logos/nhl/svg/{}_light.svg"
ASSETS_DIR = os.environ.get("ASSETS_DIR", os.path.join(os.path.dirname(DB_PATH) or ".", "assets"))
PERSIST_PATH = os.path.join(os.path.dirname(DB_PATH) or ".", "botdata.pickle")
TZ = ZoneInfo("Europe/Paris")
TOP = 5
H2H_SCORE_COUNT = 5
MENU_BTN = "📅 Matchs du jour"
PICKS_BTN = "🏆 Picks du jour"
HOME_BTN = "🏠 Accueil"
ACCOUNT_BTN = "👤 Mon compte"
ABO_BTN = "⭐ Abonnement"
HTML = ParseMode.HTML
ADMIN_SESSION_SECONDS = 12 * 3600
CACHE_LONG = 6 * 3600

# ---------- Abonnement (Telegram Stars) ----------
PRICE_WEEK_STARS = 150
PRICE_MONTH_STARS = 450
FREE_DAILY = 3             # analyses gratuites par jour (Algo 1, hors aperçus)
SUBSCRIPTION_PERIOD = 30 * 24 * 3600
REFERRAL_DAYS = 3          # jours offerts au parrain quand son filleul s'abonne
STAR_EUR = 0.013           # estimation de ce que tu touches par Star (à vérifier)

PICKS_HOUR = 10            # heure (Paris) d'envoi des picks du jour
PICK_LIMITS = (("points", 5), ("goals", 3), ("assists", 3))
PICK_TEAM_CAP = 2          # joueurs max par équipe et par marché
PICK_BOTH_TOP = 10         # un joueur est "✅✅" s'il est dans le top 10 des deux algos
MARKET_ICON = {"points": "⭐", "goals": "🥅", "assists": "🎯"}
MARKET_NAME = {"points": "POINTS (1+)", "goals": "BUTS (1+)", "assists": "PASSES (1+)"}
# Intitulés simples pour les picks (affichage grand public)
MARKET_TITLE = {"points": "POINTS", "goals": "BUTEURS", "assists": "PASSEURS"}
MARKET_HELP = {"points": "marque ou fait une passe (1 point ou plus)",
               "goals": "marque au moins 1 but",
               "assists": "fait au moins 1 passe décisive"}

# Taux moyens vers lesquels on tire les petits échantillons (évite les 88 % sur 10 matchs)
BASE_RATE = {"points": 0.45, "goals": 0.20, "assists": 0.32}
PRIOR_STRENGTH = 6

PLANS = {
    "week": {"label": "1 semaine", "days": 7, "stars": PRICE_WEEK_STARS, "recurring": False},
    "month": {"label": "1 mois", "days": 30, "stars": PRICE_MONTH_STARS, "recurring": True},
}
LEGAL = ("<i>🔞 Réservé aux +18 ans. Analyses statistiques indicatives : aucun gain n'est garanti. "
         "Jouer comporte des risques (endettement, isolement, dépendance). "
         "Aide : Joueurs Info Service 09 74 75 13 13.</i>")
AGE_TEXT = ("🔞 <b>BLACK ICE est réservé aux personnes majeures (18 ans et plus).</b>\n\n"
            "En continuant, tu confirmes avoir 18 ans ou plus. Les analyses sont statistiques et indicatives, "
            "sans aucune garantie de gain.")

ALGO_LABELS = {"h2h": "⚔️ Face-à-face", "form": "🔥 Forme du moment"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# Ne jamais afficher les URL (elles contiennent le token du bot) dans les logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("blackice")

try:
    os.makedirs(ASSETS_DIR, exist_ok=True)
except OSError as _e:
    log.warning("Dossier assets indisponible : %s", _e)

TEAM_DATA = """ANA|Anaheim|ducks|canards
BOS|Boston|bruins
BUF|Buffalo|sabres
CGY|Calgary|flames
CAR|Caroline|carolina|hurricanes|canes|caroline du nord
CHI|Chicago|blackhawks|hawks
COL|Colorado|avalanche|avs
CBJ|Columbus|blue jackets|jackets
DAL|Dallas|stars|etoiles
DET|Detroit|red wings|wings
EDM|Edmonton|oilers
FLA|Floride|florida|panthers|panthères
LAK|Los Angeles|la|kings|rois
MIN|Minnesota|wild
MTL|Montréal|canadiens|habs|glorieux|tricolore|montreal
NSH|Nashville|predators|preds
NJD|New Jersey|devils|diables
NYI|Islanders|isles|insulaires|ny islanders
NYR|Rangers|ny rangers
OTT|Ottawa|senators|sens
PHI|Philadelphie|philadelphia|flyers
PIT|Pittsburgh|penguins|pens
SEA|Seattle|kraken
SJS|San Jose|sharks|requins
STL|St. Louis|saint louis|st louis|blues
TBL|Tampa Bay|tampa|lightning|foudre
TOR|Toronto|maple leafs|leafs|feuilles d'érable
UTA|Utah|mammoth|hockey club
VAN|Vancouver|canucks
VGK|Vegas|golden knights|las vegas|chevaliers
WSH|Washington|capitals|caps|capitales
WPG|Winnipeg|jets"""


def norm(value):
    value = unicodedata.normalize("NFKD", str(value))
    value = "".join(c for c in value if not unicodedata.combining(c)).lower()
    value = re.sub(r"[.'’]", "", value)
    return re.sub(r"\s+", " ", value).strip()


def number(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def fmt(value, decimals=2):
    return "N/D" if value is None else f"{value:.{decimals}f}".replace(".", ",")


def fmt_percent(value):
    if value is None:
        return "N/D"
    return f"{value * 100:.1f}".replace(".", ",") + " %"


def fmt_minutes(seconds):
    if seconds is None:
        return "N/D"
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def parse_toi(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        parts = str(value).split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except (TypeError, ValueError):
        pass
    return None


def format_date(value):
    if not value:
        return "Date inconnue"
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return str(value)[:10]


def fmt_ts(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%d/%m/%Y à %H:%M")


def today_key():
    return datetime.now(TZ).date().isoformat()


def shift_day(day, n):
    return (ddate.fromisoformat(day) + timedelta(days=n)).isoformat()


def player_name(player):
    first, last = player.get("firstName", ""), player.get("lastName", "")
    if isinstance(first, dict):
        first = first.get("default", "")
    if isinstance(last, dict):
        last = last.get("default", "")
    return f"{first} {last}".strip() or player.get("name") or player.get("fullName") or "Joueur inconnu"


NAMES, ALIASES = {}, {}
for line in TEAM_DATA.splitlines():
    abbr, name, *_aliases = line.split("|")
    NAMES[abbr] = name
    for alias in [abbr, name, *_aliases]:
        ALIASES[norm(alias)] = abbr


def find_team(value):
    value = norm(value)
    if value in ALIASES:
        return ALIASES[value]
    close = difflib.get_close_matches(value, list(ALIASES), n=1, cutoff=0.75)
    return ALIASES[close[0]] if close else None


SPLIT = re.compile(r"\s+(?:vs\.?|versus|contre|at|c\.)\s+|\s*@\s*|\s+-\s+", re.I)


def get_algo(ctx):
    algo = ctx.user_data.get("algo", "h2h")
    return algo if algo in ALGO_LABELS else "h2h"

# =====================================================
# SQLITE
# =====================================================
def db_one(sql, args=()):
    with sqlite3.connect(DB_PATH) as db:
        return db.execute(sql, args).fetchone()


def db_all(sql, args=()):
    with sqlite3.connect(DB_PATH) as db:
        return db.execute(sql, args).fetchall()


def db_exec(sql, args=()):
    with sqlite3.connect(DB_PATH) as db:
        cur = db.execute(sql, args)
        db.commit()
        return cur.rowcount


def db_many(sql, rows):
    if not rows:
        return
    with sqlite3.connect(DB_PATH) as db:
        db.executemany(sql, rows)
        db.commit()


# Colonnes ajoutées automatiquement si une ancienne version de la table existe déjà
SCHEMA_ADDS = {
    "users": [("username", "TEXT"), ("first_seen", "TEXT"), ("last_seen", "TEXT"),
              ("adult_ok", "INTEGER DEFAULT 0"), ("banned", "INTEGER DEFAULT 0"), ("notify", "INTEGER DEFAULT 1"),
              ("referred_by", "INTEGER"), ("ref_rewarded", "INTEGER DEFAULT 0")],
    "subscriptions": [("plan", "TEXT"), ("reminded_for", "INTEGER DEFAULT 0"), ("updated_at", "TEXT")],
    "payments": [("username", "TEXT"), ("refunded", "INTEGER DEFAULT 0"), ("created_at", "TEXT")],
    "picks": [("mdate", "TEXT"), ("headshot", "TEXT"), ("info", "TEXT"), ("result", "INTEGER"),
              ("p1", "REAL"), ("p2", "REAL"), ("both", "INTEGER DEFAULT 0"), ("created_at", "TEXT")],
    "promos": [("max_uses", "INTEGER DEFAULT 0"), ("uses", "INTEGER DEFAULT 0"), ("active", "INTEGER DEFAULT 1")],
}


def migrate(db):
    for table, cols in SCHEMA_ADDS.items():
        have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        if not have:
            continue
        for name, decl in cols:
            if name not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                log.info("Migration : colonne %s.%s ajoutée", table, name)


def init_db():
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with sqlite3.connect(DB_PATH) as db:
        db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("""CREATE TABLE IF NOT EXISTS subscriptions (
            user_id INTEGER PRIMARY KEY, expires_at INTEGER NOT NULL DEFAULT 0, plan TEXT,
            reminded_for INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, username TEXT, plan TEXT NOT NULL,
            stars INTEGER NOT NULL, charge_id TEXT NOT NULL, created_at TEXT NOT NULL, refunded INTEGER NOT NULL DEFAULT 0)""")
        db.execute("""CREATE TABLE IF NOT EXISTS usage (
            user_id INTEGER NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (user_id, day))""")
        db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, first_seen TEXT, last_seen TEXT,
            adult_ok INTEGER DEFAULT 0, banned INTEGER DEFAULT 0, notify INTEGER DEFAULT 1,
            referred_by INTEGER, ref_rewarded INTEGER DEFAULT 0)""")
        db.execute("""CREATE TABLE IF NOT EXISTS picks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, mdate TEXT NOT NULL, away TEXT NOT NULL, home TEXT NOT NULL,
            market TEXT NOT NULL, rank INTEGER NOT NULL, player_id INTEGER NOT NULL, player TEXT NOT NULL, team TEXT NOT NULL,
            headshot TEXT, prob REAL NOT NULL, n INTEGER NOT NULL, info TEXT, result INTEGER, created_at TEXT NOT NULL,
            p1 REAL, p2 REAL, both INTEGER DEFAULT 0,
            UNIQUE(day, player_id, market))""")
        db.execute("""CREATE TABLE IF NOT EXISTS promos (
            code TEXT PRIMARY KEY, days INTEGER NOT NULL, max_uses INTEGER NOT NULL DEFAULT 0,
            uses INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1)""")
        db.execute("CREATE TABLE IF NOT EXISTS promo_uses (code TEXT NOT NULL, user_id INTEGER NOT NULL, PRIMARY KEY (code, user_id))")
        db.execute("""CREATE TABLE IF NOT EXISTS algo_tops (
            id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, mdate TEXT NOT NULL, away TEXT NOT NULL, home TEXT NOT NULL,
            algo TEXT NOT NULL, market TEXT NOT NULL, rank INTEGER NOT NULL, player_id INTEGER NOT NULL,
            player TEXT NOT NULL, team TEXT NOT NULL, result INTEGER,
            UNIQUE(day, away, home, algo, market, rank))""")
        db.execute("INSERT OR IGNORE INTO settings (key,value) VALUES ('bot_open','1')")
        migrate(db)
        db.commit()
    log.info("SQLite initialisée : %s", DB_PATH)


def db_setting(key, default=None):
    row = db_one("SELECT value FROM settings WHERE key=?", (key,))
    return row[0] if row else default


def set_db_setting(key, value):
    db_exec("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def bot_is_open():
    return db_setting("bot_open", "1") == "1"


# ---------- Abonnements ----------
def sub_expiry(uid):
    row = db_one("SELECT expires_at FROM subscriptions WHERE user_id=?", (uid,))
    return row[0] if row else 0


def is_subscribed(uid):
    return sub_expiry(uid) > time.time()


def add_days(uid, days, plan=None):
    now = int(time.time())
    new = max(sub_expiry(uid), now) + int(days) * 86400
    db_exec("""INSERT INTO subscriptions(user_id,expires_at,plan,reminded_for,updated_at) VALUES(?,?,?,0,?)
        ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at,
        plan=COALESCE(excluded.plan, subscriptions.plan), updated_at=excluded.updated_at""",
        (uid, new, plan, datetime.now(TZ).isoformat()))
    return new


def revoke_sub(uid):
    db_exec("UPDATE subscriptions SET expires_at=0, updated_at=? WHERE user_id=?", (datetime.now(TZ).isoformat(), uid))


def record_payment(uid, username, plan, stars, charge_id):
    db_exec("INSERT INTO payments(user_id,username,plan,stars,charge_id,created_at) VALUES(?,?,?,?,?,?)",
            (uid, username or "", plan, int(stars), charge_id, datetime.now(TZ).isoformat()))


def active_subscribers(limit=60):
    now = int(time.time())
    rows = db_all("SELECT user_id,expires_at,plan FROM subscriptions WHERE expires_at>? ORDER BY expires_at DESC LIMIT ?", (now, limit))
    total = db_one("SELECT COUNT(*) FROM subscriptions WHERE expires_at>?", (now,))[0]
    return rows, total


def usage_today(uid):
    row = db_one("SELECT count FROM usage WHERE user_id=? AND day=?", (uid, today_key()))
    return row[0] if row else 0


def usage_incr(uid):
    db_exec("INSERT INTO usage(user_id,day,count) VALUES(?,?,1) ON CONFLICT(user_id,day) DO UPDATE SET count=count+1", (uid, today_key()))


# ---------- Utilisateurs ----------
def register_user(update):
    user = update.effective_user
    now = datetime.now(TZ).isoformat()
    if db_one("SELECT 1 FROM users WHERE user_id=?", (user.id,)):
        db_exec("UPDATE users SET last_seen=?, username=? WHERE user_id=?", (now, user.username or "", user.id))
        return
    ref = None
    msg = update.message
    if msg and msg.text and msg.text.startswith("/start ref_"):
        try:
            cand = int(msg.text.split("ref_", 1)[1].split()[0])
            if cand != user.id and db_one("SELECT 1 FROM users WHERE user_id=?", (cand,)):
                ref = cand
        except (ValueError, IndexError):
            pass
    db_exec("INSERT OR IGNORE INTO users(user_id,username,first_seen,last_seen,referred_by) VALUES(?,?,?,?,?)",
            (user.id, user.username or "", now, now, ref))


def user_flags(uid):
    row = db_one("SELECT adult_ok, banned FROM users WHERE user_id=?", (uid,))
    if not row:
        return 0, 0
    return (row[0] or 0), (row[1] or 0)


def audience_ids():
    now = int(time.time())
    rows = db_all("""SELECT u.user_id FROM users u JOIN subscriptions s ON s.user_id=u.user_id
        WHERE COALESCE(u.notify,1)=1 AND COALESCE(u.banned,0)=0 AND u.adult_ok=1 AND s.expires_at>?""", (now,))
    ids = {r[0] for r in rows}
    for uid in ALLOWED:
        if db_one("SELECT 1 FROM users WHERE user_id=? AND COALESCE(notify,1)=1 AND COALESCE(banned,0)=0 AND adult_ok=1", (uid,)):
            ids.add(uid)
    return ids


async def notify_admin(bot, text):
    if not ADMIN_CHAT_ID:
        return
    try:
        await bot.send_message(int(ADMIN_CHAT_ID), text)
    except Exception as e:
        log.warning("Notification admin : %s", e)