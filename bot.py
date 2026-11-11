import asyncio
import difflib
import logging
import os
import re
import secrets
import sqlite3
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
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

# =====================================================
# BLACK ICE — CONFIGURATION
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
MENU_BTN = "🏒 OUVRIR BLACK ICE"
HTML = ParseMode.HTML
ADMIN_SESSION_SECONDS = 12 * 3600

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("blackice")

_client = None
_cache = {}


# =====================================================
# IDENTITÉ VISUELLE
# =====================================================

def header(title):
    return (
        "❄️ <b>BLACK ICE</b>\n"
        "<i>━━━━━━━━━━━━━━━━━━━━</i>\n"
        f"<b>{title}</b>\n"
    )


def separator():
    return "━━━━━━━━━━━━━━━━━━━━"


def back_keyboard(callback="backhome"):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ RETOUR", callback_data=callback)],
        [InlineKeyboardButton("🏒 AUTRES MATCHS", callback_data="d:0")],
    ])


def home_keyboard():
    return ReplyKeyboardMarkup(
        [[MENU_BTN]],
        resize_keyboard=True,
        is_persistent=True,
    )


# =====================================================
# OUTILS
# =====================================================

def norm(value):
    value = unicodedata.normalize("NFKD", str(value))
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.lower()
    value = re.sub(r"[.'’]", "", value)
    return re.sub(r"\s+", " ", value).strip()


def number(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
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
    return f"{seconds // 60}:{seconds % 60:02d}"


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

NAMES = {}
ALIASES = {}

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
        value, list(ALIASES), n=1, cutoff=0.75
    )
    return ALIASES[close[0]] if close else None


SPLIT = re.compile(
    r"\s+(?:vs\.?|versus|contre|at|c\.)\s+|\s*@\s*|\s+-\s+",
    re.I,
)


# =====================================================
# BASE DE DONNÉES
# =====================================================

def init_db():
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)

    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS odds (
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
            )
        """)
        db.execute("""
            INSERT OR IGNORE INTO settings(key, value)
            VALUES('bot_open', '1')
        """)
        db.commit()

    log.info("SQLite initialisée : %s", DB_PATH)


def db_setting(key, default=None):
    with sqlite3.connect(DB_PATH) as db:
        row = db.execute(
            "SELECT value FROM settings WHERE key=?",
            (key,),
        ).fetchone()
    return row[0] if row else default


def set_db_setting(key, value):
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            INSERT INTO settings(key, value)
            VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, str(value)))
        db.commit()


def bot_is_open():
    return db_setting("bot_open", "1") == "1"


def save_odd(match_date, away, home, player, market, odd):
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            INSERT INTO odds(
                match_date, away, home, player, player_norm,
                market, odd, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(match_date, away, home, player_norm, market)
            DO UPDATE SET
                player=excluded.player,
                odd=excluded.odd,
                updated_at=excluded.updated_at
        """, (
            match_date, away, home, player, norm(player),
            market, odd, datetime.now(TZ).isoformat(),
        ))
        db.commit()


def get_odd(match_date, away, home, player, market):
    if not match_date:
        return None
    with sqlite3.connect(DB_PATH) as db:
        row = db.execute("""
            SELECT odd FROM odds
            WHERE match_date=? AND away=? AND home=?
              AND player_norm=? AND market=?
        """, (
            match_date, away, home, norm(player), market,
        )).fetchone()
    return row[0] if row else None


# =====================================================
# ADMINISTRATION
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
        await update.message.reply_text(
            "⛔ Connecte-toi d'abord avec /admin."
        )
        return False
    return True


async def cmd_admin(update, ctx):
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "Connexion administrateur uniquement en privé."
        )
        return
    if not ADMIN_PASSWORD:
        await update.message.reply_text(
            "⚠️ ADMIN_PASSWORD n'est pas configuré dans Railway."
        )
        return
    ctx.user_data["await_admin_password"] = True
    await update.message.reply_text(
        header("PANNEAU ADMINISTRATEUR")
        + "\nEnvoie ton mot de passe en privé.",
        parse_mode=HTML,
    )


async def cmd_logout(update, ctx):
    ctx.user_data.pop("admin_expiry", None)
    ctx.user_data.pop("await_admin_password", None)
    ctx.user_data.pop("admin_flow", None)
    await update.message.reply_text("🔒 Session administrateur fermée.")


async def cmd_fermer(update, ctx):
    if not await admin_required(update, ctx):
        return
    set_db_setting("bot_open", "0")
    await update.message.reply_text(
        header("ACCÈS FERMÉ")
        + "\n🔒 Les utilisateurs ne peuvent plus lancer d'analyses.",
        parse_mode=HTML,
    )


async def cmd_ouvrir(update, ctx):
    if not await admin_required(update, ctx):
        return
    set_db_setting("bot_open", "1")
    await update.message.reply_text(
        header("ACCÈS OUVERT")
        + "\n🟢 BLACK ICE est de nouveau accessible.",
        parse_mode=HTML,
    )


async def cmd_statut(update, ctx):
    if not await admin_required(update, ctx):
        return
    status = "🟢 OUVERT" if bot_is_open() else "🔴 FERMÉ"
    await update.message.reply_text(
        header("STATUT DU BOT")
        + f"\nAccès utilisateurs : {status}\n"
        "Base : SQLite\n"
        "H2H : domicile et extérieur\n"
        "Forme récente : 5 et 10 derniers matchs\n"
        "VALUE et fiabilité : activées",
        parse_mode=HTML,
    )


async def cmd_annuler(update, ctx):
    if not await admin_required(update, ctx):
        return
    ctx.user_data.pop("admin_flow", None)
    await update.message.reply_text("❌ Saisie annulée.")


# =====================================================
# SAISIE DES COTES
# =====================================================

ODD_MARKETS = {
    "goals": "🥅 +0,5 but",
    "assists": "🎯 +0,5 passe décisive",
    "points": "⭐ +0,5 point",
}


def odds_market_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🥅 +0,5 BUT", callback_data="om:goals")],
        [InlineKeyboardButton("🎯 +0,5 PASSE", callback_data="om:assists")],
        [InlineKeyboardButton("⭐ +0,5 POINT", callback_data="om:points")],
        [InlineKeyboardButton("❌ ANNULER", callback_data="om:cancel")],
    ])


async def cmd_cotes(update, ctx):
    if not await admin_required(update, ctx):
        return

    ctx.user_data["admin_flow"] = {"step": "match"}

    await update.message.reply_text(
        header("SAISIE DES COTES")
        + "\nSélectionne le match concerné.",
        parse_mode=HTML,
    )

    for offset in range(7):
        try:
            games = await games_for(offset)
        except Exception as error:
            log.warning("Calendrier cotes : %s", error)
            continue

        if not games:
            continue

        buttons = [
            [InlineKeyboardButton(
                f"{away} @ {home} · {hour}",
                callback_data=f"oa:{away}:{home}:{match_date}",
            )]
            for away, home, hour, match_date in games
        ]

        date_label = (
            datetime.now(TZ) + timedelta(days=offset)
        ).strftime("%d/%m/%Y")

        await update.message.reply_text(
            f"📅 <b>{date_label}</b>",
            parse_mode=HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )


async def handle_admin_callback(query, ctx, parts):
    if not admin_session_valid(ctx):
        await query.answer(
            "Session admin expirée.",
            show_alert=True,
        )
        return True

    if parts[0] == "oa":
        if len(parts) != 4:
            await query.answer("Sélection invalide.")
            return True

        away, home, match_date = parts[1:4]
        if away not in NAMES or home not in NAMES:
            await query.answer("Équipes invalides.")
            return True

        ctx.user_data["admin_flow"] = {
            "step": "player",
            "away": away,
            "home": home,
            "match_date": match_date,
        }

        await query.answer()
        await query.message.reply_text(
            f"Match : <b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>\n"
            f"Date : {format_date(match_date)}\n\n"
            "Envoie le nom exact du joueur.",
            parse_mode=HTML,
        )
        return True

    if parts[0] == "om":
        market = parts[1] if len(parts) == 2 else ""

        if market == "cancel":
            ctx.user_data.pop("admin_flow", None)
            await query.answer("Saisie annulée.")
            await query.message.reply_text("❌ Saisie annulée.")
            return True

        if market not in ODD_MARKETS:
            await query.answer("Marché invalide.")
            return True

        flow = ctx.user_data.get("admin_flow", {})
        if flow.get("step") != "market":
            await query.answer("Aucune saisie en cours.")
            return True

        flow["market"] = market
        flow["step"] = "odd"

        await query.answer()
        await query.message.reply_text(
            f"Marché : {ODD_MARKETS[market]}\n\n"
            "Envoie la cote décimale. Exemple : <code>2.05</code>",
            parse_mode=HTML,
        )
        return True

    return False


# =====================================================
# API NHL ET CACHE
# =====================================================

def client():
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": "BLACK-ICE-NHL-BOT/9.0"},
        )
    return _client


async def get_json(url, params=None, tries=3):
    last_error = None

    for attempt in range(tries):
        try:
            response = await client().get(url, params=params)
            response.raise_for_status()
            return response.json()
        except Exception as error:
            last_error = error
            await asyncio.sleep(0.7 * (attempt + 1))

    raise last_error


async def cached(key, ttl, factory):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]

    value = await factory()
    _cache[key] = (time.time(), value)
    return value


def seasons():
    now = datetime.now(TZ)
    year = now.year if now.month >= 9 else now.year - 1
    return [
        f"{year - 2}{year - 1}",
        f"{year - 1}{year}",
        f"{year}{year + 1}",
    ]


def current_season():
    now = datetime.now(TZ)
    year = now.year if now.month >= 9 else now.year - 1
    return f"{year}{year + 1}"


# =====================================================
# EFFECTIFS ACTUELS
# =====================================================

async def roster(team, position="skater"):
    sid = current_season()

    async def build():
        data = await get_json(f"{API_WEB}/roster/{team}/current")

        entries = (
            data.get("goalies", [])
            if position == "goalie"
            else (data.get("forwards") or [])
            + (data.get("defensemen") or [])
        )

        players = []

        for player in entries:
            pid = player.get("id") or player.get("playerId")
            if not pid:
                continue

            players.append({
                "id": pid,
                "name": player_name(player),
                "team": team,
                "position": player.get("positionCode", ""),
            })

        return players

    return await cached(
        ("roster", team, position, sid),
        1800,
        build,
    )


# =====================================================
# JOURNAUX INDIVIDUELS
# =====================================================

def normalize_game(game):
    home_flag = game.get("homeRoadFlag")
    opponent = game.get("opponentAbbrev")
    team = game.get("teamAbbrev")

    if isinstance(opponent, dict):
        opponent = opponent.get("default")
    if isinstance(team, dict):
        team = team.get("default")

    return {
        "date": (game.get("gameDate") or "")[:10],
        "opp": opponent,
        "team_abbrev": team,
        "home": (
            True if home_flag == "H"
            else False if home_flag == "R"
            else None
        ),
        "game_id": str(game.get("gameId") or game.get("id") or ""),
        "goals": number(game.get("goals")),
        "assists": number(game.get("assists")),
        "points": number(game.get("points")),
        "shots": number(game.get("shots")),
        "hits": number(game.get("hits")),
        "blocks": number(game.get("blockedShots")),
        "pim": number(game.get("pim")),
        "toi": parse_toi(game.get("toi") or game.get("timeOnIce")),
        "powerplay": number(game.get("powerPlayPoints")),
        "powerplay_goals": number(game.get("powerPlayGoals")),
        "powerplay_assists": number(game.get("powerPlayAssists")),
        "faceoff_pct": number(game.get("faceoffWinningPctg")),
        "saves": number(game.get("saves")),
        "shots_against": number(game.get("shotsAgainst")),
        "save_pct": number(game.get("savePctg")),
        "goals_against": number(game.get("goalsAgainst")),
        "decision": game.get("decision"),
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

            rows.extend(
                normalize_game(game)
                for game in result.get("gameLog", [])
            )

        if not rows and errors:
            raise RuntimeError("Journaux joueurs indisponibles.")

        unique = {}

        for row in rows:
            key = row["game_id"] or (
                row["date"], row["opp"], row["home"]
            )
            unique[key] = row

        return list(unique.values())

    return await cached(
        ("log", pid, tuple(seasons())),
        3600,
        build,
    )


# =====================================================
# H2H DOMICILE + EXTÉRIEUR
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

    results = await asyncio.gather(
        *[
            fetch_schedule(team, season)
            for season in seasons()
            for team in (away, home)
        ],
        return_exceptions=True,
    )

    if all(isinstance(result, Exception) for result in results):
        raise RuntimeError("Aucun calendrier NHL disponible.")

    found = {}
    expected = {away, home}

    for data in results:
        if isinstance(data, Exception):
            log.warning("Calendrier H2H indisponible : %s", data)
            continue

        for game in data.get("games", []):
            away_team = game.get("awayTeam") or {}
            home_team = game.get("homeTeam") or {}

            actual_away = away_team.get("abbrev")
            actual_home = home_team.get("abbrev")

            if {actual_away, actual_home} != expected:
                continue

            try:
                if int(game.get("gameType", 0)) != 2:
                    continue
            except (TypeError, ValueError):
                continue

            if game.get("gameState") not in ("FINAL", "OFF"):
                continue

            game_id = game.get("id")
            away_score = away_team.get("score")
            home_score = home_team.get("score")

            if game_id is None or away_score is None or home_score is None:
                continue

            found[str(game_id)] = {
                "id": str(game_id),
                "date": (game.get("gameDate") or "")[:10],
                "away": actual_away,
                "home": actual_home,
                "away_score": away_score,
                "home_score": home_score,
            }

    return sorted(
        found.values(),
        key=lambda game: (
            game.get("date") or "",
            game.get("id") or "",
        ),
        reverse=True,
    )


# =====================================================
# PLAY BY PLAY
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
            players.setdefault(pid, empty_pbp())
            return players[pid]

        for play in data.get("plays", []):
            event = (play.get("typeDescKey") or "").lower()
            details = play.get("details") or {}

            if event == "shot-on-goal":
                player = stats(details.get("shootingPlayerId"))
                if player:
                    player["shots"] += 1

            elif event == "missed-shot":
                player = stats(details.get("shootingPlayerId"))
                if player:
                    player["missed"] += 1

            elif event == "hit":
                player = stats(details.get("hittingPlayerId"))
                if player:
                    player["hits"] += 1

            elif event == "blocked-shot":
                player = stats(details.get("blockingPlayerId"))
                if player:
                    player["blocks"] += 1

            elif event == "faceoff":
                winner = stats(details.get("winningPlayerId"))
                loser = stats(details.get("losingPlayerId"))
                if winner:
                    winner["faceoff_wins"] += 1
                if loser:
                    loser["faceoff_losses"] += 1

            elif event == "penalty":
                pid = (
                    details.get("committedByPlayerId")
                    or details.get("servedByPlayerId")
                )
                player = stats(pid)
                if player:
                    player["pim"] += (
                        number(details.get("duration")) or 0
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
        output[game["id"]] = (
            None if isinstance(result, Exception) else result
        )

    return output


# =====================================================
# ASSOCIATION JOUEURS / H2H
# =====================================================

def match_player_games(player, scores, pbp_data):
    allowed_ids = {
        str(game["id"]): game
        for game in scores
    }

    allowed_dates = {}

    for game in scores:
        if game.get("date"):
            allowed_dates.setdefault(
                game["date"], []
            ).append(game)

    player["vs"] = []

    for game in player.get("log", []):
        game_id = game.get("game_id")
        matched = allowed_ids.get(str(game_id)) if game_id else None

        if game_id and not matched:
            continue

        if not game_id:
            for candidate in allowed_dates.get(game.get("date"), []):
                if (
                    player["team"] in (
                        candidate["away"], candidate["home"]
                    )
                    and player["opp"] in (
                        candidate["away"], candidate["home"]
                    )
                ):
                    matched = candidate
                    break

        if not matched:
            continue

        if {
            matched["away"], matched["home"]
        } != {player["team"], player["opp"]}:
            continue

        if game.get("opp") != player["opp"]:
            continue

        if (
            game.get("team_abbrev")
            and game["team_abbrev"] != player["team"]
        ):
            continue

        if game.get("home") is None:
            continue

        expected_home = player["team"] == matched["home"]

        if game["home"] != expected_home:
            continue

        row = dict(game)

        pbp_match = pbp_data.get(str(matched["id"]))

        row["pbp"] = (
            None
            if pbp_match is None
            else pbp_match.get(
                str(player["id"]),
                empty_pbp(),
            )
        )

        row["match_id"] = str(matched["id"])
        row["match_score"] = matched

        player["vs"].append(row)

    unique = {
        game["match_id"]: game
        for game in player["vs"]
    }

    player["vs"] = sorted(
        unique.values(),
        key=lambda game: game.get("date") or "",
        reverse=True,
    )


# =====================================================
# MARCHÉS
# =====================================================

CATEGORY_INFO = {
    "points": ("⭐ POINTS", "pts", 0),
    "goals": ("🥅 BUTS", "buts", 0),
    "assists": ("🎯 PASSES DÉCISIVES", "passes", 0),
    "shots": ("🏒 TIRS CADRÉS", "tirs", 0),
    "missed": ("🎯 TIRS MANQUÉS", "tirs", 0),
    "hits": ("💥 HITS", "hits", 0),
    "blocks": ("🛡️ TIRS BLOQUÉS", "blocs", 0),
    "faceoffs": ("🔵 MISES EN JEU", "%", 1),
    "toi": ("⏱️ TEMPS DE GLACE", "secondes", 0),
    "powerplay": ("⚡ POWER PLAY", "points PP", 0),
    "penalties": ("🚨 PÉNALITÉS", "minutes", 0),
}


CATEGORIES = [
    ("⭐ POINTS", "points"),
    ("🥅 BUTS", "goals"),
    ("🎯 PASSES DÉCISIVES", "assists"),
    ("🏒 TIRS CADRÉS", "shots"),
    ("🎯 TIRS MANQUÉS", "missed"),
    ("💥 HITS", "hits"),
    ("🛡️ TIRS BLOQUÉS", "blocks"),
    ("🔵 MISES EN JEU", "faceoffs"),
    ("⏱️ TEMPS DE GLACE", "toi"),
    ("⚡ POWER PLAY", "powerplay"),
    ("🚨 PÉNALITÉS", "penalties"),
]


def metric_value(game, category):
    pbp = game.get("pbp")

    if category == "points":
        return game.get("points")
    if category == "goals":
        return game.get("goals")
    if category == "assists":
        return game.get("assists")

    if category == "shots":
        return (
            game.get("shots")
            if game.get("shots") is not None
            else pbp.get("shots") if pbp is not None else None
        )

    if category == "missed":
        return pbp.get("missed") if pbp is not None else None

    if category == "hits":
        return (
            game.get("hits")
            if game.get("hits") is not None
            else pbp.get("hits") if pbp is not None else None
        )

    if category == "blocks":
        return (
            game.get("blocks")
            if game.get("blocks") is not None
            else pbp.get("blocks") if pbp is not None else None
        )

    if category == "penalties":
        return (
            game.get("pim")
            if game.get("pim") is not None
            else pbp.get("pim") if pbp is not None else None
        )

    if category == "toi":
        return game.get("toi")

    if category == "powerplay":
        value = game.get("powerplay")
        if value is not None:
            return value

        goals = game.get("powerplay_goals")
        assists = game.get("powerplay_assists")

        return (
            goals + assists
            if goals is not None and assists is not None
            else None
        )

    if category == "faceoffs":
        if pbp is not None:
            wins = pbp.get("faceoff_wins", 0)
            losses = pbp.get("faceoff_losses", 0)
            total = wins + losses
            if total:
                return wins / total

        value = game.get("faceoff_pct")

        if value is not None:
            return value / 100 if value > 1 else value

    return None


def calculate_stats(games, category):
    values = [
        metric_value(game, category)
        for game in games
    ]

    values = [
        value for value in values
        if value is not None
    ]

    if not values:
        return None

    threshold = 0.5 if category == "faceoffs" else 1
    successes = sum(
        value >= threshold
        for value in values
    )

    return {
        "n": len(values),
        "total": sum(values),
        "average": sum(values) / len(values),
        "successes": successes,
        "hit": successes / len(values),
    }


def calculate_player(player, category):
    return calculate_stats(
        player.get("vs", []),
        category,
    )


def recent_player_games(player, limit):
    games = [
        game for game in player.get("log", [])
        if game.get("date")
    ]

    games.sort(
        key=lambda game: game["date"],
        reverse=True,
    )

    return games[:limit]


def calculate_recent(player, category, limit):
    return calculate_stats(
        recent_player_games(player, limit),
        category,
    )


# =====================================================
# ESTIMATION, VALUE ET FIABILITÉ
# =====================================================

def smoothed_probability(successes, sample_size):
    if sample_size <= 0:
        return None
    return (successes + 1) / (sample_size + 2)


def betting_estimate(player, market):
    if market not in ("goals", "assists", "points"):
        return None

    recent = calculate_recent(player, market, 10)
    h2h = calculate_player(player, market)

    p_recent = (
        smoothed_probability(
            recent["successes"], recent["n"]
        )
        if recent else None
    )

    p_h2h = (
        smoothed_probability(
            h2h["successes"], h2h["n"]
        )
        if h2h else None
    )

    if p_recent is not None and p_h2h is not None:
        probability = 0.70 * p_recent + 0.30 * p_h2h
    elif p_recent is not None:
        probability = p_recent
    elif p_h2h is not None:
        probability = p_h2h
    else:
        probability = None

    return {
        "probability": probability,
        "recent": recent,
        "h2h": h2h,
        "p_recent": p_recent,
        "p_h2h": p_h2h,
    }


def confidence_score(estimate):
    if not estimate:
        return None

    recent = estimate.get("recent")
    h2h = estimate.get("h2h")

    n_recent = recent["n"] if recent else 0
    n_h2h = h2h["n"] if h2h else 0

    score = 25
    score += min(n_recent, 10) * 3
    score += min(n_h2h, 5) * 4

    if recent and h2h:
        difference = abs(
            estimate["p_recent"] - estimate["p_h2h"]
        )
        score += max(
            0,
            25 * (1 - difference),
        )

    return max(0, min(100, round(score)))


def value_analysis(player, market, odd):
    estimate = betting_estimate(player, market)

    if not estimate or estimate["probability"] is None:
        return None

    probability = estimate["probability"]
    implied = 1 / odd

    return {
        "probability": probability,
        "implied": implied,
        "edge": probability - implied,
        "roi": (probability * odd - 1) * 100,
        "confidence": confidence_score(estimate),
        "estimate": estimate,
    }


def format_value_lines(player, market, odd):
    analysis = value_analysis(
        player,
        market,
        odd,
    )

    if not analysis:
        return [
            "📊 VALUE : données insuffisantes",
            "🔒 Fiabilité : non calculable",
        ]

    edge = analysis["edge"]

    if edge >= 0.03:
        label = "🟢 VALUE POTENTIELLE"
    elif edge > 0:
        label = "🟡 VALUE FAIBLE"
    else:
        label = "🔴 PAS DE VALUE DÉTECTÉE"

    return [
        f"Probabilité estimée : {fmt_percent(analysis['probability'])}",
        f"Probabilité implicite : {fmt_percent(analysis['implied'])}",
        f"Écart estimé : {edge * 100:+.1f} points",
        f"ROI théorique estimé : {analysis['roi']:+.1f} %",
        label,
        f"🔒 Fiabilité des données : {analysis['confidence']}/100",
    ]


# =====================================================
# HISTORIQUE H2H
# =====================================================

def performance_text(value, category):
    if category == "toi":
        return fmt_minutes(value)

    if category == "faceoffs":
        return fmt_percent(value)

    labels = {
        "penalties": "min",
        "powerplay": "points PP",
        "goals": "but(s)",
        "assists": "passe(s)",
        "points": "point(s)",
        "shots": "tir(s) cadré(s)",
        "missed": "tir(s) manqué(s)",
        "hits": "hit(s)",
        "blocks": "bloc(s)",
    }

    return (
        f"{fmt(value, 0)} "
        f"{labels.get(category, '')}"
    ).strip()


def format_player_history(player, category):
    games = sorted(
        player.get("vs", []),
        key=lambda game: game.get("date") or "",
        reverse=True,
    )

    out = [
        "<b>📅 HISTORIQUE H2H</b>",
        separator(),
    ]

    values = []

    for game in games:
        value = metric_value(game, category)
        match = game.get("match_score") or {}

        if value is None or not match:
            continue

        values.append(value)

        date = format_date(game.get("date"))
        location = "DOM" if game.get("home") else "EXT"

        actual_home = NAMES.get(
            match.get("home"),
            match.get("home", "?"),
        )
        actual_away = NAMES.get(
            match.get("away"),
            match.get("away", "?"),
        )

        score = (
            f"{actual_home} {match.get('home_score', '?')}-"
            f"{match.get('away_score', '?')} {actual_away}"
        )

        success = value >= (
            0.5 if category == "faceoffs" else 1
        )

        icon = "✅" if success else "❌"

        out.append(
            f"{date} · {location} — "
            f"{performance_text(value, category)} {icon}"
        )
        out.append(f"<i>{esc(score)}</i>")

    if not values:
        out.append("Aucune statistique individuelle vérifiable.")
        return out

    threshold = 0.5 if category == "faceoffs" else 1
    successes = sum(
        value >= threshold
        for value in values
    )

    out.extend([
        "",
        f"<b>Bilan : {successes}/{len(values)} "
        f"({successes / len(values) * 100:.0f} %)</b>",
        f"Moyenne : {performance_text(sum(values) / len(values), category)}",
        f"Total : {performance_text(sum(values), category)}",
    ])

    return out


# =====================================================
# FICHE JOUEUR
# =====================================================

def format_player_profile(
    away,
    home,
    player,
    category,
    match_date,
):
    title = CATEGORY_INFO[category][0]

    h2h = calculate_player(player, category)
    recent5 = calculate_recent(player, category, 5)
    recent10 = calculate_recent(player, category, 10)

    out = [
        header("FICHE JOUEUR"),
        "",
        f"👤 <b>{esc(player['name'])}</b>",
        f"🏒 Équipe : <b>{player['team']}</b>",
        f"⚔️ {esc(NAMES[away])} @ {esc(NAMES[home])}",
        "",
        separator(),
        f"<b>{title}</b>",
        "",
        "<b>📊 PERFORMANCE H2H</b>",
    ]

    if h2h:
        out.extend([
            f"Matchs analysés : <b>{h2h['n']}</b>",
            f"Réussites : <b>{h2h['successes']}/{h2h['n']}</b>",
            f"Taux de réussite : <b>{h2h['hit'] * 100:.0f} %</b>",
            f"Moyenne : <b>{performance_text(h2h['average'], category)}</b>",
            f"Total : {performance_text(h2h['total'], category)}",
        ])
    else:
        out.append("Aucune statistique H2H vérifiable.")

    out.extend([
        "",
        separator(),
        "<b>🔥 FORME GÉNÉRALE</b>",
    ])

    for label, stats in (
        ("5 derniers matchs", recent5),
        ("10 derniers matchs", recent10),
    ):
        if stats:
            out.append(
                f"{label} : <b>{stats['successes']}/{stats['n']}</b> "
                f"({stats['hit'] * 100:.0f} %) "
                f"· Moy. {performance_text(stats['average'], category)}"
            )
        else:
            out.append(f"{label} : données insuffisantes")

    if category in ("goals", "assists", "points"):
        estimate = betting_estimate(player, category)

        out.extend([
            "",
            separator(),
            "<b>📈 ESTIMATION INDICATIVE</b>",
        ])

        if estimate and estimate["probability"] is not None:
            out.append(
                f"Probabilité estimée : "
                f"<b>{fmt_percent(estimate['probability'])}</b>"
            )
        else:
            out.append("Données insuffisantes pour estimer.")

        odd = get_odd(
            match_date,
            away,
            home,
            player["name"],
            category,
        )

        if odd is not None:
            out.extend([
                "",
                "<b>💰 COTE ENREGISTRÉE</b>",
                f"Cote : <b>{fmt(odd, 2)}</b>",
            ])
            out.extend(
                format_value_lines(player, category, odd)
            )
        else:
            out.extend([
                "",
                "💰 Aucune cote enregistrée.",
                "L'administrateur peut l'ajouter avec /cotes.",
            ])

        confidence = (
            confidence_score(estimate)
            if estimate else None
        )

        if confidence is not None:
            out.append(
                f"🔒 Fiabilité des données : <b>{confidence}/100</b>"
            )

    out.extend([
        "",
        separator(),
    ])

    out.extend(
        format_player_history(player, category)
    )

    out.extend([
        "",
        "<i>Les estimations sont indicatives et ne garantissent "
        "aucun résultat. La fiabilité mesure la qualité des données, "
        "pas la chance de gagner.</i>",
    ])

    return "\n".join(out)


# =====================================================
# SCORES H2H
# =====================================================

def format_h2h_scores(scores):
    out = [
        "<b>📊 DERNIÈRES CONFRONTATIONS</b>",
        "<i>Domicile et extérieur réunis</i>",
        "",
    ]

    for game in sorted(
        scores,
        key=lambda item: item.get("date") or "",
        reverse=True,
    )[:H2H_SCORE_COUNT]:
        actual_home = NAMES.get(
            game["home"],
            game["home"],
        )
        actual_away = NAMES.get(
            game["away"],
            game["away"],
        )

        out.append(
            f"{format_date(game.get('date'))} — "
            f"<b>{esc(actual_home)} "
            f"{game['home_score']}-{game['away_score']} "
            f"{esc(actual_away)}</b>"
        )

    if not scores:
        out.append("Aucun score disponible.")

    out.append("")
    return out


# =====================================================
# CLAVIERS — NAVIGATION
# =====================================================

def category_keyboard(away, home, match_date=None):
    date_part = match_date or "none"
    rows = []

    for index in range(0, len(CATEGORIES), 2):
        pair = CATEGORIES[index:index + 2]

        rows.append([
            InlineKeyboardButton(
                label,
                callback_data=(
                    f"c:{away}:{home}:{date_part}:{key}"
                ),
            )
            for label, key in pair
        ])

    rows.append([
        InlineKeyboardButton(
            "📊 BILAN DES ÉQUIPES",
            callback_data=f"c:{away}:{home}:{date_part}:teams",
        )
    ])

    rows.append([
        InlineKeyboardButton(
            "🥅 GARDIENS",
            callback_data=f"c:{away}:{home}:{date_part}:goalies",
        )
    ])

    rows.append([
        InlineKeyboardButton(
            "📅 AUTRES MATCHS",
            callback_data="d:0",
        )
    ])

    return InlineKeyboardMarkup(rows)


def category_text(away, home):
    return (
        header("SÉLECTION DU MARCHÉ")
        + f"\n🏒 <b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>\n"
        + separator()
        + "\n\nChoisis la catégorie à analyser :\n\n"
        "📊 H2H : domicile et extérieur réunis\n"
        "🔥 Forme : 5 et 10 derniers matchs\n"
        "💰 VALUE : calculée avec tes cotes\n"
        "🔒 Fiabilité : qualité des données sur 100"
    )


# =====================================================
# TOP 5
# =====================================================

def rank_category_players(players, category):
    ranked = []

    for player in players:
        stats = calculate_player(player, category)
        if stats:
            ranked.append((player, stats))

    ranked.sort(
        key=lambda item: (
            -item[1]["hit"],
            -item[1]["average"],
            -item[1]["n"],
        )
    )

    return ranked[:TOP]


def top5_text(away, home, category, ranked, scores):
    title, unit, decimals = CATEGORY_INFO[category]

    out = [
        header("CLASSEMENT H2H"),
        "",
        f"🏒 <b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>",
        "",
        f"<b>🔥 TOP 5 — {title}</b>",
        "<i>Classement selon le taux de réussite H2H.</i>",
        "",
    ]

    if not ranked:
        out.append("Aucun joueur avec des statistiques vérifiables.")
        return "\n".join(out)

    for index, (player, stats) in enumerate(ranked, 1):
        if category == "toi":
            average = fmt_minutes(stats["average"])
        elif category == "faceoffs":
            average = fmt_percent(stats["average"])
        else:
            average = (
                f"{fmt(stats['average'], decimals)} {unit}"
            )

        out.extend([
            f"<b>{index}. {esc(player['name'])}</b> · {player['team']}",
            f"🎯 {stats['successes']}/{stats['n']} réussites "
            f"({stats['hit'] * 100:.0f} %)",
            f"📈 Moyenne : {average}",
            "",
        ])

    return "\n".join(out)


def top5_keyboard(ranked):
    rows = []

    for index, (player, stats) in enumerate(ranked):
        label = (
            f"{index + 1} · {player['name']} "
            f"· {stats['hit'] * 100:.0f}%"
        )

        rows.append([
            InlineKeyboardButton(
                label[:60],
                callback_data=f"p:{index}",
            )
        ])

    rows.extend([
        [
            InlineKeyboardButton(
                "⬅️ MARCHÉS",
                callback_data="backcat",
            ),
            InlineKeyboardButton(
                "🔄 AUTRE MATCH",
                callback_data="d:0",
            ),
        ],
    ])

    return InlineKeyboardMarkup(rows)


def profile_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⬅️ RETOUR TOP 5",
                callback_data="backtop",
            )
        ],
        [
            InlineKeyboardButton(
                "📊 CHANGER DE MARCHÉ",
                callback_data="backcat",
            )
        ],
        [
            InlineKeyboardButton(
                "🏒 AUTRES MATCHS",
                callback_data="d:0",
            )
        ],
    ])


# =====================================================
# GARDIENS
# =====================================================

def goalie_result(player):
    valid = [
        game for game in player.get("vs", [])
        if game.get("save_pct") is not None
        or (
            game.get("saves") is not None
            and game.get("shots_against") is not None
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

    if shots_against:
        save_pct = saves / shots_against
    else:
        percentages = [
            game["save_pct"]
            for game in valid
            if game.get("save_pct") is not None
        ]

        if not percentages:
            return None

        percentages = [
            value / 100 if value > 1 else value
            for value in percentages
        ]

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
        stats = goalie_result(player)
        if stats:
            ranked.append((player, stats))

    ranked.sort(
        key=lambda item: (
            -item[1]["save_pct"],
            -item[1]["games"],
        )
    )

    out = [
        header("ANALYSE DES GARDIENS"),
        "",
        f"🏒 <b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>",
        "",
    ]

    out.extend(format_h2h_scores(scores))
    out.extend(["<b>🥅 GARDIENS</b>", ""])

    if not ranked:
        out.append("Aucune statistique de gardien vérifiable.")
        return "\n".join(out)

    for index, (player, stats) in enumerate(ranked[:TOP], 1):
        out.extend([
            f"<b>{index}. {esc(player['name'])}</b> ({player['team']})",
            f"Arrêts : {stats['saves']}",
            f"Tirs reçus : {stats['shots_against']}",
            f"Buts encaissés : {stats['goals_against']}",
            f"% arrêts : {fmt_percent(stats['save_pct'])}",
            f"{stats['games']} matchs H2H",
            "",
        ])

    return "\n".join(out)


# =====================================================
# BILAN DES ÉQUIPES
# =====================================================

def format_teams(away, home, scores):
    stats = {
        away: {"wins": 0, "losses": 0, "gf": 0, "ga": 0},
        home: {"wins": 0, "losses": 0, "gf": 0, "ga": 0},
    }

    for game in scores:
        actual_away = game["away"]
        actual_home = game["home"]

        away_score = game["away_score"]
        home_score = game["home_score"]

        stats[actual_away]["gf"] += away_score
        stats[actual_away]["ga"] += home_score

        stats[actual_home]["gf"] += home_score
        stats[actual_home]["ga"] += away_score

        if away_score > home_score:
            stats[actual_away]["wins"] += 1
            stats[actual_home]["losses"] += 1
        elif home_score > away_score:
            stats[actual_home]["wins"] += 1
            stats[actual_away]["losses"] += 1

    out = [
        header("BILAN DES ÉQUIPES"),
        "",
        f"🏒 <b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>",
        "",
    ]

    out.extend(format_h2h_scores(scores))
    out.extend(["<b>📊 STATISTIQUES</b>", ""])

    for team in (away, home):
        data = stats[team]

        out.extend([
            f"<b>{esc(NAMES[team])} ({team})</b>",
            f"Victoires : {data['wins']}",
            f"Défaites : {data['losses']}",
            f"Buts marqués : {data['gf']}",
            f"Buts encaissés : {data['ga']}",
            f"Différence : {data['gf'] - data['ga']}",
            "",
        ])

    return "\n".join(out)


# =====================================================
# CHARGEMENT DES JOUEURS
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
            "Aucun joueur disponible dans les effectifs actuels."
        )

    return players


async def load_player_logs(players):
    semaphore = asyncio.Semaphore(8)

    async def load(player):
        async with semaphore:
            try:
                player["log"] = await game_log(player["id"])
            except Exception as error:
                log.warning(
                    "Journal %s : %s",
                    player["name"],
                    error,
                )
                player["log"] = []

    await asyncio.gather(
        *[load(player) for player in players]
    )


# =====================================================
# ANALYSE D'UN MARCHÉ
# =====================================================

async def analyze_category(
    away,
    home,
    category,
    match_date=None,
):
    scores = await head_to_head_games(away, home)

    if not scores:
        return {
            "text": (
                f"{header('ANALYSE INDISPONIBLE')}\n"
                f"<b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>\n\n"
                "Aucune confrontation terminée trouvée "
                "sur les saisons analysées."
            ),
            "ranked": [],
            "players": [],
            "scores": [],
        }

    if category == "teams":
        return {
            "text": format_teams(away, home, scores),
            "ranked": [],
            "players": [],
            "scores": scores,
        }

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

    if category != "goalies":
        pbp_data = await all_pbp(scores)

    for player in players:
        match_player_games(
            player,
            scores,
            pbp_data,
        )

    if category == "goalies":
        return {
            "text": format_goalies(
                away,
                home,
                players,
                scores,
            ),
            "ranked": [],
            "players": players,
            "scores": scores,
        }

    ranked = rank_category_players(
        players,
        category,
    )

    return {
        "text": "",
        "ranked": ranked,
        "players": players,
        "scores": scores,
    }


# =====================================================
# CALENDRIER NHL
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
                    game["startTimeUTC"].replace("Z", "+00:00")
                ).astimezone(TZ)

                output.append((
                    game["awayTeam"]["abbrev"],
                    game["homeTeam"]["abbrev"],
                    start.strftime("%H:%M"),
                    (game.get("gameDate") or date)[:10],
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

    for away, home, hour, match_date in games:
        rows.append([
            InlineKeyboardButton(
                f"🏒 {away} vs {home}  ·  {hour}",
                callback_data=f"g:{away}:{home}:{match_date}",
            )
        ])

    rows.append([
        InlineKeyboardButton(
            ("❄️ " if index == offset else "") + label,
            callback_data=f"d:{index}",
        )
        for index, label in enumerate(DAYS)
    ])

    return InlineKeyboardMarkup(rows)


async def games_view(offset):
    offset = max(0, min(2, offset))
    games = await games_for(offset)

    if games:
        text = (
            header("MATCHS DISPONIBLES")
            + f"\n📅 <b>{DAYS[offset]}</b>\n"
            "🇫🇷 Horaires de Paris\n\n"
            "Sélectionne une rencontre pour lancer l'analyse."
        )
    else:
        text = (
            header("MATCHS DISPONIBLES")
            + f"\n📅 <b>{DAYS[offset]}</b>\n\n"
            "Aucun match programmé ce jour.\n"
            "Choisis une autre date."
        )

    return text, games_view_keyboard(offset, games)


async def send_games(message, offset=0):
    try:
        text, markup = await games_view(offset)

        await message.reply_text(
            text,
            parse_mode=HTML,
            reply_markup=markup,
        )

    except Exception as error:
        log.warning("Calendrier : %s", error)

        await message.reply_text(
            "⚠️ Calendrier temporairement indisponible."
        )


async def find_upcoming_date(away, home):
    for offset in range(7):
        try:
            games = await games_for(offset)

            for actual_away, actual_home, _, match_date in games:
                if actual_away == away and actual_home == home:
                    return match_date

        except Exception:
            continue

    return None


# =====================================================
# DÉCOUPAGE DES MESSAGES
# =====================================================

def split_telegram_text(text, limit=3900):
    if len(text) <= limit:
        return [text]

    chunks = []
    current = ""

    for line in text.split("\n"):
        candidate = (
            current + "\n" + line
            if current else line
        )

        if len(candidate) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = candidate

    if current:
        chunks.append(current)

    return chunks


# =====================================================
# ACCUEIL
# =====================================================

async def start(update, ctx):
    if not user_can_access(update, ctx):
        await update.message.reply_text(
            "🔒 BLACK ICE est actuellement fermé."
        )
        return

    ctx.user_data.pop("analysis", None)

    await update.message.reply_text(
        header("BIENVENUE"),
        parse_mode=HTML,
        reply_markup=home_keyboard(),
    )

    await update.message.reply_text(
        "👋 <b>Bienvenue dans BLACK ICE.</b>\n\n"
        "Ton espace d'analyse NHL.\n\n"
        "📊 Statistiques H2H domicile + extérieur\n"
        "🔥 Forme des joueurs sur 5 et 10 matchs\n"
        "🎯 Classement TOP 5 par marché\n"
        "💰 Analyse des cotes et de la VALUE\n"
        "🔒 Indice de fiabilité des données\n\n"
        "<i>Sélectionne un match pour commencer.</i>",
        parse_mode=HTML,
    )

    await send_games(update.message, 0)


# =====================================================
# BOUTONS
# =====================================================

async def on_button(update, ctx):
    query = update.callback_query
    parts = query.data.split(":")

    if parts[0] in ("oa", "om"):
        await handle_admin_callback(
            query,
            ctx,
            parts,
        )
        return

    if not user_can_access(update, ctx):
        await query.answer(
            "🔒 BLACK ICE est fermé.",
            show_alert=True,
        )
        return

    await query.answer()

    try:
        # ---------------------------------------------
        # CALENDRIER
        # ---------------------------------------------

        if parts[0] == "d":
            offset = int(parts[1])
            text, markup = await games_view(offset)

            await query.edit_message_text(
                text,
                parse_mode=HTML,
                reply_markup=markup,
            )
            return

        # ---------------------------------------------
        # SÉLECTION D'UN MATCH
        # ---------------------------------------------

        if parts[0] in ("g", "m"):
            away, home = parts[1:3]

            match_date = (
                parts[3]
                if len(parts) > 3 and parts[3] != "none"
                else None
            )

            if away not in NAMES or home not in NAMES:
                return

            ctx.user_data["analysis"] = {
                "away": away,
                "home": home,
                "match_date": match_date,
            }

            await query.edit_message_text(
                category_text(away, home),
                parse_mode=HTML,
                reply_markup=category_keyboard(
                    away,
                    home,
                    match_date,
                ),
            )
            return

        # ---------------------------------------------
        # SÉLECTION DU MARCHÉ
        # ---------------------------------------------

        if parts[0] == "c":
            if len(parts) != 5:
                return

            away, home, date_part, category = parts[1:]

            match_date = (
                None if date_part == "none"
                else date_part
            )

            valid_categories = (
                {key for _, key in CATEGORIES}
                | {"teams", "goalies"}
            )

            if category not in valid_categories:
                return

            if away not in NAMES or home not in NAMES:
                return

            await query.edit_message_text(
                header("ANALYSE EN COURS")
                + f"\n🏒 {esc(NAMES[away])} @ {esc(NAMES[home])}\n"
                + f"📊 Marché : <b>{esc(category)}</b>\n\n"
                "⏳ Récupération des statistiques NHL...",
                parse_mode=HTML,
            )

            try:
                result = await analyze_category(
                    away,
                    home,
                    category,
                    match_date,
                )

            except Exception:
                log.exception(
                    "Erreur analyse %s",
                    category,
                )

                await query.edit_message_text(
                    header("ANALYSE INDISPONIBLE")
                    + "\nLes données de cette catégorie "
                    "sont temporairement indisponibles.\n\n"
                    "Réessaie dans quelques instants.",
                    parse_mode=HTML,
                    reply_markup=back_keyboard("backcat"),
                )
                return

            ctx.user_data["analysis"] = {
                "away": away,
                "home": home,
                "match_date": match_date,
                "category": category,
                "ranked": result["ranked"],
                "players": result["players"],
                "scores": result["scores"],
            }

            if category in ("teams", "goalies"):
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "⬅️ RETOUR AUX MARCHÉS",
                            callback_data="backcat",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🏒 AUTRES MATCHS",
                            callback_data="d:0",
                        )
                    ],
                ])

                await query.edit_message_text(
                    result["text"],
                    parse_mode=HTML,
                    reply_markup=keyboard,
                )
                return

            ranked = result["ranked"]

            text = top5_text(
                away,
                home,
                category,
                ranked,
                result["scores"],
            )

            markup = top5_keyboard(ranked)

            if not ranked:
                markup = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "⬅️ RETOUR AUX MARCHÉS",
                            callback_data="backcat",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🏒 AUTRES MATCHS",
                            callback_data="d:0",
                        )
                    ],
                ])

            await query.edit_message_text(
                text,
                parse_mode=HTML,
                reply_markup=markup,
            )
            return

        # ---------------------------------------------
        # OUVERTURE DE LA FICHE JOUEUR
        # ---------------------------------------------

        if parts[0] == "p":
            data = ctx.user_data.get("analysis", {})
            ranked = data.get("ranked", [])

            if len(parts) != 2:
                return

            try:
                index = int(parts[1])
            except ValueError:
                return

            if index < 0 or index >= len(ranked):
                await query.answer(
                    "Joueur indisponible.",
                    show_alert=True,
                )
                return

            player = ranked[index][0]

            text = format_player_profile(
                data["away"],
                data["home"],
                player,
                data["category"],
                data.get("match_date"),
            )

            await query.edit_message_text(
                text,
                parse_mode=HTML,
                reply_markup=profile_keyboard(),
            )
            return

        # ---------------------------------------------
        # RETOUR AU TOP 5
        # ---------------------------------------------

        if parts[0] == "backtop":
            data = ctx.user_data.get("analysis", {})

            if not data:
                await query.edit_message_text(
                    "Relance une analyse depuis les matchs."
                )
                return

            await query.edit_message_text(
                top5_text(
                    data["away"],
                    data["home"],
                    data["category"],
                    data["ranked"],
                    data["scores"],
                ),
                parse_mode=HTML,
                reply_markup=top5_keyboard(
                    data["ranked"]
                ),
            )
            return

        # ---------------------------------------------
        # RETOUR AUX MARCHÉS
        # ---------------------------------------------

        if parts[0] == "backcat":
            data = ctx.user_data.get("analysis", {})

            if not data:
                await query.edit_message_text(
                    "Relance une analyse depuis les matchs."
                )
                return

            await query.edit_message_text(
                category_text(
                    data["away"],
                    data["home"],
                ),
                parse_mode=HTML,
                reply_markup=category_keyboard(
                    data["away"],
                    data["home"],
                    data.get("match_date"),
                ),
            )
            return

        # ---------------------------------------------
        # RETOUR ACCUEIL
        # ---------------------------------------------

        if parts[0] == "backhome":
            await query.edit_message_text(
                header("ACCUEIL")
                + "\nSélectionne un match pour commencer.",
                parse_mode=HTML,
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "📅 MATCHS DU JOUR",
                            callback_data="d:0",
                        )
                    ]
                ]),
            )
            return

    except Exception as error:
        log.exception(
            "Erreur bouton Telegram : %s",
            error,
        )


# =====================================================
# SAISIE TEXTE
# =====================================================

async def on_text(update, ctx):
    message = update.message
    text = (message.text or "").strip()

    # ---------------------------------------------
    # MOT DE PASSE ADMINISTRATEUR
    # ---------------------------------------------

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
                parse_mode=HTML,
            )

        else:
            await message.reply_text(
                "❌ Mot de passe incorrect."
            )

        return

    # ---------------------------------------------
    # FLUX DE SAISIE DES COTES
    # ---------------------------------------------

    flow = ctx.user_data.get("admin_flow")

    if flow and is_admin(update, ctx):
        step = flow.get("step")

        if step == "player":
            if len(text) < 2 or len(text) > 80:
                await message.reply_text(
                    "Nom de joueur invalide."
                )
                return

            flow["player"] = text
            flow["step"] = "market"

            await message.reply_text(
                f"Joueur : <b>{esc(text)}</b>\n\n"
                "Choisis le marché :",
                parse_mode=HTML,
                reply_markup=odds_market_keyboard(),
            )
            return

        if step == "odd":
            try:
                odd = float(
                    text.replace(",", ".")
                )
            except ValueError:
                await message.reply_text(
                    "Cote invalide. Exemple : 2.05"
                )
                return

            if not 1.01 <= odd <= 1000:
                await message.reply_text(
                    "La cote doit être comprise entre 1.01 et 1000."
                )
                return

            save_odd(
                flow["match_date"],
                flow["away"],
                flow["home"],
                flow["player"],
                flow["market"],
                odd,
            )

            await message.reply_text(
                "✅ <b>COTE ENREGISTRÉE</b>\n\n"
                f"Match : {esc(NAMES[flow['away']])} "
                f"@ {esc(NAMES[flow['home']])}\n"
                f"Date : {format_date(flow['match_date'])}\n"
                f"Joueur : {esc(flow['player'])}\n"
                f"Marché : {ODD_MARKETS[flow['market']]}\n"
                f"Cote : <b>{fmt(odd, 2)}</b>\n\n"
                "Cote sauvegardée dans SQLite.",
                parse_mode=HTML,
            )

            ctx.user_data.pop("admin_flow", None)
            return

    # ---------------------------------------------
    # ACCÈS UTILISATEUR
    # ---------------------------------------------

    if not user_can_access(update, ctx):
        await message.reply_text(
            "🔒 BLACK ICE est actuellement fermé."
        )
        return

    if text in (
        MENU_BTN,
        "Matchs du jour",
        "📅 Matchs du jour",
    ) or norm(text) in (
        "match",
        "matchs",
        "menu",
        "ce soir",
        "aujourdhui",
    ):
        await send_games(message, 0)
        return

    # ---------------------------------------------
    # RECHERCHE PAR DEUX ÉQUIPES
    # ---------------------------------------------

    parts = [
        part.strip()
        for part in SPLIT.split(text)
        if part.strip()
    ]

    if len(parts) == 2:
        away = find_team(parts[0])
        home = find_team(parts[1])

        if away and home and away != home:
            match_date = await find_upcoming_date(
                away,
                home,
            )

            ctx.user_data["analysis"] = {
                "away": away,
                "home": home,
                "match_date": match_date,
            }

            await message.reply_text(
                category_text(away, home),
                parse_mode=HTML,
                reply_markup=category_keyboard(
                    away,
                    home,
                    match_date,
                ),
            )
            return

        bad = parts[0] if not away else parts[1]

        await message.reply_text(
            f"Je ne reconnais pas « {bad} ». "
            "Essaie le nom de la ville ou l'abréviation."
        )
        return

    # ---------------------------------------------
    # RECHERCHE PAR UNE ÉQUIPE
    # ---------------------------------------------

    if len(parts) == 1:
        team = find_team(parts[0])

        if team:
            try:
                for offset in range(7):
                    games = await games_for(offset)

                    for away, home, _, match_date in games:
                        if team in (away, home):
                            ctx.user_data["analysis"] = {
                                "away": away,
                                "home": home,
                                "match_date": match_date,
                            }

                            await message.reply_text(
                                category_text(away, home),
                                parse_mode=HTML,
                                reply_markup=category_keyboard(
                                    away,
                                    home,
                                    match_date,
                                ),
                            )
                            return

            except Exception as error:
                log.warning(
                    "Recherche équipe : %s",
                    error,
                )

            await message.reply_text(
                f"Aucun match trouvé pour {NAMES[team]} "
                "dans les 7 prochains jours."
            )
            return

    await message.reply_text(
        "Sélectionne « OUVRIR BLACK ICE » ou écris deux équipes, "
        "par exemple : Washington vs Montréal."
    )


# =====================================================
# FERMETURE HTTP
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
            on_text,
        )
    )

    log.info(
        "BLACK ICE démarré — "
        "interface Telegram améliorée — "
        "TOP 5 interactif — "
        "fiches joueurs — "
        "H2H domicile/extérieur — "
        "forme 5/10 — "
        "VALUE — "
        "fiabilité — "
        "SQLite — "
        "administration"
    )

    app.run_polling()


if __name__ == "__main__":
    main()