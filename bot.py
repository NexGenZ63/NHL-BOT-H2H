"""Bot Telegram NHL : H2H strict sur 3 saisons.
Saisons : 2024-2025, 2025-2026 et 2026-2027.
Même configuration domicile / extérieur obligatoire.
"""

import asyncio
import difflib
import json
import logging
import os
import re
import time
import unicodedata

from datetime import datetime, timedelta
from html import escape as esc
from zoneinfo import ZoneInfo

import httpx

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

TOKEN = os.environ["TELEGRAM_TOKEN"]

ALLOWED = {
    int(x)
    for x in os.environ.get("ALLOWED_USERS", "").replace(" ", "").split(",")
    if x
}

API_WEB = "https://api-web.nhle.com/v1"
API_STATS = "https://api.nhle.com/stats/rest/en"

ET = ZoneInfo("America/Toronto")

PER_TEAM = 14
SHRINK = 3
TOP = 5

MENU_BTN = "📅 Matchs du jour"
HTML = ParseMode.HTML

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

log = logging.getLogger("nhlbot")


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


def norm(s):
    s = unicodedata.normalize("NFKD", s)
    s = "".join(
        c for c in s
        if not unicodedata.combining(c)
    ).lower()
    s = re.sub(r"[.'’]", "", s)
    return re.sub(r"\s+", " ", s).strip()


NAMES = {}
ALIASES = {}

for line in TEAM_DATA.splitlines():
    abbr, name, *_aliases = line.split("|")
    NAMES[abbr] = name

    for alias in [abbr, name, *_aliases]:
        ALIASES[norm(alias)] = abbr


def find_team(text):

    n = norm(text)

    if n in ALIASES:
        return ALIASES[n]

    close = difflib.get_close_matches(
        n,
        list(ALIASES),
        n=1,
        cutoff=0.75,
    )

    return ALIASES[close[0]] if close else None


SPLIT = re.compile(
    r"\s+(?:vs\.?|versus|contre|at|c\.)\s+|\s*@\s*|\s+-\s+",
    re.I,
)


_client = None


def client():

    global _client

    if _client is None:
        _client = httpx.AsyncClient(
            timeout=25,
            follow_redirects=True,
            headers={"User-Agent": "nhl-h2h-bot/1.0"},
        )

    return _client


async def get_json(url, params=None, tries=3):

    last = None

    for i in range(tries):
        try:
            response = await client().get(
                url,
                params=params,
            )

            response.raise_for_status()
            return response.json()

        except Exception as e:
            last = e
            await asyncio.sleep(0.6 * (i + 1))

    raise last


_cache = {}


async def cached(key, ttl, factory):

    hit = _cache.get(key)

    if hit and time.time() - hit[0] < ttl:
        return hit[1]

    value = await factory()

    _cache[key] = (time.time(), value)

    return value


# =====================================================
# SAISONS : 2024-2025, 2025-2026, 2026-2027
# =====================================================

def seasons():

    return [
        "20242025",
        "20252026",
        "20262027",
    ]


def current_season():

    now = datetime.now(ET)

    year = now.year if now.month >= 9 else now.year - 1

    return f"{year}{year + 1}"


def season_label(sid):

    return f"{sid[:4]}-{sid[6:]}"


# =====================================================
# JOUEURS
# =====================================================

async def pool_by_team(sid):

    sort = json.dumps([
        {"property": "points", "direction": "DESC"},
        {"property": "goals", "direction": "DESC"},
        {"property": "playerId", "direction": "ASC"},
    ])

    async def page(start):

        try:
            data = await get_json(
                f"{API_STATS}/skater/summary",
                params={
                    "isAggregate": "false",
                    "isGame": "false",
                    "sort": sort,
                    "start": start,
                    "limit": 100,
                    "cayenneExp": (
                        f"gameTypeId=2 and seasonId={sid}"
                    ),
                },
            )

            return data.get("data", [])

        except Exception:
            return []

    pages = await asyncio.gather(
        *(page(start) for start in range(0, 700, 100))
    )

    teams = {}
    seen = set()

    for player in (
        item
        for page_data in pages
        for item in page_data
    ):

        if player["playerId"] in seen:
            continue

        seen.add(player["playerId"])

        team = (
            player.get("teamAbbrevs") or ""
        ).split(",")[-1].strip()

        teams.setdefault(team, []).append({
            "id": player["playerId"],
            "name": player["skaterFullName"],
            "points": player.get("points", 0),
        })

    return teams


async def roster(team):

    sid = current_season()

    async def build():

        try:
            data = await get_json(
                f"{API_WEB}/club-stats/{team}/{sid}/2"
            )

            skaters = data.get("skaters") or []

            if skaters:
                return [
                    {
                        "id": p["playerId"],
                        "name": (
                            f'{p["firstName"]["default"]} '
                            f'{p["lastName"]["default"]}'
                        ),
                        "points": p.get("points", 0),
                    }
                    for p in skaters
                ]

        except Exception as e:
            log.warning("club-stats %s : %s", team, e)

        pool = await cached(
            ("pool", sid),
            3600,
            lambda: pool_by_team(sid),
        )

        return pool.get(team, [])

    players = await cached(
        ("roster", team, sid),
        3600,
        build,
    )

    return sorted(
        players,
        key=lambda p: -p["points"],
    )[:PER_TEAM]


def _row(date, opp, home, goals, assists, points, game_id=None):

    return {
        "date": (date or "")[:10],
        "opp": opp,
        "home": home,
        "goals": goals or 0,
        "assists": assists or 0,
        "points": points or 0,
        "game_id": (
            str(game_id)
            if game_id is not None
            else None
        ),
    }


async def game_log(pid):

    async def build():

        season_ids = seasons()

        results = await asyncio.gather(
            *[
                get_json(
                    f"{API_WEB}/player/{pid}/game-log/{season}/2"
                )
                for season in season_ids
            ],
            return_exceptions=True,
        )

        rows = []
        errors = []

        for result in results:

            if isinstance(result, Exception):
                errors.append(result)
                continue

            for g in result.get("gameLog", []):

                rows.append(
                    _row(
                        g.get("gameDate"),
                        g.get("opponentAbbrev"),
                        g.get("homeRoadFlag") == "H",
                        g.get("goals"),
                        g.get("assists"),
                        g.get("points"),
                        g.get("gameId") or g.get("id"),
                    )
                )

        if not rows and errors:
            raise RuntimeError(
                f"Journaux joueurs indisponibles : {errors}"
            )

        # Élimination des doublons éventuels.
        unique = {}

        for row in rows:

            key = (
                row["game_id"]
                or (
                    row["date"],
                    row["opp"],
                    row["home"],
                )
            )

            unique[key] = row

        return list(unique.values())

    return await cached(
        ("log", pid, tuple(seasons())),
        3 * 3600,
        build,
    )


# =====================================================
# CONFRONTATIONS HISTORIQUES
# =====================================================

async def head_to_head_games(away, home):

    season_ids = seasons()

    async def fetch_schedule(team, season):

        return await cached(
            ("schedule", team, season),
            3600,
            lambda: get_json(
                f"{API_WEB}/club-schedule-season/{team}/{season}"
            ),
        )

    requests = [
        fetch_schedule(team, season)
        for season in season_ids
        for team in (away, home)
    ]

    results = await asyncio.gather(
        *requests,
        return_exceptions=True,
    )

    # Au moins un calendrier doit être disponible par saison.
    for index, season in enumerate(season_ids):

        season_results = results[index * 2:index * 2 + 2]

        if not any(
            not isinstance(result, Exception)
            for result in season_results
        ):
            raise RuntimeError(
                f"Calendrier NHL indisponible pour {season}"
            )

    found = {}

    for data in results:

        if isinstance(data, Exception):
            log.warning("Calendrier NHL : %s", data)
            continue

        for game in data.get("games", []):

            away_team = game.get("awayTeam") or {}
            home_team = game.get("homeTeam") or {}

            # Configuration domicile / extérieur exacte.
            if (
                away_team.get("abbrev") != away
                or home_team.get("abbrev") != home
            ):
                continue

            try:
                game_type = int(game.get("gameType", 0))
            except (TypeError, ValueError):
                game_type = 0

            # Saison régulière uniquement.
            if game_type != 2:
                continue

            if game.get("gameState") not in ("FINAL", "OFF"):
                continue

            game_id = game.get("id")

            away_score = away_team.get("score")
            home_score = home_team.get("score")

            if (
                game_id is None
                or away_score is None
                or home_score is None
            ):
                continue

            found[str(game_id)] = {
                "id": str(game_id),
                "date": game.get("gameDate"),
                "away": away,
                "home": home,
                "away_score": away_score,
                "home_score": home_score,
                "season": game.get("season"),
            }

    matches = sorted(
        found.values(),
        key=lambda x: x["date"] or "",
        reverse=True,
    )

    log.info(
        "H2H %s @ %s : %s matchs trouvés sur 3 saisons",
        away,
        home,
        len(matches),
    )

    return matches


# =====================================================
# CALENDRIER DU JOUR
# =====================================================

async def games_for(offset):

    date = (
        datetime.now(ET) + timedelta(days=offset)
    ).date().isoformat()

    async def build():

        data = await get_json(
            f"{API_WEB}/schedule/{date}"
        )

        for day in data.get("gameWeek", []):

            if day.get("date") == date:

                out = []

                for game in day.get("games", []):

                    start = datetime.fromisoformat(
                        game["startTimeUTC"].replace(
                            "Z",
                            "+00:00",
                        )
                    ).astimezone(ET)

                    out.append((
                        game["awayTeam"]["abbrev"],
                        game["homeTeam"]["abbrev"],
                        start.strftime("%H:%M"),
                    ))

                return out

        return []

    return await cached(
        ("sched", date),
        600,
        build,
    )


# =====================================================
# STATISTIQUES
# =====================================================

def score(player, market):

    vs = player["vs"]
    all_games = player["log"]

    n = len(vs)

    total = sum(g[market] for g in vs)

    rate = total / n if n else 0.0

    base = (
        sum(g[market] for g in all_games) / len(all_games)
        if all_games
        else 0.0
    )

    weight = n / (n + SHRINK)

    return {
        "n": n,
        "sum": total,
        "rate": rate,
        "base": base,
        "blend": (
            weight * rate
            + (1 - weight) * base
        ),
        "hit": (
            sum(1 for g in vs if g[market] >= 1) / n
            if n
            else 0.0
        ),
    }


def fmt(value):

    return f"{value:.2f}".replace(".", ",")


# =====================================================
# ANALYSE DES JOUEURS
# =====================================================

async def analyze(away, home):

    scores = await head_to_head_games(
        away,
        home,
    )

    allowed_ids = {
        str(g["id"])
        for g in scores
    }

    allowed_dates = {
        g["date"]
        for g in scores
        if g.get("date")
    }

    rosters = await asyncio.gather(
        roster(away),
        roster(home),
    )

    players = [
        {
            **player,
            "team": team,
            "opp": opponent,
        }
        for team, opponent, roster_players in (
            (away, home, rosters[0]),
            (home, away, rosters[1]),
        )
        for player in roster_players
    ]

    if not players:
        raise RuntimeError("Aucun joueur trouvé")

    semaphore = asyncio.Semaphore(6)

    async def load(player):

        async with semaphore:

            try:
                player["log"] = await game_log(
                    player["id"]
                )

            except Exception as e:

                log.warning(
                    "Log joueur %s : %s",
                    player["id"],
                    e,
                )

                player["log"] = None
                return

        player_is_home = player["team"] == home

        player["vs"] = []

        for game in player["log"]:

            if game.get("game_id") is not None:

                if game["game_id"] in allowed_ids:
                    player["vs"].append(game)

                continue

            # Repli si le journal ne contient pas de game ID.
            if (
                game["opp"] == player["opp"]
                and game["home"] == player_is_home
                and game["date"] in allowed_dates
            ):
                player["vs"].append(game)

    await asyncio.gather(
        *(load(player) for player in players)
    )

    ok = [
        player
        for player in players
        if player["log"] is not None
    ]

    if not ok:
        raise RuntimeError(
            "Statistiques indisponibles"
        )

    return format_result(
        away,
        home,
        ok,
        len(players) - len(ok),
        scores,
    )


# =====================================================
# AFFICHAGE
# =====================================================

def format_result(
    away,
    home,
    players,
    failed,
    scores,
):

    out = [
        f"<b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>",
        "<i>H2H sur les saisons 2024-2025, 2025-2026 et 2026-2027</i>",
        f"\n<b>SCORES DES CONFRONTATIONS ({len(scores)} MATCHS)</b>",
    ]

    if scores:

        for game in scores:

            date = game["date"] or "Date inconnue"

            out.append(
                f"{esc(date)} — "
                f"{game['away']} "
                f"{game['away_score']} - "
                f"{game['home_score']} "
                f"{game['home']}"
            )

    else:

        out.append(
            "Aucun score domicile/extérieur disponible."
        )

    with_h2h = [
        player
        for player in players
        if player.get("vs")
    ]

    if not with_h2h:

        out.append(
            "\nAucun H2H joueur disponible "
            "dans cette configuration domicile/extérieur."
        )

        if failed:
            out.append(
                f"{failed} joueur(s) n'ont pas pu être chargés."
            )

        return "\n".join(out)

    for market, title, singular, plural in (
        ("points", "Points", "pt", "pts"),
        ("goals", "Buts", "but", "buts"),
        ("assists", "Passes", "passe", "passes"),
    ):

        ranked = sorted(
            (
                (player, score(player, market))
                for player in with_h2h
            ),
            key=lambda item: (
                -item[1]["blend"],
                -item[1]["hit"],
            ),
        )[:TOP]

        out.append(
            f"\n<b>{title}</b>"
        )

        for index, (player, stats) in enumerate(
            ranked,
            1,
        ):

            warning = (
                " ⚠️"
                if stats["n"] < 3
                else ""
            )

            unit = (
                singular
                if stats["sum"] == 1
                else plural
            )

            out.append(
                f"{index}. {esc(player['name'])} "
                f"({player['team']}) : "
                f"<b>{round(stats['hit'] * 100)} %</b> "
                f"des matchs, "
                f"{stats['n']} MJ, "
                f"{stats['sum']} {unit}, "
                f"moy. {fmt(stats['rate'])} "
                f"(moyenne globale {fmt(stats['base'])})"
                f"{warning}"
            )

    out.append(
        "\n⚠️ = moins de 3 matchs H2H individuels "
        "dans la configuration exacte."
    )

    out.append(
        "Le % indique la part des matchs avec au moins "
        "1 point, but ou passe. Le classement ajuste "
        "la moyenne H2H selon la moyenne générale du joueur."
    )

    if failed:
        out.append(
            f"{failed} joueur(s) n'ont pas pu être chargés."
        )

    return "\n".join(out)


# =====================================================
# TELEGRAM
# =====================================================

def allowed(update):

    user = update.effective_user

    return (
        not ALLOWED
        or (user and user.id in ALLOWED)
    )


DAYS = (
    "Aujourd'hui",
    "Demain",
    "Après-demain",
)


BACK = InlineKeyboardMarkup([
    [
        InlineKeyboardButton(
            "📅 Autres matchs",
            callback_data="d:0",
        )
    ]
])


ERR = (
    "Les données de la NHL sont indisponibles "
    "pour l'instant. Réessaie dans un instant."
)


async def games_view(offset):

    games = await games_for(offset)

    rows = [
        [
            InlineKeyboardButton(
                f"{NAMES.get(a, a)} @ "
                f"{NAMES.get(h, h)} · {hour} HE",
                callback_data=f"g:{a}:{h}",
            )
        ]
        for a, h, hour in games
    ]

    rows.append([
        InlineKeyboardButton(
            ("• " if i == offset else "") + label,
            callback_data=f"d:{i}",
        )
        for i, label in enumerate(DAYS)
    ])

    text = (
        f"<b>Matchs : {DAYS[offset].lower()}</b>\n"
        "Touche un match, ou écris par exemple "
        "« Washington vs Montréal »."
        if games
        else
        f"Aucun match {DAYS[offset].lower()}. "
        "Choisis un autre jour, ou écris deux équipes "
        "(« Washington vs Montréal »)."
    )

    return text, InlineKeyboardMarkup(rows)


async def send_games(message, offset=0):

    try:
        text, markup = await games_view(offset)

    except Exception as e:

        log.warning("Calendrier : %s", e)

        await message.reply_text(
            "Calendrier indisponible. Écris deux équipes, "
            "par exemple « Washington vs Montréal »."
        )

        return

    await message.reply_text(
        text,
        parse_mode=HTML,
        reply_markup=markup,
    )


async def start(update, ctx):

    if not allowed(update):
        return

    keyboard = ReplyKeyboardMarkup(
        [[MENU_BTN]],
        resize_keyboard=True,
    )

    await update.message.reply_text(
        "🏒 <b>Face-à-face NHL</b>\n"
        "Choisis un match ci-dessous pour voir les joueurs "
        "avec le meilleur historique contre l'adversaire "
        "(points, buts, passes).",
        parse_mode=HTML,
        reply_markup=keyboard,
    )

    await send_games(
        update.message,
        0,
    )


async def on_button(update, ctx):

    query = update.callback_query

    if not allowed(update):
        return

    await query.answer()

    parts = query.data.split(":")

    try:

        if parts[0] == "d":

            text, markup = await games_view(
                int(parts[1])
            )

            await query.edit_message_text(
                text,
                parse_mode=HTML,
                reply_markup=markup,
            )

        elif parts[0] == "g":

            away, home = parts[1], parts[2]

            await query.edit_message_text(
                f"⏳ Analyse de {NAMES[away]} "
                f"@ {NAMES[home]}…"
            )

            try:
                result = await analyze(
                    away,
                    home,
                )

            except Exception as e:

                log.exception("Analyse : %s", e)
                result = ERR

            await query.edit_message_text(
                result,
                parse_mode=HTML,
                reply_markup=BACK,
            )

    except Exception as e:
        log.warning("Bouton : %s", e)


async def run_analysis(message, away, home):

    wait = await message.reply_text(
        f"⏳ Analyse de {NAMES[away]} "
        f"@ {NAMES[home]}…"
    )

    try:
        result = await analyze(
            away,
            home,
        )

    except Exception as e:

        log.exception("Analyse : %s", e)
        result = ERR

    await wait.edit_text(
        result,
        parse_mode=HTML,
        reply_markup=BACK,
    )


async def on_text(update, ctx):

    if not allowed(update):
        return

    message = update.message

    text = (
        message.text or ""
    ).strip()

    if (
        text == MENU_BTN
        or norm(text) in (
            "match",
            "matchs",
            "menu",
            "ce soir",
            "aujourdhui",
        )
    ):

        await send_games(
            message,
            0,
        )

        return

    parts = [
        part.strip()
        for part in SPLIT.split(text)
        if part.strip()
    ]

    if len(parts) == 2:

        away = find_team(parts[0])
        home = find_team(parts[1])

        if away and home and away != home:

            await run_analysis(
                message,
                away,
                home,
            )

            return

        bad = (
            parts[0]
            if not away
            else parts[1]
        )

        await message.reply_text(
            f"Je ne reconnais pas « {bad} ». "
            "Essaie le nom de la ville, de l'équipe "
            "ou son abréviation (MTL, WSH…)."
        )

        return

    if len(parts) == 1:

        team = find_team(parts[0])

        if team:

            try:

                for offset in range(7):

                    for away, home, _ in await games_for(offset):

                        if team in (away, home):

                            await run_analysis(
                                message,
                                away,
                                home,
                            )

                            return

            except Exception as e:

                log.warning(
                    "Recherche match : %s",
                    e,
                )

            await message.reply_text(
                f"Aucun match trouvé pour {NAMES[team]} "
                "dans les 7 prochains jours."
            )

            return

    await message.reply_text(
        "Écris deux équipes, par exemple "
        "« Washington vs Montréal », "
        "ou touche « Matchs du jour »."
    )


# =====================================================
# FERMETURE
# =====================================================

async def shutdown(app):

    if _client:
        await _client.aclose()


# =====================================================
# DÉMARRAGE
# =====================================================

def main():

    app = (
        Application.builder()
        .token(TOKEN)
        .post_shutdown(shutdown)
        .build()
    )

    app.add_handler(
        CommandHandler(
            ["start", "help"],
            start,
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            on_button
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            on_text,
        )
    )

    log.info(
        "Bot NHL démarré - H2H strict sur trois saisons"
    )

    app.run_polling()


if __name__ == "__main__":
    main()