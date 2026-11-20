import asyncio
import difflib
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
from html import escape as esc
from zoneinfo import ZoneInfo

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

# =====================================================
# CONFIGURATION
# =====================================================
TOKEN = os.environ["TELEGRAM_TOKEN"]
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
DB_PATH = os.environ.get("DATABASE_PATH", "/data/black_ice.db")
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_USERS", "").replace(" ", "").split(",") if x}
API_WEB = "https://api-web.nhle.com/v1"
TZ = ZoneInfo("Europe/Paris")
TOP = 5
H2H_SCORE_COUNT = 5
MENU_BTN = "📅 Matchs du jour"
HTML = ParseMode.HTML
ADMIN_SESSION_SECONDS = 12 * 3600

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("blackice")

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
        return float(value)
    except (TypeError, ValueError):
        return None


def fmt(value, digits=1):
    if value is None:
        return "N/D"
    return f"{value:.{digits}f}".replace(".", ",")


def chunks(items, size):
    for index in range(0, len(items), size):
        yield items[index:index + size]


def local_today():
    return datetime.now(TZ).date()


def season_for_date(date_value=None):
    date_value = date_value or local_today()
    start_year = date_value.year if date_value.month >= 10 else date_value.year - 1
    return f"{start_year}{start_year + 1}"


def db_connect():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with db_connect() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at TEXT
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS admin_sessions (
                user_id INTEGER PRIMARY KEY,
                expires_at REAL NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS user_state (
                user_id INTEGER PRIMARY KEY,
                state TEXT,
                updated_at REAL
            )
        """)
        db.commit()


def save_user(user):
    with db_connect() as db:
        db.execute(
            """INSERT INTO users(user_id, username, first_name, created_at)
               VALUES(?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
               username=excluded.username,
               first_name=excluded.first_name""",
            (user.id, user.username, user.first_name, datetime.now(TZ).isoformat())
        )
        db.commit()


def is_allowed(user_id):
    return not ALLOWED or user_id in ALLOWED


def is_admin(user_id):
    with db_connect() as db:
        row = db.execute(
            "SELECT expires_at FROM admin_sessions WHERE user_id=?",
            (user_id,)
        ).fetchone()
    return bool(row and row["expires_at"] > time.time())


def set_admin(user_id):
    with db_connect() as db:
        db.execute(
            """INSERT INTO admin_sessions(user_id, expires_at) VALUES(?, ?)
               ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at""",
            (user_id, time.time() + ADMIN_SESSION_SECONDS)
        )
        db.commit()


def set_state(user_id, state):
    with db_connect() as db:
        db.execute(
            """INSERT INTO user_state(user_id, state, updated_at) VALUES(?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
               state=excluded.state, updated_at=excluded.updated_at""",
            (user_id, state, time.time())
        )
        db.commit()


def get_state(user_id):
    with db_connect() as db:
        row = db.execute(
            "SELECT state FROM user_state WHERE user_id=?",
            (user_id,)
        ).fetchone()
    return row["state"] if row else None


async def get_json(client, url, params=None, headers=None):
    try:
        response = await client.get(url, params=params, headers=headers, timeout=20)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Erreur API %s : %s", url, exc)
        return None


async def nhl_get(client, path, params=None):
    return await get_json(client, f"{API_WEB}/{path.lstrip('/')}", params=params)


def parse_team_data():
    teams = {}
    for line in TEAM_DATA.splitlines():
        parts = [part.strip() for part in line.split("|") if part.strip()]
        if len(parts) >= 2:
            teams[parts[0]] = {
                "code": parts[0],
                "name": parts[1],
                "aliases": parts[1:]
            }
    return teams


NHL_TEAMS = parse_team_data()


def resolve_team(query):
    target = norm(query)
    if not target:
        return None
    for code, team in NHL_TEAMS.items():
        if target == norm(code) or target in {norm(alias) for alias in team["aliases"]}:
            return code
    candidates = []
    for code, team in NHL_TEAMS.items():
        for alias in team["aliases"]:
            candidates.append((difflib.SequenceMatcher(None, target, norm(alias)).ratio(), code))
    candidates.sort(reverse=True)
    return candidates[0][1] if candidates and candidates[0][0] >= 0.72 else None


def team_name(code):
    team = NHL_TEAMS.get(code)
    return team["name"] if team else code


async def nhl_schedule(client, date_value=None):
    date_value = date_value or local_today()
    data = await nhl_get(client, f"score/{date_value.isoformat()}")
    if not data:
        return []
    games = []
    for game in data.get("games", []):
        home = game.get("homeTeam", {})
        away = game.get("awayTeam", {})
        games.append({
            "id": game.get("id"),
            "date": game.get("gameDate"),
            "start": game.get("startTimeUTC"),
            "state": game.get("gameState"),
            "home": home.get("abbrev"),
            "away": away.get("abbrev"),
            "home_score": home.get("score"),
            "away_score": away.get("score"),
            "raw": game
        })
    return games


async def nhl_player_search(client, query):
    data = await nhl_get(client, "player-search", {"q": query})
    if not data:
        return []
    results = data.get("players", data if isinstance(data, list) else [])
    return results[:10]


async def nhl_player_landing(client, player_id):
    return await nhl_get(client, f"player/{player_id}/landing")


async def nhl_player_game_log(client, player_id, season=None, game_type=2):
    season = season or season_for_date()
    return await nhl_get(
        client,
        f"player/{player_id}/game-log/{season}/{game_type}"
    )


def extract_nhl_games(log_data):
    if not log_data:
        return []
    games = log_data.get("gameLog", log_data.get("games", []))
    return games if isinstance(games, list) else []


def nhl_game_stat(game, key):
    aliases = {
        "points": ("points", "point"),
        "goals": ("goals", "goal"),
        "assists": ("assists", "assist"),
        "shots": ("shots", "shotsOnGoal"),
        "toi": ("toi", "timeOnIce")
    }
    for candidate in aliases.get(key, (key,)):
        if candidate in game:
            return number(game.get(candidate))
    return None


def summarize_nhl_form(games, count=10):
    selected = games[:count]
    if not selected:
        return None
    result = {"games": len(selected)}
    for key in ("points", "goals", "assists", "shots"):
        values = [nhl_game_stat(game, key) for game in selected]
        values = [value for value in values if value is not None]
        result[key] = {
            "total": sum(values),
            "average": sum(values) / len(values) if values else None,
            "hit_rate": sum(value >= 1 for value in values) / len(values) if values else None,
            "values": values
        }
    return result


def nhl_form_text(summary):
    if not summary:
        return "Aucune donnée de forme disponible."
    lines = [f"Sur les {summary['games']} derniers matchs :"]
    labels = {
        "points": "Points",
        "goals": "Buts",
        "assists": "Passes",
        "shots": "Tirs"
    }
    for key, label in labels.items():
        item = summary.get(key, {})
        rate = item.get("hit_rate")
        rate_text = f"{rate * 100:.0f} %" if rate is not None else "N/D"
        lines.append(
            f"• {label} : moyenne {fmt(item.get('average'))} | "
            f"au moins 1 : {rate_text}"
        )
    return "\n".join(lines)


async def nhl_player_report(client, player_id, player_name):
    current = await nhl_player_game_log(client, player_id)
    games = extract_nhl_games(current)
    if len(games) < 10:
        previous_season = str(int(season_for_date()[:4]) - 1)
        previous_season = f"{previous_season}{int(previous_season) + 1}"
        previous = await nhl_player_game_log(client, player_id, previous_season)
        games.extend(extract_nhl_games(previous))
    games.sort(key=lambda item: item.get("gameDate", ""), reverse=True)
    last5 = summarize_nhl_form(games, 5)
    last10 = summarize_nhl_form(games, 10)
    last20 = summarize_nhl_form(games, 20)
    landing = await nhl_player_landing(client, player_id)
    return {
        "name": player_name,
        "id": player_id,
        "games": games,
        "last5": last5,
        "last10": last10,
        "last20": last20,
        "landing": landing
    }


async def nhl_team_roster(client, team_code):
    data = await nhl_get(client, f"roster/{team_code}/current")
    if not data:
        return []
    players = []
    for group in ("forwards", "defensemen", "goalies"):
        players.extend(data.get(group, []))
    return players


async def nhl_team_stats(client, team_code):
    data = await nhl_get(client, f"club-stats/{team_code}/now")
    return data or {}


async def nhl_standings(client):
    data = await nhl_get(client, "standings/now")
    if not data:
        return []
    return data.get("standings", [])


async def nhl_h2h(client, team_a, team_b, limit=10):
    today = local_today()
    start = today.replace(month=1, day=1)
    data = await nhl_get(
        client,
        f"club-schedule-season/{team_a}",
        {"season": season_for_date(today)}
    )
    games = data.get("games", []) if data else []
    matches = []
    for game in games:
        home = game.get("homeTeam", {}).get("abbrev")
        away = game.get("awayTeam", {}).get("abbrev")
        if {home, away} == {team_a, team_b}:
            matches.append(game)
    matches.sort(key=lambda item: item.get("gameDate", ""), reverse=True)
    return matches[:limit]


async def nhl_game_summary(client, game_id):
    return await nhl_get(client, f"gamecenter/{game_id}/landing")


async def nhl_game_play_by_play(client, game_id):
    return await nhl_get(client, f"gamecenter/{game_id}/play-by-play")


def nhl_player_id(player):
    return player.get("playerId") or player.get("id")


def nhl_player_display(player):
    first = player.get("firstName", {})
    last = player.get("lastName", {})
    if isinstance(first, dict):
        first = first.get("default", "")
    if isinstance(last, dict):
        last = last.get("default", "")
    return f"{first} {last}".strip() or player.get("name", "Joueur inconnu")


def nhl_game_line(game):
    date_text = game.get("gameDate", "Date inconnue")
    home = game.get("homeTeam", {})
    away = game.get("awayTeam", {})
    home_name = home.get("abbrev", "?")
    away_name = away.get("abbrev", "?")
    score = ""
    if home.get("score") is not None and away.get("score") is not None:
        score = f" — {away.get('score')}–{home.get('score')}"
    return f"{date_text} : {away_name} @ {home_name}{score}"


async def nhl_top_players(client, team_code, limit=5):
    roster = await nhl_team_roster(client, team_code)
    candidates = []
    for player in roster:
        player_id = nhl_player_id(player)
        if not player_id:
            continue
        candidates.append((player_id, nhl_player_display(player)))
    reports = await asyncio.gather(
        *(nhl_player_report(client, pid, name) for pid, name in candidates[:20]),
        return_exceptions=True
    )
    ranked = []
    for report in reports:
        if isinstance(report, Exception) or not report:
            continue
        landing = report.get("landing") or {}
        featured = landing.get("featuredStats", {}).get("regularSeason", {})
        stats = featured.get("subSeason", {})
        points = number(stats.get("points")) or 0
        ranked.append((points, report))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [report for _, report in ranked[:limit]]


def nhl_report_text(report):
    landing = report.get("landing") or {}
    featured = landing.get("featuredStats", {}).get("regularSeason", {})
    stats = featured.get("subSeason", {})
    lines = [
        f"<b>{esc(report.get('name', 'Joueur'))}</b>",
        f"Points : {esc(str(stats.get('points', 'N/D')))}",
        f"Buts : {esc(str(stats.get('goals', 'N/D')))}",
        f"Passes : {esc(str(stats.get('assists', 'N/D')))}",
        "",
        "<b>Forme récente</b>",
        nhl_form_text(report.get("last5")),
        "",
        "<b>10 derniers matchs</b>",
        nhl_form_text(report.get("last10")),
        "",
        "<b>20 derniers matchs</b>",
        nhl_form_text(report.get("last20"))
    ]
    return "\n".join(lines)


async def nhl_goalie_report(client, team_code):
    roster = await nhl_team_roster(client, team_code)
    goalies = []
    for player in roster:
        if player.get("positionCode") == "G":
            goalies.append(player)
    results = []
    for goalie in goalies:
        player_id = nhl_player_id(goalie)
        if not player_id:
            continue
        landing = await nhl_player_landing(client, player_id)
        stats = (
            landing.get("featuredStats", {})
            .get("regularSeason", {})
            .get("subSeason", {})
        ) if landing else {}
        results.append({
            "name": nhl_player_display(goalie),
            "wins": stats.get("wins"),
            "save_pct": stats.get("savePctg"),
            "gaa": stats.get("goalsAgainstAvg"),
            "shutouts": stats.get("shutouts")
        })
    return results


def nhl_goalie_text(goalies):
    if not goalies:
        return "Aucune statistique de gardien disponible."
    lines = ["<b>Gardiens</b>"]
    for goalie in goalies:
        lines.append(
            f"• {esc(goalie['name'])} — V: {esc(str(goalie.get('wins', 'N/D')))} | "
            f"% arrêts: {fmt(number(goalie.get('save_pct')), 3)} | "
            f"MBA: {fmt(number(goalie.get('gaa')))} | "
            f"Blanchissages: {esc(str(goalie.get('shutouts', 'N/D')))}"
        )
    return "\n".join(lines)


def nhl_poisson_probability(lam, threshold):
    if lam is None or lam < 0:
        return None
    probability = 0.0
    for goals in range(int(threshold)):
        probability += math.exp(-lam) * (lam ** goals) / math.factorial(goals)
    return max(0.0, min(1.0, 1.0 - probability))


def nhl_player_projection(report, market="points"):
    games = report.get("games", [])
    if not games:
        return None
    values = [nhl_game_stat(game, market) for game in games[:20]]
    values = [value for value in values if value is not None]
    if not values:
        return None
    recent = values[:5]
    average = sum(values) / len(values)
    recent_average = sum(recent) / len(recent)
    projection = 0.65 * recent_average + 0.35 * average
    return {
        "sample": len(values),
        "average": average,
        "recent_average": recent_average,
        "projection": projection,
        "hit_rate": sum(value >= 1 for value in values) / len(values)
    }


async def nhl_simulate_game(client, home, away, simulations=10000):
    home_stats, away_stats = await asyncio.gather(
        nhl_team_stats(client, home),
        nhl_team_stats(client, away)
    )
    def goals_for(stats):
        for key in ("goalsForPerGame", "goalsFor"):
            value = number(stats.get(key))
            if value is not None:
                return value
        return None
    def goals_against(stats):
        for key in ("goalsAgainstPerGame", "goalsAgainst"):
            value = number(stats.get(key))
            if value is not None:
                return value
        return None
    home_attack = goals_for(home_stats)
    away_attack = goals_for(away_stats)
    home_defense = goals_against(home_stats)
    away_defense = goals_against(away_stats)
    if None in (home_attack, away_attack, home_defense, away_defense):
        return None
    home_lambda = max(0.1, (home_attack + away_defense) / 2 + 0.15)
    away_lambda = max(0.1, (away_attack + home_defense) / 2)
    home_wins = away_wins = ties = 0
    totals = []
    for _ in range(simulations):
        home_goals = random.poisson(home_lambda) if hasattr(random, "poisson") else _poisson_sample(home_lambda)
        away_goals = random.poisson(away_lambda) if hasattr(random, "poisson") else _poisson_sample(away_lambda)
        totals.append(home_goals + away_goals)
        if home_goals > away_goals:
            home_wins += 1
        elif away_goals > home_goals:
            away_wins += 1
        else:
            ties += 1
    return {
        "home": home,
        "away": away,
        "home_lambda": home_lambda,
        "away_lambda": away_lambda,
        "home_win": home_wins / simulations,
        "away_win": away_wins / simulations,
        "tie": ties / simulations,
        "average_total": sum(totals) / len(totals)
    }


def _poisson_sample(lam):
    limit = math.exp(-lam)
    product = 1.0
    count = 0
    while product > limit:
        count += 1
        product *= random.random()
    return count - 1


async def nhl_find_player(client, query):
    players = await nhl_player_search(client, query)
    if not players:
        return None
    target = norm(query)
    for player in players:
        if norm(nhl_player_display(player)) == target:
            return player
    return players[0]


async def nhl_player_h2h(client, player_id, opponent_code):
    seasons = [season_for_date()]
    previous_start = int(seasons[0][:4]) - 1
    seasons.append(f"{previous_start}{previous_start + 1}")
    games = []
    for season in seasons:
        data = await nhl_player_game_log(client, player_id, season)
        games.extend(extract_nhl_games(data))
    filtered = [
        game for game in games
        if game.get("opponentAbbrev") == opponent_code
        or game.get("opponentTeamAbbrev") == opponent_code
    ]
    filtered.sort(key=lambda item: item.get("gameDate", ""), reverse=True)
    return filtered


async def nhl_full_player_analysis(client, query, opponent=None):
    player = await nhl_find_player(client, query)
    if not player:
        return None
    player_id = nhl_player_id(player)
    report = await nhl_player_report(client, player_id, nhl_player_display(player))
    if opponent:
        opponent_code = resolve_team(opponent)
        report["h2h"] = await nhl_player_h2h(client, player_id, opponent_code) if opponent_code else []
    else:
        report["h2h"] = []
    report["projections"] = {
        market: nhl_player_projection(report, market)
        for market in ("points", "goals", "assists", "shots")
    }
    return report


def nhl_h2h_text(games):
    if not games:
        return "Aucune confrontation directe disponible."
    lines = ["<b>Confrontations directes récentes</b>"]
    for game in games[:10]:
        date_text = game.get("gameDate", "Date inconnue")
        opponent = game.get("opponentName", game.get("opponentAbbrev", "Adversaire"))
        points = game.get("points", "N/D")
        goals = game.get("goals", "N/D")
        assists = game.get("assists", "N/D")
        lines.append(
            f"• {esc(str(date_text))} vs {esc(str(opponent))} : "
            f"{esc(str(points))} pts, {esc(str(goals))} buts, "
            f"{esc(str(assists))} passes"
        )
    return "\n".join(lines)


def nhl_projection_text(projections):
    lines = ["<b>Projections indicatives</b>"]
    labels = {
        "points": "Points",
        "goals": "Buts",
        "assists": "Passes",
        "shots": "Tirs"
    }
    for market, label in labels.items():
        item = projections.get(market)
        if not item:
            lines.append(f"• {label} : données insuffisantes")
            continue
        lines.append(
            f"• {label} : {fmt(item['projection'])} estimé(s) "
            f"(moyenne récente {fmt(item['recent_average'])}, "
            f"échantillon {item['sample']}, fréquence ≥1 : "
            f"{item['hit_rate'] * 100:.0f} %)"
        )
    return "\n".join(lines)


async def nhl_games_for(client, date_value):
    games = await nhl_schedule(client, date_value)
    return games


def nhl_games_view(games, date_value):
    if not games:
        return f"Aucun match NHL trouvé pour le {date_value.strftime('%d/%m/%Y')}."
    lines = [f"<b>🏒 NHL — {date_value.strftime('%d/%m/%Y')}</b>"]
    for game in games:
        state = game.get("state", "")
        home = team_name(game.get("home"))
        away = team_name(game.get("away"))
        score = ""
        if game.get("home_score") is not None and game.get("away_score") is not None:
            score = f" — {game['away_score']}–{game['home_score']}"
        start = game.get("start")
        if start:
            try:
                local_start = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(TZ)
                time_text = local_start.strftime("%H:%M")
            except ValueError:
                time_text = ""
        else:
            time_text = ""
        lines.append(
            f"• {esc(away)} @ {esc(home)} {esc(time_text)}"
            f"{esc(score)} — {esc(state)}"
        )
    return "\n".join(lines)


def nhl_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Matchs du jour", callback_data="nhl_today")],
        [InlineKeyboardButton("🔎 Analyse joueur", callback_data="nhl_player")],
        [InlineKeyboardButton("📊 TOP 5 équipe", callback_data="nhl_top")],
        [InlineKeyboardButton("🥅 Gardiens", callback_data="nhl_goalies")],
        [InlineKeyboardButton("⚔️ H2H équipes", callback_data="nhl_h2h")],
        [InlineKeyboardButton("🎲 Simulateur", callback_data="nhl_sim")]
    ])


def nhl_player_keyboard(player_id, name):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📈 Analyse complète", callback_data=f"nhl_report:{player_id}")],
        [InlineKeyboardButton("↩️ Menu NHL", callback_data="nhl_menu")]
    ])


async def nhl_handle_text(update, context, text_value):
    user_id = update.effective_user.id
    state = get_state(user_id)
    async with httpx.AsyncClient() as client:
        if state == "nhl_player":
            set_state(user_id, None)
            report = await nhl_full_player_analysis(client, text_value)
            if not report:
                await update.message.reply_text("Joueur introuvable ou données indisponibles.")
                return True
            await update.message.reply_text(
                nhl_report_text(report),
                parse_mode=HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Menu NHL", callback_data="nhl_menu")
                ]])
            )
            return True
        if state == "nhl_top":
            set_state(user_id, None)
            code = resolve_team(text_value)
            if not code:
                await update.message.reply_text("Équipe NHL introuvable.")
                return True
            reports = await nhl_top_players(client, code)
            if not reports:
                await update.message.reply_text("Aucune statistique disponible.")
                return True
            lines = [f"<b>TOP joueurs — {esc(team_name(code))}</b>"]
            for index, report in enumerate(reports, 1):
                landing = report.get("landing") or {}
                stats = landing.get("featuredStats", {}).get("regularSeason", {}).get("subSeason", {})
                lines.append(
                    f"{index}. {esc(report['name'])} — "
                    f"{esc(str(stats.get('points', 'N/D')))} points"
                )
            await update.message.reply_text("\n".join(lines), parse_mode=HTML)
            return True
        if state == "nhl_goalies":
            set_state(user_id, None)
            code = resolve_team(text_value)
            if not code:
                await update.message.reply_text("Équipe NHL introuvable.")
                return True
            goalies = await nhl_goalie_report(client, code)
            await update.message.reply_text(nhl_goalie_text(goalies), parse_mode=HTML)
            return True
        if state == "nhl_h2h":
            set_state(user_id, None)
            parts = re.split(r"\s+(?:vs|contre|/)\s+", text_value, maxsplit=1, flags=re.I)
            if len(parts) != 2:
                await update.message.reply_text("Format attendu : équipe A vs équipe B.")
                return True
            team_a, team_b = resolve_team(parts[0]), resolve_team(parts[1])
            if not team_a or not team_b:
                await update.message.reply_text("Une des équipes est introuvable.")
                return True
            games = await nhl_h2h(client, team_a, team_b)
            if not games:
                await update.message.reply_text("Aucune confrontation trouvée.")
                return True
            lines = [
                f"<b>H2H — {esc(team_name(team_a))} / {esc(team_name(team_b))}</b>"
            ]
            lines.extend(f"• {esc(nhl_game_line(game))}" for game in games)
            await update.message.reply_text("\n".join(lines), parse_mode=HTML)
            return True
        if state == "nhl_sim":
            set_state(user_id, None)
            parts = re.split(r"\s+(?:vs|contre|/)\s+", text_value, maxsplit=1, flags=re.I)
            if len(parts) != 2:
                await update.message.reply_text("Format attendu : équipe domicile vs équipe extérieur.")
                return True
            home, away = resolve_team(parts[0]), resolve_team(parts[1])
            if not home or not away:
                await update.message.reply_text("Une des équipes est introuvable.")
                return True
            result = await nhl_simulate_game(client, home, away)
            if not result:
                await update.message.reply_text("Données insuffisantes pour simuler ce match.")
                return True
            message = (
                f"<b>Simulation NHL — {esc(team_name(home))} vs {esc(team_name(away))}</b>\n"
                f"Victoire domicile : {result['home_win'] * 100:.1f} %\n"
                f"Victoire extérieur : {result['away_win'] * 100:.1f} %\n"
                f"Égalité après temps réglementaire : {result['tie'] * 100:.1f} %\n"
                f"Total moyen simulé : {result['average_total']:.2f} buts"
            )
            await update.message.reply_text(message, parse_mode=HTML)
            return True
    return False


async def nhl_on_button(update, context, data):
    query = update.callback_query
    user_id = update.effective_user.id
    if data == "nhl_menu":
        set_state(user_id, None)
        await query.edit_message_text("🏒 <b>ICE VENOM — NHL</b>", parse_mode=HTML, reply_markup=nhl_keyboard())
        return True
    if data == "nhl_today":
        async with httpx.AsyncClient() as client:
            games = await nhl_games_for(client, local_today())
        await query.edit_message_text(
            nhl_games_view(games, local_today()),
            parse_mode=HTML,
            reply_markup=nhl_keyboard()
        )
        return True
    if data in ("nhl_player", "nhl_top", "nhl_goalies", "nhl_h2h", "nhl_sim"):
        set_state(user_id, data.replace("nhl_", "nhl_"))
        prompts = {
            "nhl_player": "Envoie le nom du joueur NHL.",
            "nhl_top": "Envoie le nom ou le code de l'équipe NHL.",
            "nhl_goalies": "Envoie le nom ou le code de l'équipe NHL.",
            "nhl_h2h": "Envoie les deux équipes au format : Boston vs Toronto.",
            "nhl_sim": "Envoie les deux équipes au format : Boston vs Toronto."
        }
        await query.edit_message_text(prompts[data], reply_markup=nhl_keyboard())
        return True
    if data.startswith("nhl_report:"):
        player_id = data.split(":", 1)[1]
        async with httpx.AsyncClient() as client:
            report = await nhl_player_report(client, player_id, f"Joueur {player_id}")
        await query.edit_message_text(nhl_report_text(report), parse_mode=HTML, reply_markup=nhl_keyboard())
        return True
    return False


# =====================================================
# NBA ENGINE — ESPN PUBLIC API
# =====================================================
NBA_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
NBA_CORE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
NBA_CACHE = {}
NBA_CACHE_TTL = 300
NBA_MARKETS = {
    "points": ("points", "PTS"),
    "rebounds": ("rebounds", "REB"),
    "assists": ("assists", "AST"),
    "threes": ("threePointFieldGoalsMade", "3PM"),
    "steals": ("steals", "STL"),
    "blocks": ("blocks", "BLK"),
    "pra": ("pointsReboundsAssists", "PRA"),
    "pr": ("pointsRebounds", "P+R"),
    "pa": ("pointsAssists", "P+A"),
    "ra": ("reboundsAssists", "R+A")
}


async def nba_get(client, endpoint, params=None):
    url = f"{NBA_BASE}/{endpoint.lstrip('/')}"
    cache_key = (url, tuple(sorted((params or {}).items())))
    cached = NBA_CACHE.get(cache_key)
    if cached and time.time() - cached[0] < NBA_CACHE_TTL:
        return cached[1]
    data = await get_json(client, url, params=params)
    if data is not None:
        NBA_CACHE[cache_key] = (time.time(), data)
    return data


async def nba_scoreboard(client, date_value=None):
    date_value = date_value or local_today()
    date_text = date_value.strftime("%Y%m%d")
    data = await nba_get(client, "scoreboard", {"dates": date_text})
    if not data:
        return []
    events = data.get("events", [])
    games = []
    for event in events:
        competition = (event.get("competitions") or [{}])[0]
        competitors = competition.get("competitors", [])
        home = next((item for item in competitors if item.get("homeAway") == "home"), {})
        away = next((item for item in competitors if item.get("homeAway") == "away"), {})
        games.append({
            "id": event.get("id"),
            "name": event.get("name"),
            "date": event.get("date"),
            "status": (event.get("status") or {}).get("type", {}).get("detail", "Inconnu"),
            "state": (event.get("status") or {}).get("type", {}).get("state"),
            "home": (home.get("team") or {}).get("displayName", "Domicile"),
            "away": (away.get("team") or {}).get("displayName", "Extérieur"),
            "home_id": (home.get("team") or {}).get("id"),
            "away_id": (away.get("team") or {}).get("id"),
            "home_score": home.get("score"),
            "away_score": away.get("score"),
            "competitors": competitors
        })
    return games


def nba_games_text(games, date_value):
    if not games:
        return f"Aucun match NBA trouvé pour le {date_value.strftime('%d/%m/%Y')}."
    lines = [f"<b>🏀 NBA — {date_value.strftime('%d/%m/%Y')}</b>"]
    for game in games:
        try:
            local_time = datetime.fromisoformat(game["date"].replace("Z", "+00:00")).astimezone(TZ)
            time_text = local_time.strftime("%H:%M")
        except (ValueError, TypeError, AttributeError):
            time_text = "Horaire N/D"
        score = ""
        if game.get("home_score") is not None and game.get("away_score") is not None:
            score = f" — {game['away_score']}–{game['home_score']}"
        lines.append(
            f"• {esc(game['away'])} @ {esc(game['home'])} "
            f"{esc(time_text)}{esc(score)} — {esc(game['status'])}"
        )
    return "\n".join(lines)


async def nba_teams(client):
    data = await nba_get(client, "teams")
    if not data:
        return []
    sports = data.get("sports", [])
    leagues = sports[0].get("leagues", []) if sports else []
    teams = leagues[0].get("teams", []) if leagues else []
    return [item.get("team", {}) for item in teams]


def nba_team_match(teams, query):
    target = norm(query)
    if not target:
        return None
    scored = []
    for team in teams:
        name = team.get("displayName", "")
        short_name = team.get("name", "")
        abbreviation = team.get("abbreviation", "")
        candidates = [name, short_name, abbreviation, team.get("nickname", "")]
        for candidate in candidates:
            candidate_norm = norm(candidate)
            if target == candidate_norm:
                return team
            if target and candidate_norm:
                scored.append((difflib.SequenceMatcher(None, target, candidate_norm).ratio(), team))
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1] if scored and scored[0][0] >= 0.68 else None


async def nba_roster(client, team_id):
    data = await nba_get(client, f"teams/{team_id}/roster")
    if not data:
        return []
    athletes = data.get("athletes", [])
    if isinstance(athletes, dict):
        flattened = []
        for group in athletes.values():
            if isinstance(group, list):
                flattened.extend(group)
        athletes = flattened
    return athletes if isinstance(athletes, list) else []


def nba_player_name(player):
    return (
        player.get("fullName")
        or player.get("displayName")
        or "Joueur inconnu"
    )


async def nba_player_search(client, query):
    teams = await nba_teams(client)
    results = []
    for team in teams:
        team_id = team.get("id")
        if not team_id:
            continue
        roster = await nba_roster(client, team_id)
        for player in roster:
            name = nba_player_name(player)
            ratio = difflib.SequenceMatcher(None, norm(query), norm(name)).ratio()
            if norm(query) in norm(name) or ratio >= 0.72:
                results.append({
                    "id": player.get("id"),
                    "name": name,
                    "team": team.get("displayName"),
                    "team_id": team_id,
                    "position": player.get("position", {}).get("abbreviation")
                    if isinstance(player.get("position"), dict) else player.get("position"),
                    "ratio": ratio
                })
    results.sort(key=lambda item: item["ratio"], reverse=True)
    return results[:10]


async def nba_player_gamelog(client, athlete_id, season=None):
    season = season or local_today().year
    return await nba_get(client, f"athletes/{athlete_id}/gamelog", {"season": season})


def nba_extract_labels_and_events(data):
    if not data:
        return [], []
    labels = data.get("labels", [])
    events = data.get("events", [])
    return labels, events


def nba_stat_value(stats, key):
    if not isinstance(stats, dict):
        return None
    aliases = {
        "points": ("points", "PTS"),
        "rebounds": ("rebounds", "REB"),
        "assists": ("assists", "AST"),
        "threePointFieldGoalsMade": ("threePointFieldGoalsMade", "3PM", "threePointersMade"),
        "steals": ("steals", "STL"),
        "blocks": ("blocks", "BLK"),
        "minutes": ("minutes", "MIN"),
        "fieldGoalsMade": ("fieldGoalsMade", "FG"),
        "fieldGoalsAttempted": ("fieldGoalsAttempted", "FGA"),
        "freeThrowsMade": ("freeThrowsMade", "FT"),
        "turnovers": ("turnovers", "TO")
    }
    for candidate in aliases.get(key, (key,)):
        value = stats.get(candidate)
        parsed = number(value)
        if parsed is not None:
            return parsed
    return None


async def nba_event_summary(client, event_id):
    return await nba_get(client, "summary", {"event": event_id})


def nba_extract_boxscore_players(summary):
    if not summary:
        return []
    boxscore = summary.get("boxscore", {})
    players_data = boxscore.get("players", [])
    output = []
    for team_block in players_data:
        team = team_block.get("team", {})
        team_name = team.get("displayName", "Équipe inconnue")
        for stat_group in team_block.get("statistics", []):
            labels = stat_group.get("labels", [])
            athletes = stat_group.get("athletes", [])
            for athlete in athletes:
                athlete_info = athlete.get("athlete", {})
                stats_values = athlete.get("stats", [])
                stats = {}
                for index, label in enumerate(labels):
                    if index < len(stats_values):
                        stats[label] = stats_values[index]
                output.append({
                    "id": athlete_info.get("id"),
                    "name": athlete_info.get("displayName", "Joueur inconnu"),
                    "team": team_name,
                    "stats": stats,
                    "did_not_play": athlete.get("didNotPlay", False),
                    "reason": athlete.get("reason")
                })
    return output


def nba_boxscore_stat_map(player):
    stats = player.get("stats", {})
    labels = {
        "PTS": "points",
        "REB": "rebounds",
        "AST": "assists",
        "3PM": "threePointFieldGoalsMade",
        "STL": "steals",
        "BLK": "blocks",
        "MIN": "minutes",
        "FG": "fieldGoalsMade",
        "FGA": "fieldGoalsAttempted",
        "FT": "freeThrowsMade",
        "TO": "turnovers"
    }
    mapped = {}
    for label, key in labels.items():
        if label in stats:
            mapped[key] = number(stats[label])
    return mapped


async def nba_recent_games_from_team(client, team_id, limit=20):
    data = await nba_get(client, f"teams/{team_id}/schedule")
    if not data:
        return []
    events = data.get("events", [])
    completed = []
    for event in events:
        status = event.get("status", {}).get("type", {})
        if status.get("completed"):
            completed.append(event)
    completed.sort(key=lambda item: item.get("date", ""), reverse=True)
    return completed[:limit]


async def nba_player_recent_logs(client, athlete_id, team_id=None, count=20):
    current_year = local_today().year
    seasons = [current_year, current_year - 1]
    all_games = []
    for season in seasons:
        data = await nba_player_gamelog(client, athlete_id, season)
        labels, events = nba_extract_labels_and_events(data)
        for event in events:
            event_id = event.get("id")
            if not event_id:
                continue
            summary = await nba_event_summary(client, event_id)
            players = nba_extract_boxscore_players(summary)
            match = next(
                (player for player in players if str(player.get("id")) == str(athlete_id)),
                None
            )
            if not match or match.get("did_not_play"):
                continue
            stats = nba_boxscore_stat_map(match)
            all_games.append({
                "event_id": event_id,
                "date": event.get("date", ""),
                "name": event.get("name", ""),
                "stats": stats,
                "team": match.get("team"),
                "opponent": event.get("opponent", {}).get("displayName")
                if isinstance(event.get("opponent"), dict) else None
            })
            if len(all_games) >= count * 2:
                break
        if len(all_games) >= count:
            break
    all_games.sort(key=lambda item: item.get("date", ""), reverse=True)
    return all_games[:count]


def nba_market_value(game, market):
    if market == "pra":
        keys = ("points", "rebounds", "assists")
    elif market == "pr":
        keys = ("points", "rebounds")
    elif market == "pa":
        keys = ("points", "assists")
    elif market == "ra":
        keys = ("rebounds", "assists")
    else:
        key = NBA_MARKETS.get(market, (market, ""))[0]
        value = nba_stat_value(game.get("stats", {}), key)
        return value
    values = [nba_stat_value(game.get("stats", {}), key) for key in keys]
    return sum(values) if all(value is not None for value in values) else None


def nba_summarize_form(games, count=10):
    selected = games[:count]
    if not selected:
        return None
    result = {"games": len(selected), "markets": {}}
    for market in NBA_MARKETS:
        values = [nba_market_value(game, market) for game in selected]
        values = [value for value in values if value is not None]
        result["markets"][market] = {
            "values": values,
            "average": sum(values) / len(values) if values else None,
            "median": sorted(values)[len(values) // 2] if values else None,
            "hit_10": sum(value >= 10 for value in values) / len(values) if values else None,
            "hit_15": sum(value >= 15 for value in values) / len(values) if values else None,
            "hit_20": sum(value >= 20 for value in values) / len(values) if values else None
        }
    return result


def nba_form_text(summary):
    if not summary:
        return "Données de forme indisponibles."
    labels = {
        "points": "Points",
        "rebounds": "Rebonds",
        "assists": "Passes",
        "threes": "3 points",
        "steals": "Interceptions",
        "blocks": "Contres",
        "pra": "PRA",
        "pr": "Points + rebonds",
        "pa": "Points + passes",
        "ra": "Rebonds + passes"
    }
    lines = [f"Sur {summary['games']} matchs :"]
    for market, label in labels.items():
        item = summary["markets"].get(market, {})
        if item.get("average") is None:
            lines.append(f"• {label} : N/D")
            continue
        lines.append(f"• {label} : moyenne {fmt(item['average'])}")
    return "\n".join(lines)


async def nba_player_report(client, player):
    athlete_id = player.get("id")
    games = await nba_player_recent_logs(client, athlete_id, player.get("team_id"), 20)
    return {
        "id": athlete_id,
        "name": player.get("name", "Joueur inconnu"),
        "team": player.get("team", "Équipe inconnue"),
        "position": player.get("position"),
        "games": games,
        "last5": nba_summarize_form(games, 5),
        "last10": nba_summarize_form(games, 10),
        "last20": nba_summarize_form(games, 20)
    }


def nba_projection(games, market):
    values = [nba_market_value(game, market) for game in games[:20]]
    values = [value for value in values if value is not None]
    if not values:
        return None
    recent = values[:5]
    average = sum(values) / len(values)
    recent_average = sum(recent) / len(recent)
    projection = 0.65 * recent_average + 0.35 * average
    return {
        "sample": len(values),
        "average": average,
        "recent_average": recent_average,
        "projection": projection,
        "median": sorted(values)[len(values) // 2],
        "hit_rates": {
            threshold: sum(value >= threshold for value in values) / len(values)
            for threshold in (5, 10, 15, 20, 25, 30)
        }
    }


def nba_projection_text(report):
    labels = {
        "points": "Points",
        "rebounds": "Rebonds",
        "assists": "Passes",
        "threes": "3 points",
        "steals": "Interceptions",
        "blocks": "Contres",
        "pra": "PRA"
    }
    lines = ["<b>Projections statistiques indicatives</b>"]
    for market, label in labels.items():
        projection = nba_projection(report.get("games", []), market)
        if not projection:
            lines.append(f"• {label} : données insuffisantes")
            continue
        lines.append(
            f"• {label} : {fmt(projection['projection'])} estimé(s) "
            f"(moyenne 5 derniers : {fmt(projection['recent_average'])}, "
            f"moyenne échantillon : {fmt(projection['average'])}, "
            f"échantillon : {projection['sample']})"
        )
    return "\n".join(lines)


async def nba_player_h2h(client, player, opponent_team_id, limit=20):
    games = await nba_player_recent_logs(client, player.get("id"), player.get("team_id"), 40)
    selected = []
    for game in games:
        event_id = game.get("event_id")
        if not event_id:
            continue
        summary = await nba_event_summary(client, event_id)
        header = summary.get("header", {}) if summary else {}
        competitions = header.get("competitions", [])
        competitors = competitions[0].get("competitors", []) if competitions else []
        team_ids = {
            str((item.get("team") or {}).get("id"))
            for item in competitors
        }
        if str(opponent_team_id) in team_ids:
            selected.append(game)
    return selected[:limit]


async def nba_team_schedule(client, team_id):
    data = await nba_get(client, f"teams/{team_id}/schedule")
    if not data:
        return []
    events = data.get("events", [])
    return sorted(events, key=lambda item: item.get("date", ""), reverse=True)


def nba_team_record(team):
    record = team.get("record", {})
    items = record.get("items", []) if isinstance(record, dict) else []
    if items:
        return items[0].get("summary", "N/D")
    return "N/D"


async def nba_team_stats(client, team_id):
    data = await nba_get(client, f"teams/{team_id}/statistics")
    if not data:
        return None
    return data


def nba_extract_team_statistics(data):
    if not data:
        return {}
    output = {}
    for category in data.get("results", {}).get("stats", {}).get("categories", []):
        for stat in category.get("stats", []):
            name = stat.get("name")
            value = number(stat.get("value"))
            if name and value is not None:
                output[name] = value
    return output


def nba_team_stats_text(team, stats):
    if not stats:
        return f"Statistiques indisponibles pour {esc(team.get('displayName', 'Équipe'))}."
    lines = [f"<b>{esc(team.get('displayName', 'Équipe'))}</b>"]
    labels = {
        "avgPoints": "Points marqués",
        "avgPointsAgainst": "Points encaissés",
        "fieldGoalPct": "Réussite tirs",
        "threePointFieldGoalPct": "Réussite à 3 points",
        "rebounds": "Rebonds",
        "assists": "Passes",
        "steals": "Interceptions",
        "blocks": "Contres",
        "turnovers": "Ballons perdus"
    }
    for key, label in labels.items():
        if key in stats:
            lines.append(f"• {label} : {fmt(stats[key])}")
    return "\n".join(lines)


async def nba_matchup_analysis(client, home_team, away_team):
    home_stats_data, away_stats_data = await asyncio.gather(
        nba_team_stats(client, home_team["id"]),
        nba_team_stats(client, away_team["id"])
    )
    home_stats = nba_extract_team_statistics(home_stats_data)
    away_stats = nba_extract_team_statistics(away_stats_data)
    home_schedule, away_schedule = await asyncio.gather(
        nba_team_schedule(client, home_team["id"]),
        nba_team_schedule(client, away_team["id"])
    )
    return {
        "home": home_team,
        "away": away_team,
        "home_stats": home_stats,
        "away_stats": away_stats,
        "home_recent": home_schedule[:10],
        "away_recent": away_schedule[:10]
    }


def nba_matchup_text(report):
    lines = [
        f"<b>🏀 {esc(report['home'].get('displayName', 'Domicile'))} "
        f"vs {esc(report['away'].get('displayName', 'Extérieur'))}</b>",
        "",
        "<b>Statistiques équipes</b>",
        nba_team_stats_text(report["home"], report["home_stats"]),
        "",
        nba_team_stats_text(report["away"], report["away_stats"]),
        "",
        f"Dynamique récente — domicile : {len(report['home_recent'])} matchs récupérés",
        f"Dynamique récente — extérieur : {len(report['away_recent'])} matchs récupérés"
    ]
    return "\n".join(lines)


async def nba_team_h2h(client, team_a, team_b, limit=10):
    schedule = await nba_team_schedule(client, team_a["id"])
    matches = []
    for event in schedule:
        competitions = event.get("competitions", [])
        competitors = competitions[0].get("competitors", []) if competitions else []
        ids = {
            str((item.get("team") or {}).get("id"))
            for item in competitors
        }
        if str(team_b["id"]) in ids:
            matches.append(event)
        if len(matches) >= limit:
            break
    return matches


def nba_h2h_text(matches, team_a, team_b):
    if not matches:
        return (
            f"Aucune confrontation directe trouvée entre "
            f"{esc(team_a.get('displayName', 'Équipe A'))} et "
            f"{esc(team_b.get('displayName', 'Équipe B'))}."
        )
    lines = [
        f"<b>H2H — {esc(team_a.get('displayName', 'A'))} / "
        f"{esc(team_b.get('displayName', 'B'))}</b>"
    ]
    for event in matches:
        date_text = event.get("date", "Date inconnue")
        competitions = event.get("competitions", [])
        competitors = competitions[0].get("competitors", []) if competitions else []
        score_parts = []
        for competitor in competitors:
            name = (competitor.get("team") or {}).get("abbreviation", "?")
            score = competitor.get("score", "N/D")
            score_parts.append(f"{name} {score}")
        lines.append(f"• {esc(str(date_text))} — {esc(' / '.join(score_parts))}")
    return "\n".join(lines)


async def nba_simulate_matchup(client, home_team, away_team, simulations=10000):
    report = await nba_matchup_analysis(client, home_team, away_team)
    home = report["home_stats"]
    away = report["away_stats"]
    home_scoring = home.get("avgPoints") or home.get("pointsPerGame")
    away_scoring = away.get("avgPoints") or away.get("pointsPerGame")
    home_conceded = home.get("avgPointsAgainst") or home.get("pointsAgainstPerGame")
    away_conceded = away.get("avgPointsAgainst") or away.get("pointsAgainstPerGame")
    values = [home_scoring, away_scoring, home_conceded, away_conceded]
    if any(value is None for value in values):
        return None
    home_lambda = max(50, (home_scoring + away_conceded) / 2 + 1.5)
    away_lambda = max(50, (away_scoring + home_conceded) / 2)
    home_wins = away_wins = 0
    home_totals = []
    away_totals = []
    for _ in range(simulations):
        home_score = random.gauss(home_lambda, 12)
        away_score = random.gauss(away_lambda, 12)
        home_totals.append(home_score)
        away_totals.append(away_score)
        if home_score > away_score:
            home_wins += 1
        else:
            away_wins += 1
    return {
        "home_win": home_wins / simulations,
        "away_win": away_wins / simulations,
        "home_average": sum(home_totals) / simulations,
        "away_average": sum(away_totals) / simulations,
        "total_average": (sum(home_totals) + sum(away_totals)) / simulations
    }


def nba_simulation_text(result, home_team, away_team):
    if not result:
        return "Données insuffisantes pour effectuer la simulation."
    return (
        f"<b>Simulation NBA — {esc(home_team.get('displayName', 'Domicile'))} "
        f"vs {esc(away_team.get('displayName', 'Extérieur'))}</b>\n"
        f"Victoire domicile : {result['home_win'] * 100:.1f} %\n"
        f"Victoire extérieur : {result['away_win'] * 100:.1f} %\n"
        f"Score moyen simulé : {result['home_average']:.1f} – "
        f"{result['away_average']:.1f}\n"
        f"Total moyen : {result['total_average']:.1f} points"
    )


def nba_player_report_text(report):
    lines = [
        f"<b>{esc(report.get('name', 'Joueur'))}</b> — "
        f"{esc(report.get('team', 'Équipe inconnue'))}",
        f"Poste : {esc(str(report.get('position') or 'N/D'))}",
        "",
        "<b>5 derniers matchs</b>",
        nba_form_text(report.get("last5")),
        "",
        "<b>10 derniers matchs</b>",
        nba_form_text(report.get("last10")),
        "",
        "<b>20 derniers matchs</b>",
        nba_form_text(report.get("last20")),
        "",
        nba_projection_text(report)
    ]
    return "\n".join(lines)


def nba_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Matchs du jour", callback_data="nba_today")],
        [InlineKeyboardButton("🔎 Analyse joueur", callback_data="nba_player")],
        [InlineKeyboardButton("📊 Analyse équipe", callback_data="nba_team")],
        [InlineKeyboardButton("⚔️ H2H équipes", callback_data="nba_h2h")],
        [InlineKeyboardButton("🎲 Simulateur match", callback_data="nba_sim")]
    ])


async def nba_handle_text(update, context, text_value):
    user_id = update.effective_user.id
    state = get_state(user_id)
    async with httpx.AsyncClient() as client:
        if state == "nba_player":
            set_state(user_id, None)
            players = await nba_player_search(client, text_value)
            if not players:
                await update.message.reply_text(
                    "Joueur introuvable dans les effectifs récupérés ou données indisponibles."
                )
                return True
            report = await nba_player_report(client, players[0])
            await update.message.reply_text(
                nba_player_report_text(report),
                parse_mode=HTML,
                reply_markup=nba_keyboard()
            )
            return True
        if state == "nba_team":
            set_state(user_id, None)
            teams = await nba_teams(client)
            team = nba_team_match(teams, text_value)
            if not team:
                await update.message.reply_text("Équipe NBA introuvable.")
                return True
            stats_data = await nba_team_stats(client, team["id"])
            stats = nba_extract_team_statistics(stats_data)
            await update.message.reply_text(
                nba_team_stats_text(team, stats),
                parse_mode=HTML,
                reply_markup=nba_keyboard()
            )
            return True
        if state in ("nba_h2h", "nba_sim"):
            set_state(user_id, None)
            parts = re.split(r"\s+(?:vs|contre|/)\s+", text_value, maxsplit=1, flags=re.I)
            if len(parts) != 2:
                await update.message.reply_text("Format attendu : Boston vs New York.")
                return True
            teams = await nba_teams(client)
            team_a = nba_team_match(teams, parts[0])
            team_b = nba_team_match(teams, parts[1])
            if not team_a or not team_b:
                await update.message.reply_text("Une des équipes NBA est introuvable.")
                return True
            if state == "nba_h2h":
                matches = await nba_team_h2h(client, team_a, team_b)
                await update.message.reply_text(
                    nba_h2h_text(matches, team_a, team_b),
                    parse_mode=HTML,
                    reply_markup=nba_keyboard()
                )
            else:
                result = await nba_simulate_matchup(client, team_a, team_b)
                await update.message.reply_text(
                    nba_simulation_text(result, team_a, team_b),
                    parse_mode=HTML,
                    reply_markup=nba_keyboard()
                )
            return True
    return False


async def nba_on_button(update, context, data):
    query = update.callback_query
    user_id = update.effective_user.id
    if data == "nba_menu":
        set_state(user_id, None)
        await query.edit_message_text(
            "🏀 <b>ICE VENOM — NBA</b>",
            parse_mode=HTML,
            reply_markup=nba_keyboard()
        )
        return True
    if data == "nba_today":
        async with httpx.AsyncClient() as client:
            games = await nba_scoreboard(client, local_today())
        await query.edit_message_text(
            nba_games_text(games, local_today()),
            parse_mode=HTML,
            reply_markup=nba_keyboard()
        )
        return True
    if data in ("nba_player", "nba_team", "nba_h2h", "nba_sim"):
        set_state(user_id, data)
        prompts = {
            "nba_player": "Envoie le nom du joueur NBA.",
            "nba_team": "Envoie le nom d'une équipe NBA.",
            "nba_h2h": "Envoie les deux équipes au format : Boston vs New York.",
            "nba_sim": "Envoie les deux équipes au format : Boston vs New York."
        }
        await query.edit_message_text(prompts[data], reply_markup=nba_keyboard())
        return True
    return False


# =====================================================
# TELEGRAM BOT — MENU PRINCIPAL
# =====================================================
def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["🏒 NHL", "🏀 NBA"],
            [MENU_BTN]
        ],
        resize_keyboard=True
    )


async def start(update, context):
    user = update.effective_user
    save_user(user)
    if not is_allowed(user.id):
        await update.message.reply_text("Accès non autorisé.")
        return
    await update.message.reply_text(
        "🧊 <b>ICE VENOM</b>\nChoisis ton sport :",
        parse_mode=HTML,
        reply_markup=main_keyboard()
    )


async def on_button(update, context):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if not is_allowed(update.effective_user.id):
        await query.edit_message_text("Accès non autorisé.")
        return
    if await nhl_on_button(update, context, data):
        return
    if await nba_on_button(update, context, data):
        return


async def on_text(update, context):
    user = update.effective_user
    if not user or not update.message or not update.message.text:
        return
    save_user(user)
    if not is_allowed(user.id):
        await update.message.reply_text("Accès non autorisé.")
        return
    text_value = update.message.text.strip()
    if text_value == "🏒 NHL":
        set_state(user.id, None)
        await update.message.reply_text(
            "🏒 <b>ICE VENOM — NHL</b>",
            parse_mode=HTML,
            reply_markup=nhl_keyboard()
        )
        return
    if text_value == "🏀 NBA":
        set_state(user.id, None)
        await update.message.reply_text(
            "🏀 <b>ICE VENOM — NBA</b>",
            parse_mode=HTML,
            reply_markup=nba_keyboard()
        )
        return
    if text_value == MENU_BTN:
        set_state(user.id, None)
        await update.message.reply_text(
            "Choisis NHL ou NBA.",
            reply_markup=main_keyboard()
        )
        return
    if text_value.startswith("/"):
        return
    if await nhl_handle_text(update, context, text_value):
        return
    if await nba_handle_text(update, context, text_value):
        return
    state = get_state(user.id)
    if state:
        await update.message.reply_text("Je n'ai pas pu traiter cette demande. Réessaie.")
    else:
        await update.message.reply_text("Utilise les boutons NHL ou NBA.")


async def admin_command(update, context):
    user = update.effective_user
    if not user:
        return
    if not ADMIN_PASSWORD:
        await update.message.reply_text("Mot de passe administrateur non configuré.")
        return
    supplied = " ".join(context.args or [])
    if secrets.compare_digest(supplied, ADMIN_PASSWORD):
        set_admin(user.id)
        await update.message.reply_text("Session administrateur activée.")
    else:
        await update.message.reply_text("Mot de passe incorrect.")


async def stats_command(update, context):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.message.reply_text("Commande réservée à l'administrateur.")
        return
    with db_connect() as db:
        count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    await update.message.reply_text(f"Utilisateurs enregistrés : {count}")


async def help_command(update, context):
    await update.message.reply_text(
        "Commandes disponibles :\n"
        "/start — ouvrir le menu\n"
        "/admin MOTDEPASSE — session administrateur\n"
        "/stats — statistiques administrateur\n\n"
        "Choisis NHL ou NBA avec les boutons."
    )


def build_application():
    if not TOKEN:
        raise RuntimeError("La variable TELEGRAM_TOKEN est obligatoire.")
    init_db()
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(on_button))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return application


def main():
    application = build_application()
    log.info("ICE VENOM NHL + NBA démarré.")
    application.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()