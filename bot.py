import os
import re
import json
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import quote

import httpx
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================================================
# ICE VENOM MULTISPORTS
# NHL / NBA / TENNIS / FOOTBALL
# ============================================================

TOKEN = os.environ["TELEGRAM_TOKEN"]

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ALLOWED_USERS = {
    int(x)
    for x in os.environ.get("ALLOWED_USERS", "").split(",")
    if x.strip().isdigit()
}

TZ = ZoneInfo("Europe/Paris")

SPORTSCORE_BASE = "https://sportscore.com"
NHL_API = "https://api-web.nhle.com/v1"

SPORTSCORE_ATTRIBUTION = (
    '<a href="https://sportscore.com/">Powered by SportScore</a>'
)

SIMULATIONS = 10000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

log = logging.getLogger("ice_venom")

# ============================================================
# SPORTS
# ============================================================

SPORTS = {
    "nhl": {
        "name": "HOCKEY NHL",
        "emoji": "🏒",
        "api": "nhl",
    },
    "nba": {
        "name": "BASKET NBA",
        "emoji": "🏀",
        "api": "basketball",
    },
    "tennis": {
        "name": "TENNIS",
        "emoji": "🎾",
        "api": "tennis",
    },
    "football": {
        "name": "FOOTBALL",
        "emoji": "⚽",
        "api": "football",
    },
}

# ============================================================
# CLIENT HTTP
# ============================================================

HTTP = None

async def get_client():
    global HTTP

    if HTTP is None:
        HTTP = httpx.AsyncClient(
            timeout=25,
            follow_redirects=True,
            headers={
                "User-Agent": "ICE-VENOM-MULTISPORTS/1.0"
            },
        )

    return HTTP


async def api_get(url, params=None):
    client = await get_client()

    response = await client.get(
        url,
        params=params,
    )

    response.raise_for_status()

    return response.json()


# ============================================================
# UTILITAIRES
# ============================================================

def clean(value):
    if value is None:
        return "Non disponible"

    if isinstance(value, dict):
        return (
            value.get("name")
            or value.get("default")
            or str(value)
        )

    return str(value)


def safe_html(value):
    from html import escape
    return escape(str(value))


def today():
    return datetime.now(TZ).date()


def date_for(offset=0):
    return (
        today() + timedelta(days=offset)
    ).isoformat()


def extract_list(data):
    """
    Normalise les différentes enveloppes JSON
    utilisées par les endpoints sportifs.
    """

    if isinstance(data, list):
        return data

    if not isinstance(data, dict):
        return []

    for key in (
        "matches",
        "fixtures",
        "games",
        "data",
        "events",
        "results",
    ):
        value = data.get(key)

        if isinstance(value, list):
            return value

        if isinstance(value, dict):
            nested = extract_list(value)
            if nested:
                return nested

    return []


def extract_slug(match):
    return (
        match.get("slug")
        or match.get("match_slug")
        or match.get("id")
        or match.get("event_id")
    )


def extract_match_names(match):
    home = (
        match.get("home")
        or match.get("home_team")
        or match.get("homeTeam")
        or match.get("player1")
        or match.get("competitor1")
    )

    away = (
        match.get("away")
        or match.get("away_team")
        or match.get("awayTeam")
        or match.get("player2")
        or match.get("competitor2")
    )

    return clean(home), clean(away)


def extract_score(match):
    home_score = (
        match.get("home_score")
        or match.get("homeScore")
    )

    away_score = (
        match.get("away_score")
        or match.get("awayScore")
    )

    if home_score is None and away_score is None:
        return "Score non disponible"

    return f"{away_score} - {home_score}"


def extract_status(match):
    return clean(
        match.get("status_text")
        or match.get("status")
        or match.get("state")
        or "Statut inconnu"
    )


def sport_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🏒 HOCKEY NHL",
                callback_data="sport:nhl",
            )
        ],
        [
            InlineKeyboardButton(
                "🏀 BASKET NBA",
                callback_data="sport:nba",
            )
        ],
        [
            InlineKeyboardButton(
                "🎾 TENNIS",
                callback_data="sport:tennis",
            )
        ],
        [
            InlineKeyboardButton(
                "⚽ FOOTBALL",
                callback_data="sport:football",
            )
        ],
        [
            InlineKeyboardButton(
                "📊 MES ANALYSES",
                callback_data="analyses",
            )
        ],
    ])


def home_text():
    return (
        "🐍 <b>ICE VENOM — MULTISPORTS</b>\n\n"
        "Bienvenue dans ton centre d'analyse sportive.\n\n"
        "🏒 NHL — Hockey\n"
        "🏀 NBA — Basket\n"
        "🎾 TENNIS — Circuits internationaux\n"
        "⚽ FOOTBALL — Compétitions internationales\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🧠 Statistiques et analyses\n"
        "📊 Historique et confrontations\n"
        "🎯 Analyses individuelles\n"
        "🧬 Simulations statistiques\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "<b>SÉLECTIONNE TON SPORT :</b>"
    )


# ============================================================
# ACCÈS UTILISATEUR
# ============================================================

def authorized(update):
    user = update.effective_user

    if not user:
        return False

    if not ALLOWED_USERS:
        return True

    if user.id in ALLOWED_USERS:
        return True

    return False


async def access_denied(update):
    if update.callback_query:
        await update.callback_query.answer(
            "Accès non autorisé.",
            show_alert=True,
        )
    elif update.message:
        await update.message.reply_text(
            "⛔ Accès non autorisé."
        )


# ============================================================
# NHL — API OFFICIELLE
# ============================================================

async def nhl_matches(offset=0):
    date = date_for(offset)

    data = await api_get(
        f"{NHL_API}/schedule/{date}"
    )

    games = []

    for day in data.get("gameWeek", []):
        if day.get("date") != date:
            continue

        for game in day.get("games", []):
            away = game.get("awayTeam", {})
            home = game.get("homeTeam", {})

            games.append({
                "id": game.get("id"),
                "slug": str(game.get("id")),
                "home": home.get("commonName", {}).get(
                    "default",
                    home.get("placeName", {}).get("default", "?"),
                ),
                "away": away.get("commonName", {}).get(
                    "default",
                    away.get("placeName", {}).get("default", "?"),
                ),
                "home_score": home.get("score"),
                "away_score": away.get("score"),
                "status": game.get("gameState", "UNKNOWN"),
                "time": game.get("startTimeUTC"),
            })

    return games


async def nhl_match_detail(game_id):
    return await api_get(
        f"{NHL_API}/gamecenter/{game_id}/landing"
    )


# ============================================================
# AUTRES SPORTS — SPORT SCORE
# ============================================================

async def sportscore_matches(sport, offset=0):
    api_sport = SPORTS[sport]["api"]

    params = {
        "sport": api_sport,
        "date": date_for(offset),
        "limit": 100,
    }

    data = await api_get(
        f"{SPORTSCORE_BASE}/api/v1/fixtures/",
        params=params,
    )

    return extract_list(data)


async def sportscore_recent_matches(sport):
    api_sport = SPORTS[sport]["api"]

    data = await api_get(
        f"{SPORTSCORE_BASE}/api/widget/matches/",
        params={
            "sport": api_sport,
            "limit": 30,
        },
    )

    return extract_list(data)


async def sportscore_match_detail(sport, slug):
    api_sport = SPORTS[sport]["api"]

    return await api_get(
        f"{SPORTSCORE_BASE}/api/widget/match/",
        params={
            "sport": api_sport,
            "slug": slug,
        },
    )


async def sportscore_search(sport, query):
    api_sport = SPORTS[sport]["api"]

    return await api_get(
        f"{SPORTSCORE_BASE}/api/v1/search/",
        params={
            "sport": api_sport,
            "q": query,
            "limit": 15,
        },
    )


async def sportscore_standings(sport, competition):
    api_sport = SPORTS[sport]["api"]

    return await api_get(
        f"{SPORTSCORE_BASE}/api/widget/standings/",
        params={
            "sport": api_sport,
            "slug": competition,
        },
    )


async def sportscore_player(sport, player_slug):
    api_sport = SPORTS[sport]["api"]

    return await api_get(
        f"{SPORTSCORE_BASE}/api/widget/player/",
        params={
            "sport": api_sport,
            "slug": player_slug,
        },
    )


async def sportscore_topscorers(
    sport,
    competition,
    stat="goals",
):
    api_sport = SPORTS[sport]["api"]

    return await api_get(
        f"{SPORTSCORE_BASE}/api/widget/topscorers/",
        params={
            "sport": api_sport,
            "slug": competition,
            "limit": 20,
            "stat": stat,
        },
    )


# ============================================================
# CALENDRIER / AFFICHAGE DES MATCHS
# ============================================================

def day_keyboard(sport, offset):
    rows = []

    rows.append([
        InlineKeyboardButton(
            "Aujourd'hui",
            callback_data=f"day:{sport}:0",
        ),
        InlineKeyboardButton(
            "Demain",
            callback_data=f"day:{sport}:1",
        ),
    ])

    rows.append([
        InlineKeyboardButton(
            "Après-demain",
            callback_data=f"day:{sport}:2",
        )
    ])

    rows.append([
        InlineKeyboardButton(
            "🔍 Rechercher",
            callback_data=f"search:{sport}",
        )
    ])

    rows.append([
        InlineKeyboardButton(
            "⬅️ Accueil",
            callback_data="home",
        )
    ])

    return InlineKeyboardMarkup(rows)


def match_keyboard(sport, matches):
    rows = []

    for index, match in enumerate(matches[:35]):
        home, away = extract_match_names(match)

        label = (
            f"{away[:22]} - {home[:22]}"
        )

        slug = extract_slug(match)

        if not slug:
            continue

        rows.append([
            InlineKeyboardButton(
                label[:60],
                callback_data=f"match:{sport}:{slug}",
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "📅 Autres dates",
            callback_data=f"dates:{sport}",
        )
    ])

    rows.append([
        InlineKeyboardButton(
            "⬅️ Retour",
            callback_data="home",
        )
    ])

    return InlineKeyboardMarkup(rows)


def format_matches(sport, matches):
    info = SPORTS[sport]

    lines = [
        f"{info['emoji']} <b>{info['name']}</b>",
        "",
        f"📅 {today().strftime('%d/%m/%Y')}",
        "",
    ]

    if not matches:
        lines.extend([
            "Aucun match trouvé pour cette date.",
            "",
            "Essaie une autre journée.",
        ])

    else:
        lines.append(
            f"Nombre de rencontres : {len(matches)}"
        )

        lines.append(
            "Sélectionne une rencontre ci-dessous."
        )

    if sport != "nhl":
        lines.extend([
            "",
            SPORTSCORE_ATTRIBUTION,
        ])

    return "\n".join(lines)


async def display_matches(message, sport, offset=0):
    try:
        if sport == "nhl":
            matches = await nhl_matches(offset)
        else:
            matches = await sportscore_matches(
                sport,
                offset,
            )

        text = format_matches(sport, matches)

        await message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=match_keyboard(
                sport,
                matches,
            ),
            disable_web_page_preview=True,
        )

    except httpx.HTTPStatusError as error:
        log.exception("Erreur API %s", sport)

        await message.reply_text(
            f"⚠️ Le fournisseur de données {sport.upper()} "
            f"a répondu avec une erreur HTTP {error.response.status_code}.\n"
            "Réessaie plus tard."
        )

    except Exception:
        log.exception("Erreur calendrier %s", sport)

        await message.reply_text(
            "⚠️ Impossible de récupérer les rencontres.\n"
            "Les données sont peut-être momentanément indisponibles."
        )


# ============================================================
# MENUS PAR SPORT
# ============================================================

def sport_menu_keyboard(sport):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📅 MATCHS DU JOUR",
                callback_data=f"day:{sport}:0",
            )
        ],
        [
            InlineKeyboardButton(
                "📆 DEMAIN",
                callback_data=f"day:{sport}:1",
            )
        ],
        [
            InlineKeyboardButton(
                "📊 CLASSEMENTS",
                callback_data=f"standings:{sport}",
            )
        ],
        [
            InlineKeyboardButton(
                "🔍 RECHERCHER UNE ÉQUIPE / UN JOUEUR",
                callback_data=f"search:{sport}",
            )
        ],
        [
            InlineKeyboardButton(
                "⭐ TOP JOUEURS",
                callback_data=f"top:{sport}",
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ ACCUEIL",
                callback_data="home",
            )
        ],
    ])


def sport_menu_text(sport):
    info = SPORTS[sport]

    return (
        f"{info['emoji']} <b>ICE VENOM — {info['name']}</b>\n\n"
        "Choisis ton analyse :\n\n"
        "📅 Calendrier des rencontres\n"
        "📊 Classements disponibles\n"
        "🔍 Recherche équipes et joueurs\n"
        "⭐ Meilleurs joueurs et statistiques\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Sélectionne une fonctionnalité."
    )


# ============================================================
# FICHE MATCH
# ============================================================

async def display_match(query, sport, slug):
    try:
        if sport == "nhl":
            detail = await nhl_match_detail(slug)

            home = detail.get("homeTeam", {})
            away = detail.get("awayTeam", {})

            home_name = clean(
                home.get("commonName", {}).get("default")
            )

            away_name = clean(
                away.get("commonName", {}).get("default")
            )

            home_score = home.get("score", "?")
            away_score = away.get("score", "?")

            status = clean(
                detail.get("gameState")
            )

            text = (
                "🏒 <b>FICHE MATCH NHL</b>\n\n"
                f"<b>{safe_html(away_name)}</b> "
                f"{away_score} - {home_score} "
                f"<b>{safe_html(home_name)}</b>\n\n"
                f"Statut : {safe_html(status)}\n\n"
                "Données issues de l'API officielle NHL."
            )

        else:
            detail = await sportscore_match_detail(
                sport,
                slug,
            )

            if isinstance(detail, dict):
                match = (
                    detail.get("match")
                    or detail.get("data")
                    or detail
                )
            else:
                match = {}

            home, away = extract_match_names(match)

            score = extract_score(match)
            status = extract_status(match)

            text = (
                f"{SPORTS[sport]['emoji']} "
                f"<b>FICHE MATCH {SPORTS[sport]['name']}</b>\n\n"
                f"<b>{safe_html(away)}</b>\n"
                f"<b>{safe_html(score)}</b>\n"
                f"<b>{safe_html(home)}</b>\n\n"
                f"Statut : {safe_html(status)}\n\n"
                f"{SPORTSCORE_ATTRIBUTION}"
            )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "⬅️ Retour au sport",
                    callback_data=f"sport:{sport}",
                )
            ],
            [
                InlineKeyboardButton(
                    "🏠 Accueil",
                    callback_data="home",
                )
            ],
        ])

        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

    except Exception:
        log.exception("Erreur fiche match")

        await query.edit_message_text(
            "⚠️ Impossible de charger cette fiche match.\n"
            "La rencontre n'est peut-être plus disponible."
        )


# ============================================================
# RECHERCHE
# ============================================================

async def start_search(query, ctx, sport):
    ctx.user_data["search_sport"] = sport
    ctx.user_data["awaiting_search"] = True

    await query.message.reply_text(
        f"{SPORTS[sport]['emoji']} "
        f"<b>RECHERCHE {SPORTS[sport]['name']}</b>\n\n"
        "Envoie le nom d'une équipe, d'un joueur "
        "ou d'une compétition.",
        parse_mode=ParseMode.HTML,
    )


async def process_search(update, ctx):
    sport = ctx.user_data.get("search_sport")
    query = update.message.text.strip()

    if not sport or len(query) < 2:
        await update.message.reply_text(
            "Recherche invalide. Écris au moins deux caractères."
        )
        return

    if sport == "nhl":
        await update.message.reply_text(
            "La recherche NHL par nom sera ajoutée avec "
            "le catalogue officiel des équipes et joueurs.\n\n"
            "Tu peux déjà sélectionner les matchs depuis le calendrier."
        )
        ctx.user_data.pop("awaiting_search", None)
        return

    try:
        result = await sportscore_search(
            sport,
            query,
        )

        await update.message.reply_text(
            "🔍 <b>RÉSULTATS DE RECHERCHE</b>\n\n"
            f"{safe_html(json.dumps(result, ensure_ascii=False)[:3500])}\n\n"
            f"{SPORTSCORE_ATTRIBUTION}",
            parse_mode=ParseMode.HTML,
        )

    except Exception:
        log.exception("Recherche indisponible")

        await update.message.reply_text(
            "Aucun résultat exploitable ou service indisponible."
        )

    ctx.user_data.pop("awaiting_search", None)
    ctx.user_data.pop("search_sport", None)


# ============================================================
# CLASSEMENTS
# ============================================================

async def standings_request(query, sport, ctx):
    ctx.user_data["awaiting_competition"] = True
    ctx.user_data["competition_sport"] = sport

    await query.message.reply_text(
        "📊 <b>CLASSEMENT</b>\n\n"
        "Envoie le slug de la compétition.\n\n"
        "Exemples :\n"
        "premier-league\n"
        "la-liga\n"
        "serie-a\n"
        "bundesliga\n"
        "ligue-1",
        parse_mode=ParseMode.HTML,
    )


async def process_competition(update, ctx):
    sport = ctx.user_data.get("competition_sport")
    slug = update.message.text.strip().lower()

    try:
        data = await sportscore_standings(
            sport,
            slug,
        )

        await update.message.reply_text(
            "📊 <b>CLASSEMENT</b>\n\n"
            f"{safe_html(json.dumps(data, ensure_ascii=False)[:3500])}\n\n"
            f"{SPORTSCORE_ATTRIBUTION}",
            parse_mode=ParseMode.HTML,
        )

    except Exception:
        log.exception("Classement indisponible")

        await update.message.reply_text(
            "Classement indisponible pour cette compétition."
        )

    ctx.user_data.pop("awaiting_competition", None)
    ctx.user_data.pop("competition_sport", None)


# ============================================================
# TOP JOUEURS
# ============================================================

async def start_top(query, ctx, sport):
    if sport == "nhl":
        await query.message.reply_text(
            "⭐ Le classement des joueurs NHL sera alimenté "
            "par les statistiques officielles des joueurs."
        )
        return

    ctx.user_data["awaiting_top_competition"] = True
    ctx.user_data["top_sport"] = sport

    await query.message.reply_text(
        "⭐ <b>TOP JOUEURS</b>\n\n"
        "Envoie le slug de la compétition.\n\n"
        "Exemple : premier-league",
        parse_mode=ParseMode.HTML,
    )


async def process_top(update, ctx):
    sport = ctx.user_data.get("top_sport")
    competition = update.message.text.strip().lower()

    stat = "goals"

    if sport == "football":
        stat = "goals"

    try:
        result = await sportscore_topscorers(
            sport,
            competition,
            stat,
        )

        await update.message.reply_text(
            "⭐ <b>TOP JOUEURS</b>\n\n"
            f"{safe_html(json.dumps(result, ensure_ascii=False)[:3500])}\n\n"
            f"{SPORTSCORE_ATTRIBUTION}",
            parse_mode=ParseMode.HTML,
        )

    except Exception:
        log.exception("Top joueurs indisponible")

        await update.message.reply_text(
            "Données des meilleurs joueurs indisponibles."
        )

    ctx.user_data.pop("awaiting_top_competition", None)
    ctx.user_data.pop("top_sport", None)


# ============================================================
# MES ANALYSES
# ============================================================

async def show_analyses(query):
    await query.edit_message_text(
        "📊 <b>MES ANALYSES — ICE VENOM</b>\n\n"
        "Ton espace personnel d'analyses sera organisé "
        "par sport.\n\n"
        "🏒 NHL\n"
        "🏀 NBA\n"
        "🎾 TENNIS\n"
        "⚽ FOOTBALL\n\n"
        "Les analyses enregistrées seront ajoutées "
        "dans cette section.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "⬅️ ACCUEIL",
                    callback_data="home",
                )
            ]
        ]),
    )


# ============================================================
# COMMANDES TELEGRAM
# ============================================================

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await access_denied(update)
        return

    ctx.user_data.clear()

    await update.message.reply_text(
        home_text(),
        parse_mode=ParseMode.HTML,
        reply_markup=sport_keyboard(),
        disable_web_page_preview=True,
    )


async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await access_denied(update)
        return

    await update.message.reply_text(
        home_text(),
        parse_mode=ParseMode.HTML,
        reply_markup=sport_keyboard(),
    )


async def cmd_nhl(update, ctx):
    if not authorized(update):
        await access_denied(update)
        return

    await update.message.reply_text(
        sport_menu_text("nhl"),
        parse_mode=ParseMode.HTML,
        reply_markup=sport_menu_keyboard("nhl"),
    )


async def cmd_nba(update, ctx):
    if not authorized(update):
        await access_denied(update)
        return

    await update.message.reply_text(
        sport_menu_text("nba"),
        parse_mode=ParseMode.HTML,
        reply_markup=sport_menu_keyboard("nba"),
    )


async def cmd_tennis(update, ctx):
    if not authorized(update):
        await access_denied(update)
        return

    await update.message.reply_text(
        sport_menu_text("tennis"),
        parse_mode=ParseMode.HTML,
        reply_markup=sport_menu_keyboard("tennis"),
    )


async def cmd_football(update, ctx):
    if not authorized(update):
        await access_denied(update)
        return

    await update.message.reply_text(
        sport_menu_text("football"),
        parse_mode=ParseMode.HTML,
        reply_markup=sport_menu_keyboard("football"),
    )


# ============================================================
# CALLBACKS
# ============================================================

async def on_button(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not authorized(update):
        await access_denied(update)
        return

    await query.answer()

    try:
        parts = query.data.split(":")
        action = parts[0]

        if action == "home":
            ctx.user_data.clear()

            await query.edit_message_text(
                home_text(),
                parse_mode=ParseMode.HTML,
                reply_markup=sport_keyboard(),
            )
            return

        if action == "sport":
            sport = parts[1]

            if sport not in SPORTS:
                return

            ctx.user_data["active_sport"] = sport

            await query.edit_message_text(
                sport_menu_text(sport),
                parse_mode=ParseMode.HTML,
                reply_markup=sport_menu_keyboard(sport),
            )
            return

        if action == "day":
            sport = parts[1]
            offset = int(parts[2])

            await query.edit_message_text(
                f"⏳ Chargement des matchs {sport.upper()}..."
            )

            await display_matches(
                query.message,
                sport,
                offset,
            )
            return

        if action == "dates":
            sport = parts[1]

            await query.edit_message_text(
                "📅 <b>CHOISIS UNE DATE</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=day_keyboard(sport, 0),
            )
            return

        if action == "match":
            sport = parts[1]
            slug = ":".join(parts[2:])

            await query.edit_message_text(
                "⏳ Chargement de la fiche..."
            )

            await display_match(
                query,
                sport,
                slug,
            )
            return

        if action == "search":
            await start_search(
                query,
                ctx,
                parts[1],
            )
            return

        if action == "standings":
            await standings_request(
                query,
                parts[1],
                ctx,
            )
            return

        if action == "top":
            await start_top(
                query,
                ctx,
                parts[1],
            )
            return

        if action == "analyses":
            await show_analyses(query)
            return

    except Exception:
        log.exception("Erreur callback")

        try:
            await query.message.reply_text(
                "⚠️ Une erreur est survenue. Réessaie."
            )
        except Exception:
            pass


# ============================================================
# GESTION DES MESSAGES
# ============================================================

async def on_text(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
):
    if not authorized(update):
        await access_denied(update)
        return

    text = (update.message.text or "").strip()

    if ctx.user_data.get("awaiting_search"):
        await process_search(update, ctx)
        return

    if ctx.user_data.get("awaiting_competition"):
        await process_competition(update, ctx)
        return

    if ctx.user_data.get("awaiting_top_competition"):
        await process_top(update, ctx)
        return

    if text.upper() in (
        "MENU",
        "ACCUEIL",
        "OUVRIR ICE VENOM",
        "OUVRIR BLACK ICE",
    ):
        await cmd_menu(update, ctx)
        return

    # Reconnaissance directe des sports
    aliases = {
        "NHL": "nhl",
        "HOCKEY": "nhl",
        "NBA": "nba",
        "BASKET": "nba",
        "TENNIS": "tennis",
        "FOOT": "football",
        "FOOTBALL": "football",
    }

    selected = aliases.get(text.upper())

    if selected:
        sport = selected

        await update.message.reply_text(
            sport_menu_text(sport),
            parse_mode=ParseMode.HTML,
            reply_markup=sport_menu_keyboard(sport),
        )
        return

    await update.message.reply_text(
        "🐍 <b>ICE VENOM MULTISPORTS</b>\n\n"
        "Sélectionne un sport avec les boutons ci-dessous "
        "ou écris NHL, NBA, TENNIS ou FOOTBALL.",
        parse_mode=ParseMode.HTML,
        reply_markup=sport_keyboard(),
    )


# ============================================================
# ERREURS
# ============================================================

async def on_error(update, ctx):
    log.exception(
        "Erreur Telegram :",
        exc_info=ctx.error,
    )


# ============================================================
# FERMETURE HTTP
# ============================================================

async def post_shutdown(app):
    global HTTP

    if HTTP:
        await HTTP.aclose()
        HTTP = None


# ============================================================
# MAIN
# ============================================================

def main():
    app = (
        Application.builder()
        .token(TOKEN)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))

    app.add_handler(CommandHandler("nhl", cmd_nhl))
    app.add_handler(CommandHandler("nba", cmd_nba))
    app.add_handler(CommandHandler("tennis", cmd_tennis))
    app.add_handler(CommandHandler("football", cmd_football))

    app.add_handler(
        CallbackQueryHandler(on_button)
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            on_text,
        )
    )

    app.add_error_handler(on_error)

    log.info(
        "ICE VENOM MULTISPORTS démarré : "
        "NHL / NBA / TENNIS / FOOTBALL"
    )

    app.run_polling()


if __name__ == "__main__":
    main()