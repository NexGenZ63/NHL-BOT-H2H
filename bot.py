
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
    Update,
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
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("blackice")


# =====================================================
# SQLITE
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
            INSERT OR IGNORE INTO settings (key, value)
            VALUES ('bot_open', '1')
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
            INSERT INTO settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, str(value)))
        db.commit()


def bot_is_open():
    return db_setting("bot_open", "1") == "1"


def save_odd(match_date, away, home, player, market, odd):
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            INSERT INTO odds (
                match_date, away, home, player,
                player_norm, market, odd, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(
                match_date, away, home, player_norm, market
            )
            DO UPDATE SET
                player=excluded.player,
                odd=excluded.odd,
                updated_at=excluded.updated_at
        """, (
            match_date,
            away,
            home,
            player,
            norm(player),
            market,
            odd,
            datetime.now(TZ).isoformat(),
        ))
        db.commit()


def get_odd(match_date, away, home, player, market):
    if not match_date:
        return None

    with sqlite3.connect(DB_PATH) as db:
        row = db.execute("""
            SELECT odd FROM odds
            WHERE match_date=?
              AND away=?
              AND home=?
              AND player_norm=?
              AND market=?
        """, (
            match_date,
            away,
            home,
            norm(player),
            market,
        )).fetchone()

    return row[0] if row else None


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
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
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
        n, list(ALIASES), n=1, cutoff=0.75
    )
    return ALIASES[close[0]] if close else None


SPLIT = re.compile(
    r"\s+(?:vs\.?|versus|contre|at|c\.)\s+|\s*@\s*|\s+-\s+",
    re.I,
)


# =====================================================
# AUTHENTIFICATION ADMINISTRATEUR
# =====================================================

def is_admin(update, ctx):
    user = update.effective_user
    if not user:
        return False

    expiry = ctx.user_data.get("admin_expiry", 0)

    if expiry < time.time():
        ctx.user_data.pop("admin_expiry", None)
        return False

    return True


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
        if update.message:
            await update.message.reply_text(
                "⛔ Connecte-toi d'abord avec /admin."
            )
        return False
    return True


async def cmd_admin(update, ctx):
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "La connexion administrateur doit se faire en message privé."
        )
        return

    if not ADMIN_PASSWORD:
        await update.message.reply_text(
            "⚠️ ADMIN_PASSWORD n'est pas configuré dans Railway."
        )
        return

    ctx.user_data["await_admin_password"] = True
    await update.message.reply_text(
        "🔐 <b>PANNEAU ADMINISTRATEUR</b>\n\n"
        "Envoie ton mot de passe en message privé.",
        parse_mode=HTML,
    )


async def cmd_logout(update, ctx):
    ctx.user_data.pop("admin_expiry", None)
    ctx.user_data.pop("await_admin_password", None)
    ctx.user_data.pop("admin_flow", None)
    await update.message.reply_text(
        "🔒 Session administrateur fermée."
    )


async def cmd_fermer(update, ctx):
    if not await admin_required(update, ctx):
        return

    set_db_setting("bot_open", "0")
    await update.message.reply_text(
        "🔒 <b>BLACK ICE EST FERMÉ</b>\n\n"
        "Les utilisateurs ne peuvent plus lancer d'analyses.\n"
        "Ta session administrateur reste active.",
        parse_mode=HTML,
    )


async def cmd_ouvrir(update, ctx):
    if not await admin_required(update, ctx):
        return

    set_db_setting("bot_open", "1")
    await update.message.reply_text(
        "🔓 <b>BLACK ICE EST OUVERT</b>",
        parse_mode=HTML,
    )


async def cmd_statut(update, ctx):
    if not await admin_required(update, ctx):
        return

    status = "🟢 OUVERT" if bot_is_open() else "🔴 FERMÉ"
    await update.message.reply_text(
        "<b>BLACK ICE — STATUT</b>\n\n"
        f"Accès utilisateurs : {status}\n"
        "Base : SQLite\n"
        f"Fichier : <code>{esc(DB_PATH)}</code>",
        parse_mode=HTML,
    )


async def cmd_annuler(update, ctx):
    if not await admin_required(update, ctx):
        return

    ctx.user_data.pop("admin_flow", None)
    await update.message.reply_text(
        "❌ Saisie annulée."
    )


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
        [InlineKeyboardButton(
            "🥅 +0,5 but", callback_data="om:goals"
        )],
        [InlineKeyboardButton(
            "🎯 +0,5 passe", callback_data="om:assists"
        )],
        [InlineKeyboardButton(
            "⭐ +0,5 point", callback_data="om:points"
        )],
        [InlineKeyboardButton(
            "❌ Annuler", callback_data="om:cancel"
        )],
    ])


async def cmd_cotes(update, ctx):
    if not await admin_required(update, ctx):
        return

    ctx.user_data["admin_flow"] = {"step": "match"}

    await update.message.reply_text(
        "📊 <b>SAISIE DES COTES</b>\n\n"
        "Sélectionne le match ci-dessous.\n"
        "Les cotes seront associées à cette rencontre précise.",
        parse_mode=HTML,
    )

    for offset in range(7):
        games = await games_for(offset)

        if not games:
            continue

        buttons = []
        for away, home, hour, match_date in games:
            buttons.append([
                InlineKeyboardButton(
                    f"{NAMES.get(away, away)} @ "
                    f"{NAMES.get(home, home)} · {hour}",
                    callback_data=f"oa:{away}:{home}:{match_date}",
                )
            ])

        await update.message.reply_text(
            f"📅 {(
                datetime.now(TZ) + timedelta(days=offset)
            ).strftime('%d/%m/%Y')}",
            reply_markup=InlineKeyboardMarkup(buttons),
        )


async def handle_admin_callback(query, ctx, parts):
    if not is_admin(
        type("Obj", (), {"effective_user": query.from_user})(),
        ctx,
    ):
        await query.answer("Session admin expirée.", show_alert=True)
        return True

    if parts[0] == "oa":
        if len(parts) != 4:
            await query.answer("Sélection invalide.")
            return True

        away, home, match_date = parts[1:4]

        ctx.user_data["admin_flow"] = {
            "step": "player",
            "away": away,
            "home": home,
            "match_date": match_date,
        }

        await query.answer()
        await query.message.reply_text(
            f"Match sélectionné : <b>{esc(NAMES[away])} @ "
            f"{esc(NAMES[home])}</b>\n"
            f"Date : {format_date(match_date)}\n\n"
            "Envoie maintenant le nom exact du joueur.",
            parse_mode=HTML,
        )
        return True

    if parts[0] == "om":
        if len(parts) != 2:
            await query.answer()
            return True

        market = parts[1]

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
            "Envoie maintenant la cote décimale.\n"
            "Exemple : <code>2.05</code>",
            parse_mode=HTML,
        )
        return True

    return False


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
            headers={"User-Agent": "BLACK-ICE-NHL-BOT/5.0"},
        )
    return _client


async def get_json(url, params=None, tries=3):
    last = None
    for i in range(tries):
        try:
            response = await client().get(url, params=params)
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
    _cache[key] = (time.time(), value)
    return value


# =====================================================
# SAISONS ET OUTILS
# =====================================================

def seasons():
    return ["20242025", "20252026", "20262027"]


def current_season():
    now = datetime.now(TZ)
    year = now.year if now.month >= 9 else now.year - 1
    return f"{year}{year + 1}"


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
    return f"{seconds // 60}:{seconds % 60:02d}"


def format_date(date):
    if not date:
        return "Date inconnue"
    try:
        return datetime.strptime(
            date[:10], "%Y-%m-%d"
        ).strftime("%d/%m/%Y")
    except ValueError:
        return date[:10]


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

            players = []
            for p in entries:
                pid = p.get("id") or p.get("playerId")
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
            log.warning("Roster API %s : %s", team, e)

        try:
            data = await get_json(
                f"{API_WEB}/club-stats/{team}/{sid}/2"
            )
            key = "goalies" if position == "goalie" else "skaters"
            players = []

            for p in data.get(key) or []:
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
            log.warning("Club stats %s : %s", team, e)

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
    home_flag = g.get("homeRoadFlag")

    return {
        "date": (g.get("gameDate") or "")[:10],
        "opp": g.get("opponentAbbrev"),
        "team_abbrev": g.get("teamAbbrev"),
        "home": (
            True if home_flag == "H"
            else False if home_flag == "R"
            else None
        ),
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
        "blocks": number(g.get("blockedShots")),
        "pim": number(g.get("pim")),
        "toi": parse_toi(g.get("toi") or g.get("timeOnIce")),
        "powerplay": number(g.get("powerPlayPoints")),
        "powerplay_goals": number(g.get("powerPlayGoals")),
        "powerplay_assists": number(g.get("powerPlayAssists")),
        "faceoff_pct": number(g.get("faceoffWinningPctg")),
        "saves": number(g.get("saves")),
        "shots_against": number(g.get("shotsAgainst")),
        "save_pct": number(g.get("savePctg")),
        "goals_against": number(g.get("goalsAgainst")),
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
                rows.append(normalize_game(game))

        if not rows and errors:
            raise RuntimeError(
                f"Journaux joueurs indisponibles : {errors}"
            )

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
        season_results = results[index * 2:index * 2 + 2]
        if not any(not isinstance(r, Exception) for r in season_results):
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

            if game.get("gameState") not in ("FINAL", "OFF"):
                continue

            game_id = game.get("id")
            away_score = away_team.get("score")
            home_score = home_team.get("score")

            if game_id is None or away_score is None or home_score is None:
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
        key=lambda g: g.get("date") or "",
        reverse=True,
    )

    log.info(
        "H2H STRICT %s @ %s : %s matchs",
        away, home, len(matches)
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

        for play in data.get("plays") or []:
            event = (play.get("typeDescKey") or "").lower()
            details = play.get("details") or {}

            if event == "shot-on-goal":
                p = stats(details.get("shootingPlayerId"))
                if p:
                    p["shots"] += 1

            elif event == "missed-shot":
                p = stats(details.get("shootingPlayerId"))
                if p:
                    p["missed"] += 1

            elif event == "hit":
                p = stats(details.get("hittingPlayerId"))
                if p:
                    p["hits"] += 1

            elif event == "blocked-shot":
                p = stats(details.get("blockingPlayerId"))
                if p:
                    p["blocks"] += 1

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
                p = stats(pid)
                if p:
                    p["pim"] += number(details.get("duration")) or 0

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
                game["id"], result
            )
            output[game["id"]] = {}
        else:
            output[game["id"]] = result

    return output


# =====================================================
# ASSOCIATION STRICTE DES MATCHS AUX JOUEURS
# =====================================================

def match_player_games(player, scores, pbp_data):
    allowed_ids = {
        str(game["id"]): game
        for game in scores
    }

    allowed_dates = {}
    for game in scores:
        date = game.get("date")
        if date:
            allowed_dates.setdefault(date, []).append(game)

    player["vs"] = []

    for game in player.get("log", []):
        game_id = game.get("game_id")
        matched = allowed_ids.get(str(game_id)) if game_id else None

        if not matched:
            candidates = allowed_dates.get(game.get("date"), [])
            for candidate in candidates:
                if (
                    candidate["away"] == player["team"]
                    and candidate["home"] == player["opp"]
                ) or (
                    candidate["away"] == player["opp"]
                    and candidate["home"] == player["team"]
                ):
                    matched = candidate
                    break

        if not matched:
            continue

        if game.get("team_abbrev") and game["team_abbrev"] != player["team"]:
            continue

        if game.get("opp") != player["opp"]:
            continue

        if game.get("home") is None:
            continue

        expected_player_home = player["team"] == matched["home"]
        if game.get("home") != expected_player_home:
            continue

        correct_fixture = (
            matched["away"] == player["team"]
            and matched["home"] == player["opp"]
        ) or (
            matched["away"] == player["opp"]
            and matched["home"] == player["team"]
        )

        if not correct_fixture:
            continue

        row = dict(game)
        row["pbp"] = (
            pbp_data.get(str(matched["id"]), {})
            .get(str(player["id"]), empty_pbp())
        )
        row["match_id"] = str(matched["id"])
        row["match_score"] = matched
        player["vs"].append(row)

    unique = {}
    for game in player["vs"]:
        unique[game["match_id"]] = game

    player["vs"] = sorted(
        unique.values(),
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
        return game.get("shots") if game.get("shots") is not None else pbp.get("shots")
    if category == "missed":
        return pbp.get("missed")
    if category == "hits":
        return game.get("hits") if game.get("hits") is not None else pbp.get("hits")
    if category == "blocks":
        return game.get("blocks") if game.get("blocks") is not None else pbp.get("blocks")
    if category == "penalties":
        return game.get("pim") if game.get("pim") is not None else pbp.get("pim")
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
            return value / 100 if value > 1 else value

    return None


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


# =====================================================
# CALCULS ET FORMATTAGE
# =====================================================

def calculate_player(player, category):
    values = [
        metric_value(game, category)
        for game in player.get("vs", [])
    ]
    values = [v for v in values if v is not None]

    if not values:
        return None

    total = sum(values)
    n = len(values)

    threshold = 0.5 if category == "faceoffs" else 1
    hit = sum(1 for value in values if value >= threshold) / n

    return {
        "n": n,
        "total": total,
        "average": total / n,
        "hit": hit,
    }


def performance_text(value, category):
    if category == "toi":
        return fmt_minutes(value)
    if category == "faceoffs":
        return fmt_percent(value)
    if category == "penalties":
        return f"{fmt(value, 0)} min"
    if category == "powerplay":
        return f"{fmt(value, 0)} points PP"
    if category == "goals":
        return f"{fmt(value, 0)} but(s)"
    if category == "assists":
        return f"{fmt(value, 0)} passe(s)"
    if category == "points":
        return f"{fmt(value, 0)} point(s)"
    if category == "shots":
        return f"{fmt(value, 0)} tir(s) cadré(s)"
    if category == "missed":
        return f"{fmt(value, 0)} tir(s) manqué(s)"
    if category == "hits":
        return f"{fmt(value, 0)} hit(s)"
    if category == "blocks":
        return f"{fmt(value, 0)} bloc(s)"
    return str(value)


def format_player_odds(match_date, away, home, player):
    if not match_date:
        return []

    entries = []

    for market, label in ODD_MARKETS.items():
        odd = get_odd(
            match_date,
            away,
            home,
            player["name"],
            market,
        )
        if odd is not None:
            entries.append(f"   {label} : <b>{fmt(odd, 2)}</b>")

    if not entries:
        return []

    return ["   <b>📊 COTES MANUELLES</b>"] + entries


def format_player_history(player, category):
    games = sorted(
        player.get("vs", []),
        key=lambda g: g.get("date") or "",
        reverse=True,
    )

    out = ["   <b>📅 HISTORIQUE H2H MATCH PAR MATCH</b>"]
    valid_values = []

    for game in games:
        value = metric_value(game, category)
        if value is None:
            continue

        valid_values.append(value)
        match = game.get("match_score") or {}
        if not match:
            continue

        date = format_date(game.get("date"))
        home_name = NAMES.get(match.get("home"), match.get("home", "?"))
        away_name = NAMES.get(match.get("away"), match.get("away", "?"))

        score = (
            f"{home_name} {match.get('home_score', '?')}-"
            f"{match.get('away_score', '?')} {away_name}"
        )

        success = value >= (0.5 if category == "faceoffs" else 1)
        icon = "✅" if success else "❌"

        out.append(
            f"   {date} — {performance_text(value, category)} {icon}"
        )
        out.append(f"   <i>Score : {esc(score)}</i>")

    if not valid_values:
        out.append("   Aucune statistique individuelle vérifiable.")
        return out

    threshold = 0.5 if category == "faceoffs" else 1
    successes = sum(1 for value in valid_values if value >= threshold)
    percentage = successes / len(valid_values) * 100
    total = sum(valid_values)
    average = total / len(valid_values)

    out.append("")
    out.append(
        f"   <b>Bilan : {successes}/{len(valid_values)} réussites "
        f"({percentage:.0f} %)</b>"
    )

    if category == "toi":
        out.append(f"   Moyenne : {fmt_minutes(average)} par match")
    elif category == "faceoffs":
        out.append(f"   Moyenne : {fmt_percent(average)}")
    else:
        out.append(f"   Total : {fmt(total, 0)}")
        out.append(f"   Moyenne : {fmt(average, 2)} par match")

    return out


# =====================================================
# CLASSEMENT TOP 5
# =====================================================

def format_category(away, home, players, category, scores, match_date=None):
    title, unit, decimals = CATEGORY_INFO[category]
    ranked = []

    for player in players:
        result = calculate_player(player, category)
        if result is not None:
            ranked.append((player, result))

    ranked.sort(
        key=lambda item: (
            -item[1]["average"],
            -item[1]["hit"],
            -item[1]["n"],
        )
    )

    out = [
        f"<b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>",
        "",
    ]
    out.extend(format_h2h_scores(away, home, scores))
    out.extend([
        f"<b>{title}</b>",
        "<i>Statistiques exclusivement H2H exactes</i>",
        "",
    ])

    if not ranked:
        out.append("Aucune statistique individuelle vérifiable.")
        return "\n".join(out)

    for index, (player, stats) in enumerate(ranked[:TOP], 1):
        out.append(
            f"<b>{index}. {esc(player['name'])}</b> ({player['team']})"
        )
        out.append(f"   {stats['n']} matchs H2H analysés")
        out.append(f"   Réussite : {stats['hit'] * 100:.0f} %")

        if category == "toi":
            out.append(f"   Moyenne : {fmt_minutes(stats['average'])}")
        elif category == "faceoffs":
            out.append(f"   Moyenne : {fmt_percent(stats['average'])}")
        else:
            out.append(
                f"   Total : {fmt(stats['total'], decimals)} {unit}"
            )
            out.append(
                f"   Moyenne : {fmt(stats['average'], decimals)} {unit}"
            )

        out.extend(format_player_odds(
            match_date, away, home, player
        ))
        out.append("")
        out.extend(format_player_history(player, category))
        out.extend(["", "━━━━━━━━━━━━━━━━"])

    return "\n".join(out)


# =====================================================
# SCORES H2H
# =====================================================

def format_h2h_scores(away, home, scores):
    out = [
        "📊 <b>DERNIÈRES CONFRONTATIONS H2H</b>",
        (
            f"<i>{esc(NAMES[home])} à domicile "
            f"contre {esc(NAMES[away])} à l'extérieur</i>"
        ),
        "",
    ]

    recent = sorted(
        scores,
        key=lambda g: g.get("date") or "",
        reverse=True,
    )[:H2H_SCORE_COUNT]

    for game in recent:
        out.append(
            f"{format_date(game.get('date'))} — "
            f"<b>{esc(NAMES[home])} "
            f"{game['home_score']}-{game['away_score']} "
            f"{esc(NAMES[away])}</b>"
        )

    if not recent:
        out.append("Aucun score disponible.")

    out.append("")
    return out


# =====================================================
# GARDIENS
# =====================================================

def goalie_result(player):
    games = player.get("vs", [])

    valid = [
        game for game in games
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

    saves = sum(game.get("saves") or 0 for game in valid)
    shots_against = sum(
        game.get("shots_against") or 0 for game in valid
    )
    goals_against = sum(
        game.get("goals_against") or 0 for game in valid
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
            ranked.append((player, result))

    ranked.sort(
        key=lambda item: (
            -item[1]["save_pct"],
            -item[1]["games"],
        )
    )

    out = [
        f"<b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>",
        "",
    ]
    out.extend(format_h2h_scores(away, home, scores))
    out.extend(["<b>🥅 GARDIENS</b>", ""])

    if not ranked:
        out.append("Aucune statistique de gardien vérifiable.")
        return "\n".join(out)

    for index, (player, stats) in enumerate(ranked[:TOP], 1):
        out.append(
            f"<b>{index}. {esc(player['name'])}</b> ({player['team']})"
        )
        out.append(f"   Arrêts : {stats['saves']}")
        out.append(f"   Tirs reçus : {stats['shots_against']}")
        out.append(f"   Buts encaissés : {stats['goals_against']}")
        out.append(f"   % arrêts : {fmt_percent(stats['save_pct'])}")
        out.append(f"   {stats['games']} matchs H2H")
        out.append("")

    return "\n".join(out)


# =====================================================
# FORME H2H
# =====================================================

def format_form(away, home, players, scores):
    ranked = []

    for player in players:
        games = sorted(
            player.get("vs", []),
            key=lambda g: g.get("date") or "",
            reverse=True,
        )[:5]

        if not games:
            continue

        points = sum(g.get("points") or 0 for g in games)
        goals = sum(g.get("goals") or 0 for g in games)
        assists = sum(g.get("assists") or 0 for g in games)
        shots = sum(g.get("shots") or 0 for g in games)

        ranked.append((
            player, len(games), points, goals, assists, shots
        ))

    ranked.sort(
        key=lambda x: (-x[2], -x[5], -x[1])
    )

    out = [
        f"<b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>",
        "",
    ]
    out.extend(format_h2h_scores(away, home, scores))
    out.extend([
        "<b>📈 FORME H2H DES JOUEURS</b>",
        "<i>5 dernières confrontations exactes maximum</i>",
        "",
    ])

    for index, item in enumerate(ranked[:TOP], 1):
        player, n, points, goals, assists, shots = item

        out.append(
            f"<b>{index}. {esc(player['name'])}</b> ({player['team']})"
        )
        out.append(f"   {n} confrontations H2H")
        out.append(
            f"   {points} points | {goals} buts | {assists} passes"
        )
        out.append(f"   {shots} tirs cadrés")
        out.extend(format_player_history(player, "points"))
        out.append("")

    if not ranked:
        out.append("Aucune forme H2H disponible.")

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
        "",
    ]
    out.extend(format_h2h_scores(away, home, scores))
    out.extend(["<b>📊 BILAN DES ÉQUIPES</b>", ""])

    for team in (away, home):
        s = stats[team]
        out.append(f"<b>{esc(NAMES[team])} ({team})</b>")
        out.append(f"   Victoires : {s['wins']}")
        out.append(f"   Défaites : {s['losses']}")
        out.append(f"   Buts marqués : {s['gf']}")
        out.append(f"   Buts encaissés : {s['ga']}")
        out.append(f"   Différence : {s['gf'] - s['ga']}")
        out.append("")

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
            "Aucun joueur disponible dans les effectifs."
        )

    return players


async def load_player_logs(players):
    semaphore = asyncio.Semaphore(8)

    async def load(player):
        async with semaphore:
            try:
                player["log"] = await game_log(player["id"])
            except Exception as e:
                log.warning(
                    "Journal %s : %s",
                    player["name"], e
                )
                player["log"] = []

    await asyncio.gather(*(load(player) for player in players))


# =====================================================
# ANALYSE
# =====================================================

async def analyze_category(away, home, category, match_date=None):
    scores = await head_to_head_games(away, home)

    if not scores:
        return (
            f"<b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>\n\n"
            "Aucune confrontation terminée trouvée "
            "dans cette configuration exacte."
        )

    if category == "teams":
        return format_teams(away, home, scores)

    position = "goalie" if category == "goalies" else "skater"

    players = await load_players(away, home, position)
    await load_player_logs(players)

    pbp_data = {}

    if category not in ("form", "goalies", "teams"):
        pbp_data = await all_pbp(scores)

    for player in players:
        match_player_games(player, scores, pbp_data)

    if category == "goalies":
        return format_goalies(away, home, players, scores)

    if category == "form":
        return format_form(away, home, players, scores)

    return format_category(
        away, home, players, category, scores, match_date
    )


# =====================================================
# CATÉGORIES
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
    ("📈 Forme 5 H2H", "form"),
    ("📊 Bilan équipes", "teams"),
]


def category_keyboard(away, home, match_date=None):
    date_part = match_date or "none"
    rows = []

    for index in range(0, len(CATEGORIES), 2):
        pair = CATEGORIES[index:index + 2]
        rows.append([
            InlineKeyboardButton(
                text=label,
                callback_data=f"c:{away}:{home}:{date_part}:{key}",
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
        "Chaque classement affiche les meilleurs joueurs "
        "sur les confrontations H2H exactes.\n\n"
        "📅 Historique individuel match par match.\n"
        "📊 Scores des dernières confrontations.\n"
        "🏒 Domicile / extérieur strictement respecté."
    )


async def send_category_menu(message, away, home, match_date=None):
    await message.reply_text(
        category_text(away, home),
        parse_mode=HTML,
        reply_markup=category_keyboard(away, home, match_date),
    )


# =====================================================
# CALENDRIER NHL
# =====================================================

async def games_for(offset):
    date = (
        datetime.now(TZ) + timedelta(days=offset)
    ).date().isoformat()

    async def build():
        data = await get_json(f"{API_WEB}/schedule/{date}")

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


DAYS = ("Aujourd'hui", "Demain", "Après-demain")


def games_view_keyboard(offset, games):
    rows = []

    for away, home, hour, match_date in games:
        rows.append([
            InlineKeyboardButton(
                f"{NAMES.get(away, away)} @ "
                f"{NAMES.get(home, home)} · {hour}",
                callback_data=f"g:{away}:{home}:{match_date}",
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
    offset = max(0, min(2, offset))
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

    return text, games_view_keyboard(offset, games)


async def send_games(message, offset=0):
    try:
        text, markup = await games_view(offset)
        await message.reply_text(
            text,
            parse_mode=HTML,
            reply_markup=markup,
        )
    except Exception as e:
        log.warning("Calendrier : %s", e)
        await message.reply_text(
            "Calendrier indisponible. "
            "Écris deux équipes pour ouvrir leur analyse."
        )


async def find_upcoming_date(away, home):
    for offset in range(7):
        try:
            games = await games_for(offset)
            for a, h, _, match_date in games:
                if a == away and h == home:
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

    lines = text.split("\n")
    chunks = []
    current = ""

    for line in lines:
        candidate = current + "\n" + line if current else line

        if len(candidate) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = candidate

    if current:
        chunks.append(current)

    return chunks


# =====================================================
# COMMANDES TELEGRAM
# =====================================================

async def start(update, ctx):
    if not user_can_access(update, ctx):
        await update.message.reply_text(
            "🔒 BLACK ICE est actuellement fermé."
        )
        return

    keyboard = ReplyKeyboardMarkup(
        [[MENU_BTN]],
        resize_keyboard=True,
    )

    await update.message.reply_text(
        "🏒 <b>BLACK ICE — ANALYSE NHL</b>\n\n"
        "Sélectionne un match pour accéder aux "
        "statistiques individuelles et collectives.\n\n"
        "H2H strict sur trois saisons.\n"
        "Historique individuel détaillé.",
        parse_mode=HTML,
        reply_markup=keyboard,
    )

    await send_games(update.message, 0)


# =====================================================
# BOUTONS TELEGRAM
# =====================================================

async def on_button(update, ctx):
    query = update.callback_query
    parts = query.data.split(":")

    if parts[0] in ("oa", "om"):
        await handle_admin_callback(query, ctx, parts)
        return

    if not user_can_access(update, ctx):
        await query.answer(
            "🔒 BLACK ICE est fermé.",
            show_alert=True,
        )
        return

    await query.answer()

    try:
        if parts[0] == "d":
            offset = int(parts[1])
            text, markup = await games_view(offset)
            await query.edit_message_text(
                text,
                parse_mode=HTML,
                reply_markup=markup,
            )
            return

        if parts[0] == "g":
            away, home = parts[1], parts[2]
            match_date = parts[3] if len(parts) > 3 else None

            await query.edit_message_text(
                category_text(away, home),
                parse_mode=HTML,
                reply_markup=category_keyboard(
                    away, home, match_date
                ),
            )
            return

        if parts[0] == "m":
            away, home = parts[1], parts[2]
            match_date = parts[3] if len(parts) > 3 else None

            await query.edit_message_text(
                category_text(away, home),
                parse_mode=HTML,
                reply_markup=category_keyboard(
                    away, home, match_date
                ),
            )
            return

        if parts[0] == "c":
            away, home = parts[1], parts[2]
            match_date = parts[3] if parts[3] != "none" else None
            category = parts[4]

            valid_categories = {key for _, key in CATEGORIES}
            if category not in valid_categories:
                return

            await query.edit_message_text(
                f"⏳ Analyse {NAMES[away]} @ {NAMES[home]}...\n"
                f"Catégorie : {category}"
            )

            try:
                result = await analyze_category(
                    away, home, category, match_date
                )
            except Exception as e:
                log.exception(
                    "Analyse catégorie %s : %s",
                    category, e
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
                        callback_data=(
                            f"m:{away}:{home}:{match_date or 'none'}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📅 Autres matchs",
                        callback_data="d:0",
                    )
                ],
            ])

            chunks = split_telegram_text(result)

            await query.edit_message_text(
                chunks[0],
                parse_mode=HTML,
                reply_markup=keyboard if len(chunks) == 1 else None,
            )

            for index, chunk in enumerate(chunks[1:], 1):
                await query.message.reply_text(
                    chunk,
                    parse_mode=HTML,
                    reply_markup=(
                        keyboard if index == len(chunks) - 1 else None
                    ),
                )

    except Exception as e:
        log.exception("Erreur bouton Telegram : %s", e)


# =====================================================
# SAISIE TEXTE
# =====================================================

async def on_text(update, ctx):
    message = update.message
    text = (message.text or "").strip()

    # Connexion administrateur
    if ctx.user_data.get("await_admin_password"):
        ctx.user_data.pop("await_admin_password", None)

        try:
            await message.delete()
        except Exception:
            pass

        if ADMIN_PASSWORD and secrets.compare_digest(
            text, ADMIN_PASSWORD
        ):
            ctx.user_data["admin_expiry"] = (
                time.time() + ADMIN_SESSION_SECONDS
            )
            await message.reply_text(
                "✅ <b>Connexion administrateur réussie.</b>\n\n"
                "Commandes disponibles :\n"
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

    # Saisie d'une cote en cours
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
                f"Joueur : <b>{esc(text)}</b>\n\n"
                "Choisis le marché :",
                parse_mode=HTML,
                reply_markup=odds_market_keyboard(),
            )
            return

        if step == "odd":
            raw = text.replace(",", ".").strip()

            try:
                odd = float(raw)
            except ValueError:
                await message.reply_text(
                    "Cote invalide. Exemple : 2.05"
                )
                return

            if not (1.01 <= odd <= 1000):
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
                f"Match : {esc(NAMES[flow['away']])} @ "
                f"{esc(NAMES[flow['home']])}\n"
                f"Date : {format_date(flow['match_date'])}\n"
                f"Joueur : {esc(flow['player'])}\n"
                f"Marché : {ODD_MARKETS[flow['market']]}\n"
                f"Cote : <b>{fmt(odd, 2)}</b>\n\n"
                "La cote est sauvegardée dans SQLite.",
                parse_mode=HTML,
            )

            ctx.user_data.pop("admin_flow", None)
            return

    if not user_can_access(update, ctx):
        await message.reply_text(
            "🔒 BLACK ICE est actuellement fermé."
        )
        return

    if (
        text == MENU_BTN
        or norm(text) in (
            "match", "matchs", "menu", "ce soir", "aujourdhui"
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
            match_date = await find_upcoming_date(away, home)
            await send_category_menu(
                message, away, home, match_date
            )
            return

        bad = parts[0] if not away else parts[1]
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

                    for away, home, _, match_date in games:
                        if team in (away, home):
                            await send_category_menu(
                                message, away, home, match_date
                            )
                            return

            except Exception as e:
                log.warning("Recherche équipe : %s", e)

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

    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("logout", cmd_logout))
    app.add_handler(CommandHandler("cotes", cmd_cotes))
    app.add_handler(CommandHandler("fermer", cmd_fermer))
    app.add_handler(CommandHandler("ouvrir", cmd_ouvrir))
    app.add_handler(CommandHandler("statut", cmd_statut))
    app.add_handler(CommandHandler("annuler", cmd_annuler))

    app.add_handler(CallbackQueryHandler(on_button))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            on_text,
        )
    )

    log.info(
        "BLACK ICE démarré — 14 catégories — "
        "H2H strict domicile/extérieur — "
        "SQLite — administration par mot de passe"
    )

    app.run_polling()


if __name__ == "__main__":
    main()
