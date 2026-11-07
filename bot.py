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

# =====================================================
# CONFIGURATION
# =====================================================

TOKEN = os.environ["TELEGRAM_TOKEN"]

ALLOWED = {
    int(x)
    for x in os.environ.get("ALLOWED_USERS", "").replace(" ", "").split(",")
    if x
}

API_WEB = "https://api-web.nhle.com/v1"
API_STATS = "https://api.nhle.com/stats/rest/en"

TZ = ZoneInfo("Europe/Paris")

TOP = 5
SHRINK = 3

MENU_BTN = "📅 Matchs du jour"
HTML = ParseMode.HTML

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

log = logging.getLogger("blackice")

# =====================================================
# ÉQUIPES NHL
# =====================================================

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

# =====================================================
# CLIENT HTTP ET CACHE
# =====================================================

_client = None
_cache = {}


def client():

    global _client

    if _client is None:
        _client = httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={
                "User-Agent": "BLACK-ICE-NHL-BOT/2.0"
            },
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

            await asyncio.sleep(0.7 * (i + 1))

    raise last


async def cached(key, ttl, factory):

    hit = _cache.get(key)

    if hit and time.time() - hit[0] < ttl:
        return hit[1]

    value = await factory()

    _cache[key] = (
        time.time(),
        value,
    )

    return value


# =====================================================
# SAISONS
# =====================================================

def seasons():

    return [
        "20242025",
        "20252026",
        "20262027",
    ]


def current_season():

    now = datetime.now(TZ)

    year = (
        now.year
        if now.month >= 9
        else now.year - 1
    )

    return f"{year}{year + 1}"


# =====================================================
# OUTILS STATISTIQUES
# =====================================================

def number(value):

    if value is None:
        return None

    try:
        return float(value)

    except (TypeError, ValueError):
        return None


def parse_toi(value):

    if not value:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    try:

        parts = str(value).split(":")

        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])

        if len(parts) == 3:
            return (
                int(parts[0]) * 3600
                + int(parts[1]) * 60
                + int(parts[2])
            )

    except (TypeError, ValueError):
        return None

    return None


def fmt(value, decimals=2):

    if value is None:
        return "N/D"

    return f"{value:.{decimals}f}".replace(".", ",")


def fmt_percent(value):

    if value is None:
        return "N/D"

    if value <= 1:
        value *= 100

    return f"{value:.1f}".replace(".", ",") + " %"


def fmt_minutes(seconds):

    if seconds is None:
        return "N/D"

    seconds = int(round(seconds))

    minutes = seconds // 60
    remaining = seconds % 60

    return f"{minutes}:{remaining:02d}"


def player_name(player):

    first = player.get("firstName", "")
    last = player.get("lastName", "")

    if isinstance(first, dict):
        first = first.get("default", "")

    if isinstance(last, dict):
        last = last.get("default", "")

    return (
        f"{first} {last}".strip()
        or player.get("name")
        or player.get("fullName")
        or "Joueur inconnu"
    )


# =====================================================
# EFFECTIFS
# =====================================================

async def roster(team, position="skater"):

    sid = current_season()

    async def build():

        try:

            data = await get_json(
                f"{API_WEB}/roster/{team}/current"
            )

            if position == "goalie":

                entries = data.get("goalies") or []

            else:

                entries = (
                    data.get("forwards") or []
                ) + (
                    data.get("defensemen") or []
                )

            if entries:

                players = []

                for p in entries:

                    pid = (
                        p.get("id")
                        or p.get("playerId")
                    )

                    if not pid:
                        continue

                    players.append({
                        "id": pid,
                        "name": player_name(p),
                        "team": team,
                        "position": p.get("positionCode", ""),
                    })

                if players:
                    return players

        except Exception as e:

            log.warning(
                "Roster API %s : %s",
                team,
                e,
            )

        # Secours avec les statistiques de saison.
        try:

            data = await get_json(
                f"{API_WEB}/club-stats/{team}/{sid}/2"
            )

            key = (
                "goalies"
                if position == "goalie"
                else "skaters"
            )

            entries = data.get(key) or []

            players = []

            for p in entries:

                pid = p.get("playerId")

                if not pid:
                    continue

                first = p.get("firstName", {})
                last = p.get("lastName", {})

                players.append({
                    "id": pid,
                    "name": (
                        f'{first.get("default", "")} '
                        f'{last.get("default", "")}'
                    ).strip(),
                    "team": team,
                    "position": p.get("positionCode", ""),
                })

            return players

        except Exception as e:

            log.warning(
                "Club stats %s : %s",
                team,
                e,
            )

        return []

    return await cached(
        ("roster", team, position, sid),
        1800,
        build,
    )


# =====================================================
# JOURNAUX INDIVIDUELS
# =====================================================

def normalize_game(g):

    return {
        "date": (g.get("gameDate") or "")[:10],
        "opp": g.get("opponentAbbrev"),
        "home": g.get("homeRoadFlag") == "H",

        "game_id": (
            str(g.get("gameId") or g.get("id"))
            if g.get("gameId") or g.get("id")
            else None
        ),

        "goals": number(g.get("goals")),
        "assists": number(g.get("assists")),
        "points": number(g.get("points")),
        "shots": number(g.get("shots")),

        "hits": number(g.get("hits")),
        "blocks": number(
            g.get("blockedShots")
        ),

        "pim": number(g.get("pim")),

        "toi": parse_toi(
            g.get("toi")
            or g.get("timeOnIce")
        ),

        "powerplay": number(
            g.get("powerPlayPoints")
        ),

        "powerplay_goals": number(
            g.get("powerPlayGoals")
        ),

        "powerplay_assists": number(
            g.get("powerPlayAssists")
        ),

        "faceoff_pct": number(
            g.get("faceoffWinningPctg")
        ),

        "saves": number(g.get("saves")),
        "shots_against": number(
            g.get("shotsAgainst")
        ),

        "save_pct": number(
            g.get("savePctg")
        ),

        "goals_against": number(
            g.get("goalsAgainst")
        ),

        "decision": g.get("decision"),
    }


async def game_log(pid):

    async def build():

        results = await asyncio.gather(
            *[
                get_json(
                    f"{API_WEB}/player/{pid}/game-log/{season}/2"
                )
                for season in seasons()
            ],
            return_exceptions=True,
        )

        rows = []
        errors = []

        for result in results:

            if isinstance(result, Exception):
                errors.append(result)
                continue

            for game in result.get("gameLog", []):

                rows.append(
                    normalize_game(game)
                )

        if not rows and errors:

            raise RuntimeError(
                f"Journaux joueurs indisponibles : {errors}"
            )

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
        3600,
        build,
    )


# =====================================================
# H2H STRICT DOMICILE / EXTÉRIEUR
# =====================================================

async def head_to_head_games(away, home):

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
        for season in seasons()
        for team in (away, home)
    ]

    results = await asyncio.gather(
        *requests,
        return_exceptions=True,
    )

    for index, season in enumerate(seasons()):

        season_results = results[
            index * 2:index * 2 + 2
        ]

        if not any(
            not isinstance(r, Exception)
            for r in season_results
        ):

            raise RuntimeError(
                f"Calendrier indisponible : {season}"
            )

    found = {}

    for data in results:

        if isinstance(data, Exception):
            continue

        for game in data.get("games", []):

            away_team = game.get("awayTeam") or {}
            home_team = game.get("homeTeam") or {}

            if (
                away_team.get("abbrev") != away
                or home_team.get("abbrev") != home
            ):
                continue

            try:
                game_type = int(game.get("gameType", 0))

            except (TypeError, ValueError):
                game_type = 0

            if game_type != 2:
                continue

            if game.get("gameState") not in (
                "FINAL",
                "OFF",
            ):
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
            }

    matches = sorted(
        found.values(),
        key=lambda g: g["date"] or "",
        reverse=True,
    )

    log.info(
        "H2H %s @ %s : %s matchs",
        away,
        home,
        len(matches),
    )

    return matches


# =====================================================
# PLAY-BY-PLAY
# =====================================================

def empty_pbp():

    return {
        "shots": 0,
        "missed": 0,
        "hits": 0,
        "blocks": 0,
        "faceoff_wins": 0,
        "faceoff_losses": 0,
        "pim": 0,
    }


async def play_by_play(game_id):

    async def build():

        data = await get_json(
            f"{API_WEB}/gamecenter/{game_id}/play-by-play"
        )

        players = {}

        def stats(pid):

            if pid is None:
                return None

            pid = str(pid)

            if pid not in players:
                players[pid] = empty_pbp()

            return players[pid]

        plays = data.get("plays") or []

        for play in plays:

            event = (
                play.get("typeDescKey")
                or ""
            ).lower()

            details = play.get("details") or {}

            if event == "shot-on-goal":

                p = stats(
                    details.get("shootingPlayerId")
                )

                if p:
                    p["shots"] += 1

            elif event == "missed-shot":

                p = stats(
                    details.get("shootingPlayerId")
                )

                if p:
                    p["missed"] += 1

            elif event == "hit":

                p = stats(
                    details.get("hittingPlayerId")
                )

                if p:
                    p["hits"] += 1

            elif event == "blocked-shot":

                p = stats(
                    details.get("blockingPlayerId")
                )

                if p:
                    p["blocks"] += 1

            elif event == "faceoff":

                winner = stats(
                    details.get("winningPlayerId")
                )

                loser = stats(
                    details.get("losingPlayerId")
                )

                if winner:
                    winner["faceoff_wins"] += 1

                if loser:
                    loser["faceoff_losses"] += 1

            elif event == "penalty":

                pid = (
                    details.get("committedByPlayerId")
                    or details.get("servedByPlayerId")
                )

                p = stats(pid)

                if p:

                    p["pim"] += int(
                        details.get("duration", 0) or 0
                    )

        return players

    return await cached(
        ("pbp", str(game_id)),
        12 * 3600,
        build,
    )


async def all_pbp(scores):

    results = await asyncio.gather(
        *[
            play_by_play(game["id"])
            for game in scores
        ],
        return_exceptions=True,
    )

    output = {}

    for game, result in zip(scores, results):

        if isinstance(result, Exception):

            log.warning(
                "Play-by-play %s : %s",
                game["id"],
                result,
            )

            output[game["id"]] = {}

        else:

            output[game["id"]] = result

    return output


# =====================================================
# ASSOCIATION DES MATCHS H2H AUX JOUEURS
# =====================================================

def match_player_games(player, scores, pbp_data):

    allowed_ids = {
        str(game["id"]): game
        for game in scores
    }

    allowed_dates = {
        game["date"]: game
        for game in scores
        if game.get("date")
    }

    player["vs"] = []

    for game in player["log"]:

        game_id = game.get("game_id")

        matched = None

        if game_id:

            matched = allowed_ids.get(str(game_id))

        else:

            matched = allowed_dates.get(
                game.get("date")
            )

            if matched:

                is_home = (
                    player["team"] == matched["home"]
                )

                if (
                    game.get("opp")
                    != player["opp"]
                    or game.get("home") != is_home
                ):
                    matched = None

        if not matched:
            continue

        row = dict(game)

        row["pbp"] = (
            pbp_data
            .get(str(matched["id"]), {})
            .get(str(player["id"]), empty_pbp())
        )

        row["match_id"] = str(matched["id"])

        player["vs"].append(row)

    player["vs"].sort(
        key=lambda g: g.get("date") or "",
        reverse=True,
    )


# =====================================================
# VALEURS DES CATÉGORIES
# =====================================================

def metric_value(game, category):

    pbp = game.get("pbp") or empty_pbp()

    if category == "points":
        return game.get("points")

    if category == "goals":
        return game.get("goals")

    if category == "assists":
        return game.get("assists")

    if category == "shots":

        value = game.get("shots")

        return (
            value
            if value is not None
            else pbp.get("shots")
        )

    if category == "missed":
        return pbp.get("missed")

    if category == "hits":

        value = game.get("hits")

        return (
            value
            if value is not None
            else pbp.get("hits")
        )

    if category == "blocks":

        value = game.get("blocks")

        return (
            value
            if value is not None
            else pbp.get("blocks")
        )

    if category == "penalties":

        value = game.get("pim")

        return (
            value
            if value is not None
            else pbp.get("pim")
        )

    if category == "toi":
        return game.get("toi")

    if category == "powerplay":

        value = game.get("powerplay")

        if value is not None:
            return value

        goals = game.get("powerplay_goals")
        assists = game.get("powerplay_assists")

        if goals is not None and assists is not None:
            return goals + assists

        return None

    if category == "faceoffs":

        wins = pbp.get("faceoff_wins", 0)
        losses = pbp.get("faceoff_losses", 0)

        total = wins + losses

        if total > 0:
            return wins / total

        value = game.get("faceoff_pct")

        if value is not None:
            return value

        return None

    return None


# =====================================================
# CLASSEMENT TOP 5
# =====================================================

CATEGORY_INFO = {
    "points": (
        "⭐ POINTS",
        "points",
        "pts",
        0,
    ),

    "goals": (
        "🥅 BUTS",
        "buts",
        "buts",
        0,
    ),

    "assists": (
        "🎯 PASSES DÉCISIVES",
        "passes",
        "passes",
        0,
    ),

    "shots": (
        "🏒 TIRS CADRÉS",
        "tirs cadrés",
        "tirs",
        0,
    ),

    "missed": (
        "🎯 TIRS MANQUÉS",
        "tirs manqués",
        "tirs",
        0,
    ),

    "hits": (
        "💥 HITS",
        "hits",
        "hits",
        0,
    ),

    "blocks": (
        "🛡️ TIRS BLOQUÉS",
        "blocs",
        "blocs",
        0,
    ),

    "faceoffs": (
        "🔵 MISES EN JEU",
        "pourcentage",
        "%",
        1,
    ),

    "toi": (
        "⏱️ TEMPS DE GLACE",
        "secondes",
        "secondes",
        0,
    ),

    "powerplay": (
        "⚡ POWER PLAY",
        "points PP",
        "points PP",
        0,
    ),

    "penalties": (
        "🚨 PÉNALITÉS",
        "minutes",
        "minutes",
        0,
    ),
}


def calculate_player(player, category):

    values = [
        metric_value(game, category)
        for game in player.get("vs", [])
    ]

    values = [
        value
        for value in values
        if value is not None
    ]

    if not values:
        return None

    total = sum(values)
    n = len(values)

    return {
        "n": n,
        "total": total,
        "average": total / n,
        "hit": (
            sum(1 for value in values if value >= 1)
            / n
        ),
    }


def format_category(
    away,
    home,
    players,
    category,
    scores,
):

    title, _, unit, decimals = CATEGORY_INFO[category]

    ranked = []

    for player in players:

        result = calculate_player(
            player,
            category,
        )

        if result is None:
            continue

        ranked.append((
            player,
            result,
        ))

    ranked.sort(
        key=lambda item: (
            -item[1]["average"],
            -item[1]["hit"],
            -item[1]["n"],
        )
    )

    ranked = ranked[:TOP]

    out = [
        f"<b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>",
        f"<b>{title}</b>",
        "<i>H2H strict sur 3 saisons</i>",
        f"Confrontations trouvées : {len(scores)}",
        "",
    ]

    if not ranked:

        out.append(
            "Aucune statistique individuelle vérifiable "
            "pour cette catégorie."
        )

        return "\n".join(out)

    for index, (player, stats) in enumerate(
        ranked,
        1,
    ):

        if category == "toi":

            average = fmt_minutes(
                stats["average"]
            )

            total = fmt_minutes(
                stats["total"]
            )

            suffix = "de moyenne par match"

            total_text = f"Temps total : {total}"

        elif category == "faceoffs":

            average = fmt_percent(
                stats["average"]
            )

            total = fmt_percent(
                stats["total"] / stats["n"]
            )

            suffix = "de réussite"

            total_text = ""

        else:

            average = fmt(
                stats["average"],
                decimals,
            )

            total = fmt(
                stats["total"],
                decimals,
            )

            suffix = "par match"

            total_text = f"Total : {total} {unit}"

        out.append(
            f"<b>{index}. {esc(player['name'])}</b> "
            f"({player['team']})"
        )

        out.append(
            f"   {round(stats['hit'] * 100)} % "
            f"des matchs avec au moins 1"
        )

        out.append(
            f"   {stats['n']} matchs H2H"
        )

        out.append(
            f"   Moyenne : {average} {suffix}"
        )

        if total_text:
            out.append(
                f"   {total_text}"
            )

        out.append("")

    out.append(
        "Le classement utilise les statistiques "
        "individuelles des matchs H2H disponibles."
    )

    return "\n".join(out)


# =====================================================
# ANALYSE GARDIENS
# =====================================================

def goalie_result(player):

    games = player.get("vs", [])

    valid = [
        game
        for game in games
        if (
            game.get("save_pct") is not None
            or (
                game.get("saves") is not None
                and game.get("shots_against")
            )
        )
    ]

    if not valid:
        return None

    saves = sum(
        game.get("saves") or 0
        for game in valid
    )

    shots_against = sum(
        game.get("shots_against") or 0
        for game in valid
    )

    goals_against = sum(
        game.get("goals_against") or 0
        for game in valid
    )

    if shots_against > 0:

        save_pct = saves / shots_against

    else:

        percentages = [
            game["save_pct"]
            for game in valid
            if game.get("save_pct") is not None
        ]

        if not percentages:
            return None

        save_pct = sum(percentages) / len(percentages)

    return {
        "games": len(valid),
        "saves": saves,
        "shots_against": shots_against,
        "goals_against": goals_against,
        "save_pct": save_pct,
    }


def format_goalies(away, home, players, scores):

    ranked = []

    for player in players:

        result = goalie_result(player)

        if result:
            ranked.append((
                player,
                result,
            ))

    ranked.sort(
        key=lambda item: (
            -item[1]["save_pct"],
            -item[1]["games"],
        )
    )

    out = [
        f"<b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>",
        "<b>🥅 GARDIENS</b>",
        "<i>H2H strict sur 3 saisons</i>",
        f"Confrontations trouvées : {len(scores)}",
        "",
    ]

    if not ranked:

        out.append(
            "Aucune statistique de gardien vérifiable "
            "sur ces confrontations."
        )

        return "\n".join(out)

    for index, (player, stats) in enumerate(
        ranked[:TOP],
        1,
    ):

        out.append(
            f"<b>{index}. {esc(player['name'])}</b> "
            f"({player['team']})"
        )

        out.append(
            f"   Arrêts : {stats['saves']}"
        )

        out.append(
            f"   Tirs reçus : {stats['shots_against']}"
        )

        out.append(
            f"   Buts encaissés : {stats['goals_against']}"
        )

        out.append(
            f"   % arrêts : {fmt_percent(stats['save_pct'])}"
        )

        out.append(
            f"   {stats['games']} matchs H2H"
        )

        out.append("")

    return "\n".join(out)


# =====================================================
# FORME DES 5 DERNIERS MATCHS
# =====================================================

def format_form(away, home, players):

    ranked = []

    for player in players:

        games = sorted(
            player.get("log", []),
            key=lambda g: g.get("date") or "",
            reverse=True,
        )[:5]

        if not games:
            continue

        points = sum(
            g.get("points") or 0
            for g in games
        )

        goals = sum(
            g.get("goals") or 0
            for g in games
        )

        assists = sum(
            g.get("assists") or 0
            for g in games
        )

        shots = sum(
            g.get("shots") or 0
            for g in games
        )

        ranked.append((
            player,
            len(games),
            points,
            goals,
            assists,
            shots,
        ))

    ranked.sort(
        key=lambda x: (
            -x[2],
            -x[5],
            -x[1],
        )
    )

    out = [
        f"<b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>",
        "<b>📈 FORME DES JOUEURS</b>",
        "<i>5 derniers matchs toutes oppositions</i>",
        "",
    ]

    for index, item in enumerate(
        ranked[:TOP],
        1,
    ):

        player, n, points, goals, assists, shots = item

        out.append(
            f"<b>{index}. {esc(player['name'])}</b> "
            f"({player['team']})"
        )

        out.append(
            f"   {n} matchs analysés"
        )

        out.append(
            f"   {points} points | "
            f"{goals} buts | "
            f"{assists} passes"
        )

        out.append(
            f"   {shots} tirs cadrés"
        )

        out.append("")

    if not ranked:
        out.append("Aucune forme récente disponible.")

    return "\n".join(out)


# =====================================================
# BILAN DES ÉQUIPES
# =====================================================

def format_teams(away, home, scores):

    stats = {
        away: {
            "wins": 0,
            "losses": 0,
            "gf": 0,
            "ga": 0,
        },
        home: {
            "wins": 0,
            "losses": 0,
            "gf": 0,
            "ga": 0,
        },
    }

    for game in scores:

        a = game["away_score"]
        h = game["home_score"]

        stats[away]["gf"] += a
        stats[away]["ga"] += h

        stats[home]["gf"] += h
        stats[home]["ga"] += a

        if a > h:

            stats[away]["wins"] += 1
            stats[home]["losses"] += 1

        elif h > a:

            stats[home]["wins"] += 1
            stats[away]["losses"] += 1

    out = [
        f"<b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>",
        "<b>📊 BILAN DES ÉQUIPES</b>",
        "<i>Configuration exacte domicile / extérieur</i>",
        f"{len(scores)} confrontations trouvées",
        "",
    ]

    for team in (away, home):

        s = stats[team]

        out.append(
            f"<b>{esc(NAMES[team])} ({team})</b>"
        )

        out.append(
            f"   Victoires : {s['wins']}"
        )

        out.append(
            f"   Défaites : {s['losses']}"
        )

        out.append(
            f"   Buts marqués : {s['gf']}"
        )

        out.append(
            f"   Buts encaissés : {s['ga']}"
        )

        out.append(
            f"   Différence : {s['gf'] - s['ga']}"
        )

        out.append("")

    return "\n".join(out)


# =====================================================
# CHARGEMENT ET ANALYSE
# =====================================================

async def load_players(away, home, position="skater"):

    rosters = await asyncio.gather(
        roster(away, position),
        roster(home, position),
    )

    players = []

    for team, opponent, entries in (
        (away, home, rosters[0]),
        (home, away, rosters[1]),
    ):

        for player in entries:

            players.append({
                **player,
                "team": team,
                "opp": opponent,
                "log": [],
                "vs": [],
            })

    if not players:
        raise RuntimeError(
            "Aucun joueur disponible dans les effectifs."
        )

    return players


async def load_player_logs(players):

    semaphore = asyncio.Semaphore(8)

    async def load(player):

        async with semaphore:

            try:

                player["log"] = await game_log(
                    player["id"]
                )

            except Exception as e:

                log.warning(
                    "Journal %s : %s",
                    player["name"],
                    e,
                )

                player["log"] = []

    await asyncio.gather(
        *(load(player) for player in players)
    )


async def analyze_category(away, home, category):

    scores = await head_to_head_games(
        away,
        home,
    )

    if not scores:

        return (
            f"<b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>\n"
            "Aucune confrontation terminée trouvée "
            "dans cette configuration exacte."
        )

    if category == "teams":

        return format_teams(
            away,
            home,
            scores,
        )

    position = (
        "goalie"
        if category == "goalies"
        else "skater"
    )

    players = await load_players(
        away,
        home,
        position,
    )

    await load_player_logs(players)

    pbp_data = {}

    if category not in (
        "form",
        "goalies",
        "teams",
    ):

        pbp_data = await all_pbp(scores)

    for player in players:

        match_player_games(
            player,
            scores,
            pbp_data,
        )

    if category == "goalies":

        return format_goalies(
            away,
            home,
            players,
            scores,
        )

    if category == "form":

        return format_form(
            away,
            home,
            players,
        )

    return format_category(
        away,
        home,
        players,
        category,
        scores,
    )


# =====================================================
# MENU DES CATÉGORIES
# =====================================================

CATEGORIES = [
    ("⭐ Points", "points"),
    ("🥅 Buts", "goals"),
    ("🎯 Passes", "assists"),
    ("🏒 Tirs cadrés", "shots"),
    ("🎯 Tirs manqués", "missed"),
    ("💥 Hits", "hits"),
    ("🛡️ Tirs bloqués", "blocks"),
    ("🔵 Mises en jeu", "faceoffs"),
    ("⏱️ Temps de glace", "toi"),
    ("⚡ Power Play", "powerplay"),
    ("🚨 Pénalités", "penalties"),
    ("🥅 Gardiens", "goalies"),
    ("📈 Forme 5 matchs", "form"),
    ("📊 Bilan équipes", "teams"),
]


def category_keyboard(away, home):

    rows = []

    for index in range(0, len(CATEGORIES), 2):

        pair = CATEGORIES[index:index + 2]

        rows.append([
            InlineKeyboardButton(
                label,
                callback_data=f"c:{away}:{home}:{key}",
            )
            for label, key in pair
        ])

    rows.append([
        InlineKeyboardButton(
            "📅 Autres matchs",
            callback_data="d:0",
        )
    ])

    return InlineKeyboardMarkup(rows)


def category_text(away, home):

    return (
        f"🏒 <b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>\n\n"
        "Choisis la catégorie statistique à analyser.\n\n"
        "Chaque classement présente les 5 meilleurs "
        "joueurs des deux équipes lorsque les données "
        "sont disponibles.\n\n"
        "📊 H2H strict sur les trois saisons."
    )


async def send_category_menu(message, away, home):

    await message.reply_text(
        category_text(away, home),
        parse_mode=HTML,
        reply_markup=category_keyboard(
            away,
            home,
        ),
    )


# =====================================================
# CALENDRIER EN HEURE FRANÇAISE
# =====================================================

async def games_for(offset):

    date = (
        datetime.now(TZ) + timedelta(days=offset)
    ).date().isoformat()

    async def build():

        data = await get_json(
            f"{API_WEB}/schedule/{date}"
        )

        for day in data.get("gameWeek", []):

            if day.get("date") != date:
                continue

            output = []

            for game in day.get("games", []):

                start = datetime.fromisoformat(
                    game["startTimeUTC"].replace(
                        "Z",
                        "+00:00",
                    )
                ).astimezone(TZ)

                output.append((
                    game["awayTeam"]["abbrev"],
                    game["homeTeam"]["abbrev"],
                    start.strftime("%H:%M"),
                ))

            return output

        return []

    return await cached(
        ("schedule-day", date),
        600,
        build,
    )


DAYS = (
    "Aujourd'hui",
    "Demain",
    "Après-demain",
)


def games_view_keyboard(offset, games):

    rows = []

    for away, home, hour in games:

        rows.append([
            InlineKeyboardButton(
                f"{NAMES.get(away, away)} @ "
                f"{NAMES.get(home, home)} · {hour}",
                callback_data=f"g:{away}:{home}",
            )
        ])

    rows.append([
        InlineKeyboardButton(
            ("• " if i == offset else "") + label,
            callback_data=f"d:{i}",
        )
        for i, label in enumerate(DAYS)
    ])

    return InlineKeyboardMarkup(rows)


async def games_view(offset):

    offset = max(
        0,
        min(2, offset),
    )

    games = await games_for(offset)

    if games:

        text = (
            f"<b>Matchs : {DAYS[offset].lower()}</b>\n"
            "Horaires en heure de Paris 🇫🇷\n"
            "Sélectionne un match pour ouvrir les catégories."
        )

    else:

        text = (
            f"Aucun match {DAYS[offset].lower()}.\n"
            "Choisis un autre jour ou écris deux équipes."
        )

    return (
        text,
        games_view_keyboard(offset, games),
    )


async def send_games(message, offset=0):

    try:

        text, markup = await games_view(offset)

        await message.reply_text(
            text,
            parse_mode=HTML,
            reply_markup=markup,
        )

    except Exception as e:

        log.warning(
            "Calendrier : %s",
            e,
        )

        await message.reply_text(
            "Calendrier indisponible. "
            "Écris deux équipes pour ouvrir leur analyse."
        )


# =====================================================
# TELEGRAM
# =====================================================

def allowed(update):

    user = update.effective_user

    return (
        not ALLOWED
        or (user and user.id in ALLOWED)
    )


async def start(update, ctx):

    if not allowed(update):
        return

    keyboard = ReplyKeyboardMarkup(
        [[MENU_BTN]],
        resize_keyboard=True,
    )

    await update.message.reply_text(
        "🏒 <b>BLACK ICE — ANALYSE NHL</b>\n\n"
        "Sélectionne un match pour accéder aux "
        "statistiques individuelles et collectives.\n\n"
        "H2H strict sur trois saisons.",
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

        # -----------------------------
        # CHANGER DE JOUR
        # -----------------------------

        if parts[0] == "d":

            offset = int(parts[1])

            text, markup = await games_view(
                offset
            )

            await query.edit_message_text(
                text,
                parse_mode=HTML,
                reply_markup=markup,
            )

            return

        # -----------------------------
        # SÉLECTION DU MATCH
        # -----------------------------

        if parts[0] == "g":

            away = parts[1]
            home = parts[2]

            await query.edit_message_text(
                category_text(away, home),
                parse_mode=HTML,
                reply_markup=category_keyboard(
                    away,
                    home,
                ),
            )

            return

        # -----------------------------
        # RETOUR AUX CATÉGORIES
        # -----------------------------

        if parts[0] == "m":

            away = parts[1]
            home = parts[2]

            await query.edit_message_text(
                category_text(away, home),
                parse_mode=HTML,
                reply_markup=category_keyboard(
                    away,
                    home,
                ),
            )

            return

        # -----------------------------
        # ANALYSE D'UNE CATÉGORIE
        # -----------------------------

        if parts[0] == "c":

            away = parts[1]
            home = parts[2]
            category = parts[3]

            valid_categories = {
                key
                for _, key in CATEGORIES
            }

            if category not in valid_categories:
                return

            await query.edit_message_text(
                f"⏳ Analyse {NAMES[away]} @ "
                f"{NAMES[home]}...\n"
                f"Catégorie : {category}",
            )

            try:

                result = await analyze_category(
                    away,
                    home,
                    category,
                )

            except Exception as e:

                log.exception(
                    "Analyse catégorie %s : %s",
                    category,
                    e,
                )

                result = (
                    "Les données de cette catégorie "
                    "sont indisponibles pour le moment. "
                    "Réessaie dans un instant."
                )

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Retour aux catégories",
                        callback_data=f"m:{away}:{home}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📅 Autres matchs",
                        callback_data="d:0",
                    )
                ],
            ])

            await query.edit_message_text(
                result,
                parse_mode=HTML,
                reply_markup=keyboard,
            )

            return

    except Exception as e:

        log.exception(
            "Erreur bouton Telegram : %s",
            e,
        )


# =====================================================
# SAISIE MANUELLE DES MATCHS
# =====================================================

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

            await send_category_menu(
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
            "ou son abréviation."
        )

        return

    if len(parts) == 1:

        team = find_team(parts[0])

        if team:

            try:

                for offset in range(7):

                    games = await games_for(offset)

                    for away, home, _ in games:

                        if team in (away, home):

                            await send_category_menu(
                                message,
                                away,
                                home,
                            )

                            return

            except Exception as e:

                log.warning(
                    "Recherche équipe : %s",
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

    global _client

    if _client:

        await _client.aclose()

        _client = None


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
        "BLACK ICE NHL BOT démarré - "
        "14 catégories statistiques - "
        "H2H strict sur trois saisons - "
        "Europe/Paris"
    )

    app.run_polling()


if __name__ == "__main__":
    main()