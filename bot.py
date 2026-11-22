# requirements.txt :
#   python-telegram-bot[job-queue]>=21
#   httpx
#   pillow
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
from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto,
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

ALGO_LABELS = {"h2h": "Algo 1 · H2H", "form": "Algo 2 · Forme 10/20"}

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

# =====================================================
# AUTHENTIFICATION / ADMIN
# =====================================================
def admin_session_valid(ctx):
    expiry = ctx.user_data.get("admin_expiry", 0)
    if expiry < time.time():
        ctx.user_data.pop("admin_expiry", None)
        return False
    return True


def is_admin(update, ctx):
    return bool(update.effective_user and admin_session_valid(ctx))


def user_can_access(update, ctx):
    user = update.effective_user
    if not user:
        return False
    if is_admin(update, ctx) or user.id in ALLOWED:
        return True
    return bot_is_open()


def is_premium(update, ctx):
    user = update.effective_user
    if not user:
        return False
    return admin_session_valid(ctx) or user.id in ALLOWED or is_subscribed(user.id)


async def admin_required(update, ctx):
    if not is_admin(update, ctx):
        await update.message.reply_text("⛔ Connecte-toi d'abord avec /admin.")
        return False
    return True


async def cmd_admin(update, ctx):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Connexion administrateur uniquement en message privé.")
        return
    if not ADMIN_PASSWORD:
        await update.message.reply_text("⚠️ ADMIN_PASSWORD n'est pas configuré dans Railway.")
        return
    ctx.user_data["await_admin_password"] = True
    await update.message.reply_text("🔐 <b>PANNEAU ADMINISTRATEUR</b>\n\nEnvoie ton mot de passe en privé.", parse_mode=HTML)


async def cmd_logout(update, ctx):
    for key in ("admin_expiry", "await_admin_password", "pending_announce"):
        ctx.user_data.pop(key, None)
    await update.message.reply_text("🔒 Session administrateur fermée.")


async def cmd_fermer(update, ctx):
    if not await admin_required(update, ctx): return
    set_db_setting("bot_open", "0")
    await update.message.reply_text("🔒 <b>BLACK ICE EST FERMÉ</b>\n\nLes utilisateurs ne peuvent plus lancer d'analyses. Ta session admin reste active.", parse_mode=HTML)


async def cmd_ouvrir(update, ctx):
    if not await admin_required(update, ctx): return
    set_db_setting("bot_open", "1")
    await update.message.reply_text("🔓 <b>BLACK ICE EST OUVERT</b>", parse_mode=HTML)


async def cmd_statut(update, ctx):
    if not await admin_required(update, ctx): return
    status = "🟢 OUVERT" if bot_is_open() else "🔴 FERMÉ"
    _, total = active_subscribers(1)
    today_picks = db_one("SELECT COUNT(*) FROM picks WHERE day=?", (today_key(),))[0]
    await update.message.reply_text("<b>BLACK ICE — STATUT</b>\n\n"
        f"Accès utilisateurs : {status}\nBase : <code>{esc(DB_PATH)}</code>\n"
        f"Cartes images : activées (logos {'OK' if cairosvg else 'texte'})\n"
        f"Algo actif pour toi : {ALGO_LABELS[get_algo(ctx)]}\n"
        f"Abonnés actifs : <b>{total}</b>\n"
        f"Prix : {PRICE_WEEK_STARS} ⭐ / semaine · {PRICE_MONTH_STARS} ⭐ / mois\n"
        f"Analyses gratuites / jour : {FREE_DAILY}\n"
        f"Picks du jour en base : {today_picks} (envoi à {PICKS_HOUR}h)\n"
        f"API NHL : {'🔴 en panne' if _health['alerted'] else '🟢 OK'}", parse_mode=HTML)


async def cmd_stats(update, ctx):
    if not await admin_required(update, ctx): return
    now = datetime.now(TZ)
    d7, d30 = (now - timedelta(days=7)).isoformat(), (now - timedelta(days=30)).isoformat()
    s7 = db_one("SELECT COUNT(*), COALESCE(SUM(stars),0) FROM payments WHERE COALESCE(refunded,0)=0 AND created_at>=?", (d7,))
    s30 = db_one("SELECT COUNT(*), COALESCE(SUM(stars),0) FROM payments WHERE COALESCE(refunded,0)=0 AND created_at>=?", (d30,))
    refunds = db_one("SELECT COUNT(*) FROM payments WHERE refunded=1 AND created_at>=?", (d30,))[0]
    users = db_one("SELECT COUNT(*) FROM users")[0]
    new7 = db_one("SELECT COUNT(*) FROM users WHERE first_seen>=?", (d7,))[0]
    _, active = active_subscribers(1)
    ts = int(time.time())
    exp48 = db_one("SELECT COUNT(*) FROM subscriptions WHERE expires_at>? AND expires_at<=?", (ts, ts + 172800))[0]
    promo_uses = db_one("SELECT COUNT(*) FROM promo_uses")[0]
    refs = db_one("SELECT COUNT(*), COALESCE(SUM(ref_rewarded),0) FROM users WHERE referred_by IS NOT NULL")
    pk = db_one("SELECT COALESCE(SUM(CASE WHEN result=1 THEN 1 ELSE 0 END),0), COALESCE(SUM(CASE WHEN result=0 THEN 1 ELSE 0 END),0) FROM picks WHERE day>=?", (shift_day(today_key(), -30),))
    pk_rate = f"{pk[0] / (pk[0] + pk[1]) * 100:.0f} % ({pk[0]}/{pk[0] + pk[1]})" if (pk[0] + pk[1]) else "N/D"
    await update.message.reply_text(
        "📊 <b>STATISTIQUES</b>\n\n"
        f"👥 Utilisateurs : <b>{users}</b> (+{new7} sur 7 jours)\n"
        f"✅ Abonnés actifs : <b>{active}</b>\n"
        f"⏰ Expirent sous 48 h : {exp48}\n\n"
        f"💰 7 jours : {s7[0]} vente(s) · {s7[1]} ⭐ (≈ {s7[1] * STAR_EUR:.0f} €)\n"
        f"💰 30 jours : {s30[0]} vente(s) · {s30[1]} ⭐ (≈ {s30[1] * STAR_EUR:.0f} €)\n"
        f"↩️ Remboursements 30 j : {refunds}\n\n"
        f"🎟️ Codes promo utilisés : {promo_uses}\n"
        f"🤝 Filleuls : {refs[0]} (dont {refs[1]} convertis)\n"
        f"🏆 Picks 30 j : {pk_rate}\n\n"
        "<i>Les euros sont une estimation : vérifie ce que tu touches réellement.</i>", parse_mode=HTML)


async def cmd_bannir(update, ctx):
    if not await admin_required(update, ctx): return
    try:
        uid = int(ctx.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage : /bannir ID")
        return
    now = datetime.now(TZ).isoformat()
    db_exec("INSERT OR IGNORE INTO users(user_id,username,first_seen,last_seen) VALUES(?,?,?,?)", (uid, "", now, now))
    db_exec("UPDATE users SET banned=1 WHERE user_id=?", (uid,))
    await update.message.reply_text(f"⛔ {uid} est banni.")


async def cmd_debannir(update, ctx):
    if not await admin_required(update, ctx): return
    try:
        uid = int(ctx.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage : /debannir ID")
        return
    db_exec("UPDATE users SET banned=0 WHERE user_id=?", (uid,))
    await update.message.reply_text(f"✅ {uid} n'est plus banni.")


async def cmd_annonce(update, ctx):
    if not await admin_required(update, ctx): return
    text = (update.message.text or "").partition(" ")[2].strip()
    if not text:
        await update.message.reply_text("Usage : /annonce ton message\n(Un aperçu avec confirmation s'affiche avant l'envoi.)")
        return
    n = db_one("SELECT COUNT(*) FROM users WHERE COALESCE(banned,0)=0 AND adult_ok=1")[0]
    ctx.user_data["pending_announce"] = text
    await update.message.reply_text(
        f"📢 Aperçu de l'annonce ({n} destinataires) :\n\n{text}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ Envoyer à {n} personnes", callback_data="an:yes"),
                                            InlineKeyboardButton("❌ Annuler", callback_data="an:no")]]))


async def handle_announce(query, ctx, parts):
    if not admin_session_valid(ctx):
        await query.answer("Session admin expirée. Refais /admin.", show_alert=True)
        return
    await query.answer()
    text = ctx.user_data.pop("pending_announce", None)
    if parts[1] != "yes" or not text:
        await query.message.reply_text("❌ Annonce annulée.")
        return
    ids = [r[0] for r in db_all("SELECT user_id FROM users WHERE COALESCE(banned,0)=0 AND adult_ok=1")]
    await query.message.reply_text(f"⏳ Envoi à {len(ids)} personnes...")
    ok = fail = 0
    for uid in ids:
        try:
            await query.get_bot().send_message(uid, text)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await query.message.reply_text(f"✅ Annonce envoyée : {ok} réussi(s), {fail} échec(s).")

# =====================================================
# CODES PROMO / PARRAINAGE / NOTIFICATIONS
# =====================================================
async def cmd_promo(update, ctx):
    if not await admin_required(update, ctx): return
    try:
        code = ctx.args[0].upper()
        days = int(ctx.args[1])
        max_uses = int(ctx.args[2]) if len(ctx.args) > 2 else 0
        if not 1 <= days <= 365 or max_uses < 0:
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text("Usage : /promo CODE jours [utilisations max]\nExemple : /promo LANCEMENT 7 50\n(0 ou omis = illimité)")
        return
    db_exec("""INSERT INTO promos(code,days,max_uses,uses,active) VALUES(?,?,?,0,1)
        ON CONFLICT(code) DO UPDATE SET days=excluded.days, max_uses=excluded.max_uses, active=1""", (code, days, max_uses))
    await update.message.reply_text(f"🎟️ Code <b>{esc(code)}</b> créé : {days} jour(s), {'illimité' if not max_uses else str(max_uses) + ' utilisations'}.", parse_mode=HTML)


async def cmd_code(update, ctx):
    uid = update.effective_user.id
    if not ctx.args:
        await update.message.reply_text("Usage : /code TONCODE")
        return
    code = ctx.args[0].upper()
    row = db_one("SELECT days,max_uses,uses,active FROM promos WHERE code=?", (code,))
    if not row or not row[3]:
        await update.message.reply_text("❌ Code invalide.")
        return
    days, max_uses, uses, _ = row
    if max_uses and uses >= max_uses:
        await update.message.reply_text("❌ Ce code a atteint sa limite d'utilisations.")
        return
    if db_one("SELECT 1 FROM promo_uses WHERE code=? AND user_id=?", (code, uid)):
        await update.message.reply_text("❌ Tu as déjà utilisé ce code.")
        return
    db_exec("INSERT INTO promo_uses(code,user_id) VALUES(?,?)", (code, uid))
    db_exec("UPDATE promos SET uses=uses+1 WHERE code=?", (code,))
    expiry = add_days(uid, days, "promo")
    await update.message.reply_text(f"🎟️ Code accepté : <b>+{days} jour(s)</b> d'accès complet.\nActif jusqu'au {fmt_ts(expiry)}.", parse_mode=HTML)


async def cmd_parrainage(update, ctx):
    uid = update.effective_user.id
    link = f"https://t.me/{ctx.bot.username}?start=ref_{uid}"
    n, ok = db_one("SELECT COUNT(*), COALESCE(SUM(ref_rewarded),0) FROM users WHERE referred_by=?", (uid,))
    await update.message.reply_text(
        "🤝 <b>PARRAINAGE</b>\n\n"
        f"Partage ton lien : quand un ami s'abonne grâce à toi, tu reçois <b>{REFERRAL_DAYS} jours offerts</b>.\n\n"
        f"<code>{link}</code>\n\n"
        f"Filleuls : {n} (abonnés : {ok})", parse_mode=HTML)


async def cmd_notif(update, ctx):
    uid = update.effective_user.id
    arg = ctx.args[0].lower() if ctx.args else ""
    if arg in ("on", "off"):
        db_exec("UPDATE users SET notify=? WHERE user_id=?", (1 if arg == "on" else 0, uid))
    row = db_one("SELECT notify FROM users WHERE user_id=?", (uid,))
    state = "activés ✅" if (row and (row[0] is None or row[0])) else "désactivés 🔕"
    await update.message.reply_text(f"🔔 Picks du matin : <b>{state}</b>\nChange avec /notif on ou /notif off.", parse_mode=HTML)

# =====================================================
# ABONNEMENT : TELEGRAM STARS
# =====================================================
def plans_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🗓️ 1 semaine · {PRICE_WEEK_STARS} ⭐", callback_data="sb:week")],
        [InlineKeyboardButton(f"📆 1 mois · {PRICE_MONTH_STARS} ⭐ (renouvelable)", callback_data="sb:month")],
    ])


def account_lines(uid):
    exp = sub_expiry(uid)
    if exp > time.time():
        left = max(1, math.ceil((exp - time.time()) / 86400))
        return [f"✅ <b>Abonnement actif</b> jusqu'au {fmt_ts(exp)} ({left} j restant{'s' if left > 1 else ''})"]
    used = usage_today(uid)
    return ["🔓 <b>Accès gratuit</b>", f"Analyses du jour : {min(used, FREE_DAILY)}/{FREE_DAILY} (Algo 1) + aperçus"]


def plans_text(uid):
    out = ["⭐ <b>BLACK ICE — ABONNEMENT</b>", ""] + account_lines(uid) + [
        "",
        "<b>Accès complet :</b>",
        "• 🏆 Picks du jour envoyés chaque matin",
        "• Analyses illimitées (TOP 5, fiches joueurs)",
        "• Algo 1 (H2H) + Algo 2 (forme 10/20 matchs)",
        "• 🧬 Match Simulator complet",
        "• 🧭 Contexte : ligne, défense adverse, back-to-back", "",
        f"🗓️ <b>1 semaine</b> : {PRICE_WEEK_STARS} ⭐ (achat unique de 7 jours)",
        f"📆 <b>1 mois</b> : {PRICE_MONTH_STARS} ⭐ (30 jours, renouvelé automatiquement par Telegram, annulable à tout moment)",
        "", "🎟️ Un code promo ? /code TONCODE · 🤝 /parrainage", "", LEGAL]
    return "\n".join(out)


async def cmd_abonnement(update, ctx):
    await update.message.reply_text(plans_text(update.effective_user.id), parse_mode=HTML, reply_markup=plans_keyboard())


async def cmd_moncompte(update, ctx):
    uid = update.effective_user.id
    lines = ["👤 <b>MON COMPTE</b>", ""]
    lines += ["👑 Accès VIP illimité."] if uid in ALLOWED else account_lines(uid)
    await update.message.reply_text("\n".join(lines), parse_mode=HTML,
                                    reply_markup=None if (uid in ALLOWED or is_subscribed(uid)) else plans_keyboard())


async def cmd_paysupport(update, ctx):
    contact = f"Écris à {esc(SUPPORT_CONTACT)}" if SUPPORT_CONTACT else "Contacte l'administrateur du bot"
    await update.message.reply_text(
        "💬 <b>Support paiement</b>\n\n"
        f"{contact} avec ton identifiant Telegram et la date du paiement. Les remboursements sont étudiés au cas par cas.\n\n"
        "Pour annuler un renouvellement automatique : Telegram → Paramètres → Mes Stars → Abonnements.", parse_mode=HTML)


async def handle_subscribe(query, ctx, parts):
    await query.answer()
    chat = query.message.chat
    plan = parts[1] if len(parts) > 1 else "menu"
    uid = query.from_user.id
    if plan == "menu":
        await chat.send_message(plans_text(uid), parse_mode=HTML, reply_markup=plans_keyboard())
        return
    if plan not in PLANS:
        return
    p = PLANS[plan]
    title = f"BLACK ICE — {p['label']}"
    description = f"Accès complet BLACK ICE pendant {p['days']} jours : picks du jour, analyses illimitées, Algo 1 + Algo 2, Match Simulator."
    prices = [LabeledPrice(p["label"], p["stars"])]
    if p["recurring"]:
        try:
            link = await ctx.bot.create_invoice_link(
                title=title, description=description, payload=plan, provider_token="",
                currency="XTR", prices=prices, subscription_period=SUBSCRIPTION_PERIOD)
            await chat.send_message(
                f"📆 <b>Abonnement mensuel</b> — {p['stars']} ⭐ / 30 jours\n"
                "Renouvelé automatiquement par Telegram. Annulable à tout moment depuis tes abonnements Stars.",
                parse_mode=HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"⭐ Payer {p['stars']} Stars", url=link)]]))
            return
        except Exception as e:
            log.warning("Abonnement récurrent indisponible, achat unique : %s", e)
    await ctx.bot.send_invoice(chat_id=chat.id, title=title, description=description, payload=plan,
                               provider_token="", currency="XTR", prices=prices)


async def on_precheckout(update, ctx):
    q = update.pre_checkout_query
    ok = q.invoice_payload in PLANS
    await q.answer(ok=ok, error_message=None if ok else "Offre invalide. Relance /abonnement.")


async def on_payment(update, ctx):
    pay = update.message.successful_payment
    plan = pay.invoice_payload
    user = update.effective_user
    if plan not in PLANS:
        log.warning("Paiement avec payload inconnu : %s", plan)
        return
    expiry = add_days(user.id, PLANS[plan]["days"], plan)
    record_payment(user.id, user.username, plan, pay.total_amount, pay.telegram_payment_charge_id)
    renewal = bool(getattr(pay, "is_recurring", False)) and not bool(getattr(pay, "is_first_recurring", False))
    head = "🔁 <b>Abonnement renouvelé</b>" if renewal else "✅ <b>Paiement reçu, merci !</b>"
    await update.message.reply_text(
        f"{head}\n\nAccès complet actif jusqu'au <b>{fmt_ts(expiry)}</b>.\n"
        "Touche « 🏆 Picks du jour » ou « 📅 Matchs du jour » pour commencer.\n\n" + LEGAL, parse_mode=HTML)
    row = db_one("SELECT referred_by, ref_rewarded FROM users WHERE user_id=?", (user.id,))
    if row and row[0] and not row[1]:
        bonus = add_days(row[0], REFERRAL_DAYS, "referral")
        db_exec("UPDATE users SET ref_rewarded=1 WHERE user_id=?", (user.id,))
        try:
            await ctx.bot.send_message(row[0], f"🎁 Un filleul vient de s'abonner : +{REFERRAL_DAYS} jours offerts ! Actif jusqu'au {fmt_ts(bonus)}.")
        except Exception:
            pass
    who = f"@{user.username}" if user.username else str(user.id)
    await notify_admin(ctx.bot, f"💰 Vente : {who} (ID {user.id}) — {PLANS[plan]['label']} — {pay.total_amount} ⭐")


async def deny_access(query, reason):
    try:
        await query.message.chat.send_message(
            f"🔒 <b>{esc(reason)}</b>\n\n" + plans_text(query.from_user.id), parse_mode=HTML, reply_markup=plans_keyboard())
    except Exception as e:
        log.warning("Paywall : %s", e)


async def cmd_donner(update, ctx):
    if not await admin_required(update, ctx): return
    try:
        uid, days = int(ctx.args[0]), int(ctx.args[1])
        if not 1 <= days <= 3650:
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text("Usage : /donner ID jours\nExemple : /donner 123456789 30")
        return
    expiry = add_days(uid, days, "gift")
    await update.message.reply_text(f"✅ {days} jour(s) ajouté(s) à {uid}. Expire le {fmt_ts(expiry)}.")
    try:
        await ctx.bot.send_message(uid, f"🎁 Tu as reçu {days} jour(s) d'accès complet à BLACK ICE ! Actif jusqu'au {fmt_ts(expiry)}.")
    except Exception:
        await update.message.reply_text("(Impossible de prévenir l'utilisateur : il n'a pas démarré le bot.)")


async def cmd_revoquer(update, ctx):
    if not await admin_required(update, ctx): return
    try:
        uid = int(ctx.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage : /revoquer ID")
        return
    revoke_sub(uid)
    await update.message.reply_text(f"🚫 Abonnement de {uid} révoqué.")


async def cmd_abonnes(update, ctx):
    if not await admin_required(update, ctx): return
    rows, total = active_subscribers(60)
    if not rows:
        await update.message.reply_text("Aucun abonné actif.")
        return
    lines = [f"👥 <b>ABONNÉS ACTIFS : {total}</b>", ""]
    for uid, exp, plan in rows:
        lines.append(f"<code>{uid}</code> — {plan or '?'} — jusqu'au {fmt_ts(exp)}")
    await update.message.reply_text("\n".join(lines), parse_mode=HTML)


async def cmd_rembourser(update, ctx):
    if not await admin_required(update, ctx): return
    try:
        uid = int(ctx.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage : /rembourser ID\nRembourse le dernier paiement de cet utilisateur et révoque son accès.")
        return
    row = db_one("SELECT id,charge_id,stars FROM payments WHERE user_id=? AND COALESCE(refunded,0)=0 ORDER BY id DESC LIMIT 1", (uid,))
    if not row:
        await update.message.reply_text("Aucun paiement remboursable trouvé pour cet ID.")
        return
    pid, charge_id, stars = row
    try:
        await ctx.bot.refund_star_payment(user_id=uid, telegram_payment_charge_id=charge_id)
    except Exception as e:
        log.warning("Remboursement : %s", e)
        await update.message.reply_text(f"⚠️ Remboursement refusé par Telegram : {esc(str(e))}", parse_mode=HTML)
        return
    db_exec("UPDATE payments SET refunded=1 WHERE id=?", (pid,))
    revoke_sub(uid)
    await update.message.reply_text(f"✅ {stars} ⭐ remboursés à {uid}. Accès révoqué.")
    try:
        await ctx.bot.send_message(uid, f"↩️ Ton paiement de {stars} ⭐ a été remboursé.")
    except Exception:
        pass


async def reminder_job(ctx):
    now = int(time.time())
    rows = db_all("SELECT user_id,expires_at FROM subscriptions WHERE expires_at>? AND expires_at<=? AND COALESCE(reminded_for,0)!=expires_at", (now, now + 86400))
    for uid, exp in rows:
        try:
            await ctx.bot.send_message(uid, f"⏰ Ton accès BLACK ICE expire le <b>{fmt_ts(exp)}</b>.\nRenouvelle-le pour garder l'accès complet :",
                                       parse_mode=HTML, reply_markup=plans_keyboard())
        except Exception as e:
            log.info("Rappel impossible pour %s : %s", uid, e)
        db_exec("UPDATE subscriptions SET reminded_for=? WHERE user_id=?", (exp, uid))

# =====================================================
# CLIENT HTTP / CACHE / SAISONS
# =====================================================
_client = None
_cache = {}
_inflight = {}
_health = {"fails": 0, "alerted": False}


def client():
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": "BLACK-ICE-NHL-BOT/14.0"})
    return _client


async def get_json(url, params=None, tries=3):
    last_error = None
    for attempt in range(tries):
        try:
            response = await client().get(url, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as error:
            last_error = error
            code = error.response.status_code
            if code < 500 and code != 429:
                raise
        except (httpx.TimeoutException, httpx.TransportError) as error:
            last_error = error
        await asyncio.sleep(0.6 * (attempt + 1))
    raise last_error


async def cached(key, ttl, factory):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    task = _inflight.get(key)
    if task is None:
        task = asyncio.ensure_future(factory())
        _inflight[key] = task
        task.add_done_callback(lambda t, k=key: _inflight.pop(k, None))
    value = await asyncio.shield(task)
    _cache[key] = (time.time(), value)
    if len(_cache) > 3000:
        for k, _ in sorted(_cache.items(), key=lambda kv: kv[1][0])[:600]:
            _cache.pop(k, None)
    return value


def seasons():
    now = datetime.now(TZ)
    year = now.year if now.month >= 9 else now.year - 1
    return [f"{year-2}{year-1}", f"{year-1}{year}", f"{year}{year+1}"]


def current_season():
    now = datetime.now(TZ)
    year = now.year if now.month >= 9 else now.year - 1
    return f"{year}{year+1}"


async def health_job(ctx):
    """Alerte l'admin si l'API NHL est injoignable (2 échecs consécutifs), puis au rétablissement."""
    try:
        await get_json(f"{API_WEB}/schedule/{today_key()}", tries=1)
        if _health["alerted"]:
            await notify_admin(ctx.bot, "✅ L'API NHL répond de nouveau.")
        _health.update(fails=0, alerted=False)
    except Exception as e:
        _health["fails"] += 1
        log.warning("Health check NHL : %s", e)
        if _health["fails"] >= 2 and not _health["alerted"]:
            _health["alerted"] = True
            await notify_admin(ctx.bot, "🔴 L'API NHL ne répond plus (2 vérifications en échec). Les analyses peuvent être indisponibles.")

# =====================================================
# IMAGES : LOGOS / PHOTOS / CARTES
# =====================================================
BG, PANEL, PANEL2 = "#0b1220", "#141d30", "#1e293b"
ACCENT, GREEN, YELLOW, RED = "#38bdf8", "#4ade80", "#facc15", "#f87171"
WHITE, MUTED = "#ffffff", "#94a3b8"
FONT_PATHS = ["Inter-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf"]
_asset_miss = set()


@lru_cache(maxsize=64)
def _font(size):
    for path in FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def clean_label(text):
    return re.sub(r"[^\w\s%/+\-]", "", str(text)).strip()


async def asset(kind, key, url):
    if not url:
        return None
    path = os.path.join(ASSETS_DIR, f"{kind}_{key}.png")
    if path in _asset_miss:
        return None
    try:
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()
    except OSError:
        pass
    try:
        response = await client().get(url)
        response.raise_for_status()
        data = response.content
        if url.endswith(".svg"):
            if cairosvg is None:
                _asset_miss.add(path)
                return None
            data = await asyncio.to_thread(cairosvg.svg2png, bytestring=data, output_width=300)
        try:
            with open(path, "wb") as f:
                f.write(data)
        except OSError:
            pass
        return data
    except Exception as error:
        log.warning("Asset %s/%s indisponible : %s", kind, key, error)
        _asset_miss.add(path)
        return None


async def team_logo(abbr):
    return await asset("logo", abbr, LOGO_URL.format(abbr))


async def player_photo(player):
    return await asset("head", player.get("id"), player.get("headshot"))


def _ctext(d, cx, y, text, font, fill):
    d.text((cx - d.textlength(text, font=font) / 2, y), text, font=font, fill=fill)


def _fit(d, text, size, maxw, minimum=18):
    while size > minimum and d.textlength(text, font=_font(size)) > maxw:
        size -= 2
    return _font(size)


def _circle_mask(size):
    mask = Image.new("L", (size * 3, size * 3), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 3 - 1, size * 3 - 1), fill=255)
    return mask.resize((size, size), Image.LANCZOS)


def _avatar(data, name, size):
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    base = Image.new("RGBA", (size, size), PANEL2)
    if data:
        try:
            pic = Image.open(io.BytesIO(data)).convert("RGBA")
            side = min(pic.size)
            left, top = (pic.width - side) // 2, 0
            pic = pic.crop((left, top, left + side, top + side)).resize((size, size), Image.LANCZOS)
            base.alpha_composite(pic)
        except Exception:
            data = None
    if not data:
        d = ImageDraw.Draw(base)
        initials = "".join(w[0] for w in str(name).split()[:2]).upper() or "?"
        _ctext(d, size / 2, size * 0.30, initials, _font(int(size * 0.38)), MUTED)
    canvas.paste(base, (0, 0), _circle_mask(size))
    return canvas


def _logo(data, abbr, size):
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    disc = Image.new("RGBA", (size, size), "#f1f5f9")
    ok = False
    if data:
        try:
            pic = Image.open(io.BytesIO(data)).convert("RGBA")
            inner = int(size * 0.78)
            pic.thumbnail((inner, inner), Image.LANCZOS)
            disc.alpha_composite(pic, ((size - pic.width) // 2, (size - pic.height) // 2))
            ok = True
        except Exception:
            ok = False
    if not ok:
        d = ImageDraw.Draw(disc)
        _ctext(d, size / 2, size * 0.34, abbr, _font(int(size * 0.28)), "#0b1220")
    canvas.paste(disc, (0, 0), _circle_mask(size))
    return canvas


def _paste(img, pic, xy):
    img.paste(pic, xy, pic)


def _bar(d, x, y, w, h, ratio, color):
    d.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, fill=PANEL2)
    ratio = max(0.0, min(1.0, ratio or 0))
    if ratio > 0:
        d.rounded_rectangle((x, y, x + max(h, int(w * ratio)), y + h), radius=h // 2, fill=color)


def _header(d, w, right_text=""):
    d.rectangle((0, 0, w, 70), fill=PANEL)
    d.text((30, 17), "BLACK ICE", font=_font(32), fill=ACCENT)
    if right_text:
        f = _font(26)
        d.text((w - 30 - d.textlength(right_text, font=f), 21), right_text, font=f, fill=MUTED)


def _png(img):
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _hit_color(ratio):
    return GREEN if ratio >= 0.6 else YELLOW if ratio >= 0.4 else RED


def card_match(away, home, away_logo, home_logo, scores, date_label):
    w, h = 1080, 620
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    _header(d, w, date_label)
    _paste(img, _logo(away_logo, away, 260), (110, 120))
    _paste(img, _logo(home_logo, home, 260), (710, 120))
    _ctext(d, 240, 400, NAMES[away], _fit(d, NAMES[away], 40, 380), WHITE)
    _ctext(d, 840, 400, NAMES[home], _fit(d, NAMES[home], 40, 380), WHITE)
    _ctext(d, 240, 450, "EXTÉRIEUR", _font(22), MUTED)
    _ctext(d, 840, 450, "DOMICILE", _font(22), MUTED)
    _ctext(d, 540, 170, "@", _font(80), ACCENT)
    wins = {away: 0, home: 0}
    for g in scores:
        winner = g["away"] if g["away_score"] > g["home_score"] else g["home"]
        if winner in wins:
            wins[winner] += 1
    _ctext(d, 540, 275, "H2H", _font(26), MUTED)
    _ctext(d, 540, 310, f"{wins[away]} - {wins[home]}" if scores else "N/D", _font(72), WHITE)
    d.rounded_rectangle((40, 490, w - 40, 595), radius=18, fill=PANEL)
    if scores:
        recent = sorted(scores, key=lambda g: g.get("date") or "", reverse=True)[:3]
        for i, g in enumerate(recent):
            line = f"{format_date(g['date'])}   {g['home']} {g['home_score']} - {g['away_score']} {g['away']}"
            d.text((70, 502 + i * 30), line, font=_font(24), fill=WHITE)
    else:
        d.text((70, 530), "Aucune confrontation récente trouvée", font=_font(26), fill=MUTED)
    return _png(img)


def card_player(name, team, category_title, photo, logo, big, big_ratio, sub, forms, prob_text=None):
    w, h = 1080, 700
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    _header(d, w, clean_label(category_title))
    _paste(img, _avatar(photo, name, 300), (40, 110))
    _paste(img, _logo(logo, team, 90), (950, 100))
    d.text((380, 105), name, font=_fit(d, name, 54, 550), fill=WHITE)
    d.text((380, 175), NAMES.get(team, team), font=_font(30), fill=MUTED)
    d.text((380, 235), big, font=_font(120), fill=_hit_color(big_ratio) if big_ratio is not None else MUTED)
    d.text((380, 375), sub, font=_font(28), fill=MUTED)
    y = 460 if len(forms) > 2 else 470
    step = 62 if len(forms) > 2 else 70
    for label, succ, n in forms:
        d.text((40, y), label, font=_font(30), fill=WHITE)
        ratio = (succ / n) if n else 0
        _bar(d, 230, y + 6, 560, 30, ratio, _hit_color(ratio) if n else PANEL2)
        d.text((810, y), f"{succ}/{n}" if n else "N/D", font=_font(30), fill=WHITE)
        y += step
    if prob_text:
        _ctext(d, 540, 655, prob_text, _fit(d, prob_text, 24, 1000, 14), MUTED)
    return _png(img)


def card_top(away, home, title, rows, mode_label="H2H"):
    w = 1080
    h = 100 + len(rows) * 125 + 20
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    _header(d, w, f"{away} @ {home}")
    _ctext(d, w / 2, 82, clean_label(title) + f" - TOP 5 {mode_label}", _font(30), ACCENT)
    suffix = "H2H" if mode_label == "H2H" else "récents"
    y = 130
    for rank, name, team, photo, hit, n, succ, avg_text in rows:
        d.rounded_rectangle((30, y, w - 30, y + 110), radius=20, fill=PANEL)
        d.text((50, y + 32), str(rank), font=_font(44), fill=ACCENT)
        _paste(img, _avatar(photo, name, 90), (110, y + 10))
        d.text((225, y + 14), name, font=_fit(d, name, 36, 420), fill=WHITE)
        line = f"{team} | {n} matchs {suffix} | moy. {avg_text}"
        d.text((225, y + 62), line, font=_fit(d, line, 22, 450, 13), fill=MUTED)
        _bar(d, 690, y + 38, 220, 26, hit, _hit_color(hit))
        d.text((925, y + 30), f"{hit * 100:.0f}%", font=_font(36), fill=WHITE)
        y += 125
    return _png(img)


def card_sim(away, home, away_logo, home_logo, m):
    w, h = 1080, 560
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    _header(d, w, "MATCH SIMULATOR")
    _paste(img, _logo(away_logo, away, 170), (60, 100))
    _paste(img, _logo(home_logo, home, 170), (850, 100))
    _ctext(d, 145, 285, NAMES[away], _fit(d, NAMES[away], 28, 250), WHITE)
    _ctext(d, 935, 285, NAMES[home], _fit(d, NAMES[home], 28, 250), WHITE)
    _ctext(d, 540, 110, "BUTS ATTENDUS", _font(24), MUTED)
    _ctext(d, 540, 150, f"{m['away_avg']:.1f} - {m['home_avg']:.1f}".replace(".", ","), _font(80), WHITE)
    _ctext(d, 540, 255, f"5+ buts {m['over_4_5'] * 100:.0f}%   6+ {m['over_5_5'] * 100:.0f}%   7+ {m['over_6_5'] * 100:.0f}%", _font(24), MUTED)
    bx, by, bw, bh = 60, 345, 960, 54
    aw = int(bw * m["away_win"])
    d.rounded_rectangle((bx, by, bx + bw, by + bh), radius=27, fill=GREEN)
    d.rounded_rectangle((bx, by, bx + max(bh, aw), by + bh), radius=27, fill=ACCENT)
    d.text((bx + 20, by + 8), f"{m['away_win'] * 100:.0f}%", font=_font(34), fill="#0b1220")
    t = f"{m['home_win'] * 100:.0f}%"
    d.text((bx + bw - 20 - d.textlength(t, font=_font(34)), by + 8), t, font=_font(34), fill="#0b1220")
    _ctext(d, 540, 410, "PROBABILITÉ DE VICTOIRE (PROLONGATION INCLUSE)", _font(20), MUTED)
    d.rounded_rectangle((40, 455, w - 40, 530), radius=20, fill=PANEL)
    _ctext(d, 540, 475, f"Égalité après 60 min : {m['reg_tie'] * 100:.0f} %", _font(28), WHITE)
    return _png(img)

# =====================================================
# EFFECTIFS / JOURNAUX NHL
# =====================================================
async def roster(team):
    async def build():
        data = await get_json(f"{API_WEB}/roster/{team}/current")
        entries = (data.get("forwards") or []) + (data.get("defensemen") or [])
        players = []
        for p in entries:
            pid = p.get("id") or p.get("playerId")
            if pid:
                players.append({"id": pid, "name": player_name(p), "team": team, "position": p.get("positionCode", ""), "headshot": p.get("headshot")})
        return players
    try:
        return await cached(("roster", team, current_season()), CACHE_LONG, build)
    except Exception as error:
        log.warning("Roster indisponible %s : %s", team, error)
        return []


def normalize_game(game):
    opponent, team = game.get("opponentAbbrev"), game.get("teamAbbrev")
    if isinstance(opponent, dict): opponent = opponent.get("default")
    if isinstance(team, dict): team = team.get("default")
    flag = game.get("homeRoadFlag")
    return {
        "date": (game.get("gameDate") or "")[:10], "opp": opponent, "team_abbrev": team,
        "home": True if flag == "H" else False if flag == "R" else None,
        "game_id": str(game.get("gameId") or game.get("id")) if game.get("gameId") or game.get("id") else None,
        "goals": number(game.get("goals")), "assists": number(game.get("assists")), "points": number(game.get("points")),
        "shots": number(game.get("shots")), "hits": number(game.get("hits")), "blocks": number(game.get("blockedShots")),
        "pim": number(game.get("pim")), "toi": parse_toi(game.get("toi") or game.get("timeOnIce")),
        "powerplay": number(game.get("powerPlayPoints")), "powerplay_goals": number(game.get("powerPlayGoals")),
        "powerplay_assists": number(game.get("powerPlayAssists")), "faceoff_pct": number(game.get("faceoffWinningPctg")),
    }


async def game_log(pid):
    async def build():
        results = await asyncio.gather(*[get_json(f"{API_WEB}/player/{pid}/game-log/{season}/2") for season in seasons()], return_exceptions=True)
        rows, errors = [], []
        for result in results:
            if isinstance(result, Exception):
                errors.append(result)
                continue
            rows.extend(normalize_game(g) for g in result.get("gameLog", []))
        if not rows and errors:
            raise RuntimeError(f"Journaux joueurs indisponibles : {errors}")
        unique = {}
        for row in rows:
            key = row["game_id"] or (row["date"], row["opp"], row["home"])
            unique[key] = row
        return list(unique.values())
    return await cached(("log", pid, tuple(seasons())), CACHE_LONG, build)

# =====================================================
# H2H / PLAY-BY-PLAY
# =====================================================
async def head_to_head_games(away, home):
    async def fetch_schedule(team, season):
        return await cached(("schedule", team, season), CACHE_LONG, lambda: get_json(f"{API_WEB}/club-schedule-season/{team}/{season}"))
    results = await asyncio.gather(*[fetch_schedule(t, s) for s in seasons() for t in (away, home)], return_exceptions=True)
    if all(isinstance(x, Exception) for x in results):
        raise RuntimeError("Aucun calendrier NHL disponible.")
    found = {}
    for data in results:
        if isinstance(data, Exception):
            log.warning("Calendrier H2H indisponible : %s", data)
            continue
        for game in data.get("games", []):
            at, ht = game.get("awayTeam") or {}, game.get("homeTeam") or {}
            aa, hh = at.get("abbrev"), ht.get("abbrev")
            try: game_type = int(game.get("gameType", 0))
            except (TypeError, ValueError): continue
            if {aa, hh} != {away, home} or game_type != 2 or game.get("gameState") not in ("FINAL", "OFF"):
                continue
            gid, ascore, hscore = game.get("id"), at.get("score"), ht.get("score")
            if gid is None or ascore is None or hscore is None: continue
            found[str(gid)] = {"id": str(gid), "date": (game.get("gameDate") or "")[:10], "away": aa, "home": hh, "away_score": ascore, "home_score": hscore}
    matches = sorted(found.values(), key=lambda g: (g.get("date") or "", g.get("id") or ""), reverse=True)
    log.info("H2H %s / %s : %s matchs", away, home, len(matches))
    return matches


def empty_pbp():
    return {"shots": 0, "missed": 0, "hits": 0, "blocks": 0, "faceoff_wins": 0, "faceoff_losses": 0, "pim": 0}


async def play_by_play(game_id):
    async def build():
        data = await get_json(f"{API_WEB}/gamecenter/{game_id}/play-by-play")
        players = {}
        def get_stats(pid):
            if pid is None: return None
            pid = str(pid)
            if pid not in players: players[pid] = empty_pbp()
            return players[pid]
        for play in data.get("plays", []):
            event = (play.get("typeDescKey") or "").lower()
            d = play.get("details") or {}
            if event == "shot-on-goal":
                p = get_stats(d.get("shootingPlayerId"))
                if p: p["shots"] += 1
            elif event == "missed-shot":
                p = get_stats(d.get("shootingPlayerId"))
                if p: p["missed"] += 1
            elif event == "hit":
                p = get_stats(d.get("hittingPlayerId"))
                if p: p["hits"] += 1
            elif event == "blocked-shot":
                p = get_stats(d.get("blockingPlayerId"))
                if p: p["blocks"] += 1
            elif event == "faceoff":
                w = get_stats(d.get("winningPlayerId")); l = get_stats(d.get("losingPlayerId"))
                if w: w["faceoff_wins"] += 1
                if l: l["faceoff_losses"] += 1
            elif event == "penalty":
                p = get_stats(d.get("committedByPlayerId") or d.get("servedByPlayerId"))
                if p: p["pim"] += number(d.get("duration")) or 0
        return players
    return await cached(("pbp", str(game_id)), 12 * 3600, build)


async def all_pbp(scores):
    results = await asyncio.gather(*[play_by_play(g["id"]) for g in scores], return_exceptions=True)
    return {g["id"]: (None if isinstance(r, Exception) else r) for g, r in zip(scores, results)}


def match_player_games(player, scores, pbp_data):
    allowed_ids = {str(g["id"]): g for g in scores}
    allowed_dates = {}
    for g in scores:
        if g.get("date"): allowed_dates.setdefault(g["date"], []).append(g)
    player["vs"] = []
    for game in player.get("log", []):
        matched = None
        if game.get("game_id"):
            matched = allowed_ids.get(str(game["game_id"]))
            if not matched: continue
        else:
            for candidate in allowed_dates.get(game.get("date"), []):
                if player["team"] in (candidate["away"], candidate["home"]) and player["opp"] in (candidate["away"], candidate["home"]):
                    matched = candidate
                    break
        if not matched or {matched["away"], matched["home"]} != {player["team"], player["opp"]}: continue
        if game.get("opp") != player["opp"]: continue
        if game.get("team_abbrev") and game["team_abbrev"] != player["team"]: continue
        if game.get("home") is None or game["home"] != (player["team"] == matched["home"]): continue
        row = dict(game)
        pbp = pbp_data.get(str(matched["id"]))
        row["pbp"] = None if pbp is None else pbp.get(str(player["id"]), empty_pbp())
        row["match_id"], row["match_score"] = str(matched["id"]), matched
        player["vs"].append(row)
    unique = {g["match_id"]: g for g in player["vs"]}
    player["vs"] = sorted(unique.values(), key=lambda g: g.get("date") or "", reverse=True)

# =====================================================
# MARCHÉS / STATISTIQUES
# =====================================================
CATEGORY_INFO = {
    "points": ("⭐ POINTS", "pts", 0), "goals": ("🥅 BUTS", "buts", 0), "assists": ("🎯 PASSES DÉCISIVES", "passes", 0),
    "shots": ("🏒 TIRS CADRÉS", "tirs", 0), "missed": ("🎯 TIRS MANQUÉS", "tirs", 0), "hits": ("💥 HITS", "hits", 0),
    "blocks": ("🛡️ TIRS BLOQUÉS", "blocs", 0), "faceoffs": ("🔵 MISES EN JEU", "%", 1), "toi": ("⏱️ TEMPS DE GLACE", "secondes", 0),
    "powerplay": ("⚡ POWER PLAY", "points PP", 0), "penalties": ("🚨 PÉNALITÉS", "minutes", 0)
}


def metric_value(game, category):
    pbp = game.get("pbp")
    if category in ("points", "goals", "assists", "toi"):
        return game.get(category)
    if category == "shots":
        return game.get("shots") if game.get("shots") is not None else (pbp.get("shots") if pbp is not None else None)
    if category == "missed": return pbp.get("missed") if pbp is not None else None
    if category == "hits": return game.get("hits") if game.get("hits") is not None else (pbp.get("hits") if pbp is not None else None)
    if category == "blocks": return game.get("blocks") if game.get("blocks") is not None else (pbp.get("blocks") if pbp is not None else None)
    if category == "penalties": return game.get("pim") if game.get("pim") is not None else (pbp.get("pim") if pbp is not None else None)
    if category == "powerplay":
        value = game.get("powerplay")
        if value is not None: return value
        g, a = game.get("powerplay_goals"), game.get("powerplay_assists")
        return g + a if g is not None and a is not None else None
    if category == "faceoffs":
        if pbp is not None:
            total = pbp.get("faceoff_wins", 0) + pbp.get("faceoff_losses", 0)
            if total: return pbp.get("faceoff_wins", 0) / total
        value = game.get("faceoff_pct")
        return value / 100 if value is not None and value > 1 else value
    return None


def calculate_values(games, category):
    vals = [metric_value(g, category) for g in games]
    vals = [v for v in vals if v is not None]
    if not vals: return None
    threshold = 0.5 if category == "faceoffs" else 1
    successes = sum(v >= threshold for v in vals)
    return {"n": len(vals), "total": sum(vals), "average": sum(vals) / len(vals), "successes": successes, "hit": successes / len(vals)}


def calculate_player(player, category):
    return calculate_values(player.get("vs", []), category)


def recent_player_games(player, limit):
    games = [g for g in player.get("log", []) if g.get("date")]
    games.sort(key=lambda g: g["date"], reverse=True)
    return games[:limit]


def calculate_recent(player, category, limit):
    return calculate_values(recent_player_games(player, limit), category)


def form_stats(player, category):
    """Algo 2 : forme récente (10 derniers matchs pondérés 60 %, 20 derniers 40 %)."""
    r10 = calculate_recent(player, category, 10)
    if not r10 or r10["n"] < 5:
        return None
    r20 = calculate_recent(player, category, 20)
    hit20 = r20["hit"] if r20 else r10["hit"]
    stats = dict(r10)
    stats["r20"] = r20
    stats["score"] = 0.6 * r10["hit"] + 0.4 * hit20
    return stats


def recent_form_text(player, category, limit):
    result = calculate_recent(player, category, limit)
    if not result:
        return f"Forme {limit} matchs : données insuffisantes"
    return (f"Forme {limit} matchs : {result['successes']}/{result['n']} "
            f"réussites ({result['hit'] * 100:.0f} %) — moyenne {fmt(result['average'], 2)}")


def performance_text(value, category):
    if category == "toi": return fmt_minutes(value)
    if category == "faceoffs": return fmt_percent(value)
    labels = {"penalties": "min", "powerplay": "points PP", "goals": "but(s)", "assists": "passe(s)", "points": "point(s)", "shots": "tir(s) cadré(s)", "missed": "tir(s) manqué(s)", "hits": "hit(s)", "blocks": "bloc(s)"}
    return f"{fmt(value, 0)} {labels.get(category, '')}".strip()

# =====================================================
# PROBABILITÉ INDICATIVE / FIABILITÉ DE L'ÉCHANTILLON
# =====================================================
def smoothed_probability(successes, n, base=0.5):
    """Probabilité lissée : tire les petits échantillons vers le taux moyen du marché."""
    return (successes + base * PRIOR_STRENGTH) / (n + PRIOR_STRENGTH) if n > 0 else None


def betting_estimate(player, market, algo="h2h"):
    if market not in BASE_RATE: return None
    base = BASE_RATE[market]
    if algo == "form":
        r10 = calculate_recent(player, market, 10)
        r20 = calculate_recent(player, market, 20)
        p10 = smoothed_probability(r10["successes"], r10["n"], base) if r10 else None
        p20 = smoothed_probability(r20["successes"], r20["n"], base) if r20 else None
        if p10 is not None and p20 is not None: probability = .60 * p10 + .40 * p20
        else: probability = p10 if p10 is not None else p20
        return {"probability": probability, "recent": r10, "h2h": r20}
    recent = calculate_recent(player, market, 10)
    h2h = calculate_player(player, market)
    p_recent = smoothed_probability(recent["successes"], recent["n"], base) if recent else None
    p_h2h = smoothed_probability(h2h["successes"], h2h["n"], base) if h2h else None
    if p_recent is not None and p_h2h is not None: probability = .70 * p_recent + .30 * p_h2h
    else: probability = p_recent if p_recent is not None else p_h2h
    return {"probability": probability, "recent": recent, "h2h": h2h}


def sample_label(estimate, algo):
    nr = estimate["recent"]["n"] if estimate.get("recent") else 0
    second = estimate["h2h"]["n"] if estimate.get("h2h") else 0
    if algo == "form":
        label = "bonne" if second >= 18 else "moyenne" if second >= 10 else "faible"
        return f"{label} ({second} matchs analysés)"
    label = "bonne" if (second >= 4 and nr >= 10) else "moyenne" if (second >= 2 and nr >= 8) else "faible"
    return f"{label} ({nr} derniers matchs + {second} H2H)"

# =====================================================
# CONTEXTE DU MATCH : LIGNE, DÉFENSE, BACK-TO-BACK
# =====================================================
def line_label(p):
    return ("Paire " if p.get("position") == "D" else "Ligne ") + str(p.get("line"))


def defense_label(ga):
    return "perméable" if ga >= 3.4 else "solide" if ga <= 2.6 else "dans la moyenne"


def assign_lines(players):
    """Estime ligne / paire de chaque joueur d'après son temps de glace moyen (10 derniers matchs)."""
    for team in {p["team"] for p in players}:
        grp = [p for p in players if p["team"] == team]
        for p in grp:
            tois = [g["toi"] for g in recent_player_games(p, 10) if g.get("toi")]
            p["avg_toi"] = sum(tois) / len(tois) if tois else None
            dates = [g["date"] for g in p.get("log", []) if g.get("date")]
            p["last_game"] = max(dates) if dates else None
        lasts = [p["last_game"] for p in grp if p["last_game"]]
        if not lasts:
            continue
        ref = ddate.fromisoformat(max(lasts))
        for is_def in (False, True):
            elig = [p for p in grp if (p.get("position") == "D") == is_def and p["avg_toi"] and p["last_game"]
                    and (ref - ddate.fromisoformat(p["last_game"])).days <= 10]
            elig.sort(key=lambda p: -p["avg_toi"])
            for i, p in enumerate(elig):
                p["line"] = min(i // (2 if is_def else 3) + 1, 3 if is_def else 4)


async def match_context(away, home, mdate):
    async def build():
        res = {}
        for team in (away, home):
            games = await team_recent_results(team, 30)
            last10 = games[:10]
            res[team] = {
                "b2b": bool(mdate) and any(g["date"] == shift_day(mdate, -1) for g in games),
                "ga10": (sum(g["ga"] for g in last10) / len(last10)) if len(last10) >= 5 else None,
            }
        return res
    return await cached(("ctx", away, home, mdate or "none"), 3 * 3600, build)


async def safe_context(away, home, mdate):
    try:
        return await match_context(away, home, mdate)
    except Exception as e:
        log.warning("Contexte %s @ %s : %s", away, home, e)
        return None


def played_recently(player, ref_date, days=7):
    last = player.get("last_game")
    if not last:
        return False
    try:
        return (ddate.fromisoformat(ref_date) - ddate.fromisoformat(last)).days <= days
    except ValueError:
        return False


def context_factor(player, info):
    """Coefficient appliqué à la probabilité selon la ligne, la défense adverse et les back-to-back."""
    f = 1.0
    line = player.get("line")
    if line:
        f *= {1: 1.04, 2: 1.0, 3: 0.94, 4: 0.90}.get(line, 1.0)
    if info:
        opp, own = info.get(player.get("opp")), info.get(player.get("team"))
        if opp and opp.get("ga10") is not None:
            f *= max(0.90, min(1.12, 1 + 0.07 * (opp["ga10"] - 3.0)))
        if opp and opp.get("b2b"):
            f *= 1.03
        if own and own.get("b2b"):
            f *= 0.96
    return f


def context_lines(player, info):
    out = []
    if player.get("line") and player.get("avg_toi"):
        out.append(f"{line_label(player)} · temps de glace moyen {fmt_minutes(player['avg_toi'])} (10 derniers)")
    last = player.get("last_game")
    if last:
        gap = (datetime.now(TZ).date() - ddate.fromisoformat(last)).days
        if gap >= 8:
            out.append(f"⚠️ Dernier match il y a {gap} jours : absence possible (blessures non fournies par l'API NHL)")
    if info:
        opp, own = info.get(player.get("opp")), info.get(player.get("team"))
        if opp:
            if opp.get("ga10") is not None:
                out.append(f"Défense adverse : {fmt(opp['ga10'], 1)} buts encaissés/match (10 derniers) — {defense_label(opp['ga10'])}")
            if opp.get("b2b"):
                out.append("⚠️ Adversaire en back-to-back")
        if own and own.get("b2b"):
            out.append(f"⚠️ {NAMES.get(player.get('team'), player.get('team'))} joue en back-to-back")
    return out


def pick_info(player, info):
    parts = []
    if player.get("line") and player.get("avg_toi"):
        parts.append(f"{line_label(player)} · {fmt_minutes(player['avg_toi'])}")
    if info:
        opp, own = info.get(player.get("opp")), info.get(player.get("team"))
        if opp and opp.get("ga10") is not None:
            parts.append(f"défense adv. {fmt(opp['ga10'], 1)} b/m ({defense_label(opp['ga10'])})")
        if opp and opp.get("b2b"):
            parts.append("adv. en back-to-back")
        if own and own.get("b2b"):
            parts.append("⚠️ back-to-back")
    return " · ".join(parts)

# =====================================================
# AFFICHAGE DES FICHES JOUEURS
# =====================================================
def format_h2h_scores(scores):
    out = ["📊 <b>DERNIÈRES CONFRONTATIONS H2H</b>", "<i>Domicile + extérieur</i>", ""]
    for g in sorted(scores, key=lambda x: x.get("date") or "", reverse=True)[:H2H_SCORE_COUNT]:
        hn = NAMES.get(g["home"], g["home"]); an = NAMES.get(g["away"], g["away"])
        out.append(f"{format_date(g.get('date'))} — <b>{esc(hn)} {g['home_score']}-{g['away_score']} {esc(an)}</b>")
    if not scores: out.append("Aucun score disponible.")
    out.append("")
    return out


def format_player_history(player, category):
    out = ["<b>📅 HISTORIQUE H2H MATCH PAR MATCH</b>"]
    games = sorted(player.get("vs", []), key=lambda g: g.get("date") or "", reverse=True)
    valid = []
    for game in games:
        value = metric_value(game, category)
        match = game.get("match_score") or {}
        if value is None or not match: continue
        valid.append(value)
        success = value >= (.5 if category == "faceoffs" else 1)
        loc = "DOM" if game.get("home") else "EXT"
        out.append(f"{format_date(game.get('date'))} [{loc}] — {performance_text(value, category)} {'✅' if success else '❌'}")
        hn = NAMES.get(match.get("home"), match.get("home", "?")); an = NAMES.get(match.get("away"), match.get("away", "?"))
        out.append(f"<i>Score : {esc(hn)} {match.get('home_score', '?')}-{match.get('away_score', '?')} {esc(an)}</i>")
    if not valid:
        out.append("Aucune statistique individuelle vérifiable.")
        return out
    successes = sum(v >= (.5 if category == "faceoffs" else 1) for v in valid)
    out.extend(["", f"<b>Bilan : {successes}/{len(valid)} réussites ({successes / len(valid) * 100:.0f} %)</b>"])
    avg = sum(valid) / len(valid)
    out.append(f"Moyenne : {fmt_minutes(avg) if category == 'toi' else fmt_percent(avg) if category == 'faceoffs' else fmt(avg, 2)} par match")
    return out


def format_recent_history(player, category, limit=20):
    out = [f"<b>📅 HISTORIQUE DES {limit} DERNIERS MATCHS</b>"]
    valid = []
    for game in recent_player_games(player, limit):
        value = metric_value(game, category)
        if value is None: continue
        valid.append(value)
        success = value >= (.5 if category == "faceoffs" else 1)
        loc = "DOM" if game.get("home") else "EXT"
        out.append(f"{format_date(game.get('date'))} [{loc}] vs {game.get('opp') or '?'} — {performance_text(value, category)} {'✅' if success else '❌'}")
    if not valid:
        out.append("Aucune statistique individuelle vérifiable.")
        return out
    successes = sum(v >= (.5 if category == "faceoffs" else 1) for v in valid)
    out.extend(["", f"<b>Bilan : {successes}/{len(valid)} réussites ({successes / len(valid) * 100:.0f} %)</b>"])
    avg = sum(valid) / len(valid)
    out.append(f"Moyenne : {fmt_minutes(avg) if category == 'toi' else fmt_percent(avg) if category == 'faceoffs' else fmt(avg, 2)} par match")
    return out


def format_player_profile(away, home, player, category, match_date, scores, history=True, algo="h2h", info=None):
    title, unit, decimals = CATEGORY_INFO[category]
    out = [f"<b>{esc(player['name'])}</b> — {esc(NAMES.get(player['team'], player['team']))}", f"<b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>", f"<b>{title}</b> · <i>{ALGO_LABELS[algo]}</i>", ""]
    if algo == "form":
        out.extend(["<b>🔥 FORME RÉCENTE (TOUS ADVERSAIRES)</b>", recent_form_text(player, category, 5), recent_form_text(player, category, 10), recent_form_text(player, category, 20), ""])
    else:
        h2h = calculate_player(player, category)
        if h2h:
            out.extend(["<b>📊 STATISTIQUES H2H</b>", f"Matchs analysés : {h2h['n']}", f"Réussite : {h2h['successes']}/{h2h['n']} ({h2h['hit'] * 100:.0f} %)"])
            if category == "toi": avg = fmt_minutes(h2h["average"])
            elif category == "faceoffs": avg = fmt_percent(h2h["average"])
            else: avg = f"{fmt(h2h['average'], decimals)} {unit}"
            out.extend([f"Total H2H : {fmt(h2h['total'], decimals)} {unit}", f"Moyenne H2H : {avg}"])
        else:
            out.append("Aucune statistique H2H vérifiable pour ce joueur sur ce marché.")
        out.extend(["", "<b>🔥 FORME GÉNÉRALE (TOUS ADVERSAIRES)</b>", recent_form_text(player, category, 5), recent_form_text(player, category, 10), ""])
    ctx_lines = context_lines(player, info)
    if ctx_lines:
        out.extend(["<b>🧭 CONTEXTE DU MATCH</b>"] + ctx_lines + [""])
    if category in BASE_RATE:
        est = betting_estimate(player, category, algo)
        if est and est.get("probability") is not None:
            out.extend(["<b>📈 ESTIMATION INDICATIVE</b>", f"Probabilité estimée : {fmt_percent(est['probability'])}",
                        f"Fiabilité de l'échantillon : {sample_label(est, algo)}",
                        "<i>Plus l'échantillon est petit, plus l'estimation est incertaine.</i>", ""])
    if history:
        out.extend(format_recent_history(player, category, 20) if algo == "form" else format_player_history(player, category))
    out.extend(["", "<i>Les estimations sont indicatives et ne garantissent aucun résultat. 🔞 Jouez de façon responsable.</i>"])
    return "\n".join(out)

# =====================================================
# AUTRES AFFICHAGES / ANALYSES
# =====================================================
def format_category_top5(away, home, category, ranked, algo="h2h", visible=None):
    title, unit, decimals = CATEGORY_INFO[category]
    mode = "TOP 5 H2H" if algo == "h2h" else "TOP 5 FORME 10/20"
    out = [f"<b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>", f"<b>{title} — {mode}</b>",
           "<i>Choisis un joueur pour ouvrir sa fiche complète.</i>" if visible is None else "", ""]
    if not ranked: return "\n".join(out + ["Aucun joueur avec des statistiques vérifiables."])
    for i, (p, s) in enumerate(ranked, 1):
        if visible is not None and i > visible:
            out.extend([f"🔒 <b>{i}. ••••••••</b>", ""])
            continue
        avg = fmt_minutes(s["average"]) if category == "toi" else fmt_percent(s["average"]) if category == "faceoffs" else f"{fmt(s['average'], decimals)} {unit}"
        if algo == "form":
            r20 = s.get("r20")
            l20 = f"{r20['successes']}/{r20['n']} ({r20['hit'] * 100:.0f} %)" if r20 else "N/D"
            out.extend([f"<b>{i}. {esc(p['name'])}</b> ({p['team']})", f"   10 derniers : {s['successes']}/{s['n']} ({s['hit'] * 100:.0f} %) | 20 derniers : {l20}", f"   Moyenne 10 : {avg}", ""])
        else:
            out.extend([f"<b>{i}. {esc(p['name'])}</b> ({p['team']})", f"   {s['n']} matchs H2H | {s['successes']}/{s['n']} réussites ({s['hit'] * 100:.0f} %)", f"   Moyenne : {avg}", ""])
    return "\n".join(out)


def player_top_keyboard(ranked, algo="h2h"):
    rows = []
    for i, (p, s) in enumerate(ranked):
        tail = f"{s['n']} H2H" if algo == "h2h" else f"{s['n']} derniers"
        rows.append([InlineKeyboardButton(f"{i + 1}. {p['name']} · {s['hit'] * 100:.0f}% ({tail})", callback_data=f"p:{i}")])
    rows.extend([[InlineKeyboardButton("⬅️ Retour aux catégories", callback_data="backcat")], [InlineKeyboardButton("📅 Autres matchs", callback_data="d:0")]])
    return InlineKeyboardMarkup(rows)


def player_profile_keyboard(index, count):
    rows = []
    nav = []
    if index > 0: nav.append(InlineKeyboardButton("⬅️ Précédent", callback_data=f"pn:{index - 1}"))
    if index < count - 1: nav.append(InlineKeyboardButton("Suivant ➡️", callback_data=f"pn:{index + 1}"))
    if nav: rows.append(nav)
    rows.extend([[InlineKeyboardButton("📜 Historique détaillé", callback_data=f"ph:{index}")], [InlineKeyboardButton("📋 Retour au TOP 5", callback_data="backtop")], [InlineKeyboardButton("📊 Autres catégories", callback_data="backcat")], [InlineKeyboardButton("📅 Autres matchs", callback_data="d:0")]])
    return InlineKeyboardMarkup(rows)


def format_teams(away, home, scores):
    stats = {t: {"wins": 0, "losses": 0, "gf": 0, "ga": 0} for t in (away, home)}
    for g in scores:
        aa, hh = g["away"], g["home"]; a, h = g["away_score"], g["home_score"]
        stats[aa]["gf"] += a; stats[aa]["ga"] += h; stats[hh]["gf"] += h; stats[hh]["ga"] += a
        if a > h: stats[aa]["wins"] += 1; stats[hh]["losses"] += 1
        elif h > a: stats[hh]["wins"] += 1; stats[aa]["losses"] += 1
    out = [f"<b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>", "", "<b>📊 BILAN DES ÉQUIPES — H2H</b>", f"Matchs analysés : {len(scores)}", ""]
    for t in (away, home):
        d = stats[t]; out.extend([f"<b>{esc(NAMES[t])} ({t})</b>", f"Victoires : {d['wins']}", f"Défaites : {d['losses']}", f"Buts marqués : {d['gf']}", f"Buts encaissés : {d['ga']}", f"Différence : {d['gf'] - d['ga']}", ""])
    out.extend(format_h2h_scores(scores))
    return "\n".join(out)


def format_form_h2h(away, home, players):
    ranked = []
    for p in players:
        games = sorted(p.get("vs", []), key=lambda g: g.get("date") or "", reverse=True)[:5]
        if games:
            pts = sum(g.get("points") or 0 for g in games); goals = sum(g.get("goals") or 0 for g in games); assists = sum(g.get("assists") or 0 for g in games); shots = sum(g.get("shots") or 0 for g in games)
            ranked.append((p, len(games), pts, goals, assists, shots))
    ranked.sort(key=lambda x: (-x[2], -x[5], -x[1]))
    out = [f"<b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>", "", "<b>📈 FORME H2H DES JOUEURS</b>", "<i>5 dernières confrontations maximum</i>", ""]
    for i, (p, n, pts, g, a, sh) in enumerate(ranked[:TOP], 1): out.extend([f"<b>{i}. {esc(p['name'])}</b> ({p['team']})", f"{n} confrontations H2H", f"{pts} points | {g} buts | {a} passes | {sh} tirs", ""])
    if not ranked: out.append("Aucune forme H2H disponible.")
    return "\n".join(out)

# =====================================================
# CHARGEMENT DES DONNÉES
# =====================================================
async def load_players(away, home):
    rosters = await asyncio.gather(roster(away), roster(home))
    players = []
    for team, opp, entries in ((away, home, rosters[0]), (home, away, rosters[1])):
        for p in entries: players.append({**p, "team": team, "opp": opp, "log": [], "vs": []})
    if not players: raise RuntimeError("Aucun joueur disponible dans les effectifs actuels.")
    sem = asyncio.Semaphore(12)
    async def load(p):
        async with sem:
            try: p["log"] = await game_log(p["id"])
            except Exception as e: log.warning("Journal %s : %s", p["name"], e); p["log"] = []
    await asyncio.gather(*(load(p) for p in players))
    assign_lines(players)
    return players

# =====================================================
# MATCH SIMULATOR — MODÈLE DE POISSON (CALCUL DIRECT)
# =====================================================
async def team_recent_results(team, limit=30):
    async def fetch(season):
        return await cached(
            ("sim-team-schedule", team, season), CACHE_LONG,
            lambda: get_json(f"{API_WEB}/club-schedule-season/{team}/{season}")
        )
    responses = await asyncio.gather(*(fetch(season) for season in seasons()), return_exceptions=True)
    games = {}
    for data in responses:
        if isinstance(data, Exception):
            log.warning("Simulation calendrier %s : %s", team, data)
            continue
        for game in data.get("games", []):
            try:
                if int(game.get("gameType", 0)) != 2:
                    continue
            except (TypeError, ValueError):
                continue
            if game.get("gameState") not in ("FINAL", "OFF"):
                continue
            away = game.get("awayTeam") or {}
            home = game.get("homeTeam") or {}
            if away.get("score") is None or home.get("score") is None:
                continue
            if team not in (away.get("abbrev"), home.get("abbrev")):
                continue
            gid = str(game.get("id") or (game.get("gameDate"), away.get("abbrev"), home.get("abbrev")))
            games[gid] = {
                "date": (game.get("gameDate") or "")[:10],
                "team_home": home.get("abbrev") == team,
                "gf": int(home["score"] if home.get("abbrev") == team else away["score"]),
                "ga": int(away["score"] if home.get("abbrev") == team else home["score"]),
            }
    return sorted(games.values(), key=lambda g: g["date"], reverse=True)[:limit]


def venue_average(games, venue, key, fallback):
    selected = [g[key] for g in games if g["team_home"] is venue]
    return sum(selected) / len(selected) if len(selected) >= 4 else fallback


def estimate_team_goals(team_games, opponent_games, team_is_home):
    if len(team_games) < 8 or len(opponent_games) < 8:
        return None
    own_gf = sum(g["gf"] for g in team_games) / len(team_games)
    opp_ga = sum(g["ga"] for g in opponent_games) / len(opponent_games)
    own_venue_gf = venue_average(team_games, team_is_home, "gf", own_gf)
    opp_venue_ga = venue_average(opponent_games, not team_is_home, "ga", opp_ga)
    return max(0.5, min(6.5, 0.35 * own_gf + 0.35 * own_venue_gf + 0.30 * opp_venue_ga))


def match_probabilities(away_lambda, home_lambda, max_goals=15):
    """Calcul exact (loi de Poisson indépendante) : aucune simulation aléatoire nécessaire."""
    pa = [math.exp(-away_lambda) * away_lambda ** k / math.factorial(k) for k in range(max_goals + 1)]
    ph = [math.exp(-home_lambda) * home_lambda ** k / math.factorial(k) for k in range(max_goals + 1)]
    aw = hw = tie = o4 = o5 = o6 = avg_a = avg_h = total_p = 0.0
    for i, a in enumerate(pa):
        for j, h in enumerate(ph):
            p = a * h
            total_p += p
            avg_a += i * p
            avg_h += j * p
            if i > j: aw += p
            elif j > i: hw += p
            else: tie += p
            t = i + j
            if t >= 5: o4 += p
            if t >= 6: o5 += p
            if t >= 7: o6 += p
    aw, hw, tie, o4, o5, o6, avg_a, avg_h = (x / total_p for x in (aw, hw, tie, o4, o5, o6, avg_a, avg_h))
    home_share = home_lambda / (away_lambda + home_lambda)
    return {"away_win": aw + tie * (1 - home_share), "home_win": hw + tie * home_share, "reg_tie": tie,
            "away_avg": avg_a, "home_avg": avg_h, "over_4_5": o4, "over_5_5": o5, "over_6_5": o6}


async def build_match_simulation(away, home):
    away_games, home_games = await asyncio.gather(team_recent_results(away), team_recent_results(home))
    away_lambda = estimate_team_goals(away_games, home_games, False)
    home_lambda = estimate_team_goals(home_games, away_games, True)
    if away_lambda is None or home_lambda is None:
        raise RuntimeError("Historique insuffisant pour simuler les deux équipes (minimum 8 matchs exploitables chacune).")
    m = match_probabilities(away_lambda, home_lambda)
    head = [f"<b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>"]
    win = ["<b>🏆 VAINQUEUR DU MATCH (prolongation incluse)</b>",
           f"{esc(NAMES[away])} : <b>{fmt_percent(m['away_win'])}</b>",
           f"{esc(NAMES[home])} : <b>{fmt_percent(m['home_win'])}</b>"]
    text = "\n".join(["<b>🧬 MATCH SIMULATOR — MODÈLE DE POISSON</b>"] + head + [
        "<i>Buts attendus calibrés sur les résultats récents domicile/extérieur des deux équipes.</i>", ""] + win + [
        f"Égalité après 60 min : {fmt_percent(m['reg_tie'])}", "",
        "<b>🥅 BUTS ATTENDUS</b>",
        f"{esc(NAMES[away])} : {fmt(m['away_avg'], 2)} buts",
        f"{esc(NAMES[home])} : {fmt(m['home_avg'], 2)} buts", "",
        "<b>📊 TOTAL DE BUTS</b>",
        f"5 buts ou plus : {fmt_percent(m['over_4_5'])}",
        f"6 buts ou plus : {fmt_percent(m['over_5_5'])}",
        f"7 buts ou plus : {fmt_percent(m['over_6_5'])}", "",
        "<i>Modèle probabiliste, pas une prédiction certaine. Il ne tient pas compte des blessures.</i>"])
    caption = "\n".join(["<b>🧬 MATCH SIMULATOR</b>"] + head + [""] + win + [
        f"Buts attendus : {fmt(m['away_avg'], 1)} - {fmt(m['home_avg'], 1)}", "",
        "<i>Probabilités indicatives, aucune garantie.</i>"])
    teaser = "\n".join(["<b>🧬 MATCH SIMULATOR</b>"] + head + [""] + win + [
        "", "🔒 Buts attendus, totaux de buts et détails : réservés aux abonnés."])
    return text, caption, m, teaser


async def build_top_card(away, home, category, ranked, algo):
    photos = await asyncio.gather(*(player_photo(p) for p, _ in ranked))
    title, unit, decimals = CATEGORY_INFO[category]
    rows = []
    for i, ((p, s), photo) in enumerate(zip(ranked, photos), 1):
        avg = fmt_minutes(s["average"]) if category == "toi" else fmt_percent(s["average"]) if category == "faceoffs" else fmt(s["average"], decimals)
        if algo == "form":
            r20 = s.get("r20")
            avg = f"{avg} | 20m {r20['hit'] * 100:.0f}%" if r20 else avg
        rows.append((i, p["name"], p["team"], photo, s["hit"], s["n"], s["successes"], avg))
    return await asyncio.to_thread(card_top, away, home, title, rows, "H2H" if algo == "h2h" else "FORME")


async def prepare_form_category(away, home, category, match_date):
    if category == "missed":
        return {"kind": "text", "text": f"<b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>\n\n🎯 Les tirs manqués ne sont disponibles qu'avec l'<b>Algo 1 · H2H</b> (données play-by-play).\nChange d'algo avec les boutons du menu."}
    players = await load_players(away, home)
    ranked = []
    for p in players:
        s = form_stats(p, category)
        if s:
            ranked.append((p, s))
    ranked.sort(key=lambda x: (-x[1]["score"], -x[1]["average"], -x[1]["n"]))
    ranked = ranked[:TOP]
    card = None
    if ranked:
        try:
            card = await build_top_card(away, home, category, ranked, "form")
        except Exception as e:
            log.warning("Carte TOP forme : %s", e)
    info = await safe_context(away, home, match_date)
    return {"kind": "players", "text": format_category_top5(away, home, category, ranked, "form"), "ranked": ranked, "scores": [], "card": card, "info": info}


async def prepare_category(away, home, category, match_date, algo="h2h"):
    if category == "simulation":
        text, caption, m, teaser = await build_match_simulation(away, home)
        card = None
        try:
            al, hl = await asyncio.gather(team_logo(away), team_logo(home))
            card = await asyncio.to_thread(card_sim, away, home, al, hl, m)
        except Exception as e:
            log.warning("Carte simulation : %s", e)
        return {"kind": "text", "text": text, "caption": caption, "card": card, "teaser": teaser}
    if algo == "form" and category in CATEGORY_INFO:
        return await prepare_form_category(away, home, category, match_date)
    scores = await head_to_head_games(away, home)
    if not scores: return {"kind": "text", "text": f"<b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>\n\nAucune confrontation terminée trouvée entre ces équipes sur les saisons analysées."}
    if category == "teams": return {"kind": "text", "text": format_teams(away, home, scores)}
    players = await load_players(away, home)
    pbp_data = {}
    if category != "form":
        pbp_data = await all_pbp(scores)
    for p in players: match_player_games(p, scores, pbp_data)
    if category == "form": return {"kind": "text", "text": format_form_h2h(away, home, players)}
    ranked = []
    for p in players:
        result = calculate_player(p, category)
        if result: ranked.append((p, result))
    ranked.sort(key=lambda x: (-x[1]["average"], -x[1]["hit"], -x[1]["n"]))
    ranked = ranked[:TOP]
    card = None
    if ranked:
        try:
            card = await build_top_card(away, home, category, ranked, "h2h")
        except Exception as e:
            log.warning("Carte TOP : %s", e)
    info = await safe_context(away, home, match_date)
    return {"kind": "players", "text": format_category_top5(away, home, category, ranked, "h2h"), "ranked": ranked, "scores": scores, "card": card, "info": info}


async def build_player_card(player, category, away, home, match_date, algo="h2h"):
    try:
        photo, logo = await asyncio.gather(player_photo(player), team_logo(player["team"]))
        forms = []
        if algo == "form":
            r10 = calculate_recent(player, category, 10)
            big = f"{r10['successes']}/{r10['n']}" if r10 else "N/D"
            ratio = r10["hit"] if r10 else None
            sub = "réussites sur les 10 derniers" if r10 else "aucune donnée récente"
            limits = (5, 10, 20)
            title = CATEGORY_INFO[category][0] + " FORME"
        else:
            h2h = calculate_player(player, category)
            big = f"{h2h['successes']}/{h2h['n']}" if h2h else "N/D"
            ratio = h2h["hit"] if h2h else None
            sub = "réussites H2H" if h2h else "aucune donnée H2H"
            limits = (5, 10)
            title = CATEGORY_INFO[category][0]
        for limit in limits:
            r = calculate_recent(player, category, limit)
            forms.append((f"Forme {limit}", r["successes"] if r else 0, r["n"] if r else 0))
        prob_text = None
        if category in BASE_RATE:
            est = betting_estimate(player, category, algo)
            if est and est.get("probability") is not None:
                prob_text = f"Probabilité estimée indicative : {est['probability'] * 100:.0f} % · échantillon {sample_label(est, algo).split(' ')[0]}"
        return await asyncio.to_thread(card_player, player["name"], player["team"], title, photo, logo, big, ratio, sub, forms, prob_text)
    except Exception as e:
        log.warning("Carte joueur : %s", e)
        return None

# =====================================================
# PICKS DU JOUR (ALGO 1 + ALGO 2 + CONTEXTE) / HISTORIQUE / BACKTEST
# =====================================================
PICKS_LOCK = asyncio.Lock()


async def generate_picks():
    """Combine l'Algo 1 (H2H + forme) et l'Algo 2 (forme 10/20), ajuste avec le contexte, enregistre en base."""
    day = today_key()
    games = await games_for(0)
    cands = []
    for away, home, _hour, gdate in games:
        try:
            players = await load_players(away, home)
            try:
                scores = await head_to_head_games(away, home)
            except Exception:
                scores = []
            for p in players:
                match_player_games(p, scores, {})
        except Exception as e:
            log.warning("Picks %s @ %s : %s", away, home, e)
            continue
        info = await safe_context(away, home, gdate)
        for p in players:
            if not played_recently(p, gdate):
                continue
            factor = context_factor(p, info)
            pinfo = pick_info(p, info)
            for market, _ in PICK_LIMITS:
                r20 = calculate_recent(p, market, 20)
                if not r20 or r20["n"] < 10:
                    continue
                e1 = betting_estimate(p, market, "h2h")
                e2 = betting_estimate(p, market, "form")
                if not e1 or not e2 or e1["probability"] is None or e2["probability"] is None:
                    continue
                p1, p2 = e1["probability"], e2["probability"]
                cands.append({"market": market, "p1": p1, "p2": p2, "prob": min(0.92, (p1 + p2) / 2 * factor),
                              "avg": r20["average"], "n": r20["n"], "pid": p["id"], "name": p["name"],
                              "team": p["team"], "headshot": p.get("headshot"), "away": away, "home": home,
                              "mdate": gdate, "info": pinfo, "both": False})
        await asyncio.sleep(0.3)

    # ✅✅ : joueur présent dans le top 10 des deux algos
    for market, _ in PICK_LIMITS:
        pool = [c for c in cands if c["market"] == market]
        top1 = {c["pid"] for c in sorted(pool, key=lambda c: -c["p1"])[:PICK_BOTH_TOP]}
        top2 = {c["pid"] for c in sorted(pool, key=lambda c: -c["p2"])[:PICK_BOTH_TOP]}
        for c in pool:
            c["both"] = c["pid"] in top1 and c["pid"] in top2

    chosen, used = [], set()
    for market, limit in PICK_LIMITS:
        pool = sorted([c for c in cands if c["market"] == market],
                      key=lambda c: (-(c["prob"] + (0.03 if c["both"] else 0)), -c["avg"]))
        per_team, rank = {}, 0
        for c in pool:
            if c["pid"] in used or per_team.get(c["team"], 0) >= PICK_TEAM_CAP:
                continue
            rank += 1
            used.add(c["pid"])
            per_team[c["team"]] = per_team.get(c["team"], 0) + 1
            chosen.append((rank, c))
            if rank >= limit:
                break

    db_exec("DELETE FROM picks WHERE day=? AND result IS NULL", (day,))
    now = datetime.now(TZ).isoformat()
    for rank, c in chosen:
        db_exec("""INSERT OR IGNORE INTO picks(day,mdate,away,home,market,rank,player_id,player,team,headshot,prob,n,info,created_at,p1,p2,both)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (day, c["mdate"], c["away"], c["home"], c["market"], rank, c["pid"], c["name"], c["team"], c["headshot"],
             c["prob"], c["n"], c["info"], now, c["p1"], c["p2"], 1 if c["both"] else 0))
    log.info("Picks du jour : %s sélectionnés sur %s candidats", len(chosen), len(cands))
    return len(chosen)


def get_picks(day):
    return db_all("""SELECT market, rank, player, team, away, home, prob, n, info, result, p1, p2, both FROM picks WHERE day=?
        ORDER BY CASE market WHEN 'points' THEN 0 WHEN 'goals' THEN 1 ELSE 2 END, rank""", (day,))


def picks_text(rows, premium):
    out = [f"🏆 <b>PICKS DU JOUR — {format_date(today_key())}</b>",
           "<i>Combinaison de l'Algo 1 (H2H + forme) et de l'Algo 2 (forme 10/20), ajustée par le contexte (ligne, défense adverse, back-to-back). ✅✅ = dans le top 10 des deux algos. Seuls les joueurs ayant joué dans les 7 derniers jours sont retenus.</i>", ""]
    current = None
    for market, rank, player, team, away, home, prob, n, info, result, p1, p2, both in rows:
        if market != current:
            if current is not None:
                out.append("")
            out.append(f"{MARKET_ICON[market]} <b>{MARKET_NAME[market]}</b>")
            current = market
        if not premium and not (market == "points" and rank == 1):
            out.append(f"🔒 {rank}. ••••••••")
            continue
        out.append(f"<b>{rank}. {esc(player)}</b> ({team}) — {away} @ {home} · <b>{prob * 100:.0f} %</b>{' ✅✅' if both else ''}")
        if p1 is not None and p2 is not None:
            out.append(f"   <i>Algo 1 : {p1 * 100:.0f} % · Algo 2 : {p2 * 100:.0f} %</i>")
        if info:
            out.append(f"   <i>{esc(info)}</i>")
    out += ["", "<i>Estimations statistiques indicatives, aucun gain garanti. 🔞 Résultats réels : /historique</i>"]
    return "\n".join(out)


async def send_picks(message, premium):
    day = today_key()
    rows = get_picks(day)
    if not rows:
        try:
            games = await games_for(0)
        except Exception:
            games = []
        if not games:
            await message.reply_text("Aucun match aujourd'hui, donc pas de picks.")
            return
        wait = await message.reply_text("⏳ Génération des picks du jour (environ 1 minute)...")
        try:
            async with PICKS_LOCK:
                if not get_picks(day):
                    await generate_picks()
        except Exception as e:
            log.warning("Génération picks : %s", e)
        rows = get_picks(day)
        try:
            await wait.delete()
        except Exception:
            pass
    if not rows:
        await message.reply_text("Pas assez de données pour proposer des picks aujourd'hui. Réessaie plus tard.")
        return
    chunks = split_telegram_text(picks_text(rows, premium))
    for i, chunk in enumerate(chunks):
        last = i == len(chunks) - 1
        await message.reply_text(chunk, parse_mode=HTML, reply_markup=plans_keyboard() if (last and not premium) else None)


async def cmd_picks(update, ctx):
    if not user_can_access(update, ctx):
        await update.message.reply_text("🔒 BLACK ICE est actuellement fermé.")
        return
    await send_picks(update.message, is_premium(update, ctx))


async def daily_job(ctx):
    try:
        async with PICKS_LOCK:
            n = await generate_picks()
    except Exception as e:
        log.exception("Picks du matin : %s", e)
        await notify_admin(ctx.bot, f"⚠️ Échec de la génération des picks : {e}")
        return
    if not n:
        return
    text = picks_text(get_picks(today_key()), True)
    ids = audience_ids()
    for uid in ids:
        try:
            for chunk in split_telegram_text(text):
                await ctx.bot.send_message(uid, chunk, parse_mode=HTML)
        except Exception as e:
            log.info("Envoi picks impossible à %s : %s", uid, e)
        await asyncio.sleep(0.05)
    await notify_admin(ctx.bot, f"✅ Picks du jour envoyés : {n} picks, {len(ids)} destinataires.")


async def cmd_genpicks(update, ctx):
    if not await admin_required(update, ctx): return
    wait = await update.message.reply_text("⏳ Régénération des picks...")
    try:
        async with PICKS_LOCK:
            n = await generate_picks()
        await wait.edit_text(f"✅ {n} picks enregistrés. Utilise /picks pour les voir.")
    except Exception as e:
        await wait.edit_text(f"⚠️ Échec : {e}")


def season_of(day):
    d = ddate.fromisoformat(day)
    return f"{d.year}{d.year + 1}" if d.month >= 9 else f"{d.year - 1}{d.year}"


async def resolve_picks_job(ctx):
    """Vérifie automatiquement les résultats réels des picks passés (via le journal de matchs du joueur)."""
    rows = db_all("SELECT id, mdate, player_id, market FROM picks WHERE result IS NULL AND mdate<=?", (today_key(),))
    by_player = {}
    for pick_id, mdate, pid, market in rows:
        by_player.setdefault((pid, season_of(mdate)), []).append((pick_id, mdate, market))
    for (pid, season), picks in by_player.items():
        try:
            data = await get_json(f"{API_WEB}/player/{pid}/game-log/{season}/2")
        except Exception as e:
            log.info("Résolution picks %s : %s", pid, e)
            continue
        by_date = {}
        for g in data.get("gameLog", []):
            ng = normalize_game(g)
            by_date[ng["date"]] = ng
        for pick_id, mdate, market in picks:
            game = by_date.get(mdate)
            if game and game.get(market) is not None:
                db_exec("UPDATE picks SET result=? WHERE id=?", (1 if game[market] >= 1 else 0, pick_id))
            elif not game and mdate < shift_day(today_key(), -3):
                db_exec("UPDATE picks SET result=2 WHERE id=?", (pick_id,))  # annulé / non joué
        await asyncio.sleep(0.2)


def compute_backtest(players):
    """Backtest walk-forward : chaque prédiction n'utilise que les matchs précédents."""
    bins = [(0, .30), (.30, .45), (.45, .60), (.60, .75), (.75, 1.01)]
    body, total = [], 0
    for market in ("points", "goals", "assists"):
        base_rate = BASE_RATE[market]
        obs = []
        for p in players:
            rows = sorted([g for g in p.get("log", []) if g.get("date") and g.get(market) is not None], key=lambda g: g["date"])
            hits = [1 if g[market] >= 1 else 0 for g in rows]
            for i in range(20, len(rows)):
                h10, h20 = hits[i - 10:i], hits[i - 20:i]
                p10 = smoothed_probability(sum(h10), len(h10), base_rate)
                p20 = smoothed_probability(sum(h20), len(h20), base_rate)
                obs.append((0.6 * p10 + 0.4 * p20, hits[i]))
        total += len(obs)
        body.append(f"{MARKET_ICON[market]} <b>{MARKET_NAME[market]}</b>")
        if not obs:
            body += ["Données insuffisantes.", ""]
            continue
        for lo, hi in bins:
            sel = [(pp, y) for pp, y in obs if lo <= pp < hi]
            if len(sel) >= 30:
                body.append(f"• prédit {lo * 100:.0f}–{min(hi, 1) * 100:.0f} % → réel {sum(y for _, y in sel) / len(sel) * 100:.0f} % (n={len(sel)})")
        brier = sum((pp - y) ** 2 for pp, y in obs) / len(obs)
        base = sum(y for _, y in obs) / len(obs)
        body += [f"Score de Brier : {brier:.3f} (référence taux moyen : {base * (1 - base):.3f})", ""]
    head = ["<b>🧪 BACKTEST DU MODÈLE (forme 10/20)</b>",
            f"<i>Prédictions faites match par match avec uniquement les données précédentes — {total} observations. Mis à jour le {datetime.now(TZ).strftime('%d/%m/%Y')}.</i>", ""]
    return "\n".join(head + body)


async def cmd_backtest(update, ctx):
    if not await admin_required(update, ctx): return
    wait = await update.message.reply_text("⏳ Backtest en cours (chargement des historiques)...")
    try:
        sample, seen = [], set()
        for offset in range(4):
            for away, home, _, _ in await games_for(offset):
                if (away, home) not in seen and len(sample) < 4:
                    seen.add((away, home))
                    sample.append((away, home))
        if not sample:
            await wait.edit_text("Aucun match à venir pour échantillonner des joueurs.")
            return
        players = []
        for away, home in sample:
            players += await load_players(away, home)
        text = await asyncio.to_thread(compute_backtest, players)
        set_db_setting("backtest", text)
        await wait.edit_text("✅ Backtest enregistré, visible dans /historique.")
        for chunk in split_telegram_text(text):
            await update.message.reply_text(chunk, parse_mode=HTML)
    except Exception as e:
        log.exception("Backtest : %s", e)
        await wait.edit_text(f"⚠️ Échec du backtest : {e}")


async def cmd_historique(update, ctx):
    since = shift_day(today_key(), -30)
    rows = db_all("""SELECT market, SUM(CASE WHEN result=1 THEN 1 ELSE 0 END), SUM(CASE WHEN result=0 THEN 1 ELSE 0 END)
        FROM picks WHERE day>=? AND result IN (0,1) GROUP BY market""", (since,))
    out = ["📈 <b>HISTORIQUE DES PICKS — 30 DERNIERS JOURS</b>", "<i>Résultats vérifiés automatiquement, sans tri : tous les picks publiés sont comptés.</i>", ""]
    stats = {m: (w, l) for m, w, l in rows}
    if not stats:
        out.append("Pas encore de picks terminés.")
    for market in ("points", "goals", "assists"):
        if market in stats:
            w, l = stats[market]
            out.append(f"{MARKET_ICON[market]} {MARKET_NAME[market]} : <b>{w}/{w + l}</b> ({w / (w + l) * 100:.0f} %)")
    both = db_one("""SELECT COALESCE(SUM(CASE WHEN result=1 THEN 1 ELSE 0 END),0), COALESCE(SUM(CASE WHEN result=0 THEN 1 ELSE 0 END),0)
        FROM picks WHERE day>=? AND result IN (0,1) AND both=1""", (since,))
    if both and (both[0] + both[1]):
        out.append(f"✅✅ Picks validés par les 2 algos : <b>{both[0]}/{both[0] + both[1]}</b> ({both[0] / (both[0] + both[1]) * 100:.0f} %)")
    recent = db_all("SELECT day, player, market, result FROM picks WHERE result IN (0,1) ORDER BY day DESC, id DESC LIMIT 10")
    if recent:
        out += ["", "<b>Derniers résultats</b>"]
        for day, player, market, result in recent:
            out.append(f"{'✅' if result == 1 else '❌'} {format_date(day)} — {esc(player)} ({MARKET_ICON[market]} 1+)")
    backtest = db_setting("backtest")
    if backtest:
        out += ["", backtest]
    out += ["", "<i>Les performances passées ne garantissent pas les résultats futurs. 🔞</i>"]
    for chunk in split_telegram_text("\n".join(out)):
        await update.message.reply_text(chunk, parse_mode=HTML)

# =====================================================
# CATÉGORIES / CALENDRIER
# =====================================================
MAIN_CATS = [("⭐ Points", "points"), ("🥅 Buts", "goals"), ("🎯 Passes", "assists"),
             ("🏒 Tirs cadrés", "shots"), ("💥 Hits", "hits"), ("🛡️ Tirs bloqués", "blocks")]
MORE_CATS = [("🔵 Mises en jeu", "faceoffs"), ("⏱️ Temps de glace", "toi"), ("⚡ Power Play", "powerplay"),
             ("🚨 Pénalités", "penalties"), ("🎯 Tirs manqués", "missed"),
             ("📈 Forme 5 H2H", "form"), ("📊 Bilan équipes", "teams")]
VALID_CATS = {k for _, k in MAIN_CATS + MORE_CATS} | {"simulation"}


def category_keyboard(away, home, match_date=None, algo="h2h", more=False):
    dp = match_date or "none"
    rows = [[
        InlineKeyboardButton(("✅ " if algo == "h2h" else "") + "Algo 1 · H2H", callback_data=f"al:h2h:{away}:{home}:{dp}"),
        InlineKeyboardButton(("✅ " if algo == "form" else "") + "Algo 2 · Forme", callback_data=f"al:form:{away}:{home}:{dp}"),
    ]]
    cats = MORE_CATS if more else MAIN_CATS
    for i in range(0, len(cats), 2):
        rows.append([InlineKeyboardButton(label, callback_data=f"c:{away}:{home}:{dp}:{key}") for label, key in cats[i:i + 2]])
    if more:
        rows.append([InlineKeyboardButton("⬅️ Modules principaux", callback_data=f"mm:{away}:{home}:{dp}")])
    else:
        rows.append([InlineKeyboardButton("🧬 Match Simulator", callback_data=f"c:{away}:{home}:{dp}:simulation")])
        rows.append([InlineKeyboardButton("➕ Plus de modules", callback_data=f"mo:{away}:{home}:{dp}")])
    rows.append([InlineKeyboardButton("📅 Autres matchs", callback_data="d:0")])
    return InlineKeyboardMarkup(rows)


def category_text(away, home, algo="h2h"):
    algo_line = "📊 H2H domicile + extérieur." if algo == "h2h" else "🔥 Forme récente : 10 et 20 derniers matchs."
    return (f"🏒 <b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>\n\nChoisis l'algo puis le module à analyser.\n"
            f"⚙️ <b>Algo actif : {ALGO_LABELS[algo]}</b>\n\n"
            f"{algo_line}\n🧬 Match Simulator : victoire et buts attendus.\n👤 TOP 5 avec une fiche par joueur.\n"
            "🧭 Contexte : ligne, défense adverse, back-to-back.")


async def make_match_card(away, home, match_date):
    try:
        try:
            scores = await head_to_head_games(away, home)
        except Exception:
            scores = []
        al, hl = await asyncio.gather(team_logo(away), team_logo(home))
        label = format_date(match_date) if match_date else "Prochain match"
        return await asyncio.to_thread(card_match, away, home, al, hl, scores, label)
    except Exception as e:
        log.warning("Carte match : %s", e)
        return None


async def send_category_menu(message, ctx, away, home, match_date=None, algo="h2h"):
    ctx.user_data["last"] = {"away": away, "home": home, "date": match_date, "category": None, "algo": algo}
    card = await make_match_card(away, home, match_date)
    if card:
        await message.reply_photo(photo=io.BytesIO(card), caption=category_text(away, home, algo), parse_mode=HTML, reply_markup=category_keyboard(away, home, match_date, algo))
    else:
        await message.reply_text(category_text(away, home, algo), parse_mode=HTML, reply_markup=category_keyboard(away, home, match_date, algo))


async def games_for(offset):
    date = (datetime.now(TZ) + timedelta(days=offset)).date().isoformat()
    async def build():
        data = await get_json(f"{API_WEB}/schedule/{date}")
        for day in data.get("gameWeek", []):
            if day.get("date") != date: continue
            output = []
            for g in day.get("games", []):
                start = datetime.fromisoformat(g["startTimeUTC"].replace("Z", "+00:00")).astimezone(TZ)
                output.append((g["awayTeam"]["abbrev"], g["homeTeam"]["abbrev"], start.strftime("%H:%M"), (g.get("gameDate") or date)[:10]))
            return output
        return []
    return await cached(("schedule-day", date), 600, build)


DAYS = ("Aujourd'hui", "Demain", "Après-demain")


def games_view_keyboard(offset, games):
    rows = [[InlineKeyboardButton("🏆 Picks du jour", callback_data="pk")]]
    rows += [[InlineKeyboardButton(f"{NAMES.get(a, a)} @ {NAMES.get(h, h)} · {hour}", callback_data=f"g:{a}:{h}:{date}")] for a, h, hour, date in games]
    rows.append([InlineKeyboardButton(("• " if i == offset else "") + label, callback_data=f"d:{i}") for i, label in enumerate(DAYS)])
    return InlineKeyboardMarkup(rows)


async def games_view(offset):
    offset = max(0, min(2, offset))
    games = await games_for(offset)
    text = (f"<b>Matchs : {DAYS[offset].lower()}</b>\nHoraires en heure de Paris 🇫🇷\nSélectionne un match." if games else f"Aucun match {DAYS[offset].lower()}.\nChoisis un autre jour ou écris deux équipes.")
    return text, games_view_keyboard(offset, games)


async def send_games(message, offset=0):
    try:
        text, markup = await games_view(offset)
        await message.reply_text(text, parse_mode=HTML, reply_markup=markup)
    except Exception as e:
        log.warning("Calendrier : %s", e)
        await message.reply_text("Calendrier indisponible. Écris deux équipes pour ouvrir leur analyse.")


async def find_upcoming_date(away, home):
    for offset in range(7):
        try:
            for a, h, _, date in await games_for(offset):
                if a == away and h == home: return date
        except Exception: continue
    return None

# =====================================================
# AFFICHAGE (TEXTE / PHOTO) & NAVIGATION CALLBACKS
# =====================================================
def split_telegram_text(text, limit=3900):
    if len(text) <= limit: return [text]
    chunks = []
    current = ""
    for line in text.split("\n"):
        candidate = current + "\n" + line if current else line
        if len(candidate) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current: chunks.append(current)
    return chunks


def truncate_caption(text, limit=1024):
    if len(text) <= limit:
        return text
    out, size = [], 0
    for line in text.split("\n"):
        if size + len(line) + 1 > limit - 4:
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out) + "\n…"


async def edit_any(query, text, markup=None):
    msg = query.message
    try:
        if msg.photo:
            await query.edit_message_caption(caption=truncate_caption(text), parse_mode=HTML, reply_markup=markup)
        else:
            await query.edit_message_text(text, parse_mode=HTML, reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def render(query, text, markup, card=None):
    msg = query.message
    if card:
        caption = truncate_caption(text)
        if msg.photo:
            try:
                await query.edit_message_media(InputMediaPhoto(media=io.BytesIO(card), caption=caption, parse_mode=HTML), reply_markup=markup)
                return
            except BadRequest as e:
                if "not modified" in str(e).lower():
                    return
        try:
            await msg.delete()
        except Exception:
            pass
        await msg.chat.send_photo(photo=io.BytesIO(card), caption=caption, parse_mode=HTML, reply_markup=markup)
        return
    chunks = split_telegram_text(text)
    first_markup = markup if len(chunks) == 1 else None
    if msg.photo:
        try:
            await msg.delete()
        except Exception:
            pass
        await msg.chat.send_message(chunks[0], parse_mode=HTML, reply_markup=first_markup)
    else:
        try:
            await query.edit_message_text(chunks[0], parse_mode=HTML, reply_markup=first_markup)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
    for i, chunk in enumerate(chunks[1:], 1):
        await msg.chat.send_message(chunk, parse_mode=HTML, reply_markup=markup if i == len(chunks) - 1 else None)


async def send_chunks(query, text):
    for chunk in split_telegram_text(text):
        await query.message.chat.send_message(chunk, parse_mode=HTML)


# ---------- État des analyses (en mémoire + reprise après redémarrage) ----------
_states = {}


def set_state(uid, state):
    _states.pop(uid, None)
    _states[uid] = state
    while len(_states) > 400:
        _states.pop(next(iter(_states)))


async def ensure_state(query, ctx):
    uid = query.from_user.id
    state = _states.get(uid)
    if state:
        return state
    last = ctx.user_data.get("last")
    if not last:
        return None
    if last.get("category"):
        await edit_any(query, "⏳ Reprise de ton analyse...")
        try:
            state = await prepare_category(last["away"], last["home"], last["category"], last.get("date"), last.get("algo", "h2h"))
        except Exception as e:
            log.warning("Reprise d'analyse : %s", e)
            return None
    else:
        state = {"kind": "menu"}
    state.update({"away": last["away"], "home": last["home"], "category": last.get("category"),
                  "match_date": last.get("date"), "algo": last.get("algo", "h2h")})
    set_state(uid, state)
    return state


async def show_player_profile(query, state, index):
    if state.get("kind") != "players":
        await render(query, "Cette analyse a expiré. Sélectionne à nouveau un match.", None)
        return
    ranked = state["ranked"]
    if index < 0 or index >= len(ranked):
        return
    p, _ = ranked[index]
    away, home, category, match_date = state["away"], state["home"], state["category"], state.get("match_date")
    algo = state.get("algo", "h2h")
    info = state.get("info")
    markup = player_profile_keyboard(index, len(ranked))
    card = await build_player_card(p, category, away, home, match_date, algo)
    if card:
        caption = format_player_profile(away, home, p, category, match_date, state["scores"], history=False, algo=algo, info=info)
        await render(query, caption, markup, card=card)
    else:
        full = format_player_profile(away, home, p, category, match_date, state["scores"], history=True, algo=algo, info=info)
        await render(query, full, markup)


async def show_preview(query, away, home, match_date, category, algo):
    """Aperçu gratuit d'un module réservé aux abonnés : premier résultat visible, le reste masqué."""
    await edit_any(query, f"⏳ Aperçu {esc(NAMES[away])} @ {esc(NAMES[home])}...")
    try:
        st = await prepare_category(away, home, category, match_date, algo)
    except Exception as e:
        log.warning("Aperçu : %s", e)
        st = None
    if st and st["kind"] == "players":
        text = format_category_top5(away, home, category, st["ranked"], algo, visible=1)
    elif st and st.get("teaser"):
        text = st["teaser"]
    elif st:
        text = st["text"]
    else:
        text = "Analyse indisponible pour le moment."
    text += "\n\n🔒 <b>Aperçu gratuit</b> — l'accès complet est réservé aux abonnés."
    back = [[InlineKeyboardButton("⬅️ Retour aux catégories", callback_data="backcat")], [InlineKeyboardButton("📅 Autres matchs", callback_data="d:0")]]
    await render(query, text, InlineKeyboardMarkup(list(plans_keyboard().inline_keyboard) + back))


async def on_button(update, ctx):
    query = update.callback_query
    parts = query.data.split(":")
    action = parts[0]
    uid = query.from_user.id
    if action == "sb":
        try:
            await handle_subscribe(query, ctx, parts)
        except Exception as e:
            log.exception("Erreur abonnement : %s", e)
            try:
                await query.message.chat.send_message("⚠️ Impossible d'ouvrir le paiement pour le moment. Réessaie avec /abonnement.")
            except Exception:
                pass
        return
    if action == "an":
        await handle_announce(query, ctx, parts)
        return
    if not user_can_access(update, ctx):
        await query.answer("🔒 BLACK ICE est fermé.", show_alert=True)
        return
    await query.answer()
    algo = get_algo(ctx)
    premium = is_premium(update, ctx)
    try:
        if action == "pk":
            await send_picks(query.message, premium)
            return
        if action == "d":
            text, markup = await games_view(int(parts[1]))
            await render(query, text, markup)
            return
        if action == "al":
            if len(parts) != 5 or parts[1] not in ALGO_LABELS or parts[2] not in NAMES or parts[3] not in NAMES:
                return
            algo = parts[1]
            ctx.user_data["algo"] = algo
            away, home = parts[2], parts[3]
            match_date = None if parts[4] == "none" else parts[4]
            ctx.user_data["last"] = {"away": away, "home": home, "date": match_date, "category": None, "algo": algo}
            _states.pop(uid, None)
            await edit_any(query, category_text(away, home, algo), category_keyboard(away, home, match_date, algo))
            return
        if action in ("g", "m"):
            away, home = parts[1], parts[2]
            if away not in NAMES or home not in NAMES:
                return
            match_date = parts[3] if len(parts) > 3 and parts[3] != "none" else None
            ctx.user_data["last"] = {"away": away, "home": home, "date": match_date, "category": None, "algo": algo}
            _states.pop(uid, None)
            card = await make_match_card(away, home, match_date)
            await render(query, category_text(away, home, algo), category_keyboard(away, home, match_date, algo), card=card)
            return
        if action in ("mo", "mm"):
            if len(parts) != 4 or parts[1] not in NAMES or parts[2] not in NAMES:
                return
            away, home = parts[1], parts[2]
            match_date = None if parts[3] == "none" else parts[3]
            await edit_any(query, category_text(away, home, algo), category_keyboard(away, home, match_date, algo, more=(action == "mo")))
            return
        if action == "c":
            if len(parts) != 5: return
            away, home = parts[1], parts[2]
            match_date = None if parts[3] == "none" else parts[3]
            category = parts[4]
            if away not in NAMES or home not in NAMES or category not in VALID_CATS: return
            if not premium:
                locked = category == "simulation" or (algo == "form" and category in CATEGORY_INFO)
                if locked:
                    await show_preview(query, away, home, match_date, category, algo)
                    return
                if usage_today(uid) >= FREE_DAILY:
                    await deny_access(query, f"Tu as utilisé tes {FREE_DAILY} analyses gratuites du jour.")
                    return
                usage_incr(uid)
            await edit_any(query, f"⏳ Analyse {esc(NAMES[away])} @ {esc(NAMES[home])}...\nCatégorie : {esc(category)}\nAlgo : {ALGO_LABELS[algo]}")
            try:
                new_state = await prepare_category(away, home, category, match_date, algo)
            except Exception as e:
                log.exception("Analyse %s : %s", category, e)
                new_state = {"kind": "text", "text": "Les données de cette catégorie sont indisponibles pour le moment. Réessaie dans un instant."}
            new_state.update({"away": away, "home": home, "category": category, "match_date": match_date, "algo": algo})
            set_state(uid, new_state)
            ctx.user_data["last"] = {"away": away, "home": home, "date": match_date, "category": category, "algo": algo}
            back_kb = [[InlineKeyboardButton("⬅️ Retour aux catégories", callback_data="backcat")], [InlineKeyboardButton("📅 Autres matchs", callback_data="d:0")]]
            if new_state["kind"] == "players":
                await render(query, new_state["text"], player_top_keyboard(new_state["ranked"], algo), card=new_state.get("card"))
            elif new_state.get("card"):
                rows = [[InlineKeyboardButton("📜 Détails complets", callback_data="dt")]] + back_kb
                await render(query, new_state.get("caption") or new_state["text"], InlineKeyboardMarkup(rows), card=new_state["card"])
            else:
                await render(query, new_state["text"], InlineKeyboardMarkup(back_kb))
            return

        # ----- actions qui s'appuient sur l'analyse en cours -----
        state = await ensure_state(query, ctx)
        if state is None:
            await render(query, "Cette analyse a expiré. Sélectionne à nouveau un match.",
                         InlineKeyboardMarkup([[InlineKeyboardButton("📅 Matchs", callback_data="d:0")]]))
            return
        if action in ("p", "pn"):
            await show_player_profile(query, state, int(parts[1]))
            return
        if action == "ph":
            index = int(parts[1])
            if state.get("kind") != "players" or index < 0 or index >= len(state["ranked"]):
                return
            p, _ = state["ranked"][index]
            await send_chunks(query, format_player_profile(state["away"], state["home"], p, state["category"], state.get("match_date"), state["scores"], history=True, algo=state.get("algo", "h2h"), info=state.get("info")))
            return
        if action == "dt":
            await send_chunks(query, state["text"])
            return
        if action == "backtop":
            if state.get("kind") != "players":
                card = await make_match_card(state["away"], state["home"], state.get("match_date"))
                await render(query, category_text(state["away"], state["home"], algo), category_keyboard(state["away"], state["home"], state.get("match_date"), algo), card=card)
                return
            await render(query, state["text"], player_top_keyboard(state["ranked"], state.get("algo", "h2h")), card=state.get("card"))
            return
        if action == "backcat":
            card = await make_match_card(state["away"], state["home"], state.get("match_date"))
            await render(query, category_text(state["away"], state["home"], algo), category_keyboard(state["away"], state["home"], state.get("match_date"), algo), card=card)
            return
    except Exception as e:
        log.exception("Erreur bouton Telegram : %s", e)
        try:
            await query.message.chat.send_message("⚠️ Une erreur est survenue. Réessaie dans un instant.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📅 Matchs", callback_data="d:0")]]))
        except Exception:
            pass

# =====================================================
# GARDE : INSCRIPTION, BANNISSEMENT, MAJORITÉ
# =====================================================
def age_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ J'ai 18 ans ou plus", callback_data="age:yes")],
                                 [InlineKeyboardButton("❌ Non", callback_data="age:no")]])


async def guard(update, ctx):
    user = update.effective_user
    if not user:
        return
    if update.pre_checkout_query or (update.message and update.message.successful_payment):
        return
    try:
        register_user(update)
        adult, banned = user_flags(user.id)
    except Exception as e:
        # Une erreur de base de données ne doit jamais bloquer ni alerter l'utilisateur
        log.warning("Garde (base de données) : %s", e)
        return
    q = update.callback_query
    if banned:
        if q:
            await q.answer("⛔ Accès suspendu.", show_alert=True)
        raise ApplicationHandlerStop
    if adult:
        return
    if q and q.data == "age:yes":
        db_exec("UPDATE users SET adult_ok=1 WHERE user_id=?", (user.id,))
        await q.answer("Merci !")
        if user_can_access(update, ctx):
            await send_welcome(q.message)
        else:
            await q.message.reply_text("🔒 BLACK ICE est actuellement fermé.")
        raise ApplicationHandlerStop
    if q and q.data == "age:no":
        await q.answer("Ce bot est réservé aux personnes majeures.", show_alert=True)
        raise ApplicationHandlerStop
    if q:
        await q.answer()
    chat = update.effective_chat
    if chat:
        await ctx.bot.send_message(chat.id, AGE_TEXT, parse_mode=HTML, reply_markup=age_keyboard())
    raise ApplicationHandlerStop

# =====================================================
# START / TEXTE / COMMANDES
# =====================================================
async def send_welcome(message):
    keyboard = ReplyKeyboardMarkup([[MENU_BTN, PICKS_BTN]], resize_keyboard=True)
    await message.reply_text(
        "🏒 <b>BLACK ICE — ANALYSE NHL</b>\n\nSélectionne un match pour accéder aux statistiques.\n\n"
        "🏆 Picks du jour chaque matin (2 algos combinés).\n"
        "🖼️ Cartes visuelles : logos, photos, barres.\n"
        "🔀 2 algos : <b>H2H</b> ou <b>Forme 10/20 derniers matchs</b>.\n"
        "👤 TOP 5 avec fiches individuelles et contexte du match.\n"
        "🧬 Match Simulator : victoire et buts attendus.\n\n"
        f"🎁 Gratuit : {FREE_DAILY} analyses par jour (Algo 1) + aperçus.\n"
        "⭐ /abonnement · /moncompte · 🎟️ /code · 🤝 /parrainage · 📈 /historique",
        parse_mode=HTML, reply_markup=keyboard
    )
    await send_games(message, 0)


async def start(update, ctx):
    if not user_can_access(update, ctx):
        await update.message.reply_text("🔒 BLACK ICE est actuellement fermé.")
        return
    await send_welcome(update.message)


async def on_text(update, ctx):
    message = update.message
    text = (message.text or "").strip()
    if ctx.user_data.get("await_admin_password"):
        ctx.user_data.pop("await_admin_password", None)
        try:
            await message.delete()
        except Exception:
            pass
        if ADMIN_PASSWORD and secrets.compare_digest(text, ADMIN_PASSWORD):
            ctx.user_data["admin_expiry"] = time.time() + ADMIN_SESSION_SECONDS
            await message.reply_text(
                "✅ <b>Connexion administrateur réussie.</b>\n\n"
                "/stats — revenus, abonnés, expirations\n"
                "/annonce texte — message à tous (avec confirmation)\n"
                "/donner ID jours — offrir des jours\n"
                "/revoquer ID — retirer l'accès\n"
                "/abonnes — abonnés actifs\n"
                "/rembourser ID — rembourser le dernier paiement\n"
                "/promo CODE jours [max] — créer un code promo\n"
                "/bannir ID · /debannir ID\n"
                "/genpicks — régénérer les picks du jour\n"
                "/backtest — recalculer le backtest du modèle\n"
                "/fermer · /ouvrir · /statut · /logout",
                parse_mode=HTML
            )
        else:
            await message.reply_text("❌ Mot de passe incorrect.")
        return
    if not user_can_access(update, ctx):
        await message.reply_text("🔒 BLACK ICE est actuellement fermé.")
        return
    algo = get_algo(ctx)
    if text == MENU_BTN or norm(text) in ("match", "matchs", "menu", "ce soir", "aujourdhui"):
        await send_games(message, 0)
        return
    if text == PICKS_BTN or norm(text) in ("picks", "pick"):
        await send_picks(message, is_premium(update, ctx))
        return
    parts = [p.strip() for p in SPLIT.split(text) if p.strip()]
    if len(parts) == 2:
        away, home = find_team(parts[0]), find_team(parts[1])
        if away and home and away != home:
            await message.chat.send_action("upload_photo")
            date = await find_upcoming_date(away, home)
            await send_category_menu(message, ctx, away, home, date, algo)
            return
        bad = parts[0] if not away else parts[1]
        await message.reply_text(f"Je ne reconnais pas « {bad} ». Essaie le nom de la ville, de l'équipe ou son abréviation.")
        return
    if len(parts) == 1:
        team = find_team(parts[0])
        if team:
            for offset in range(7):
                try:
                    games = await games_for(offset)
                except Exception:
                    continue
                for away, home, _, date in games:
                    if team in (away, home):
                        await message.chat.send_action("upload_photo")
                        await send_category_menu(message, ctx, away, home, date, algo)
                        return
            await message.reply_text(f"Aucun match trouvé pour {NAMES[team]} dans les 7 prochains jours.")
            return
    await message.reply_text("Écris deux équipes, par exemple « Washington vs Montréal », ou touche « Matchs du jour ».")


async def on_error(update, ctx):
    log.error("Erreur non gérée : %s", ctx.error, exc_info=ctx.error)
    try:
        if update and getattr(update, "effective_chat", None):
            await ctx.bot.send_message(update.effective_chat.id, "⚠️ Une erreur est survenue. Réessaie dans un instant.")
    except Exception:
        pass


async def shutdown(app):
    global _client
    if _client:
        await _client.aclose()
        _client = None


def main():
    init_db()
    persistence = PicklePersistence(
        filepath=PERSIST_PATH,
        store_data=PersistenceInput(bot_data=False, chat_data=False, callback_data=False),
        update_interval=30)
    app = (Application.builder().token(TOKEN).persistence(persistence)
           .concurrent_updates(True).post_shutdown(shutdown).build())
    app.add_handler(TypeHandler(Update, guard), group=-1)
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("picks", cmd_picks))
    app.add_handler(CommandHandler("historique", cmd_historique))
    app.add_handler(CommandHandler("abonnement", cmd_abonnement))
    app.add_handler(CommandHandler("moncompte", cmd_moncompte))
    app.add_handler(CommandHandler("paysupport", cmd_paysupport))
    app.add_handler(CommandHandler("code", cmd_code))
    app.add_handler(CommandHandler("parrainage", cmd_parrainage))
    app.add_handler(CommandHandler("notif", cmd_notif))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("logout", cmd_logout))
    app.add_handler(CommandHandler("fermer", cmd_fermer))
    app.add_handler(CommandHandler("ouvrir", cmd_ouvrir))
    app.add_handler(CommandHandler("statut", cmd_statut))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("annonce", cmd_annonce))
    app.add_handler(CommandHandler("donner", cmd_donner))
    app.add_handler(CommandHandler("revoquer", cmd_revoquer))
    app.add_handler(CommandHandler("abonnes", cmd_abonnes))
    app.add_handler(CommandHandler("rembourser", cmd_rembourser))
    app.add_handler(CommandHandler("promo", cmd_promo))
    app.add_handler(CommandHandler("bannir", cmd_bannir))
    app.add_handler(CommandHandler("debannir", cmd_debannir))
    app.add_handler(CommandHandler("genpicks", cmd_genpicks))
    app.add_handler(CommandHandler("backtest", cmd_backtest))
    app.add_handler(PreCheckoutQueryHandler(on_precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, on_payment))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    if app.job_queue:
        app.job_queue.run_repeating(reminder_job, interval=3 * 3600, first=90)
        app.job_queue.run_repeating(resolve_picks_job, interval=3 * 3600, first=300)
        app.job_queue.run_repeating(health_job, interval=1800, first=120)
        app.job_queue.run_daily(daily_job, time=dtime(PICKS_HOUR, 0, tzinfo=TZ))
    else:
        log.warning("JobQueue absente : installe python-telegram-bot[job-queue] (rappels, picks du matin, alertes).")
    log.info("BLACK ICE démarré — picks 2 algos — contexte — Stars — cartes images — Simulator Poisson — SQLite")
    app.run_polling()


if __name__ == "__main__":
    main()
