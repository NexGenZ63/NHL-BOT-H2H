# requirements.txt :
#   python-telegram-bot>=20
#   httpx
#   pillow
#   cairosvg   (optionnel : sans lui, les logos sont remplacés par l'abréviation de l'équipe)
#
# Police optionnelle : mets "Inter-Bold.ttf" à côté du script (sinon police par défaut).

import asyncio
import difflib
import io
import logging
import math
import os
import random
import re
import secrets
import sqlite3
import time
import unicodedata
from datetime import datetime, timedelta
from functools import lru_cache
from html import escape as esc
from zoneinfo import ZoneInfo

import httpx
from PIL import Image, ImageDraw, ImageFont
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, ReplyKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

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
API_WEB = "https://api-web.nhle.com/v1"
LOGO_URL = "https://assets.nhle.com/logos/nhl/svg/{}_light.svg"
ASSETS_DIR = os.environ.get("ASSETS_DIR", os.path.join(os.path.dirname(DB_PATH) or ".", "assets"))
TZ = ZoneInfo("Europe/Paris")
TOP = 5
H2H_SCORE_COUNT = 5
MENU_BTN = "📅 Matchs du jour"
HTML = ParseMode.HTML
ADMIN_SESSION_SECONDS = 12 * 3600

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
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

# =====================================================
# SQLITE / COTES
# =====================================================
def init_db():
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with sqlite3.connect(DB_PATH) as db:
        db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("""CREATE TABLE IF NOT EXISTS odds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_date TEXT NOT NULL, away TEXT NOT NULL, home TEXT NOT NULL,
            player TEXT NOT NULL, player_norm TEXT NOT NULL, market TEXT NOT NULL,
            odd REAL NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(match_date, away, home, player_norm, market))""")
        db.execute("INSERT OR IGNORE INTO settings (key,value) VALUES ('bot_open','1')")
        db.commit()
    log.info("SQLite initialisée : %s", DB_PATH)


def db_setting(key, default=None):
    with sqlite3.connect(DB_PATH) as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_db_setting(key, value):
    with sqlite3.connect(DB_PATH) as db:
        db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        db.commit()


def bot_is_open():
    return db_setting("bot_open", "1") == "1"


def save_odd(match_date, away, home, player, market, odd):
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""INSERT INTO odds(match_date,away,home,player,player_norm,market,odd,updated_at)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(match_date,away,home,player_norm,market)
            DO UPDATE SET player=excluded.player, odd=excluded.odd, updated_at=excluded.updated_at""",
            (match_date, away, home, player, norm(player), market, odd, datetime.now(TZ).isoformat()))
        db.commit()


def get_odd(match_date, away, home, player, market):
    if not match_date:
        return None
    with sqlite3.connect(DB_PATH) as db:
        row = db.execute("SELECT odd FROM odds WHERE match_date=? AND away=? AND home=? AND player_norm=? AND market=?",
                         (match_date, away, home, norm(player), market)).fetchone()
    return row[0] if row else None

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
    if is_admin(update, ctx):
        return True
    if ALLOWED and user.id not in ALLOWED:
        return False
    return bot_is_open()


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
    for key in ("admin_expiry", "await_admin_password", "admin_flow"):
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
    await update.message.reply_text("<b>BLACK ICE — STATUT</b>\n\n"
        f"Accès utilisateurs : {status}\nBase : SQLite\nFichier : <code>{esc(DB_PATH)}</code>\n"
        f"Cartes images : activées (logos {'OK' if cairosvg else 'texte'})\n"
        "H2H : domicile + extérieur.\nForme récente : 5 et 10 derniers matchs.\nVALUE et confiance : activées.", parse_mode=HTML)


async def cmd_annuler(update, ctx):
    if not await admin_required(update, ctx): return
    ctx.user_data.pop("admin_flow", None)
    await update.message.reply_text("❌ Saisie annulée.")


ODD_MARKETS = {"goals": "🥅 +0,5 but", "assists": "🎯 +0,5 passe décisive", "points": "⭐ +0,5 point"}


def odds_market_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🥅 +0,5 but", callback_data="om:goals")],
        [InlineKeyboardButton("🎯 +0,5 passe", callback_data="om:assists")],
        [InlineKeyboardButton("⭐ +0,5 point", callback_data="om:points")],
        [InlineKeyboardButton("❌ Annuler", callback_data="om:cancel")],
    ])


async def cmd_cotes(update, ctx):
    if not await admin_required(update, ctx): return
    ctx.user_data["admin_flow"] = {"step": "match"}
    await update.message.reply_text("📊 <b>SAISIE DES COTES</b>\n\nSélectionne le match ci-dessous.", parse_mode=HTML)
    sent = False
    for offset in range(7):
        try: games = await games_for(offset)
        except Exception as error:
            log.warning("Calendrier cotes : %s", error)
            continue
        if not games: continue
        buttons = [[InlineKeyboardButton(f"{NAMES[a]} @ {NAMES[h]} · {hour}", callback_data=f"oa:{a}:{h}:{date}")] for a, h, hour, date in games]
        date_label = (datetime.now(TZ) + timedelta(days=offset)).strftime("%d/%m/%Y")
        await update.message.reply_text(f"📅 {date_label}", reply_markup=InlineKeyboardMarkup(buttons))
        sent = True
    if not sent:
        await update.message.reply_text("Aucun match trouvé dans les 7 prochains jours.")


async def handle_admin_callback(query, ctx, parts):
    if not admin_session_valid(ctx):
        await query.answer("Session admin expirée.", show_alert=True)
        return True
    if parts[0] == "oa":
        if len(parts) != 4 or parts[1] not in NAMES or parts[2] not in NAMES:
            await query.answer("Sélection invalide.")
            return True
        away, home, match_date = parts[1:]
        ctx.user_data["admin_flow"] = {"step": "player", "away": away, "home": home, "match_date": match_date}
        await query.answer()
        await query.message.reply_text(f"Match : <b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>\nDate : {format_date(match_date)}\n\nEnvoie le nom exact du joueur.", parse_mode=HTML)
        return True
    if parts[0] == "om":
        market = parts[1] if len(parts) == 2 else ""
        if market == "cancel":
            ctx.user_data.pop("admin_flow", None)
            await query.answer("Saisie annulée.")
            await query.message.reply_text("❌ Saisie annulée.")
            return True
        if market not in ODD_MARKETS or ctx.user_data.get("admin_flow", {}).get("step") != "market":
            await query.answer("Marché invalide ou aucune saisie en cours.")
            return True
        flow = ctx.user_data["admin_flow"]
        flow["market"], flow["step"] = market, "odd"
        await query.answer()
        await query.message.reply_text(f"Marché : {ODD_MARKETS[market]}\n\nEnvoie la cote décimale (exemple : <code>2.05</code>).", parse_mode=HTML)
        return True
    return False

# =====================================================
# CLIENT HTTP / CACHE / SAISONS
# =====================================================
_client = None
_cache = {}
_inflight = {}


def client():
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": "BLACK-ICE-NHL-BOT/9.0"})
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


def card_player(name, team, category_title, photo, logo, big, big_ratio, sub, forms, odd_info):
    w, h = 1080, 680
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    _header(d, w, clean_label(category_title))
    _paste(img, _avatar(photo, name, 300), (40, 110))
    _paste(img, _logo(logo, team, 90), (950, 100))
    d.text((380, 105), name, font=_fit(d, name, 54, 550), fill=WHITE)
    d.text((380, 175), NAMES.get(team, team), font=_font(30), fill=MUTED)
    d.text((380, 235), big, font=_font(120), fill=_hit_color(big_ratio) if big_ratio is not None else MUTED)
    d.text((380, 375), sub, font=_font(28), fill=MUTED)
    y = 470
    for label, succ, n in forms:
        d.text((40, y), label, font=_font(30), fill=WHITE)
        ratio = (succ / n) if n else 0
        _bar(d, 230, y + 6, 500, 30, ratio, _hit_color(ratio) if n else PANEL2)
        d.text((750, y), f"{succ}/{n}" if n else "N/D", font=_font(30), fill=WHITE)
        y += 70
    if odd_info:
        odd_text, label, color, prob_text = odd_info
        d.rounded_rectangle((830, 440, 1040, 640), radius=22, fill=PANEL)
        _ctext(d, 935, 455, "COTE", _font(22), MUTED)
        _ctext(d, 935, 485, odd_text, _font(56), WHITE)
        _ctext(d, 935, 560, label, _font(24), color)
        _ctext(d, 935, 598, prob_text, _font(22), MUTED)
    return _png(img)


def card_top(away, home, title, rows):
    w = 1080
    h = 100 + len(rows) * 125 + 20
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    _header(d, w, f"{away} @ {home}")
    _ctext(d, w / 2, 82, clean_label(title) + " - TOP 5 H2H", _font(30), ACCENT)
    y = 130
    for rank, name, team, photo, hit, n, succ, avg_text in rows:
        d.rounded_rectangle((30, y, w - 30, y + 110), radius=20, fill=PANEL)
        d.text((50, y + 32), str(rank), font=_font(44), fill=ACCENT)
        _paste(img, _avatar(photo, name, 90), (110, y + 10))
        d.text((225, y + 14), name, font=_fit(d, name, 36, 420), fill=WHITE)
        d.text((225, y + 62), f"{team} | {n} matchs H2H | moy. {avg_text}", font=_font(22), fill=MUTED)
        _bar(d, 690, y + 38, 220, 26, hit, _hit_color(hit))
        d.text((925, y + 30), f"{hit * 100:.0f}%", font=_font(36), fill=WHITE)
        y += 125
    return _png(img)


def card_sim(away, home, away_logo, home_logo, m, top):
    w, h = 1080, 700
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    _header(d, w, "MATCH SIMULATOR")
    _paste(img, _logo(away_logo, away, 170), (60, 100))
    _paste(img, _logo(home_logo, home, 170), (850, 100))
    _ctext(d, 145, 285, NAMES[away], _fit(d, NAMES[away], 28, 250), WHITE)
    _ctext(d, 935, 285, NAMES[home], _fit(d, NAMES[home], 28, 250), WHITE)
    _ctext(d, 540, 110, "SCORE MOYEN SIMULÉ", _font(24), MUTED)
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
    d.rounded_rectangle((40, 455, w - 40, 670), radius=20, fill=PANEL)
    d.text((70, 468), "TOP JOUEURS - 1+ POINT", font=_font(24), fill=ACCENT)
    for i, (name, team, pct) in enumerate(top[:3]):
        y = 510 + i * 52
        d.text((70, y), name, font=_fit(d, name, 32, 520), fill=WHITE)
        d.text((620, y + 4), team, font=_font(24), fill=MUTED)
        _bar(d, 720, y + 8, 200, 24, pct, _hit_color(pct))
        d.text((935, y + 2), f"{pct * 100:.0f}%", font=_font(32), fill=WHITE)
    if not top:
        d.text((70, 530), "Données joueurs insuffisantes", font=_font(28), fill=MUTED)
    return _png(img)

# =====================================================
# EFFECTIFS / JOURNAUX NHL
# =====================================================
async def roster(team, position="skater"):
    async def build():
        data = await get_json(f"{API_WEB}/roster/{team}/current")
        entries = (data.get("goalies") or []) if position == "goalie" else ((data.get("forwards") or []) + (data.get("defensemen") or []))
        players = []
        for p in entries:
            pid = p.get("id") or p.get("playerId")
            if pid:
                players.append({"id": pid, "name": player_name(p), "team": team, "position": p.get("positionCode", ""), "headshot": p.get("headshot")})
        return players
    try:
        return await cached(("roster", team, position, current_season()), 1800, build)
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
        "saves": number(game.get("saves")), "shots_against": number(game.get("shotsAgainst")),
        "save_pct": number(game.get("savePctg")), "goals_against": number(game.get("goalsAgainst")), "decision": game.get("decision")
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
    return await cached(("log", pid, tuple(seasons())), 3600, build)

# =====================================================
# H2H / PLAY-BY-PLAY
# =====================================================
async def head_to_head_games(away, home):
    async def fetch_schedule(team, season):
        return await cached(("schedule", team, season), 3600, lambda: get_json(f"{API_WEB}/club-schedule-season/{team}/{season}"))
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
# PROBABILITÉ INDICATIVE / VALUE / CONFIANCE
# =====================================================
def smoothed_probability(successes, n):
    return (successes + 1) / (n + 2) if n > 0 else None


def betting_estimate(player, market):
    if market not in ("goals", "assists", "points"): return None
    recent = calculate_recent(player, market, 10)
    h2h = calculate_player(player, market)
    p_recent = smoothed_probability(recent["successes"], recent["n"]) if recent else None
    p_h2h = smoothed_probability(h2h["successes"], h2h["n"]) if h2h else None
    if p_recent is not None and p_h2h is not None: probability = .70 * p_recent + .30 * p_h2h
    else: probability = p_recent if p_recent is not None else p_h2h
    return {"probability": probability, "recent": recent, "h2h": h2h, "p_recent": p_recent, "p_h2h": p_h2h}


def confidence_score(estimate):
    if not estimate or estimate.get("probability") is None: return None
    recent, h2h = estimate.get("recent"), estimate.get("h2h")
    nr = recent["n"] if recent else 0
    nh = h2h["n"] if h2h else 0
    score = 25 + min(nr, 10) * 3 + min(nh, 5) * 4
    if recent and h2h:
        diff = abs(estimate["p_recent"] - estimate["p_h2h"])
        score += max(0, 25 * (1 - diff))
    return max(0, min(100, round(score)))


def value_analysis(player, market, odd):
    estimate = betting_estimate(player, market)
    if not estimate or estimate["probability"] is None: return None
    p = estimate["probability"]
    implied = 1 / odd
    return {"probability": p, "implied": implied, "edge": p - implied, "roi": (p * odd - 1) * 100, "confidence": confidence_score(estimate)}


def value_label(edge):
    if edge >= .03: return "VALUE POTENTIELLE", GREEN, "🟢 VALUE POTENTIELLE"
    if edge > 0: return "VALUE FAIBLE", YELLOW, "🟡 VALUE FAIBLE"
    return "PAS DE VALUE", RED, "🔴 PAS DE VALUE DÉTECTÉE"


def format_value_lines(player, market, odd):
    a = value_analysis(player, market, odd)
    if not a: return ["📊 VALUE : données insuffisantes", "🔒 Confiance : non calculable"]
    label = value_label(a["edge"])[2]
    return [f"Probabilité estimée indicative : {fmt_percent(a['probability'])}", f"Probabilité implicite de la cote : {fmt_percent(a['implied'])}", f"Écart estimé : {a['edge'] * 100:+.1f} points", f"ROI théorique indicatif : {a['roi']:+.1f} %", label, f"🔒 Confiance des données : {a['confidence']}/100"]

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


def format_player_profile(away, home, player, category, match_date, scores, history=True):
    title, unit, decimals = CATEGORY_INFO[category]
    h2h = calculate_player(player, category)
    out = [f"<b>{esc(player['name'])}</b> — {esc(NAMES.get(player['team'], player['team']))}", f"<b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>", f"<b>{title}</b>", ""]
    if h2h:
        out.extend(["<b>📊 STATISTIQUES H2H</b>", f"Matchs analysés : {h2h['n']}", f"Réussite : {h2h['successes']}/{h2h['n']} ({h2h['hit'] * 100:.0f} %)"])
        if category == "toi": avg = fmt_minutes(h2h["average"])
        elif category == "faceoffs": avg = fmt_percent(h2h["average"])
        else: avg = f"{fmt(h2h['average'], decimals)} {unit}"
        out.extend([f"Total H2H : {fmt(h2h['total'], decimals)} {unit}", f"Moyenne H2H : {avg}"])
    else:
        out.append("Aucune statistique H2H vérifiable pour ce joueur sur ce marché.")
    out.extend(["", "<b>🔥 FORME GÉNÉRALE (TOUS ADVERSAIRES)</b>", recent_form_text(player, category, 5), recent_form_text(player, category, 10), ""])
    if category in ("goals", "assists", "points"):
        estimate = betting_estimate(player, category)
        if estimate and estimate.get("probability") is not None:
            out.extend(["<b>📈 ESTIMATION INDICATIVE</b>", f"Probabilité estimée : {fmt_percent(estimate['probability'])}", f"Confiance des données : {confidence_score(estimate)}/100", "<i>La confiance mesure la qualité des données, pas la chance de gagner.</i>", ""])
    if match_date:
        odds = []
        for market, label in ODD_MARKETS.items():
            odd = get_odd(match_date, away, home, player["name"], market)
            if odd is not None:
                odds.extend([f"<b>{label} : {fmt(odd, 2)}</b>"] + format_value_lines(player, market, odd) + [""])
        if odds: out.extend(["<b>💰 COTES SAISIES / VALUE</b>"] + odds)
        else: out.extend(["<b>💰 COTES</b>", "Aucune cote enregistrée pour ce joueur et ce match.", ""])
    if history:
        out.extend(format_player_history(player, category))
    out.extend(["", "<i>Les estimations sont indicatives et ne garantissent aucun résultat.</i>"])
    return "\n".join(out)

# =====================================================
# AUTRES AFFICHAGES / ANALYSES
# =====================================================
def format_category_top5(away, home, category, ranked):
    title, unit, decimals = CATEGORY_INFO[category]
    out = [f"<b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>", f"<b>{title} — TOP 5 H2H</b>", "<i>Choisis un joueur pour ouvrir sa fiche complète.</i>", ""]
    if not ranked: return "\n".join(out + ["Aucun joueur avec des statistiques vérifiables."])
    for i, (p, s) in enumerate(ranked, 1):
        avg = fmt_minutes(s["average"]) if category == "toi" else fmt_percent(s["average"]) if category == "faceoffs" else f"{fmt(s['average'], decimals)} {unit}"
        out.extend([f"<b>{i}. {esc(p['name'])}</b> ({p['team']})", f"   {s['n']} matchs H2H | {s['successes']}/{s['n']} réussites ({s['hit'] * 100:.0f} %)", f"   Moyenne : {avg}", ""])
    return "\n".join(out)


def player_top_keyboard(ranked):
    rows = []
    for i, (p, s) in enumerate(ranked):
        rows.append([InlineKeyboardButton(f"{i + 1}. {p['name']} · {s['hit'] * 100:.0f}% ({s['n']} H2H)", callback_data=f"p:{i}")])
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


def goalie_result(player):
    valid = [g for g in player.get("vs", []) if g.get("save_pct") is not None or (g.get("saves") is not None and g.get("shots_against") is not None)]
    if not valid: return None
    saves = sum(g.get("saves") or 0 for g in valid); shots = sum(g.get("shots_against") or 0 for g in valid); ga = sum(g.get("goals_against") or 0 for g in valid)
    if shots: pct = saves / shots
    else:
        pcts = [g["save_pct"] for g in valid if g.get("save_pct") is not None]
        if not pcts: return None
        pcts = [v / 100 if v > 1 else v for v in pcts]; pct = sum(pcts) / len(pcts)
    return {"games": len(valid), "saves": saves, "shots_against": shots, "goals_against": ga, "save_pct": pct}


def format_goalies(away, home, players):
    ranked = [(p, goalie_result(p)) for p in players if goalie_result(p)]
    ranked.sort(key=lambda x: (-x[1]["save_pct"], -x[1]["games"]))
    out = [f"<b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>", "", "<b>🥅 GARDIENS — H2H</b>", ""]
    for i, (p, s) in enumerate(ranked[:TOP], 1): out.extend([f"<b>{i}. {esc(p['name'])}</b> ({p['team']})", f"Matchs : {s['games']}", f"Arrêts : {s['saves']} / {s['shots_against']}", f"Buts encaissés : {s['goals_against']}", f"% arrêts : {fmt_percent(s['save_pct'])}", ""])
    if not ranked: out.append("Aucune statistique de gardien vérifiable.")
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
# CHARGEMENT DES DONNÉES POUR UNE CATÉGORIE
# =====================================================
async def load_players(away, home, position="skater"):
    rosters = await asyncio.gather(roster(away, position), roster(home, position))
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
    return players

# =====================================================
# MATCH SIMULATOR — MONTE-CARLO ÉQUIPES + FRÉQUENCES JOUEURS
# =====================================================
SIMULATIONS = 10000
SIM_PLAYER_GAMES = 20


def poisson_sample(lam):
    lam = max(0.05, min(float(lam), 12.0))
    threshold = math.exp(-lam)
    product = 1.0
    count = 0
    while product > threshold:
        count += 1
        product *= random.random()
    return count - 1


async def team_recent_results(team, limit=30):
    async def fetch(season):
        return await cached(
            ("sim-team-schedule", team, season), 3600,
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


def simulate_team_match(away_lambda, home_lambda, count=SIMULATIONS):
    away_wins = home_wins = ties = 0
    away_goals = home_goals = 0
    o4 = o5 = o6 = 0
    for _ in range(count):
        ag = poisson_sample(away_lambda)
        hg = poisson_sample(home_lambda)
        away_goals += ag
        home_goals += hg
        if ag > hg:
            away_wins += 1
        elif hg > ag:
            home_wins += 1
        else:
            ties += 1
            if random.random() < home_lambda / (away_lambda + home_lambda):
                home_wins += 1
            else:
                away_wins += 1
        total = ag + hg
        o4 += total >= 5
        o5 += total >= 6
        o6 += total >= 7
    return {
        "away_win": away_wins / count, "home_win": home_wins / count, "reg_tie": ties / count,
        "away_avg": away_goals / count, "home_avg": home_goals / count,
        "over_4_5": o4 / count, "over_5_5": o5 / count, "over_6_5": o6 / count,
    }


def simulate_player_logs(players):
    """Fréquences empiriques sur les 20 derniers matchs (équivalent exact d'un bootstrap, sans tirages)."""
    results = []
    for player in players:
        recent = sorted(player.get("log", []), key=lambda g: g.get("date") or "", reverse=True)[:SIM_PLAYER_GAMES]
        if len(recent) < 5:
            continue
        samples = {}
        for market in ("points", "goals", "assists", "shots"):
            values = [max(0, int(round(g[market] or 0))) for g in recent if g.get(market) is not None]
            if not values:
                continue
            n = len(values)
            samples[market] = {
                "avg": sum(values) / n,
                "one_plus": sum(v >= 1 for v in values) / n,
                "two_plus": sum(v >= 2 for v in values) / n,
                "three_plus": sum(v >= 3 for v in values) / n,
            }
        if "points" in samples:
            results.append({"player": player, "n": len(recent), "samples": samples})
    return results


async def build_match_simulation(away, home):
    away_games, home_games = await asyncio.gather(team_recent_results(away), team_recent_results(home))
    away_lambda = estimate_team_goals(away_games, home_games, False)
    home_lambda = estimate_team_goals(home_games, away_games, True)
    if away_lambda is None or home_lambda is None:
        raise RuntimeError("Historique insuffisant pour simuler les deux équipes (minimum 8 matchs exploitables chacune).")
    match = await asyncio.to_thread(simulate_team_match, away_lambda, home_lambda)

    players = await load_players(away, home, "skater")
    simulated_players = simulate_player_logs(players)
    simulated_players.sort(
        key=lambda item: (item["samples"].get("points", {}).get("one_plus", 0), item["samples"].get("points", {}).get("avg", 0)),
        reverse=True
    )

    out = [
        f"<b>🧬 MATCH SIMULATOR — {SIMULATIONS:,} SIMULATIONS</b>".replace(",", " "),
        f"<b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>",
        "<i>Modèle de buts Poisson calibré sur les résultats récents domicile/extérieur.</i>", "",
        "<b>🏆 VAINQUEUR DU MATCH (prolongation incluse)</b>",
        f"{esc(NAMES[away])} : <b>{fmt_percent(match['away_win'])}</b>",
        f"{esc(NAMES[home])} : <b>{fmt_percent(match['home_win'])}</b>",
        f"Égalité après 60 min : {fmt_percent(match['reg_tie'])}", "",
        "<b>🥅 SCORES MOYENS SIMULÉS</b>",
        f"{esc(NAMES[away])} : {fmt(match['away_avg'], 2)} buts",
        f"{esc(NAMES[home])} : {fmt(match['home_avg'], 2)} buts", "",
        "<b>📊 TOTAL DE BUTS</b>",
        f"5 buts ou plus : {fmt_percent(match['over_4_5'])}",
        f"6 buts ou plus : {fmt_percent(match['over_5_5'])}",
        f"7 buts ou plus : {fmt_percent(match['over_6_5'])}", "",
        "<b>👤 PLAYER SIMULATOR — TOP 5 POINTS</b>",
        f"<i>Fréquences observées sur les {SIM_PLAYER_GAMES} derniers matchs disponibles.</i>",
    ]
    for item in simulated_players[:5]:
        p, data = item["player"], item["samples"]
        point = data.get("points", {})
        goal = data.get("goals", {})
        assist = data.get("assists", {})
        shots = data.get("shots", {})
        out.extend([
            f"<b>{esc(p['name'])}</b> ({esc(NAMES.get(p['team'], p['team']))}) — {item['n']} matchs",
            f"⭐ 1+ point : {fmt_percent(point.get('one_plus'))} | moyenne {fmt(point.get('avg'), 2)}",
            f"🥅 1+ but : {fmt_percent(goal.get('one_plus')) if goal else 'N/D'} | 🎯 1+ passe : {fmt_percent(assist.get('one_plus')) if assist else 'N/D'}",
            f"🏒 3+ tirs : {fmt_percent(shots.get('three_plus')) if shots else 'N/D'}", ""
        ])
    if not simulated_players:
        out.extend(["Aucun journal joueur suffisamment complet pour le module individuel.", ""])
    out.extend([
        "<i>Méthode : simulation probabiliste, pas une prédiction certaine. Les fréquences des joueurs sont empiriques et ne sont pas forcées à correspondre au score simulé de l'équipe.</i>",
        "<i>Probabilités expérimentales à valider par backtesting ; elles ne garantissent aucun résultat.</i>"
    ])
    caption = "\n".join([
        "<b>🧬 MATCH SIMULATOR</b>",
        f"<b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>", "",
        f"{esc(NAMES[away])} : <b>{fmt_percent(match['away_win'])}</b>",
        f"{esc(NAMES[home])} : <b>{fmt_percent(match['home_win'])}</b>",
        f"Score moyen : {fmt(match['away_avg'], 1)} - {fmt(match['home_avg'], 1)}", "",
        "<i>Probabilités expérimentales, aucune garantie.</i>"
    ])
    top = []
    for item in simulated_players[:3]:
        top.append((item["player"]["name"], item["player"]["team"], item["samples"]["points"]["one_plus"]))
    return "\n".join(out), caption, match, top


async def prepare_category(away, home, category, match_date):
    if category == "simulation":
        text, caption, match, top = await build_match_simulation(away, home)
        card = None
        try:
            al, hl = await asyncio.gather(team_logo(away), team_logo(home))
            card = await asyncio.to_thread(card_sim, away, home, al, hl, match, top)
        except Exception as e:
            log.warning("Carte simulation : %s", e)
        return {"kind": "text", "text": text, "caption": caption, "card": card}
    scores = await head_to_head_games(away, home)
    if not scores: return {"kind": "text", "text": f"<b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>\n\nAucune confrontation terminée trouvée entre ces équipes sur les saisons analysées."}
    if category == "teams": return {"kind": "text", "text": format_teams(away, home, scores)}
    position = "goalie" if category == "goalies" else "skater"
    players = await load_players(away, home, position)
    pbp_data = {}
    if category not in ("form", "goalies"):
        pbp_data = await all_pbp(scores)
    for p in players: match_player_games(p, scores, pbp_data)
    if category == "goalies": return {"kind": "text", "text": format_goalies(away, home, players)}
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
            photos = await asyncio.gather(*(player_photo(p) for p, _ in ranked))
            title, unit, decimals = CATEGORY_INFO[category]
            rows = []
            for i, ((p, s), photo) in enumerate(zip(ranked, photos), 1):
                avg = fmt_minutes(s["average"]) if category == "toi" else fmt_percent(s["average"]) if category == "faceoffs" else fmt(s["average"], decimals)
                rows.append((i, p["name"], p["team"], photo, s["hit"], s["n"], s["successes"], avg))
            card = await asyncio.to_thread(card_top, away, home, title, rows)
        except Exception as e:
            log.warning("Carte TOP : %s", e)
    return {"kind": "players", "text": format_category_top5(away, home, category, ranked), "ranked": ranked, "scores": scores, "card": card}


async def build_player_card(player, category, away, home, match_date):
    try:
        photo, logo = await asyncio.gather(player_photo(player), team_logo(player["team"]))
        h2h = calculate_player(player, category)
        big = f"{h2h['successes']}/{h2h['n']}" if h2h else "N/D"
        ratio = h2h["hit"] if h2h else None
        sub = "réussites H2H" if h2h else "aucune donnée H2H"
        forms = []
        for limit in (5, 10):
            r = calculate_recent(player, category, limit)
            forms.append((f"Forme {limit}", r["successes"] if r else 0, r["n"] if r else 0))
        odd_info = None
        if category in ("goals", "assists", "points") and match_date:
            odd = get_odd(match_date, away, home, player["name"], category)
            if odd is not None:
                a = value_analysis(player, category, odd)
                if a:
                    label, color, _ = value_label(a["edge"])
                    odd_info = (fmt(odd, 2), label, color, f"proba {a['probability'] * 100:.0f}%")
                else:
                    odd_info = (fmt(odd, 2), "DONNÉES INSUFF.", MUTED, "")
        title = CATEGORY_INFO[category][0]
        return await asyncio.to_thread(card_player, player["name"], player["team"], title, photo, logo, big, ratio, sub, forms, odd_info)
    except Exception as e:
        log.warning("Carte joueur : %s", e)
        return None

# =====================================================
# CATÉGORIES / CALENDRIER
# =====================================================
CATEGORIES = [
    ("⭐ Points", "points"), ("🥅 Buts", "goals"), ("🎯 Passes", "assists"),
    ("🏒 Tirs cadrés", "shots"), ("🎯 Tirs manqués", "missed"), ("💥 Hits", "hits"),
    ("🛡️ Tirs bloqués", "blocks"), ("🔵 Mises en jeu", "faceoffs"),
    ("⏱️ Temps de glace", "toi"), ("⚡ Power Play", "powerplay"),
    ("🚨 Pénalités", "penalties"), ("🥅 Gardiens", "goalies"),
    ("📈 Forme 5 H2H", "form"), ("📊 Bilan équipes", "teams"),
    ("🧬 Match Simulator", "simulation")
]


def category_keyboard(away, home, match_date=None):
    date_part = match_date or "none"
    rows = []
    for i in range(0, len(CATEGORIES), 2):
        rows.append([InlineKeyboardButton(label, callback_data=f"c:{away}:{home}:{date_part}:{key}") for label, key in CATEGORIES[i:i + 2]])
    rows.append([InlineKeyboardButton("📅 Autres matchs", callback_data="d:0")])
    return InlineKeyboardMarkup(rows)


def category_text(away, home):
    return (f"🏒 <b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>\n\nChoisis le module à analyser.\n\n"
            "🧬 Simulation Monte-Carlo des équipes et joueurs.\n📊 H2H domicile + extérieur.\n👤 TOP 5 avec une fiche par joueur.\n🔥 Forme générale sur 5 et 10 matchs dans chaque fiche.\n💰 VALUE avec tes cotes saisies.\n🔒 Confiance des données /100.")


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


async def send_category_menu(message, away, home, match_date=None):
    card = await make_match_card(away, home, match_date)
    if card:
        await message.reply_photo(photo=io.BytesIO(card), caption=category_text(away, home), parse_mode=HTML, reply_markup=category_keyboard(away, home, match_date))
    else:
        await message.reply_text(category_text(away, home), parse_mode=HTML, reply_markup=category_keyboard(away, home, match_date))


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
    rows = [[InlineKeyboardButton(f"{NAMES.get(a, a)} @ {NAMES.get(h, h)} · {hour}", callback_data=f"g:{a}:{h}:{date}")] for a, h, hour, date in games]
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


async def show_player_profile(query, ctx, index):
    state = ctx.user_data.get("analysis_state")
    if not state or state.get("kind") != "players":
        await render(query, "Cette analyse a expiré. Sélectionne à nouveau un match.", None)
        return
    ranked = state["ranked"]
    if index < 0 or index >= len(ranked):
        return
    p, _ = ranked[index]
    away, home, category, match_date = state["away"], state["home"], state["category"], state.get("match_date")
    markup = player_profile_keyboard(index, len(ranked))
    card = await build_player_card(p, category, away, home, match_date)
    if card:
        caption = format_player_profile(away, home, p, category, match_date, state["scores"], history=False)
        await render(query, caption, markup, card=card)
    else:
        full = format_player_profile(away, home, p, category, match_date, state["scores"], history=True)
        await render(query, full, markup)


async def on_button(update, ctx):
    query = update.callback_query
    parts = query.data.split(":")
    if parts[0] in ("oa", "om"):
        await handle_admin_callback(query, ctx, parts)
        return
    if not user_can_access(update, ctx):
        await query.answer("🔒 BLACK ICE est fermé.", show_alert=True)
        return
    action = parts[0]
    state = ctx.user_data.get("analysis_state")
    if action in ("p", "pn", "ph", "backtop", "backcat", "dt") and not state:
        await query.answer("Analyse expirée. Sélectionne à nouveau un match.", show_alert=True)
        return
    await query.answer()
    try:
        if action == "d":
            text, markup = await games_view(int(parts[1]))
            await render(query, text, markup)
            return
        if action in ("g", "m"):
            away, home = parts[1], parts[2]
            if away not in NAMES or home not in NAMES:
                return
            match_date = parts[3] if len(parts) > 3 and parts[3] != "none" else None
            card = await make_match_card(away, home, match_date)
            await render(query, category_text(away, home), category_keyboard(away, home, match_date), card=card)
            return
        if action == "c":
            if len(parts) != 5: return
            away, home = parts[1], parts[2]
            match_date = None if parts[3] == "none" else parts[3]
            category = parts[4]
            if away not in NAMES or home not in NAMES or category not in {k for _, k in CATEGORIES}: return
            await edit_any(query, f"⏳ Analyse {esc(NAMES[away])} @ {esc(NAMES[home])}...\nCatégorie : {esc(category)}")
            try:
                new_state = await prepare_category(away, home, category, match_date)
            except Exception as e:
                log.exception("Analyse %s : %s", category, e)
                new_state = {"kind": "text", "text": "Les données de cette catégorie sont indisponibles pour le moment. Réessaie dans un instant."}
            new_state.update({"away": away, "home": home, "category": category, "match_date": match_date})
            ctx.user_data["analysis_state"] = new_state
            back_kb = [[InlineKeyboardButton("⬅️ Retour aux catégories", callback_data="backcat")], [InlineKeyboardButton("📅 Autres matchs", callback_data="d:0")]]
            if new_state["kind"] == "players":
                await render(query, new_state["text"], player_top_keyboard(new_state["ranked"]), card=new_state.get("card"))
            elif new_state.get("card"):
                rows = [[InlineKeyboardButton("📜 Détails complets", callback_data="dt")]] + back_kb
                await render(query, new_state.get("caption") or new_state["text"], InlineKeyboardMarkup(rows), card=new_state["card"])
            else:
                await render(query, new_state["text"], InlineKeyboardMarkup(back_kb))
            return
        if action in ("p", "pn"):
            await show_player_profile(query, ctx, int(parts[1]))
            return
        if action == "ph":
            index = int(parts[1])
            if state.get("kind") != "players" or index < 0 or index >= len(state["ranked"]):
                return
            p, _ = state["ranked"][index]
            await send_chunks(query, format_player_profile(state["away"], state["home"], p, state["category"], state.get("match_date"), state["scores"], history=True))
            return
        if action == "dt":
            await send_chunks(query, state["text"])
            return
        if action == "backtop":
            if state.get("kind") != "players":
                card = await make_match_card(state["away"], state["home"], state.get("match_date"))
                await render(query, category_text(state["away"], state["home"]), category_keyboard(state["away"], state["home"], state.get("match_date")), card=card)
                return
            await render(query, state["text"], player_top_keyboard(state["ranked"]), card=state.get("card"))
            return
        if action == "backcat":
            card = await make_match_card(state["away"], state["home"], state.get("match_date"))
            await render(query, category_text(state["away"], state["home"]), category_keyboard(state["away"], state["home"], state.get("match_date")), card=card)
            return
    except Exception as e:
        log.exception("Erreur bouton Telegram : %s", e)
        try:
            await query.message.chat.send_message("⚠️ Une erreur est survenue. Réessaie dans un instant.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📅 Matchs", callback_data="d:0")]]))
        except Exception:
            pass

# =====================================================
# START / TEXTE / COMMANDES
# =====================================================
async def start(update, ctx):
    if not user_can_access(update, ctx):
        await update.message.reply_text("🔒 BLACK ICE est actuellement fermé.")
        return
    keyboard = ReplyKeyboardMarkup([[MENU_BTN]], resize_keyboard=True)
    await update.message.reply_text(
        "🏒 <b>BLACK ICE — ANALYSE NHL</b>\n\nSélectionne un match pour accéder aux statistiques.\n\n"
        "🖼️ Cartes visuelles : logos, photos, barres.\n📊 H2H domicile + extérieur.\n👤 TOP 5 avec fiches individuelles.\n"
        "🔥 Forme récente sur 5 et 10 matchs.\n💰 VALUE automatique avec tes cotes.\n"
        "🧬 Match Simulator : équipes + joueurs.\n🔒 Confiance des données /100.",
        parse_mode=HTML, reply_markup=keyboard
    )
    await send_games(update.message, 0)


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
                "/cotes — saisir les cotes\n/fermer — fermer le bot\n"
                "/ouvrir — ouvrir le bot\n/statut — état du bot\n"
                "/annuler — annuler une saisie\n/logout — fermer la session",
                parse_mode=HTML
            )
        else:
            await message.reply_text("❌ Mot de passe incorrect.")
        return
    flow = ctx.user_data.get("admin_flow")
    if flow and is_admin(update, ctx):
        step = flow.get("step")
        if step == "player":
            if len(text) < 2 or len(text) > 80:
                await message.reply_text("Nom de joueur invalide. Réessaie.")
                return
            flow["player"] = text
            flow["step"] = "market"
            await message.reply_text(
                f"Joueur : <b>{esc(text)}</b>\n\nChoisis le marché :",
                parse_mode=HTML, reply_markup=odds_market_keyboard()
            )
            return
        if step == "odd":
            try:
                odd = float(text.replace(",", "."))
            except ValueError:
                await message.reply_text("Cote invalide. Exemple : 2.05")
                return
            if not 1.01 <= odd <= 1000:
                await message.reply_text("La cote doit être comprise entre 1.01 et 1000.")
                return
            save_odd(flow["match_date"], flow["away"], flow["home"], flow["player"], flow["market"], odd)
            await message.reply_text(
                "✅ <b>COTE ENREGISTRÉE</b>\n\n"
                f"Match : {esc(NAMES[flow['away']])} @ {esc(NAMES[flow['home']])}\n"
                f"Date : {format_date(flow['match_date'])}\n"
                f"Joueur : {esc(flow['player'])}\n"
                f"Marché : {ODD_MARKETS[flow['market']]}\n"
                f"Cote : <b>{fmt(odd, 2)}</b>\n\nCote sauvegardée dans SQLite.",
                parse_mode=HTML
            )
            ctx.user_data.pop("admin_flow", None)
            return
    if not user_can_access(update, ctx):
        await message.reply_text("🔒 BLACK ICE est actuellement fermé.")
        return
    if text == MENU_BTN or norm(text) in ("match", "matchs", "menu", "ce soir", "aujourdhui"):
        await send_games(message, 0)
        return
    parts = [p.strip() for p in SPLIT.split(text) if p.strip()]
    if len(parts) == 2:
        away, home = find_team(parts[0]), find_team(parts[1])
        if away and home and away != home:
            await message.chat.send_action("upload_photo")
            date = await find_upcoming_date(away, home)
            await send_category_menu(message, away, home, date)
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
                        await send_category_menu(message, away, home, date)
                        return
            await message.reply_text(f"Aucun match trouvé pour {NAMES[team]} dans les 7 prochains jours.")
            return
    await message.reply_text("Écris deux équipes, par exemple « Washington vs Montréal », ou touche « Matchs du jour ».")


async def on_error(update, ctx):
    log.error("Erreur non gérée : %s", ctx.error, exc_info=ctx.error)
    try:
        if update and update.effective_chat:
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
    app = Application.builder().token(TOKEN).concurrent_updates(True).post_shutdown(shutdown).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("logout", cmd_logout))
    app.add_handler(CommandHandler("cotes", cmd_cotes))
    app.add_handler(CommandHandler("fermer", cmd_fermer))
    app.add_handler(CommandHandler("ouvrir", cmd_ouvrir))
    app.add_handler(CommandHandler("statut", cmd_statut))
    app.add_handler(CommandHandler("annuler", cmd_annuler))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    log.info("BLACK ICE démarré — cartes images — TOP 5 — fiches joueurs — H2H — VALUE — Match Simulator — SQLite")
    app.run_polling()


if __name__ == "__main__":
    main()
