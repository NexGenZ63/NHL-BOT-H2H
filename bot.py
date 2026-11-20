import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

# ============================================================
# ICE VENOM MULTISPORTS — NHL / NBA / TENNIS / FOOTBALL
# Calendrier : aujourd'hui + demain uniquement.
# ============================================================

TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED_USERS = {int(x.strip()) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x.strip().isdigit()}
TZ = ZoneInfo("Europe/Paris")
NHL_API = "https://api-web.nhle.com/v1"
SPORTSCORE_API = "https://sportscore.com/api"
ATTRIBUTION = '<a href="https://sportscore.com/">Powered by SportScore</a>'
CACHE_TTL = 60
MAX_BUTTONS = 35

SPORTS = {
    "nhl": {"name": "HOCKEY NHL", "emoji": "🏒"},
    "nba": {"name": "BASKET NBA", "emoji": "🏀"},
    "tennis": {"name": "TENNIS", "emoji": "🎾"},
    "football": {"name": "FOOTBALL", "emoji": "⚽"},
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ice_venom")
HTTP = None
CACHE = {}


# ------------------------- HTTP / CACHE -------------------------

async def http_client():
    global HTTP
    if HTTP is None or HTTP.is_closed:
        HTTP = httpx.AsyncClient(
            timeout=httpx.Timeout(20),
            follow_redirects=True,
            headers={"User-Agent": "ICE-VENOM-MULTISPORTS/2.1"},
        )
    return HTTP


async def get_json(url, params=None):
    client = await http_client()
    response = await client.get(url, params=params)
    response.raise_for_status()
    return response.json()


async def cached(key, factory):
    now = time.time()
    item = CACHE.get(key)
    if item and now - item[0] < CACHE_TTL:
        return item[1]
    value = await factory()
    CACHE[key] = (now, value)
    return value


# ------------------------- HELPERS -------------------------

def day_date(offset):
    return datetime.now(TZ).date() + timedelta(days=offset)


def utc_dates_for_paris_day(offset):
    """UTC dates qui intersectent la journée civile de Paris demandée."""
    day = day_date(offset)
    start = datetime.combine(day, datetime.min.time(), tzinfo=TZ).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=TZ).astimezone(timezone.utc)
    current = start.date()
    last = (end - timedelta(microseconds=1)).date()
    result = []
    while current <= last:
        result.append(current.isoformat())
        current += timedelta(days=1)
    return result


def first(obj, *keys):
    if not isinstance(obj, dict):
        return None
    for key in keys:
        value = obj.get(key)
        if value is not None:
            return value
    return None


def text(value, default="?"):
    if value is None:
        return default
    if isinstance(value, dict):
        return str(value.get("name") or value.get("default") or value.get("shortName") or default)
    return str(value)


def items_from(payload):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("matches", "fixtures", "events", "results", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = items_from(value)
            if nested:
                return nested
    return []


def participant(match, side):
    keys = ("home", "home_team", "homeTeam", "player1", "competitor1") if side == "home" else ("away", "away_team", "awayTeam", "player2", "competitor2")
    value = first(match, *keys)
    if isinstance(value, dict):
        value = first(value, "name", "shortName", "commonName", "displayName")
    return text(value)


def match_datetime(match):
    raw = first(match, "time", "start_time", "startTime", "datetime", "startTimeUTC")
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(TZ)
    except (ValueError, TypeError):
        return None


def match_id(match):
    return first(match, "id", "match_id", "fixture_id", "event_id", "gameId")


def match_slug(match):
    return first(match, "slug", "match_slug", "fixture_slug")


def match_status(match):
    return text(first(match, "status_text", "status", "state", "gameState"), "Inconnu")


def match_score(match):
    home = first(match, "home_score", "homeScore", "score_home")
    away = first(match, "away_score", "awayScore", "score_away")
    if home is None and away is None:
        return "À venir"
    return f"{away if away is not None else '-'} - {home if home is not None else '-'}"


def api_sport(sport):
    return {"nba": "basketball", "tennis": "tennis", "football": "football"}[sport]


# ------------------------- NHL -------------------------

async def nhl_day(offset):
    day = day_date(offset).isoformat()

    async def request():
        payload = await get_json(f"{NHL_API}/schedule/{day}")
        result = []
        for week_day in payload.get("gameWeek", []):
            if week_day.get("date") != day:
                continue
            for game in week_day.get("games", []):
                away, home = game.get("awayTeam") or {}, game.get("homeTeam") or {}
                away_name = first(away.get("commonName", {}), "default") or first(away.get("placeName", {}), "default") or away.get("abbrev")
                home_name = first(home.get("commonName", {}), "default") or first(home.get("placeName", {}), "default") or home.get("abbrev")
                result.append({
                    "id": str(game.get("id")),
                    "slug": str(game.get("id")),
                    "home": text(home_name),
                    "away": text(away_name),
                    "home_score": home.get("score"),
                    "away_score": away.get("score"),
                    "status": game.get("gameState", "Inconnu"),
                    "time": game.get("startTimeUTC"),
                    "competition": "NHL",
                })
        return result

    return await cached(("nhl", day), request)


# ------------------------- SPORTScore -------------------------

async def sportscore_utc_day(sport, utc_day):
    async def request():
        return await get_json(
            f"{SPORTSCORE_API}/v1/fixtures/",
            params={"sport": api_sport(sport), "date": utc_day, "limit": 200},
        )
    return await cached(("sportscore", sport, utc_day), request)


async def sportscore_day(sport, offset):
    utc_days = utc_dates_for_paris_day(offset)
    responses = await asyncio.gather(
        *(sportscore_utc_day(sport, day) for day in utc_days),
        return_exceptions=True,
    )
    found = {}
    errors = []

    for utc_day, payload in zip(utc_days, responses):
        if isinstance(payload, Exception):
            errors.append(payload)
            log.warning("SportScore %s %s : %s", sport, utc_day, payload)
            continue

        for match in items_from(payload):
            if not isinstance(match, dict):
                continue

            kickoff = match_datetime(match)
            if kickoff and kickoff.date() != day_date(offset):
                continue

            identifier = match_id(match) or match_slug(match)
            if identifier is None:
                identifier = (
                    participant(match, "home"),
                    participant(match, "away"),
                    str(first(match, "time", "date")),
                )
            found[str(identifier)] = match

    if not found and errors and len(errors) == len(utc_days):
        raise RuntimeError("Aucune réponse valide de SportScore pour cette journée.")

    result = list(found.values())
    result.sort(key=lambda m: match_datetime(m) or datetime.max.replace(tzinfo=TZ))
    return result


async def load_matches(sport, offset, ctx):
    if offset not in (0, 1):
        raise ValueError("Le calendrier est limité à aujourd'hui et demain.")
    matches = await (nhl_day(offset) if sport == "nhl" else sportscore_day(sport, offset))
    ctx.user_data[f"matches:{sport}:{offset}"] = matches
    return matches


# ------------------------- TELEGRAM UI -------------------------

def home_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏒 HOCKEY NHL", callback_data="sport:nhl"), InlineKeyboardButton("🏀 BASKET NBA", callback_data="sport:nba")],
        [InlineKeyboardButton("🎾 TENNIS", callback_data="sport:tennis"), InlineKeyboardButton("⚽ FOOTBALL", callback_data="sport:football")],
    ])


def sport_keyboard(sport):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 MATCHS AUJOURD'HUI", callback_data=f"day:{sport}:0")],
        [InlineKeyboardButton("📆 MATCHS DEMAIN", callback_data=f"day:{sport}:1")],
        [InlineKeyboardButton("⬅️ ACCUEIL", callback_data="home")],
    ])


def calendar_keyboard(sport, offset, matches):
    rows = []
    for index, match in enumerate(matches[:MAX_BUTTONS]):
        label = f"{participant(match, 'away')} - {participant(match, 'home')}"
        if len(label) > 58:
            label = label[:55] + "..."
        rows.append([InlineKeyboardButton(label, callback_data=f"match:{sport}:{offset}:{index}")])

    if len(matches) > MAX_BUTTONS:
        rows.append([InlineKeyboardButton(f"ℹ️ {len(matches) - MAX_BUTTONS} autres matchs non affichés", callback_data="noop")])

    rows.extend([
        [InlineKeyboardButton("📅 Aujourd'hui", callback_data=f"day:{sport}:0"), InlineKeyboardButton("📆 Demain", callback_data=f"day:{sport}:1")],
        [InlineKeyboardButton("⬅️ Retour au sport", callback_data=f"sport:{sport}")],
        [InlineKeyboardButton("🏠 Accueil", callback_data="home")],
    ])
    return InlineKeyboardMarkup(rows)


def calendar_text(sport, offset, matches):
    day = day_date(offset)
    label = "Aujourd'hui" if offset == 0 else "Demain"
    lines = [
        f"{SPORTS[sport]['emoji']} <b>{SPORTS[sport]['name']}</b>",
        f"📅 {label} — {day.strftime('%d/%m/%Y')}",
        "",
    ]
    if matches:
        lines += [f"Rencontres trouvées : <b>{len(matches)}</b>", "Sélectionne une rencontre ci-dessous."]
    else:
        lines += ["Aucun match trouvé pour cette date.", "Choisis l'autre journée ou retourne au menu."]
    if sport != "nhl":
        lines += ["", ATTRIBUTION]
    return "\n".join(lines)


async def show_calendar(message, sport, offset, ctx):
    matches = await load_matches(sport, offset, ctx)
    await message.edit_text(
        calendar_text(sport, offset, matches),
        parse_mode=ParseMode.HTML,
        reply_markup=calendar_keyboard(sport, offset, matches),
        disable_web_page_preview=True,
    )


# ------------------------- MATCH DETAILS -------------------------

async def match_detail(query, sport, offset, index, ctx):
    matches = ctx.user_data.get(f"matches:{sport}:{offset}", [])
    if index < 0 or index >= len(matches):
        await query.edit_message_text("Cette sélection a expiré. Recharge les matchs.", reply_markup=sport_keyboard(sport))
        return

    match = matches[index]

    if sport == "nhl":
        game_id = match_id(match)
        if game_id:
            try:
                detail = await get_json(f"{NHL_API}/gamecenter/{game_id}/landing")
                away, home = detail.get("awayTeam") or {}, detail.get("homeTeam") or {}
                away_name = first(away.get("commonName", {}), "default") or first(away.get("placeName", {}), "default") or away.get("abbrev")
                home_name = first(home.get("commonName", {}), "default") or first(home.get("placeName", {}), "default") or home.get("abbrev")
                body = (
                    f"🏒 <b>FICHE MATCH NHL</b>\n\n"
                    f"<b>{escape(text(away_name))}</b> "
                    f"{away.get('score', '-')} - {home.get('score', '-')} "
                    f"<b>{escape(text(home_name))}</b>\n\n"
                    f"Statut : {escape(str(detail.get('gameState', 'Inconnu')))}"
                )
            except Exception:
                log.exception("Détail NHL indisponible")
                body = fixture_text(sport, match)
        else:
            body = fixture_text(sport, match)
    else:
        slug = match_slug(match)
        if slug:
            try:
                detail = await get_json(
                    f"{SPORTSCORE_API}/widget/match/",
                    params={"sport": api_sport(sport), "slug": slug},
                )
                if isinstance(detail, dict):
                    match = detail.get("match") or detail.get("data") or detail
                body = fixture_text(sport, match)
            except Exception:
                log.exception("Détail SportScore indisponible")
                body = fixture_text(sport, match)
        else:
            body = fixture_text(sport, match)

    await query.edit_message_text(
        body,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ RETOUR AUX MATCHS", callback_data=f"day:{sport}:{offset}")],
            [InlineKeyboardButton("🏠 ACCUEIL", callback_data="home")],
        ]),
        disable_web_page_preview=True,
    )


def fixture_text(sport, match):
    kickoff = match_datetime(match)
    when = f"\nHeure de Paris : {kickoff.strftime('%d/%m/%Y %H:%M')}" if kickoff else ""
    competition = first(match, "competition", "league", "tournament")
    comp = f"\nCompétition : {escape(text(competition))}" if competition else ""
    return (
        f"{SPORTS[sport]['emoji']} <b>FICHE MATCH {SPORTS[sport]['name']}</b>\n\n"
        f"<b>{escape(participant(match, 'away'))}</b>\n"
        f"<b>{escape(match_score(match))}</b>\n"
        f"<b>{escape(participant(match, 'home'))}</b>\n\n"
        f"Statut : {escape(match_status(match))}{when}{comp}"
        + (f"\n\n{ATTRIBUTION}" if sport != "nhl" else "")
    )


# ------------------------- COMMANDES / CALLBACKS -------------------------

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and update.effective_user.id not in ALLOWED_USERS:
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    ctx.user_data.clear()
    await update.message.reply_text(
        "🐍 <b>BIENVENUE SUR ICE VENOM</b>\n\n"
        "Ton bot d'analyse et de statistiques multisports.\n\n"
        "Sélectionne un sport :",
        parse_mode=ParseMode.HTML,
        reply_markup=home_keyboard(),
    )


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if ALLOWED_USERS and update.effective_user.id not in ALLOWED_USERS:
        await query.answer("Accès non autorisé.", show_alert=True)
        return

    await query.answer()
    parts = query.data.split(":")
    action = parts[0]

    if action == "noop":
        return

    if action == "home":
        await query.edit_message_text(
            "🐍 <b>BIENVENUE SUR ICE VENOM</b>\n\nSélectionne un sport :",
            parse_mode=ParseMode.HTML,
            reply_markup=home_keyboard(),
        )
        return

    if action == "sport":
        sport = parts[1]
        if sport not in SPORTS:
            return
        await query.edit_message_text(
            f"{SPORTS[sport]['emoji']} <b>ICE VENOM — {SPORTS[sport]['name']}</b>\n\n"
            "Choisis une journée. Seuls aujourd'hui et demain sont proposés.",
            parse_mode=ParseMode.HTML,
            reply_markup=sport_keyboard(sport),
        )
        return

    if action == "day":
        sport, offset = parts[1], int(parts[2])
        if sport not in SPORTS or offset not in (0, 1):
            return

        # Le message de chargement est édité, jamais envoyé comme un nouveau message.
        await query.edit_message_text(f"⏳ Chargement des matchs {SPORTS[sport]['name']}...")
        try:
            await show_calendar(query.message, sport, offset, ctx)
        except Exception as error:
            log.exception("Chargement calendrier %s impossible", sport)
            await query.edit_message_text(
                "⚠️ Impossible de charger les matchs pour le moment.\n"
                f"Erreur : {escape(str(error)[:250])}",
                parse_mode=ParseMode.HTML,
                reply_markup=sport_keyboard(sport),
            )
        return

    if action == "match":
        sport, offset, index = parts[1], int(parts[2]), int(parts[3])
        if sport not in SPORTS or offset not in (0, 1):
            return
        await query.edit_message_text("⏳ Chargement de la fiche du match...")
        try:
            await match_detail(query, sport, offset, index, ctx)
        except Exception:
            log.exception("Ouverture fiche match impossible")
            await query.edit_message_text("⚠️ Impossible d'ouvrir cette rencontre.", reply_markup=sport_keyboard(sport))


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and update.effective_user.id not in ALLOWED_USERS:
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    value = (update.message.text or "").strip().upper()
    if value in ("MENU", "ACCUEIL", "OUVRIR ICE VENOM", "OUVRIR BLACK ICE"):
        await update.message.reply_text(
            "🐍 <b>ICE VENOM MULTISPORTS</b>\n\nSélectionne ton sport :",
            parse_mode=ParseMode.HTML,
            reply_markup=home_keyboard(),
        )
        return

    aliases = {
        "NHL": "nhl", "HOCKEY": "nhl",
        "NBA": "nba", "BASKET": "nba",
        "TENNIS": "tennis",
        "FOOT": "football", "FOOTBALL": "football",
    }
    sport = aliases.get(value)
    if sport:
        await update.message.reply_text(
            f"{SPORTS[sport]['emoji']} <b>{SPORTS[sport]['name']}</b>\n\nChoisis une journée :",
            parse_mode=ParseMode.HTML,
            reply_markup=sport_keyboard(sport),
        )
    else:
        await update.message.reply_text(
            "Sélectionne un sport avec les boutons ou écris NHL, NBA, TENNIS ou FOOTBALL.",
            reply_markup=home_keyboard(),
        )


async def on_error(update, ctx):
    log.error("Erreur Telegram", exc_info=ctx.error)


async def shutdown(app):
    global HTTP
    if HTTP is not None and not HTTP.is_closed:
        await HTTP.aclose()
    HTTP = None


def main():
    app = Application.builder().token(TOKEN).post_shutdown(shutdown).build()
    app.add_handler(CommandHandler(["start", "help", "menu"], start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    log.info("ICE VENOM lancé : NHL / NBA / TENNIS / FOOTBALL — aujourd'hui + demain uniquement")
    app.run_polling()


if __name__ == "__main__":
    main()