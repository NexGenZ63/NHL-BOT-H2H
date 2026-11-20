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
ALLOWED = {
    int(x)
    for x in os.environ.get("ALLOWED_USERS", "").replace(" ", "").split(",")
    if x
}

API_WEB = "https://api-web.nhle.com/v1"
TZ = ZoneInfo("Europe/Paris")
TOP = 5
H2H_SCORE_COUNT = 5
MENU_BTN = "📅 Matchs du jour"
HTML = ParseMode.HTML
ADMIN_SESSION_SECONDS = 12 * 3600

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
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
            return (
                int(parts[0]) * 3600
                + int(parts[1]) * 60
                + int(parts[2])
            )
    except (TypeError, ValueError):
        pass
    return None


def format_date(value):
    if not value:
        return "Date inconnue"
    try:
        return datetime.strptime(
            str(value)[:10],
            "%Y-%m-%d"
        ).strftime("%d/%m/%Y")
    except ValueError:
        return str(value)[:10]


def player_name(player):
    first, last = player.get("firstName", ""), player.get("lastName", "")
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
    close = difflib.get_close_matches(
        value,
        list(ALIASES),
        n=1,
        cutoff=0.75
    )
    return ALIASES[close[0]] if close else None


SPLIT = re.compile(
    r"\s+(?:vs\.?|versus|contre|at|c\.)\s+|\s*@\s*|\s+-\s+",
    re.I
)

# =====================================================
# SQLITE
# =====================================================

def init_db():
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)

    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS settings "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS odds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_date TEXT NOT NULL,
                away TEXT NOT NULL,
                home TEXT NOT NULL,
                player TEXT NOT NULL,
                player_norm TEXT NOT NULL,
                market TEXT NOT NULL,
                odd REAL NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(match_date, away, home, player_norm, market)
            )"""
        )
        db.execute(
            "INSERT OR IGNORE INTO settings (key,value) "
            "VALUES ('bot_open','1')"
        )
        db.commit()

    log.info("SQLite initialisée : %s", DB_PATH)


def db_setting(key, default=None):
    with sqlite3.connect(DB_PATH) as db:
        row = db.execute(
            "SELECT value FROM settings WHERE key=?",
            (key,)
        ).fetchone()
    return row[0] if row else default


def set_db_setting(key, value):
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """INSERT INTO settings(key,value) VALUES(?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, str(value))
        )
        db.commit()


def bot_is_open():
    return db_setting("bot_open", "1") == "1"


def save_odd(match_date, away, home, player, market, odd):
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """INSERT INTO odds(
                match_date,away,home,player,player_norm,market,odd,updated_at
            )
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(match_date,away,home,player_norm,market)
            DO UPDATE SET
                player=excluded.player,
                odd=excluded.odd,
                updated_at=excluded.updated_at""",
            (
                match_date,
                away,
                home,
                player,
                norm(player),
                market,
                odd,
                datetime.now(TZ).isoformat()
            )
        )
        db.commit()


def get_odd(match_date, away, home, player, market):
    if not match_date:
        return None
    with sqlite3.connect(DB_PATH) as db:
        row = db.execute(
            """SELECT odd FROM odds
               WHERE match_date=? AND away=? AND home=?
               AND player_norm=? AND market=?""",
            (match_date, away, home, norm(player), market)
        ).fetchone()
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
    return bool(
        update.effective_user
        and admin_session_valid(ctx)
    )


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
        await update.message.reply_text(
            "⛔ Connecte-toi d'abord avec /admin."
        )
        return False
    return True


async def cmd_admin(update, ctx):
    ctx.user_data["await_admin_password"] = True
    await update.message.reply_text(
        "🔐 Envoie le mot de passe administrateur."
    )


async def cmd_logout(update, ctx):
    ctx.user_data.pop("admin_expiry", None)
    ctx.user_data.pop("admin_flow", None)
    await update.message.reply_text("Session administrateur fermée.")


async def cmd_fermer(update, ctx):
    if not await admin_required(update, ctx):
        return
    set_db_setting("bot_open", "0")
    await update.message.reply_text("🔒 Bot fermé aux utilisateurs.")


async def cmd_ouvrir(update, ctx):
    if not await admin_required(update, ctx):
        return
    set_db_setting("bot_open", "1")
    await update.message.reply_text("🔓 Bot ouvert aux utilisateurs.")


async def cmd_statut(update, ctx):
    if not await admin_required(update, ctx):
        return
    status = "OUVERT" if bot_is_open() else "FERMÉ"
    await update.message.reply_text(
        f"État du bot : <b>{status}</b>",
        parse_mode=HTML
    )


async def cmd_annuler(update, ctx):
    ctx.user_data.pop("admin_flow", None)
    await update.message.reply_text("Saisie annulée.")


# =====================================================
# HTTP / NHL API
# =====================================================

_client = None


async def get_client():
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": "ICE-VENOM/1.0"}
        )
    return _client


async def api_get(url, params=None):
    client = await get_client()
    response = await client.get(url, params=params)
    response.raise_for_status()
    return response.json()


async def nhl_get(path, params=None):
    return await api_get(
        f"{API_WEB}/{path.lstrip('/')}",
        params=params
    )


async def games_for(offset=0):
    date = datetime.now(TZ).date() + timedelta(days=offset)
    data = await nhl_get(f"score/{date.isoformat()}")
    games = []

    for game in data.get("games", []):
        home = game.get("homeTeam", {})
        away = game.get("awayTeam", {})

        home_abbr = home.get("abbrev")
        away_abbr = away.get("abbrev")

        if home_abbr not in NAMES or away_abbr not in NAMES:
            continue

        games.append((
            away_abbr,
            home_abbr,
            game.get("id"),
            game.get("gameDate")
        ))

    return games


async def find_upcoming_date(away, home):
    for offset in range(8):
        try:
            games = await games_for(offset)
        except Exception:
            continue
        for game_away, game_home, _, date in games:
            if game_away == away and game_home == home:
                return date
    return None


async def send_games(message, offset=0):
    try:
        games = await games_for(offset)
    except Exception as error:
        log.warning("Calendrier NHL indisponible : %s", error)
        await message.reply_text(
            "Calendrier NHL indisponible pour le moment."
        )
        return

    if not games:
        await message.reply_text(
            "Aucun match NHL trouvé pour cette date."
        )
        return

    date = datetime.now(TZ).date() + timedelta(days=offset)
    lines = [
        f"<b>🏒 NHL — {date.strftime('%d/%m/%Y')}</b>",
        "Horaires de Paris 🇫🇷",
        ""
    ]
    rows = []

    for away, home, game_id, game_date in games:
        lines.append(
            f"• {esc(NAMES[away])} @ {esc(NAMES[home])}"
        )
        rows.append([
            InlineKeyboardButton(
                f"{NAMES[away]} @ {NAMES[home]}",
                callback_data=f"g:{away}:{home}:{game_date or ''}"
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "➡️ Matchs du lendemain",
            callback_data=f"d:{offset + 1}"
        )
    ])

    await message.reply_text(
        "\n".join(lines),
        parse_mode=HTML,
        reply_markup=InlineKeyboardMarkup(rows)
    )


async def send_category_menu(message, away, home, match_date=None):
    await message.reply_text(
        category_text(away, home),
        parse_mode=HTML,
        reply_markup=category_keyboard(away, home, match_date)
    )


def category_text(away, home):
    return (
        f"<b>🏒 {esc(NAMES[away])} @ {esc(NAMES[home])}</b>\n\n"
        "Choisis une catégorie d'analyse."
    )


CATEGORIES = [
    ("⭐ TOP 5 joueurs", "players"),
    ("🎯 Buteurs", "goals"),
    ("🅰️ Pointeurs", "points"),
    ("🤝 Passeurs", "assists"),
    ("🏒 Tirs", "shots"),
    ("🥅 Gardiens", "goalies"),
    ("⚔️ H2H", "h2h"),
    ("🧬 Simulation", "sim")
]


def category_keyboard(away, home, match_date=None):
    date_value = match_date or ""
    rows = []

    for label, category in CATEGORIES:
        rows.append([
            InlineKeyboardButton(
                label,
                callback_data=f"a:{away}:{home}:{category}:{date_value}"
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "📅 Autres matchs",
            callback_data="d:0"
        )
    ])

    return InlineKeyboardMarkup(rows)


def split_telegram_text(text, limit=3900):
    chunks = []
    current = ""

    for line in text.splitlines():
        candidate = current + ("\n" if current else "") + line
        if len(candidate) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = candidate

    if current:
        chunks.append(current)

    return chunks


# =====================================================
# NHL PLAYERS / ROSTERS / GAME LOGS
# =====================================================

async def player_search(query):
    data = await nhl_get(
        "player-search",
        {"q": query}
    )
    return data.get("players", []) if data else []


async def player_landing(player_id):
    return await nhl_get(f"player/{player_id}/landing")


async def player_game_log(player_id, season, game_type=2):
    return await nhl_get(
        f"player/{player_id}/game-log/{season}/{game_type}"
    )


def season_code(date=None):
    date = date or datetime.now(TZ).date()
    start = date.year if date.month >= 10 else date.year - 1
    return f"{start}{start + 1}"


def extract_player_stats(data):
    if not data:
        return []
    return data.get("gameLog", [])


async def player_recent_games(player_id, count=20):
    season = season_code()
    previous_start = int(season[:4]) - 1
    previous_season = f"{previous_start}{previous_start + 1}"

    current = await player_game_log(player_id, season)
    games = extract_player_stats(current)

    if len(games) < count:
        previous = await player_game_log(
            player_id,
            previous_season
        )
        games.extend(extract_player_stats(previous))

    games.sort(
        key=lambda game: game.get("gameDate", ""),
        reverse=True
    )
    return games[:count]


def player_game_value(game, market):
    aliases = {
        "points": ("points",),
        "goals": ("goals",),
        "assists": ("assists",),
        "shots": ("shots", "shotsOnGoal")
    }

    for key in aliases.get(market, (market,)):
        value = number(game.get(key))
        if value is not None:
            return value

    return None


def summarize_player_form(games, market, threshold=1):
    values = [
        player_game_value(game, market)
        for game in games
    ]
    values = [value for value in values if value is not None]

    if not values:
        return None

    return {
        "games": len(values),
        "average": sum(values) / len(values),
        "hit_rate": sum(value >= threshold for value in values) / len(values),
        "values": values
    }


def nhl_form_summary(games):
    markets = {
        "points": "Points",
        "goals": "Buts",
        "assists": "Passes",
        "shots": "Tirs"
    }

    lines = []

    for market, label in markets.items():
        form = summarize_player_form(games, market)
        if not form:
            lines.append(f"{label} : N/D")
            continue

        lines.append(
            f"{label} : moyenne {fmt(form['average'])} | "
            f"au moins 1 : {fmt_percent(form['hit_rate'])}"
        )

    return "\n".join(lines)


async def player_profile_data(player_id):
    landing = await player_landing(player_id)
    games = await player_recent_games(player_id, 20)

    featured = (
        landing.get("featuredStats", {})
        .get("regularSeason", {})
        .get("subSeason", {})
    ) if landing else {}

    return {
        "landing": landing,
        "games": games,
        "featured": featured,
        "last5": games[:5],
        "last10": games[:10],
        "last20": games[:20]
    }


async def show_player_profile(query, ctx, player_id):
    await query.edit_message_text(
        "⏳ Chargement de la fiche joueur..."
    )

    try:
        data = await player_profile_data(player_id)
    except Exception as error:
        log.warning("Fiche joueur NHL indisponible : %s", error)
        await query.edit_message_text(
            "Les données de ce joueur sont indisponibles."
        )
        return

    featured = data["featured"]
    landing = data["landing"] or {}
    name = (
        landing.get("firstName", {}).get("default", "")
        + " "
        + landing.get("lastName", {}).get("default", "")
    ).strip() or f"Joueur {player_id}"

    lines = [
        f"<b>🏒 {esc(name)}</b>",
        "",
        f"Points saison : {esc(str(featured.get('points', 'N/D')))}",
        f"Buts saison : {esc(str(featured.get('goals', 'N/D')))}",
        f"Passes saison : {esc(str(featured.get('assists', 'N/D')))}",
        "",
        "<b>Forme — 5 derniers matchs</b>",
        nhl_form_summary(data["last5"]),
        "",
        "<b>Forme — 10 derniers matchs</b>",
        nhl_form_summary(data["last10"]),
        "",
        "<b>Forme — 20 derniers matchs</b>",
        nhl_form_summary(data["last20"])
    ]

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode=HTML,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "⬅️ Retour",
                    callback_data="backtop"
                )
            ]
        ])
    )


async def get_team_roster(team):
    data = await nhl_get(f"roster/{team}/current")
    if not data:
        return []

    players = []
    for group in ("forwards", "defensemen", "goalies"):
        players.extend(data.get(group, []))

    return players


def get_player_id(player):
    return player.get("playerId") or player.get("id")


async def team_top_players(team, limit=TOP):
    roster = await get_team_roster(team)
    candidates = []

    for player in roster:
        player_id = get_player_id(player)
        if not player_id:
            continue
        candidates.append((player_id, player_name(player)))

    reports = await asyncio.gather(
        *[
            player_profile_data(player_id)
            for player_id, _ in candidates[:20]
        ],
        return_exceptions=True
    )

    ranked = []

    for index, report in enumerate(reports):
        if isinstance(report, Exception) or not report:
            continue

        featured = report.get("featured", {})
        points = number(featured.get("points")) or 0
        ranked.append((
            points,
            candidates[index][0],
            candidates[index][1]
        ))

    ranked.sort(reverse=True)
    return ranked[:limit]


def player_top_keyboard(ranked):
    rows = []

    for points, player_id, name in ranked:
        rows.append([
            InlineKeyboardButton(
                f"{name} — {points} pts",
                callback_data=f"p:{player_id}"
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "⬅️ Retour aux catégories",
            callback_data="backcat"
        )
    ])

    return InlineKeyboardMarkup(rows)


# =====================================================
# NHL H2H / TEAM STATS / GOALIES / SIMULATION
# =====================================================

async def team_schedule(team):
    data = await nhl_get(
        f"club-schedule-season/{team}",
        {"season": season_code()}
    )
    return data.get("games", []) if data else []


async def team_h2h(team_a, team_b, limit=10):
    games = await team_schedule(team_a)

    selected = []

    for game in games:
        home = game.get("homeTeam", {}).get("abbrev")
        away = game.get("awayTeam", {}).get("abbrev")

        if {home, away} == {team_a, team_b}:
            selected.append(game)

    selected.sort(
        key=lambda item: item.get("gameDate", ""),
        reverse=True
    )

    return selected[:limit]


async def team_stats(team):
    return await nhl_get(f"club-stats/{team}/now")


async def goalie_stats(team):
    roster = await get_team_roster(team)
    goalies = [
        player for player in roster
        if player.get("positionCode") == "G"
    ]

    output = []

    for goalie in goalies:
        player_id = get_player_id(goalie)
        if not player_id:
            continue

        landing = await player_landing(player_id)
        stats = (
            landing.get("featuredStats", {})
            .get("regularSeason", {})
            .get("subSeason", {})
        ) if landing else {}

        output.append({
            "name": player_name(goalie),
            "wins": stats.get("wins"),
            "save_pct": stats.get("savePctg"),
            "gaa": stats.get("goalsAgainstAvg"),
            "shutouts": stats.get("shutouts")
        })

    return output


def nhl_poisson_sample(lam):
    limit = math.exp(-lam)
    product = 1.0
    count = 0

    while product > limit:
        count += 1
        product *= random.random()

    return count - 1


async def simulate_nhl(home, away, simulations=10000):
    home_stats, away_stats = await asyncio.gather(
        team_stats(home),
        team_stats(away)
    )

    if not home_stats or not away_stats:
        return None

    home_attack = number(home_stats.get("goalsForPerGame"))
    away_attack = number(away_stats.get("goalsForPerGame"))
    home_defense = number(home_stats.get("goalsAgainstPerGame"))
    away_defense = number(away_stats.get("goalsAgainstPerGame"))

    if None in (
        home_attack,
        away_attack,
        home_defense,
        away_defense
    ):
        return None

    home_lambda = max(0.1, (home_attack + away_defense) / 2 + 0.15)
    away_lambda = max(0.1, (away_attack + home_defense) / 2)

    home_wins = 0
    away_wins = 0
    ties = 0
    totals = []

    for _ in range(simulations):
        home_goals = nhl_poisson_sample(home_lambda)
        away_goals = nhl_poisson_sample(away_lambda)
        totals.append(home_goals + away_goals)

        if home_goals > away_goals:
            home_wins += 1
        elif away_goals > home_goals:
            away_wins += 1
        else:
            ties += 1

    return {
        "home_win": home_wins / simulations,
        "away_win": away_wins / simulations,
        "tie": ties / simulations,
        "average_total": sum(totals) / len(totals)
    }


# =====================================================
# NHL CATEGORY ANALYSIS
# =====================================================

async def prepare_category(away, home, category, match_date=None):
    if category == "players":
        ranked = await team_top_players(home, TOP)
        if not ranked:
            return {
                "kind": "text",
                "text": "Aucune donnée de joueurs disponible."
            }

        lines = [
            f"<b>⭐ TOP {TOP} — {esc(NAMES[home])}</b>",
            ""
        ]

        for index, (points, player_id, name) in enumerate(ranked, 1):
            lines.append(
                f"{index}. {esc(name)} — {points} points"
            )

        return {
            "kind": "players",
            "ranked": ranked,
            "text": "\n".join(lines)
        }

    if category == "h2h":
        games = await team_h2h(away, home)

        if not games:
            return {
                "kind": "text",
                "text": "Aucune confrontation directe disponible."
            }

        lines = [
            f"<b>⚔️ H2H — {esc(NAMES[away])} / {esc(NAMES[home])}</b>",
            ""
        ]

        for game in games:
            date = format_date(game.get("gameDate"))
            home_team = game.get("homeTeam", {})
            away_team = game.get("awayTeam", {})
            score = (
                f"{away_team.get('score', 'N/D')}–"
                f"{home_team.get('score', 'N/D')}"
            )
            lines.append(f"• {date} — {score}")

        return {
            "kind": "text",
            "text": "\n".join(lines)
        }

    if category == "goalies":
        goalies = await goalie_stats(home)

        if not goalies:
            return {
                "kind": "text",
                "text": "Statistiques des gardiens indisponibles."
            }

        lines = [
            f"<b>🥅 GARDIENS — {esc(NAMES[home])}</b>",
            ""
        ]

        for goalie in goalies:
            lines.append(
                f"• {esc(goalie['name'])}\n"
                f"Victoires : {esc(str(goalie['wins'] or 'N/D'))}\n"
                f"% arrêts : {fmt(number(goalie['save_pct']), 3)}\n"
                f"MBA : {fmt(number(goalie['gaa']))}\n"
                f"Blanchissages : {esc(str(goalie['shutouts'] or 'N/D'))}"
            )

        return {
            "kind": "text",
            "text": "\n\n".join(lines)
        }

    if category == "sim":
        result = await simulate_nhl(home, away)

        if not result:
            return {
                "kind": "text",
                "text": "Données insuffisantes pour simuler ce match."
            }

        text = (
            f"<b>🧬 SIMULATION NHL — 10 000 TIRAGES</b>\n\n"
            f"{esc(NAMES[away])} @ {esc(NAMES[home])}\n\n"
            f"Victoire domicile : {fmt_percent(result['home_win'])}\n"
            f"Victoire extérieur : {fmt_percent(result['away_win'])}\n"
            f"Égalité après 60 minutes : {fmt_percent(result['tie'])}\n"
            f"Total moyen simulé : {fmt(result['average_total'])} buts"
        )

        return {
            "kind": "text",
            "text": text
        }

    market_map = {
        "points": "points",
        "goals": "goals",
        "assists": "assists",
        "shots": "shots"
    }

    if category in market_map:
        roster = await get_team_roster(home)
        players = [
            player for player in roster
            if player.get("positionCode") != "G"
        ]

        reports = await asyncio.gather(
            *[
                player_profile_data(get_player_id(player))
                for player in players[:18]
                if get_player_id(player)
            ],
            return_exceptions=True
        )

        ranked = []

        for index, report in enumerate(reports):
            if isinstance(report, Exception) or not report:
                continue

            games = report.get("games", [])
            values = [
                player_game_value(game, market_map[category])
                for game in games[:10]
            ]
            values = [value for value in values if value is not None]

            if not values:
                continue

            average = sum(values) / len(values)
            hit_rate = sum(value >= 1 for value in values) / len(values)
            ranked.append((
                hit_rate,
                average,
                players[index],
                values
            ))

        ranked.sort(
            key=lambda item: (item[0], item[1]),
            reverse=True
        )

        if not ranked:
            return {
                "kind": "text",
                "text": "Données insuffisantes pour ce marché."
            }

        labels = {
            "points": "POINTS",
            "goals": "BUTS",
            "assists": "PASSES",
            "shots": "TIRS"
        }

        lines = [
            f"<b>🏒 {labels[category]} — {esc(NAMES[home])}</b>",
            "Forme sur les 10 derniers matchs disponibles",
            ""
        ]

        for index, (rate, average, player, values) in enumerate(ranked[:10], 1):
            lines.append(
                f"{index}. {esc(player_name(player))}\n"
                f"Moyenne : {fmt(average)} | Fréquence ≥1 : {fmt_percent(rate)}"
            )

        return {
            "kind": "text",
            "text": "\n\n".join(lines)
        }

    return {
        "kind": "text",
        "text": "Catégorie non reconnue."
    }


# =====================================================
# NHL ODDS ADMINISTRATION
# =====================================================

ODD_MARKETS = {
    "points": "Point",
    "goals": "But",
    "assists": "Passe",
    "shots": "Tirs"
}


def odds_market_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                label,
                callback_data=f"oddmarket:{market}"
            )
        ]
        for market, label in ODD_MARKETS.items()
    ])


def admin_games_keyboard(games):
    rows = []

    for away, home, game_id, date in games:
        rows.append([
            InlineKeyboardButton(
                f"{NAMES[away]} @ {NAMES[home]}",
                callback_data=f"oddgame:{away}:{home}:{date or ''}"
            )
        ])

    return InlineKeyboardMarkup(rows)


async def cmd_cotes(update, ctx):
    if not await admin_required(update, ctx):
        return

    games = await games_for(0)

    if not games:
        await update.message.reply_text(
            "Aucun match NHL trouvé pour aujourd'hui."
        )
        return

    ctx.user_data["admin_flow"] = {"step": "game"}

    await update.message.reply_text(
        "Choisis le match :",
        reply_markup=admin_games_keyboard(games)
    )


# =====================================================
# MULTISPORT / NBA MENU
# =====================================================

def is_multisport_request(text):
    value = norm(text)
    return value in (
        "menu sports",
        "choix sports",
        "multisport",
        "sports"
    )


def multisport_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🏒 NHL — moteur original",
                callback_data="ms:nhl"
            )
        ],
        [
            InlineKeyboardButton(
                "🏀 NBA — moteur d'analyse",
                callback_data="nba:days"
            )
        ]
    ])


async def send_multisport_menu(message):
    await message.reply_text(
        "🧊 <b>ICE VENOM</b>\n\nChoisis ton moteur :",
        parse_mode=HTML,
        reply_markup=multisport_keyboard()
    )


# =====================================================
# NBA ENGINE
# =====================================================

NBA_API = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
NBA_CACHE = {}
NBA_CACHE_TTL = 240

NBA_TEAMS = {
    "ATL": ("Atlanta Hawks", "Hawks"),
    "BOS": ("Boston Celtics", "Celtics"),
    "BKN": ("Brooklyn Nets", "Nets"),
    "CHA": ("Charlotte Hornets", "Hornets"),
    "CHI": ("Chicago Bulls", "Bulls"),
    "CLE": ("Cleveland Cavaliers", "Cavaliers"),
    "DAL": ("Dallas Mavericks", "Mavericks"),
    "DEN": ("Denver Nuggets", "Nuggets"),
    "DET": ("Detroit Pistons", "Pistons"),
    "GSW": ("Golden State Warriors", "Warriors"),
    "HOU": ("Houston Rockets", "Rockets"),
    "IND": ("Indiana Pacers", "Pacers"),
    "LAC": ("LA Clippers", "Clippers"),
    "LAL": ("Los Angeles Lakers", "Lakers"),
    "MEM": ("Memphis Grizzlies", "Grizzlies"),
    "MIA": ("Miami Heat", "Heat"),
    "MIL": ("Milwaukee Bucks", "Bucks"),
    "MIN": ("Minnesota Timberwolves", "Timberwolves"),
    "NOP": ("New Orleans Pelicans", "Pelicans"),
    "NYK": ("New York Knicks", "Knicks"),
    "OKC": ("Oklahoma City Thunder", "Thunder"),
    "ORL": ("Orlando Magic", "Magic"),
    "PHI": ("Philadelphia 76ers", "76ers"),
    "PHX": ("Phoenix Suns", "Suns"),
    "POR": ("Portland Trail Blazers", "Trail Blazers"),
    "SAC": ("Sacramento Kings", "Kings"),
    "SAS": ("San Antonio Spurs", "Spurs"),
    "TOR": ("Toronto Raptors", "Raptors"),
    "UTA": ("Utah Jazz", "Jazz"),
    "WAS": ("Washington Wizards", "Wizards")
}

NBA_TEAM_NAMES = {
    abbr: data[0]
    for abbr, data in NBA_TEAMS.items()
}

NBA_TEAM_ALIASES = {}

for _abbr, (_, _name) in NBA_TEAMS.items():
    NBA_TEAM_ALIASES[norm(_abbr)] = _abbr
    NBA_TEAM_ALIASES[norm(_name)] = _abbr
    NBA_TEAM_ALIASES[norm(_name.replace("Los Angeles", "LA"))] = _abbr

NBA_TEAM_ALIASES.update({
    norm("Golden State"): "GSW",
    norm("New York"): "NYK",
    norm("San Antonio"): "SAS"
})


def nba_find_team(value):
    value = norm(value)

    if value in NBA_TEAM_ALIASES:
        return NBA_TEAM_ALIASES[value]

    close = difflib.get_close_matches(
        value,
        list(NBA_TEAM_ALIASES),
        n=1,
        cutoff=.78
    )

    return NBA_TEAM_ALIASES[close[0]] if close else None


async def nba_get(path, params=None):
    url = f"{NBA_API}/{path.lstrip('/')}"
    cache_key = (url, tuple(sorted((params or {}).items())))
    cached = NBA_CACHE.get(cache_key)

    if cached and time.time() - cached[0] < NBA_CACHE_TTL:
        return cached[1]

    data = await api_get(url, params=params)

    if data is not None:
        NBA_CACHE[cache_key] = (time.time(), data)

    return data


async def nba_scoreboard(date_value=None):
    date_value = date_value or datetime.now(TZ).date()
    date_text = date_value.strftime("%Y%m%d")
    data = await nba_get(
        "scoreboard",
        {"dates": date_text}
    )

    if not data:
        return []

    output = []

    for event in data.get("events", []):
        competition = (event.get("competitions") or [{}])[0]
        competitors = competition.get("competitors", [])

        home = next(
            (item for item in competitors if item.get("homeAway") == "home"),
            {}
        )
        away = next(
            (item for item in competitors if item.get("homeAway") == "away"),
            {}
        )

        home_team = home.get("team") or {}
        away_team = away.get("team") or {}

        output.append({
            "id": event.get("id"),
            "date": event.get("date"),
            "name": event.get("name"),
            "status": (
                event.get("status", {})
                .get("type", {})
                .get("detail", "Inconnu")
            ),
            "state": (
                event.get("status", {})
                .get("type", {})
                .get("state")
            ),
            "home": home_team.get("abbreviation"),
            "away": away_team.get("abbreviation"),
            "home_name": home_team.get("displayName"),
            "away_name": away_team.get("displayName"),
            "home_score": home.get("score"),
            "away_score": away.get("score")
        })

    return output


def nba_games_keyboard(games):
    rows = []

    for game in games:
        away = game.get("away")
        home = game.get("home")

        if not away or not home:
            continue

        rows.append([
            InlineKeyboardButton(
                f"{away} @ {home}",
                callback_data=f"nba:game:{away}:{home}"
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "⬅️ Retour NHL / NBA",
            callback_data="ms:home"
        )
    ])

    return InlineKeyboardMarkup(rows)


async def show_nba_days(query):
    await query.edit_message_text(
        "⏳ Chargement du calendrier NBA…"
    )

    try:
        games = await nba_scoreboard()
    except Exception as error:
        log.warning("Calendrier NBA indisponible : %s", error)
        await query.edit_message_text(
            "🏀 NBA\n\nCalendrier indisponible pour le moment.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Retour",
                        callback_data="ms:home"
                    )
                ]
            ])
        )
        return

    date_label = datetime.now(TZ).strftime("%d/%m/%Y")

    if not games:
        await query.edit_message_text(
            f"🏀 NBA — {date_label}\n\nAucun match trouvé.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Retour",
                        callback_data="ms:home"
                    )
                ]
            ])
        )
        return

    lines = [
        f"<b>🏀 NBA — {date_label}</b>",
        "Horaires de Paris 🇫🇷",
        ""
    ]

    for game in games:
        try:
            start = datetime.fromisoformat(
                game["date"].replace("Z", "+00:00")
            ).astimezone(TZ)
            time_label = start.strftime("%H:%M")
        except (ValueError, TypeError, AttributeError):
            time_label = "Horaire N/D"

        score = ""

        if game.get("home_score") is not None:
            score = (
                f" — {game.get('away_score')}–"
                f"{game.get('home_score')}"
            )

        lines.append(
            f"• {esc(game.get('away_name') or game.get('away') or '?')} "
            f"@ {esc(game.get('home_name') or game.get('home') or '?')} "
            f"{time_label}{score}"
        )

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode=HTML,
        reply_markup=nba_games_keyboard(games)
    )


NBA_MARKETS = [
    ("Points", "points"),
    ("Rebonds", "rebounds"),
    ("Passes", "assists"),
    ("Tirs à 3 points", "threes"),
    ("Interceptions", "steals"),
    ("Contres", "blocks"),
    ("Points + rebonds + passes", "pra"),
    ("Points + rebonds", "pr"),
    ("Points + passes", "pa"),
    ("Rebonds + passes", "ra"),
    ("Analyse équipes", "teams"),
    ("Simulation", "sim")
]


NBA_STAT_LABELS = {
    "points": "Points",
    "rebounds": "Rebonds",
    "assists": "Passes",
    "threes": "Tirs à 3 points réussis",
    "steals": "Interceptions",
    "blocks": "Contres",
    "pra": "Points + rebonds + passes",
    "pr": "Points + rebonds",
    "pa": "Points + passes",
    "ra": "Rebonds + passes"
}


def nba_category_keyboard(away, home, match_date=None):
    rows = []

    for index in range(0, len(NBA_MARKETS), 2):
        pair = NBA_MARKETS[index:index + 2]
        rows.append([
            InlineKeyboardButton(
                label,
                callback_data=f"nba:market:{away}:{home}:{market}"
            )
            for label, market in pair
        ])

    rows.append([
        InlineKeyboardButton(
            "📅 Autres matchs NBA",
            callback_data="nba:days"
        )
    ])

    rows.append([
        InlineKeyboardButton(
            "⬅️ Retour NHL / NBA",
            callback_data="ms:home"
        )
    ])

    return InlineKeyboardMarkup(rows)


def nba_category_text(away, home):
    return (
        f"<b>🏀 {esc(NBA_TEAM_NAMES.get(away, away))} "
        f"@ {esc(NBA_TEAM_NAMES.get(home, home))}</b>\n\n"
        "Choisis une catégorie d'analyse.\n\n"
        "<i>Joueurs : forme 5/10/20 matchs et H2H. "
        "Équipes : forme récente et domicile/extérieur.</i>"
    )


async def nba_team_roster(team_abbr):
    info = NBA_TEAMS.get(team_abbr)

    if not info:
        return []

    data = await nba_get("teams")
    if not data:
        return []

    try:
        sports = data.get("sports", [])
        leagues = sports[0].get("leagues", [])
        teams = leagues[0].get("teams", [])
    except (IndexError, KeyError, TypeError):
        return []

    team_id = None

    for item in teams:
        team = item.get("team", {})
        if team.get("abbreviation") == team_abbr:
            team_id = team.get("id")
            break

    if not team_id:
        return []

    roster_data = await nba_get(f"teams/{team_id}/roster")

    if not roster_data:
        return []

    athletes = roster_data.get("athletes", [])

    if isinstance(athletes, dict):
        flattened = []

        for group in athletes.values():
            if isinstance(group, list):
                flattened.extend(group)

        athletes = flattened

    output = []

    for player in athletes:
        athlete_id = player.get("id")

        if not athlete_id:
            continue

        output.append({
            "id": athlete_id,
            "name": (
                player.get("fullName")
                or player.get("displayName")
                or "Joueur inconnu"
            ),
            "team": team_abbr,
            "position": (
                player.get("position", {}).get("abbreviation")
                if isinstance(player.get("position"), dict)
                else player.get("position")
            )
        })

    return output


async def nba_event_summary(event_id):
    return await nba_get(
        "summary",
        {"event": event_id}
    )


def nba_normalize_stat_labels(labels, values):
    output = {}

    for index, label in enumerate(labels):
        if index >= len(values):
            continue

        value = number(values[index])

        if value is None:
            continue

        key = norm(label)

        if key in ("pts", "points"):
            output["points"] = value
        elif key in ("reb", "rebounds"):
            output["rebounds"] = value
        elif key in ("ast", "assists"):
            output["assists"] = value
        elif key in ("stl", "steals"):
            output["steals"] = value
        elif key in ("blk", "blocks"):
            output["blocks"] = value
        elif key in ("3pm", "3pt", "3ptm", "three pointers made"):
            output["threes"] = value
        elif key in ("min", "minutes"):
            output["minutes"] = value
        elif key in ("to", "turnovers"):
            output["turnovers"] = value

    return output


def nba_extract_boxscore(summary):
    if not summary:
        return []

    boxscore = summary.get("boxscore", {})
    players_blocks = boxscore.get("players", [])
    output = []

    for team_block in players_blocks:
        team = team_block.get("team", {})
        team_abbr = team.get("abbreviation")

        for stat_group in team_block.get("statistics", []):
            labels = stat_group.get("labels", [])

            for athlete in stat_group.get("athletes", []):
                player = athlete.get("athlete", {})
                stats = nba_normalize_stat_labels(
                    labels,
                    athlete.get("stats", [])
                )

                output.append({
                    "id": str(player.get("id")),
                    "name": player.get("displayName", "Joueur inconnu"),
                    "team": team_abbr,
                    "stats": stats,
                    "did_not_play": athlete.get("didNotPlay", False)
                })

    return output


async def nba_player_game_logs(player_id, limit=20):
    current_year = datetime.now(TZ).year
    seasons = [current_year, current_year - 1]
    games = []

    for season in seasons:
        data = await nba_get(
            f"athletes/{player_id}/gamelog",
            {"season": season}
        )

        if not data:
            continue

        events = data.get("events", [])

        for event in events:
            event_id = event.get("id")

            if not event_id:
                continue

            summary = await nba_event_summary(event_id)

            if not summary:
                continue

            players = nba_extract_boxscore(summary)

            player = next(
                (
                    item for item in players
                    if str(item.get("id")) == str(player_id)
                ),
                None
            )

            if not player or player.get("did_not_play"):
                continue

            games.append({
                "id": event_id,
                "date": event.get("date"),
                "name": event.get("name"),
                "team": player.get("team"),
                "stats": player.get("stats", {})
            })

        if len(games) >= limit:
            break

    games.sort(
        key=lambda item: item.get("date") or "",
        reverse=True
    )

    return games[:limit]


def nba_market_value(game, market):
    stats = game.get("stats", {})

    if market == "pra":
        keys = ("points", "rebounds", "assists")
    elif market == "pr":
        keys = ("points", "rebounds")
    elif market == "pa":
        keys = ("points", "assists")
    elif market == "ra":
        keys = ("rebounds", "assists")
    else:
        return number(stats.get(market))

    values = [number(stats.get(key)) for key in keys]

    if any(value is None for value in values):
        return None

    return sum(values)


async def nba_player_analysis(away, home, market):
    roster = await nba_team_roster(home)
    players = [
        player for player in roster
        if player.get("position") != "C"
    ]

    reports = await asyncio.gather(
        *[
            nba_player_game_logs(player["id"], 20)
            for player in players[:18]
        ],
        return_exceptions=True
    )

    ranked = []

    for index, games in enumerate(reports):
        if isinstance(games, Exception) or not games:
            continue

        values = [
            nba_market_value(game, market)
            for game in games
        ]
        values = [
            value for value in values
            if value is not None
        ]

        if not values:
            continue

        recent5 = values[:5]
        recent10 = values[:10]
        recent20 = values[:20]

        ranked.append({
            "player": players[index],
            "games": games,
            "values": values,
            "average5": sum(recent5) / len(recent5),
            "average10": sum(recent10) / len(recent10),
            "average20": sum(recent20) / len(recent20),
            "hit5": sum(value >= 1 for value in recent5) / len(recent5),
            "hit10": sum(value >= 1 for value in recent10) / len(recent10),
            "hit20": sum(value >= 1 for value in recent20) / len(recent20)
        })

    ranked.sort(
        key=lambda item: (
            item["hit5"],
            item["average5"],
            item["hit10"]
        ),
        reverse=True
    )

    if not ranked:
        return {
            "kind": "text",
            "text": "Aucune statistique joueur exploitable n'a été récupérée."
        }

    label = NBA_STAT_LABELS.get(market, market)

    lines = [
        f"<b>🏀 ANALYSE JOUEURS — {esc(label.upper())}</b>",
        f"{esc(NBA_TEAM_NAMES.get(home, home))} vs "
        f"{esc(NBA_TEAM_NAMES.get(away, away))}",
        "",
        "<i>Classement basé sur les matchs récupérés. "
        "Aucune statistique manquante n'est estimée.</i>",
        ""
    ]

    for index, item in enumerate(ranked[:10], 1):
        player = item["player"]

        lines.append(
            f"<b>{index}. {esc(player['name'])}</b>\n"
            f"5 derniers : moyenne {fmt(item['average5'])} | "
            f"fréquence ≥1 : {fmt_percent(item['hit5'])}\n"
            f"10 derniers : moyenne {fmt(item['average10'])} | "
            f"fréquence ≥1 : {fmt_percent(item['hit10'])}\n"
            f"20 derniers : moyenne {fmt(item['average20'])} | "
            f"fréquence ≥1 : {fmt_percent(item['hit20'])}"
        )

    return {
        "kind": "text",
        "text": "\n\n".join(lines)
    }


async def nba_team_schedule(team_abbr):
    data = await nba_get("scoreboard")

    if not data:
        return []

    events = data.get("events", [])
    output = []

    for event in events:
        competition = (event.get("competitions") or [{}])[0]
        competitors = competition.get("competitors", [])

        abbreviations = [
            (item.get("team") or {}).get("abbreviation")
            for item in competitors
        ]

        if team_abbr in abbreviations:
            output.append(event)

    return output


async def nba_team_form(team_abbr):
    events = await nba_team_schedule(team_abbr)
    output = []

    for event in events:
        event_id = event.get("id")

        if not event_id:
            continue

        summary = await nba_event_summary(event_id)

        if not summary:
            continue

        header = summary.get("header", {})
        competitions = header.get("competitions", [])
        competitors = (
            competitions[0].get("competitors", [])
            if competitions else []
        )

        team_data = next(
            (
                item for item in competitors
                if (item.get("team") or {}).get("abbreviation") == team_abbr
            ),
            None
        )

        if not team_data:
            continue

        output.append({
            "date": event.get("date"),
            "score": number(team_data.get("score")),
            "home_away": team_data.get("homeAway"),
            "won": team_data.get("winner")
        })

    output.sort(
        key=lambda item: item.get("date") or "",
        reverse=True
    )

    return output[:10]


async def nba_team_analysis(away, home):
    away_form, home_form = await asyncio.gather(
        nba_team_form(away),
        nba_team_form(home)
    )

    if not away_form and not home_form:
        return {
            "kind": "text",
            "text": "Historique récent des équipes indisponible."
        }

    def team_text(team, games):
        if not games:
            return f"<b>{esc(NBA_TEAM_NAMES.get(team, team))}</b>\nDonnées indisponibles."

        scores = [
            item["score"]
            for item in games
            if item["score"] is not None
        ]

        wins = sum(bool(item["won"]) for item in games)
        average = sum(scores) / len(scores) if scores else None

        return (
            f"<b>{esc(NBA_TEAM_NAMES.get(team, team))}</b>\n"
            f"Matchs récupérés : {len(games)}\n"
            f"Victoires : {wins}\n"
            f"Moyenne de points : {fmt(average)}"
        )

    return {
        "kind": "text",
        "text": (
            "<b>📊 ANALYSE DES ÉQUIPES — NBA</b>\n\n"
            + team_text(away, away_form)
            + "\n\n"
            + team_text(home, home_form)
        )
    }


async def nba_simulation(away, home, simulations=10000):
    away_form, home_form = await asyncio.gather(
        nba_team_form(away),
        nba_team_form(home)
    )

    away_scores = [
        game["score"]
        for game in away_form
        if game["score"] is not None
    ]

    home_scores = [
        game["score"]
        for game in home_form
        if game["score"] is not None
    ]

    if len(away_scores) < 5 or len(home_scores) < 5:
        return {
            "kind": "text",
            "text": (
                "<b>🧬 SIMULATION NBA</b>\n\n"
                "Historique insuffisant : au moins 5 résultats "
                "d'équipe sont nécessaires de chaque côté."
            )
        }

    away_average = sum(away_scores) / len(away_scores)
    home_average = sum(home_scores) / len(home_scores)

    away_wins = 0
    home_wins = 0
    away_simulated = []
    home_simulated = []

    for _ in range(simulations):
        away_score = max(
            50,
            random.gauss(away_average, 12)
        )
        home_score = max(
            50,
            random.gauss(home_average, 12)
        )

        away_simulated.append(away_score)
        home_simulated.append(home_score)

        if away_score > home_score:
            away_wins += 1
        else:
            home_wins += 1

    text = (
        "<b>🧬 SIMULATION NBA — 10 000 TIRAGES</b>\n\n"
        f"<b>{esc(NBA_TEAM_NAMES.get(away, away))} @ "
        f"{esc(NBA_TEAM_NAMES.get(home, home))}</b>\n\n"
        f"Victoire extérieur : {fmt_percent(away_wins / simulations)}\n"
        f"Victoire domicile : {fmt_percent(home_wins / simulations)}\n"
        f"Score moyen extérieur : "
        f"{fmt(sum(away_simulated) / simulations, 1)}\n"
        f"Score moyen domicile : "
        f"{fmt(sum(home_simulated) / simulations, 1)}\n"
        f"Total moyen : "
        f"{fmt((sum(away_simulated) + sum(home_simulated)) / simulations, 1)}"
    )

    return {
        "kind": "text",
        "text": text
    }


async def prepare_nba_category(away, home, market):
    if market in NBA_STAT_LABELS:
        return await nba_player_analysis(away, home, market)

    if market == "teams":
        return await nba_team_analysis(away, home)

    if market == "sim":
        return await nba_simulation(away, home)

    return {
        "kind": "text",
        "text": "Catégorie NBA inconnue."
    }


# =====================================================
# CALLBACKS TELEGRAM
# =====================================================

async def on_button(update, ctx):
    query = update.callback_query
    await query.answer()

    if not user_can_access(update, ctx):
        await query.edit_message_text(
            "🔒 BLACK ICE est actuellement fermé."
        )
        return

    data = query.data or ""
    parts = data.split(":")
    action = parts[0]

    try:
        if action == "ms":
            if len(parts) > 1 and parts[1] == "home":
                await query.edit_message_text(
                    "🧊 <b>ICE VENOM</b>\n\nChoisis ton moteur :",
                    parse_mode=HTML,
                    reply_markup=multisport_keyboard()
                )
                return

            if len(parts) > 1 and parts[1] == "nhl":
                await query.edit_message_text(
                    "🏒 <b>NHL — moteur d'analyse original</b>\n\n"
                    "Choisis les matchs NHL.",
                    parse_mode=HTML
                )
                await send_games(query.message, 0)
                return

        if action == "nba":
            if len(parts) > 1 and parts[1] == "days":
                await show_nba_days(query)
                return

            if len(parts) > 1 and parts[1] == "game":
                away, home = parts[2], parts[3]

                if (
                    away not in NBA_TEAM_NAMES
                    or home not in NBA_TEAM_NAMES
                ):
                    await query.answer(
                        "Équipe inconnue.",
                        show_alert=True
                    )
                    return

                await query.edit_message_text(
                    nba_category_text(away, home),
                    parse_mode=HTML,
                    reply_markup=nba_category_keyboard(away, home)
                )
                return

            if len(parts) > 1 and parts[1] == "market":
                away, home, market = parts[2], parts[3], parts[4]

                if (
                    away not in NBA_TEAM_NAMES
                    or home not in NBA_TEAM_NAMES
                    or (
                        market not in NBA_STAT_LABELS
                        and market not in ("teams", "sim")
                    )
                ):
                    return

                await query.edit_message_text(
                    "⏳ Analyse NBA en cours…"
                )

                try:
                    state = await prepare_nba_category(
                        away,
                        home,
                        market
                    )
                except Exception as error:
                    log.exception(
                        "Analyse NBA %s : %s",
                        market,
                        error
                    )
                    state = {
                        "kind": "text",
                        "text": (
                            "Données NBA indisponibles pour cette catégorie. "
                            "La source gratuite n'a pas fourni de statistiques exploitables."
                        )
                    }

                rows = [
                    [
                        InlineKeyboardButton(
                            "⬅️ Catégories NBA",
                            callback_data=f"nba:game:{away}:{home}"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "📅 Matchs NBA",
                            callback_data="nba:days"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⬅️ Retour NHL / NBA",
                            callback_data="ms:home"
                        )
                    ]
                ]

                await query.edit_message_text(
                    state["text"],
                    parse_mode=HTML,
                    reply_markup=InlineKeyboardMarkup(rows)
                )
                return

        if action == "d":
            offset = int(parts[1]) if len(parts) > 1 else 0
            await query.edit_message_text(
                "⏳ Chargement des matchs NHL..."
            )
            games = await games_for(offset)

            if not games:
                await query.edit_message_text(
                    "Aucun match NHL pour cette date.",
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "⬅️ Retour NHL / NBA",
                                callback_data="ms:home"
                            )
                        ]
                    ])
                )
                return

            date = datetime.now(TZ).date() + timedelta(days=offset)
            lines = [
                f"<b>🏒 NHL — {date.strftime('%d/%m/%Y')}</b>",
                ""
            ]
            rows = []

            for away, home, game_id, match_date in games:
                lines.append(
                    f"• {esc(NAMES[away])} @ {esc(NAMES[home])}"
                )
                rows.append([
                    InlineKeyboardButton(
                        f"{NAMES[away]} @ {NAMES[home]}",
                        callback_data=f"g:{away}:{home}:{match_date or ''}"
                    )
                ])

            rows.append([
                InlineKeyboardButton(
                    "➡️ Jour suivant",
                    callback_data=f"d:{offset + 1}"
                )
            ])
            rows.append([
                InlineKeyboardButton(
                    "⬅️ Retour NHL / NBA",
                    callback_data="ms:home"
                )
            ])

            await query.edit_message_text(
                "\n".join(lines),
                parse_mode=HTML,
                reply_markup=InlineKeyboardMarkup(rows)
            )
            return

        if action == "g":
            away, home = parts[1], parts[2]
            match_date = parts[3] if len(parts) > 3 else None

            if away not in NAMES or home not in NAMES:
                return

            await query.edit_message_text(
                category_text(away, home),
                parse_mode=HTML,
                reply_markup=category_keyboard(
                    away,
                    home,
                    match_date
                )
            )
            return

        if action == "a":
            away, home, category = parts[1], parts[2], parts[3]
            match_date = parts[4] if len(parts) > 4 else None

            if (
                away not in NAMES
                or home not in NAMES
                or category not in {key for _, key in CATEGORIES}
            ):
                return

            await query.edit_message_text(
                f"⏳ Analyse {NAMES[away]} @ {NAMES[home]}...\n"
                f"Catégorie : {category}"
            )

            try:
                state = await prepare_category(
                    away,
                    home,
                    category,
                    match_date
                )
            except Exception as error:
                log.exception(
                    "Analyse %s : %s",
                    category,
                    error
                )
                state = {
                    "kind": "text",
                    "text": (
                        "Les données de cette catégorie sont "
                        "indisponibles pour le moment. Réessaie."
                    )
                }

            state.update({
                "away": away,
                "home": home,
                "category": category,
                "match_date": match_date
            })
            ctx.user_data["analysis_state"] = state

            if state["kind"] == "players":
                await query.edit_message_text(
                    state["text"],
                    parse_mode=HTML,
                    reply_markup=player_top_keyboard(state["ranked"])
                )
            else:
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "⬅️ Retour aux catégories",
                            callback_data="backcat"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "📅 Autres matchs",
                            callback_data="d:0"
                        )
                    ]
                ])

                text_chunks = split_telegram_text(state["text"])

                await query.edit_message_text(
                    text_chunks[0],
                    parse_mode=HTML,
                    reply_markup=keyboard if len(text_chunks) == 1 else None
                )

                for index, chunk in enumerate(text_chunks[1:], 1):
                    await query.message.reply_text(
                        chunk,
                        parse_mode=HTML,
                        reply_markup=(
                            keyboard
                            if index == len(text_chunks) - 1
                            else None
                        )
                    )

            return

        if action == "p":
            await show_player_profile(
                query,
                ctx,
                int(parts[1])
            )
            return

        if action == "backtop":
            state = ctx.user_data.get("analysis_state")

            if not state or state.get("kind") != "players":
                await query.answer(
                    "Analyse expirée.",
                    show_alert=True
                )
                return

            await query.edit_message_text(
                state["text"],
                parse_mode=HTML,
                reply_markup=player_top_keyboard(state["ranked"])
            )
            return

        if action == "backcat":
            state = ctx.user_data.get("analysis_state")

            if not state:
                await query.answer(
                    "Sélectionne à nouveau un match.",
                    show_alert=True
                )
                return

            await query.edit_message_text(
                category_text(
                    state["away"],
                    state["home"]
                ),
                parse_mode=HTML,
                reply_markup=category_keyboard(
                    state["away"],
                    state["home"],
                    state.get("match_date")
                )
            )
            return

    except Exception as error:
        log.exception(
            "Erreur bouton Telegram : %s",
            error
        )


# =====================================================
# START / TEXTE / COMMANDES
# =====================================================

async def start(update, ctx):
    if not user_can_access(update, ctx):
        await update.message.reply_text(
            "🔒 BLACK ICE est actuellement fermé."
        )
        return

    keyboard = ReplyKeyboardMarkup(
        [
            ["🏒 NHL", "🏀 NBA"]
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        is_persistent=True,
        input_field_placeholder="Choisis NHL ou NBA"
    )

    await update.message.reply_text(
        "🧊 <b>ICE VENOM</b>\n\n"
        "Choisis ton moteur :\n"
        "🏒 NHL — moteur original\n"
        "🏀 NBA — moteur d'analyse",
        parse_mode=HTML,
        reply_markup=keyboard
    )


async def on_text(update, ctx):
    message = update.message
    text = (message.text or "").strip()

    if ctx.user_data.get("await_admin_password"):
        ctx.user_data.pop("await_admin_password", None)

        try:
            await message.delete()
        except Exception:
            pass

        if (
            ADMIN_PASSWORD
            and secrets.compare_digest(text, ADMIN_PASSWORD)
        ):
            ctx.user_data["admin_expiry"] = (
                time.time() + ADMIN_SESSION_SECONDS
            )

            await message.reply_text(
                "✅ <b>Connexion administrateur réussie.</b>\n\n"
                "/cotes — saisir les cotes\n"
                "/fermer — fermer le bot\n"
                "/ouvrir — ouvrir le bot\n"
                "/statut — état du bot\n"
                "/annuler — annuler une saisie\n"
                "/logout — fermer la session",
                parse_mode=HTML
            )
        else:
            await message.reply_text(
                "❌ Mot de passe incorrect."
            )

        return

    flow = ctx.user_data.get("admin_flow")

    if flow and is_admin(update, ctx):
        step = flow.get("step")

        if step == "player":
            if len(text) < 2 or len(text) > 80:
                await message.reply_text(
                    "Nom de joueur invalide. Réessaie."
                )
                return

            flow["player"] = text
            flow["step"] = "market"

            await message.reply_text(
                f"Joueur : <b>{esc(text)}</b>\n\nChoisis le marché :",
                parse_mode=HTML,
                reply_markup=odds_market_keyboard()
            )
            return

        if step == "odd":
            try:
                odd = float(text.replace(",", "."))
            except ValueError:
                await message.reply_text(
                    "Valeur invalide."
                )
                return

            if not 1.01 <= odd <= 1000:
                await message.reply_text(
                    "Valeur hors limites."
                )
                return

            save_odd(
                flow["match_date"],
                flow["away"],
                flow["home"],
                flow["player"],
                flow["market"],
                odd
            )

            await message.reply_text(
                "✅ Valeur enregistrée."
            )
            ctx.user_data.pop("admin_flow", None)
            return

    if not user_can_access(update, ctx):
        await message.reply_text(
            "🔒 BLACK ICE est actuellement fermé."
        )
        return

    if norm(text) in ("nhl", "🏒 nhl"):
        await message.reply_text(
            "🏒 <b>NHL — moteur original</b>\n\n"
            "Voici les matchs à analyser.",
            parse_mode=HTML
        )
        await send_games(message, 0)
        return

    if norm(text) in ("nba", "🏀 nba"):
        await message.reply_text(
            "🏀 <b>NBA — moteur d'analyse</b>\n\n"
            "Choisis une action :",
            parse_mode=HTML,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "📅 Matchs NBA",
                        callback_data="nba:days"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Retour au choix NHL / NBA",
                        callback_data="ms:home"
                    )
                ]
            ])
        )
        return

    if is_multisport_request(text):
        await send_multisport_menu(message)
        return

    if (
        text == MENU_BTN
        or norm(text) in (
            "match",
            "matchs",
            "menu",
            "ce soir",
            "aujourdhui"
        )
    ):
        await send_games(message, 0)
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
            date = await find_upcoming_date(away, home)
            await send_category_menu(
                message,
                away,
                home,
                date
            )
            return

        bad = parts[0] if not away else parts[1]

        await message.reply_text(
            f"Je ne reconnais pas « {bad} ». "
            "Essaie le nom de la ville, de l'équipe ou son abréviation."
        )
        return

    if len(parts) == 1:
        team = find_team(parts[0])

        if team:
            for offset in range(2):
                try:
                    games = await games_for(offset)
                except Exception:
                    continue

                for away, home, _, date in games:
                    if team in (away, home):
                        await send_category_menu(
                            message,
                            away,
                            home,
                            date
                        )
                        return

            await message.reply_text(
                f"Aucun match trouvé pour {NAMES[team]} aujourd’hui ou demain."
            )
            return

    await message.reply_text(
        "Écris deux équipes, par exemple « Washington vs Montréal », "
        "ou touche « Matchs du jour »."
    )


# =====================================================
# SHUTDOWN / MAIN
# =====================================================

async def shutdown(app):
    global _client

    if _client:
        await _client.aclose()
        _client = None


def main():
    init_db()

    app = (
        Application.builder()
        .token(TOKEN)
        .post_shutdown(shutdown)
        .build()
    )

    app.add_handler(
        CommandHandler(["start", "help"], start)
    )
    app.add_handler(
        CommandHandler("admin", cmd_admin)
    )
    app.add_handler(
        CommandHandler("logout", cmd_logout)
    )
    app.add_handler(
        CommandHandler("cotes", cmd_cotes)
    )
    app.add_handler(
        CommandHandler("fermer", cmd_fermer)
    )
    app.add_handler(
        CommandHandler("ouvrir", cmd_ouvrir)
    )
    app.add_handler(
        CommandHandler("statut", cmd_statut)
    )
    app.add_handler(
        CommandHandler("annuler", cmd_annuler)
    )

    app.add_handler(
        CallbackQueryHandler(on_button)
    )
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            on_text
        )
    )

    log.info(
        "ICE VENOM démarré — NHL + NBA — "
        "TOP 5 — H2H — forme 5/10/20 — simulation"
    )

    app.run_polling()


if __name__ == "__main__":
    main()