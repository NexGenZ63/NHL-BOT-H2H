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
        buttons = [[InlineKeyboardButton(f"{NAMES[a]} @ {NAMES[h]} · {hour}", callback_data=f"oa:{a}:{h}:{date}")] for a,h,hour,date in games]
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
        ctx.user_data["admin_flow"] = {"step":"player", "away":away, "home":home, "match_date":match_date}
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


def client():
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent":"BLACK-ICE-NHL-BOT/8.0"})
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
    return [f"{year-2}{year-1}", f"{year-1}{year}", f"{year}{year+1}"]


def current_season():
    now = datetime.now(TZ)
    year = now.year if now.month >= 9 else now.year - 1
    return f"{year}{year+1}"

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
                players.append({"id":pid, "name":player_name(p), "team":team, "position":p.get("positionCode", "")})
        return players
    try:
        return await cached(("roster",team,position,current_season()),1800,build)
    except Exception as error:
        log.warning("Roster indisponible %s : %s", team, error)
        return []


def normalize_game(game):
    opponent, team = game.get("opponentAbbrev"), game.get("teamAbbrev")
    if isinstance(opponent, dict): opponent = opponent.get("default")
    if isinstance(team, dict): team = team.get("default")
    flag = game.get("homeRoadFlag")
    return {
        "date":(game.get("gameDate") or "")[:10], "opp":opponent, "team_abbrev":team,
        "home":True if flag == "H" else False if flag == "R" else None,
        "game_id":str(game.get("gameId") or game.get("id")) if game.get("gameId") or game.get("id") else None,
        "goals":number(game.get("goals")), "assists":number(game.get("assists")), "points":number(game.get("points")),
        "shots":number(game.get("shots")), "hits":number(game.get("hits")), "blocks":number(game.get("blockedShots")),
        "pim":number(game.get("pim")), "toi":parse_toi(game.get("toi") or game.get("timeOnIce")),
        "powerplay":number(game.get("powerPlayPoints")), "powerplay_goals":number(game.get("powerPlayGoals")),
        "powerplay_assists":number(game.get("powerPlayAssists")), "faceoff_pct":number(game.get("faceoffWinningPctg")),
        "saves":number(game.get("saves")), "shots_against":number(game.get("shotsAgainst")),
        "save_pct":number(game.get("savePctg")), "goals_against":number(game.get("goalsAgainst")), "decision":game.get("decision")
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
            key = row["game_id"] or (row["date"],row["opp"],row["home"])
            unique[key] = row
        return list(unique.values())
    return await cached(("log",pid,tuple(seasons())),3600,build)

# =====================================================
# H2H / PLAY-BY-PLAY
# =====================================================
async def head_to_head_games(away, home):
    async def fetch_schedule(team, season):
        return await cached(("schedule",team,season),3600,lambda:get_json(f"{API_WEB}/club-schedule-season/{team}/{season}"))
    results = await asyncio.gather(*[fetch_schedule(t,s) for s in seasons() for t in (away,home)], return_exceptions=True)
    if all(isinstance(x,Exception) for x in results):
        raise RuntimeError("Aucun calendrier NHL disponible.")
    found = {}
    for data in results:
        if isinstance(data,Exception):
            log.warning("Calendrier H2H indisponible : %s",data)
            continue
        for game in data.get("games",[]):
            at, ht = game.get("awayTeam") or {}, game.get("homeTeam") or {}
            aa, hh = at.get("abbrev"), ht.get("abbrev")
            try: game_type = int(game.get("gameType",0))
            except (TypeError,ValueError): continue
            if {aa,hh} != {away,home} or game_type != 2 or game.get("gameState") not in ("FINAL","OFF"):
                continue
            gid, ascore, hscore = game.get("id"), at.get("score"), ht.get("score")
            if gid is None or ascore is None or hscore is None: continue
            found[str(gid)] = {"id":str(gid),"date":(game.get("gameDate") or "")[:10],"away":aa,"home":hh,"away_score":ascore,"home_score":hscore}
    matches = sorted(found.values(),key=lambda g:(g.get("date") or "",g.get("id") or ""),reverse=True)
    log.info("H2H %s / %s : %s matchs",away,home,len(matches))
    return matches


def empty_pbp():
    return {"shots":0,"missed":0,"hits":0,"blocks":0,"faceoff_wins":0,"faceoff_losses":0,"pim":0}


async def play_by_play(game_id):
    async def build():
        data = await get_json(f"{API_WEB}/gamecenter/{game_id}/play-by-play")
        players = {}
        def get_stats(pid):
            if pid is None: return None
            pid = str(pid)
            if pid not in players: players[pid] = empty_pbp()
            return players[pid]
        for play in data.get("plays",[]):
            event = (play.get("typeDescKey") or "").lower()
            d = play.get("details") or {}
            if event == "shot-on-goal":
                p=get_stats(d.get("shootingPlayerId"));
                if p: p["shots"] += 1
            elif event == "missed-shot":
                p=get_stats(d.get("shootingPlayerId"));
                if p: p["missed"] += 1
            elif event == "hit":
                p=get_stats(d.get("hittingPlayerId"));
                if p: p["hits"] += 1
            elif event == "blocked-shot":
                p=get_stats(d.get("blockingPlayerId"));
                if p: p["blocks"] += 1
            elif event == "faceoff":
                w=get_stats(d.get("winningPlayerId")); l=get_stats(d.get("losingPlayerId"))
                if w: w["faceoff_wins"] += 1
                if l: l["faceoff_losses"] += 1
            elif event == "penalty":
                p=get_stats(d.get("committedByPlayerId") or d.get("servedByPlayerId"))
                if p: p["pim"] += number(d.get("duration")) or 0
        return players
    return await cached(("pbp",str(game_id)),12*3600,build)


async def all_pbp(scores):
    results = await asyncio.gather(*[play_by_play(g["id"]) for g in scores],return_exceptions=True)
    return {g["id"]:(None if isinstance(r,Exception) else r) for g,r in zip(scores,results)}


def match_player_games(player,scores,pbp_data):
    allowed_ids = {str(g["id"]):g for g in scores}
    allowed_dates = {}
    for g in scores:
        if g.get("date"): allowed_dates.setdefault(g["date"],[]).append(g)
    player["vs"] = []
    for game in player.get("log",[]):
        matched = None
        if game.get("game_id"):
            matched = allowed_ids.get(str(game["game_id"]))
            if not matched: continue
        else:
            for candidate in allowed_dates.get(game.get("date"),[]):
                if player["team"] in (candidate["away"],candidate["home"]) and player["opp"] in (candidate["away"],candidate["home"]):
                    matched = candidate
                    break
        if not matched or {matched["away"],matched["home"]} != {player["team"],player["opp"]}: continue
        if game.get("opp") != player["opp"]: continue
        if game.get("team_abbrev") and game["team_abbrev"] != player["team"]: continue
        if game.get("home") is None or game["home"] != (player["team"] == matched["home"]): continue
        row = dict(game)
        pbp = pbp_data.get(str(matched["id"]))
        row["pbp"] = None if pbp is None else pbp.get(str(player["id"]),empty_pbp())
        row["match_id"],row["match_score"] = str(matched["id"]),matched
        player["vs"].append(row)
    unique = {g["match_id"]:g for g in player["vs"]}
    player["vs"] = sorted(unique.values(),key=lambda g:g.get("date") or "",reverse=True)

# =====================================================
# MARCHÉS / STATISTIQUES
# =====================================================
CATEGORY_INFO = {
    "points":("⭐ POINTS","pts",0),"goals":("🥅 BUTS","buts",0),"assists":("🎯 PASSES DÉCISIVES","passes",0),
    "shots":("🏒 TIRS CADRÉS","tirs",0),"missed":("🎯 TIRS MANQUÉS","tirs",0),"hits":("💥 HITS","hits",0),
    "blocks":("🛡️ TIRS BLOQUÉS","blocs",0),"faceoffs":("🔵 MISES EN JEU","%",1),"toi":("⏱️ TEMPS DE GLACE","secondes",0),
    "powerplay":("⚡ POWER PLAY","points PP",0),"penalties":("🚨 PÉNALITÉS","minutes",0)
}


def metric_value(game,category):
    pbp=game.get("pbp")
    if category in ("points","goals","assists","toi"):
        return game.get(category)
    if category == "shots":
        return game.get("shots") if game.get("shots") is not None else (pbp.get("shots") if pbp is not None else None)
    if category == "missed": return pbp.get("missed") if pbp is not None else None
    if category == "hits": return game.get("hits") if game.get("hits") is not None else (pbp.get("hits") if pbp is not None else None)
    if category == "blocks": return game.get("blocks") if game.get("blocks") is not None else (pbp.get("blocks") if pbp is not None else None)
    if category == "penalties": return game.get("pim") if game.get("pim") is not None else (pbp.get("pim") if pbp is not None else None)
    if category == "powerplay":
        value=game.get("powerplay")
        if value is not None: return value
        g,a=game.get("powerplay_goals"),game.get("powerplay_assists")
        return g+a if g is not None and a is not None else None
    if category == "faceoffs":
        if pbp is not None:
            total=pbp.get("faceoff_wins",0)+pbp.get("faceoff_losses",0)
            if total: return pbp.get("faceoff_wins",0)/total
        value=game.get("faceoff_pct")
        return value/100 if value is not None and value>1 else value
    return None


def calculate_values(games,category):
    vals=[metric_value(g,category) for g in games]
    vals=[v for v in vals if v is not None]
    if not vals: return None
    threshold=0.5 if category=="faceoffs" else 1
    successes=sum(v>=threshold for v in vals)
    return {"n":len(vals),"total":sum(vals),"average":sum(vals)/len(vals),"successes":successes,"hit":successes/len(vals)}


def calculate_player(player,category):
    return calculate_values(player.get("vs",[]),category)


def recent_player_games(player,limit):
    games=[g for g in player.get("log",[]) if g.get("date")]
    games.sort(key=lambda g:g["date"],reverse=True)
    return games[:limit]


def calculate_recent(player,category,limit):
    return calculate_values(recent_player_games(player,limit),category)


def recent_form_text(player,category,limit):
    result=calculate_recent(player,category,limit)
    if not result:
        return f"Forme {limit} matchs : données insuffisantes"
    return (f"Forme {limit} matchs : {result['successes']}/{result['n']} "
            f"réussites ({result['hit']*100:.0f} %) — moyenne {fmt(result['average'],2)}")


def performance_text(value,category):
    if category=="toi": return fmt_minutes(value)
    if category=="faceoffs": return fmt_percent(value)
    labels={"penalties":"min","powerplay":"points PP","goals":"but(s)","assists":"passe(s)","points":"point(s)","shots":"tir(s) cadré(s)","missed":"tir(s) manqué(s)","hits":"hit(s)","blocks":"bloc(s)"}
    return f"{fmt(value,0)} {labels.get(category,'')}".strip()

# =====================================================
# PROBABILITÉ INDICATIVE / VALUE / CONFIANCE
# =====================================================
def smoothed_probability(successes,n):
    return (successes+1)/(n+2) if n>0 else None


def betting_estimate(player,market):
    if market not in ("goals","assists","points"): return None
    recent=calculate_recent(player,market,10)
    h2h=calculate_player(player,market)
    p_recent=smoothed_probability(recent["successes"],recent["n"]) if recent else None
    p_h2h=smoothed_probability(h2h["successes"],h2h["n"]) if h2h else None
    if p_recent is not None and p_h2h is not None: probability=.70*p_recent+.30*p_h2h
    else: probability=p_recent if p_recent is not None else p_h2h
    return {"probability":probability,"recent":recent,"h2h":h2h,"p_recent":p_recent,"p_h2h":p_h2h}


def confidence_score(estimate):
    if not estimate or estimate.get("probability") is None: return None
    recent,h2h=estimate.get("recent"),estimate.get("h2h")
    nr=recent["n"] if recent else 0
    nh=h2h["n"] if h2h else 0
    score=25+min(nr,10)*3+min(nh,5)*4
    if recent and h2h:
        diff=abs(estimate["p_recent"]-estimate["p_h2h"])
        score+=max(0,25*(1-diff))
    return max(0,min(100,round(score)))


def value_analysis(player,market,odd):
    estimate=betting_estimate(player,market)
    if not estimate or estimate["probability"] is None: return None
    p=estimate["probability"]
    implied=1/odd
    return {"probability":p,"implied":implied,"edge":p-implied,"roi":(p*odd-1)*100,"confidence":confidence_score(estimate)}


def format_value_lines(player,market,odd):
    a=value_analysis(player,market,odd)
    if not a: return ["📊 VALUE : données insuffisantes","🔒 Confiance : non calculable"]
    if a["edge"]>=.03: label="🟢 VALUE POTENTIELLE"
    elif a["edge"]>0: label="🟡 VALUE FAIBLE"
    else: label="🔴 PAS DE VALUE DÉTECTÉE"
    return [f"Probabilité estimée indicative : {fmt_percent(a['probability'])}",f"Probabilité implicite de la cote : {fmt_percent(a['implied'])}",f"Écart estimé : {a['edge']*100:+.1f} points",f"ROI théorique indicatif : {a['roi']:+.1f} %",label,f"🔒 Confiance des données : {a['confidence']}/100"]

# =====================================================
# AFFICHAGE DES FICHES JOUEURS
# =====================================================
def format_h2h_scores(scores):
    out=["📊 <b>DERNIÈRES CONFRONTATIONS H2H</b>","<i>Domicile + extérieur</i>",""]
    for g in sorted(scores,key=lambda x:x.get("date") or "",reverse=True)[:H2H_SCORE_COUNT]:
        hn=NAMES.get(g["home"],g["home"]); an=NAMES.get(g["away"],g["away"])
        out.append(f"{format_date(g.get('date'))} — <b>{esc(hn)} {g['home_score']}-{g['away_score']} {esc(an)}</b>")
    if not scores: out.append("Aucun score disponible.")
    out.append("")
    return out


def format_player_history(player,category):
    out=["<b>📅 HISTORIQUE H2H MATCH PAR MATCH</b>"]
    games=sorted(player.get("vs",[]),key=lambda g:g.get("date") or "",reverse=True)
    valid=[]
    for game in games:
        value=metric_value(game,category)
        match=game.get("match_score") or {}
        if value is None or not match: continue
        valid.append(value)
        success=value >= (.5 if category=="faceoffs" else 1)
        loc="DOM" if game.get("home") else "EXT"
        out.append(f"{format_date(game.get('date'))} [{loc}] — {performance_text(value,category)} {'✅' if success else '❌'}")
        hn=NAMES.get(match.get("home"),match.get("home","?")); an=NAMES.get(match.get("away"),match.get("away","?"))
        out.append(f"<i>Score : {esc(hn)} {match.get('home_score','?')}-{match.get('away_score','?')} {esc(an)}</i>")
    if not valid:
        out.append("Aucune statistique individuelle vérifiable.")
        return out
    successes=sum(v >= (.5 if category=="faceoffs" else 1) for v in valid)
    out.extend(["",f"<b>Bilan : {successes}/{len(valid)} réussites ({successes/len(valid)*100:.0f} %)</b>"])
    avg=sum(valid)/len(valid)
    out.append(f"Moyenne : {fmt_minutes(avg) if category=='toi' else fmt_percent(avg) if category=='faceoffs' else fmt(avg,2)} par match")
    return out


def format_player_profile(away,home,player,category,match_date,scores):
    title,unit,decimals=CATEGORY_INFO[category]
    h2h=calculate_player(player,category)
    out=[f"<b>{esc(player['name'])}</b> — {esc(NAMES.get(player['team'],player['team']))}",f"<b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>",f"<b>{title}</b>",""]
    if h2h:
        out.extend(["<b>📊 STATISTIQUES H2H</b>",f"Matchs analysés : {h2h['n']}",f"Réussite : {h2h['successes']}/{h2h['n']} ({h2h['hit']*100:.0f} %)"])
        if category=="toi": avg=fmt_minutes(h2h["average"])
        elif category=="faceoffs": avg=fmt_percent(h2h["average"])
        else: avg=f"{fmt(h2h['average'],decimals)} {unit}"
        out.extend([f"Total H2H : {fmt(h2h['total'],decimals)} {unit}",f"Moyenne H2H : {avg}"])
    else:
        out.append("Aucune statistique H2H vérifiable pour ce joueur sur ce marché.")
    out.extend(["","<b>🔥 FORME GÉNÉRALE (TOUS ADVERSAIRES)</b>",recent_form_text(player,category,5),recent_form_text(player,category,10),""])
    if category in ("goals","assists","points"):
        estimate=betting_estimate(player,category)
        if estimate and estimate.get("probability") is not None:
            out.extend(["<b>📈 ESTIMATION INDICATIVE</b>",f"Probabilité estimée : {fmt_percent(estimate['probability'])}",f"Confiance des données : {confidence_score(estimate)}/100","<i>La confiance mesure la qualité des données, pas la chance de gagner.</i>",""])
    if match_date:
        odds=[]
        for market,label in ODD_MARKETS.items():
            odd=get_odd(match_date,away,home,player["name"],market)
            if odd is not None:
                odds.extend([f"<b>{label} : {fmt(odd,2)}</b>"]+format_value_lines(player,market,odd)+[""])
        if odds: out.extend(["<b>💰 COTES SAISIES / VALUE</b>"]+odds)
        else: out.extend(["<b>💰 COTES</b>","Aucune cote enregistrée pour ce joueur et ce match.",""])
    out.extend(format_player_history(player,category))
    out.extend(["","<i>Les estimations sont indicatives et ne garantissent aucun résultat.</i>"])
    return "\n".join(out)

# =====================================================
# AUTRES AFFICHAGES / ANALYSES
# =====================================================
def format_category_top5(away,home,category,ranked):
    title,unit,decimals=CATEGORY_INFO[category]
    out=[f"<b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>",f"<b>{title} — TOP 5 H2H</b>","<i>Choisis un joueur pour ouvrir sa fiche complète.</i>",""]
    if not ranked: return "\n".join(out+["Aucun joueur avec des statistiques vérifiables."])
    for i,(p,s) in enumerate(ranked,1):
        avg=fmt_minutes(s["average"]) if category=="toi" else fmt_percent(s["average"]) if category=="faceoffs" else f"{fmt(s['average'],decimals)} {unit}"
        out.extend([f"<b>{i}. {esc(p['name'])}</b> ({p['team']})",f"   {s['n']} matchs H2H | {s['successes']}/{s['n']} réussites ({s['hit']*100:.0f} %)",f"   Moyenne : {avg}",""])
    return "\n".join(out)


def player_top_keyboard(ranked):
    rows=[]
    for i,(p,s) in enumerate(ranked):
        rows.append([InlineKeyboardButton(f"{i+1}. {p['name']} · {s['hit']*100:.0f}% ({s['n']} H2H)",callback_data=f"p:{i}")])
    rows.extend([[InlineKeyboardButton("⬅️ Retour aux catégories",callback_data="backcat")],[InlineKeyboardButton("📅 Autres matchs",callback_data="d:0")]])
    return InlineKeyboardMarkup(rows)


def player_profile_keyboard(index,count):
    rows=[]
    nav=[]
    if index>0: nav.append(InlineKeyboardButton("⬅️ Précédent",callback_data=f"pn:{index-1}"))
    if index<count-1: nav.append(InlineKeyboardButton("Suivant ➡️",callback_data=f"pn:{index+1}"))
    if nav: rows.append(nav)
    rows.extend([[InlineKeyboardButton("📋 Retour au TOP 5",callback_data="backtop")],[InlineKeyboardButton("📊 Autres catégories",callback_data="backcat")],[InlineKeyboardButton("📅 Autres matchs",callback_data="d:0")]])
    return InlineKeyboardMarkup(rows)


def format_teams(away,home,scores):
    stats={t:{"wins":0,"losses":0,"gf":0,"ga":0} for t in (away,home)}
    for g in scores:
        aa,hh=g["away"],g["home"]; a,h=g["away_score"],g["home_score"]
        stats[aa]["gf"]+=a; stats[aa]["ga"]+=h; stats[hh]["gf"]+=h; stats[hh]["ga"]+=a
        if a>h: stats[aa]["wins"]+=1; stats[hh]["losses"]+=1
        elif h>a: stats[hh]["wins"]+=1; stats[aa]["losses"]+=1
    out=[f"<b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>","","<b>📊 BILAN DES ÉQUIPES — H2H</b>",f"Matchs analysés : {len(scores)}",""]
    for t in (away,home):
        d=stats[t]; out.extend([f"<b>{esc(NAMES[t])} ({t})</b>",f"Victoires : {d['wins']}",f"Défaites : {d['losses']}",f"Buts marqués : {d['gf']}",f"Buts encaissés : {d['ga']}",f"Différence : {d['gf']-d['ga']}",""])
    out.extend(format_h2h_scores(scores))
    return "\n".join(out)


def goalie_result(player):
    valid=[g for g in player.get("vs",[]) if g.get("save_pct") is not None or (g.get("saves") is not None and g.get("shots_against") is not None)]
    if not valid: return None
    saves=sum(g.get("saves") or 0 for g in valid); shots=sum(g.get("shots_against") or 0 for g in valid); ga=sum(g.get("goals_against") or 0 for g in valid)
    if shots: pct=saves/shots
    else:
        pcts=[g["save_pct"] for g in valid if g.get("save_pct") is not None]
        if not pcts: return None
        pcts=[v/100 if v>1 else v for v in pcts]; pct=sum(pcts)/len(pcts)
    return {"games":len(valid),"saves":saves,"shots_against":shots,"goals_against":ga,"save_pct":pct}


def format_goalies(away,home,players):
    ranked=[(p,goalie_result(p)) for p in players if goalie_result(p)]
    ranked.sort(key=lambda x:(-x[1]["save_pct"],-x[1]["games"]))
    out=[f"<b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>","","<b>🥅 GARDIENS — H2H</b>",""]
    for i,(p,s) in enumerate(ranked[:TOP],1): out.extend([f"<b>{i}. {esc(p['name'])}</b> ({p['team']})",f"Matchs : {s['games']}",f"Arrêts : {s['saves']} / {s['shots_against']}",f"Buts encaissés : {s['goals_against']}",f"% arrêts : {fmt_percent(s['save_pct'])}",""])
    if not ranked: out.append("Aucune statistique de gardien vérifiable.")
    return "\n".join(out)


def format_form_h2h(away,home,players):
    ranked=[]
    for p in players:
        games=sorted(p.get("vs",[]),key=lambda g:g.get("date") or "",reverse=True)[:5]
        if games:
            pts=sum(g.get("points") or 0 for g in games); goals=sum(g.get("goals") or 0 for g in games); assists=sum(g.get("assists") or 0 for g in games); shots=sum(g.get("shots") or 0 for g in games)
            ranked.append((p,len(games),pts,goals,assists,shots))
    ranked.sort(key=lambda x:(-x[2],-x[5],-x[1]))
    out=[f"<b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>","","<b>📈 FORME H2H DES JOUEURS</b>","<i>5 dernières confrontations maximum</i>",""]
    for i,(p,n,pts,g,a,sh) in enumerate(ranked[:TOP],1): out.extend([f"<b>{i}. {esc(p['name'])}</b> ({p['team']})",f"{n} confrontations H2H",f"{pts} points | {g} buts | {a} passes | {sh} tirs",""])
    if not ranked: out.append("Aucune forme H2H disponible.")
    return "\n".join(out)

# =====================================================
# CHARGEMENT DES DONNÉES POUR UNE CATÉGORIE
# =====================================================
async def load_players(away,home,position="skater"):
    rosters=await asyncio.gather(roster(away,position),roster(home,position))
    players=[]
    for team,opp,entries in ((away,home,rosters[0]),(home,away,rosters[1])):
        for p in entries: players.append({**p,"team":team,"opp":opp,"log":[],"vs":[]})
    if not players: raise RuntimeError("Aucun joueur disponible dans les effectifs actuels.")
    sem=asyncio.Semaphore(8)
    async def load(p):
        async with sem:
            try: p["log"]=await game_log(p["id"])
            except Exception as e: log.warning("Journal %s : %s",p["name"],e); p["log"]=[]
    await asyncio.gather(*(load(p) for p in players))
    return players

# =====================================================
# MATCH SIMULATOR — MONTE-CARLO + BOOTSTRAP JOUEURS
# =====================================================
SIMULATIONS = 10000
SIM_PLAYER_GAMES = 20


def poisson_sample(lam):
    """Tirage de Poisson sans dépendance externe (Knuth)."""
    lam = max(0.05, min(float(lam), 12.0))
    threshold = math.exp(-lam)
    product = 1.0
    count = 0
    while product > threshold:
        count += 1
        product *= random.random()
    return count - 1


async def team_recent_results(team, limit=30):
    """Résultats réguliers terminés, toutes les saisons récupérées par seasons()."""
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
    away_goals, home_goals = [], []
    totals_over_4 = totals_over_5 = totals_over_6 = 0
    for _ in range(count):
        ag = poisson_sample(away_lambda)
        hg = poisson_sample(home_lambda)
        away_goals.append(ag)
        home_goals.append(hg)
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
        totals_over_4 += total >= 5
        totals_over_5 += total >= 6
        totals_over_6 += total >= 7
    return {
        "away_win": away_wins / count,
        "home_win": home_wins / count,
        "reg_tie": ties / count,
        "away_avg": sum(away_goals) / count,
        "home_avg": sum(home_goals) / count,
        "over_4_5": totals_over_4 / count,
        "over_5_5": totals_over_5 / count,
        "over_6_5": totals_over_6 / count,
    }


def simulate_player_logs(players, count=SIMULATIONS):
    """Bootstrap empirique des 20 dernières performances disponibles par joueur."""
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
            draws = [random.choice(values) for _ in range(count)]
            samples[market] = {
                "avg": sum(draws) / count,
                "one_plus": sum(v >= 1 for v in draws) / count,
                "two_plus": sum(v >= 2 for v in draws) / count,
                "three_plus": sum(v >= 3 for v in draws) / count,
            }
        if "points" in samples:
            results.append({"player": player, "n": len(recent), "samples": samples})
    return results


async def build_match_simulation(away, home):
    away_games, home_games = await asyncio.gather(
        team_recent_results(away), team_recent_results(home)
    )
    away_lambda = estimate_team_goals(away_games, home_games, False)
    home_lambda = estimate_team_goals(home_games, away_games, True)
    if away_lambda is None or home_lambda is None:
        raise RuntimeError("Historique insuffisant pour simuler les deux équipes (minimum 8 matchs exploitables chacune).")
    match = simulate_team_match(away_lambda, home_lambda)

    players = await load_players(away, home, "skater")
    simulated_players = simulate_player_logs(players)
    simulated_players.sort(
        key=lambda item: (item["samples"].get("points", {}).get("one_plus", 0),
                          item["samples"].get("points", {}).get("avg", 0)),
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
        f"<i>Bootstrap des {SIM_PLAYER_GAMES} derniers matchs disponibles, 10 000 tirages par joueur.</i>",
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
        "<i>Méthode : simulation probabiliste, pas une prédiction certaine. Les tirages de buts des joueurs sont empiriques et ne sont pas forcés à correspondre au score simulé de l'équipe.</i>",
        "<i>Probabilités expérimentales à valider par backtesting ; elles ne garantissent aucun résultat.</i>"
    ])
    return "\n".join(out)


async def prepare_category(away,home,category,match_date):
    if category == "simulation":
        return {"kind":"text", "text":await build_match_simulation(away, home)}
    scores=await head_to_head_games(away,home)
    if not scores: return {"kind":"text","text":f"<b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>\n\nAucune confrontation terminée trouvée entre ces équipes sur les saisons analysées."}
    if category=="teams": return {"kind":"text","text":format_teams(away,home,scores)}
    position="goalie" if category=="goalies" else "skater"
    players=await load_players(away,home,position)
    pbp_data={}
    if category not in ("form","goalies"):
        pbp_data=await all_pbp(scores)
    for p in players: match_player_games(p,scores,pbp_data)
    if category=="goalies": return {"kind":"text","text":format_goalies(away,home,players)}
    if category=="form": return {"kind":"text","text":format_form_h2h(away,home,players)}
    ranked=[]
    for p in players:
        result=calculate_player(p,category)
        if result: ranked.append((p,result))
    ranked.sort(key=lambda x:(-x[1]["average"],-x[1]["hit"],-x[1]["n"]))
    ranked=ranked[:TOP]
    return {"kind":"players","text":format_category_top5(away,home,category,ranked),"ranked":ranked,"scores":scores}

# =====================================================
# CATÉGORIES / CALENDRIER
# =====================================================
CATEGORIES=[
    ("⭐ Points","points"),("🥅 Buts","goals"),("🎯 Passes","assists"),
    ("🏒 Tirs cadrés","shots"),("🎯 Tirs manqués","missed"),("💥 Hits","hits"),
    ("🛡️ Tirs bloqués","blocks"),("🔵 Mises en jeu","faceoffs"),
    ("⏱️ Temps de glace","toi"),("⚡ Power Play","powerplay"),
    ("🚨 Pénalités","penalties"),("🥅 Gardiens","goalies"),
    ("📈 Forme 5 H2H","form"),("📊 Bilan équipes","teams"),
    ("🧬 Match Simulator","simulation")
]


def category_keyboard(away,home,match_date=None):
    date_part=match_date or "none"
    rows=[]
    for i in range(0,len(CATEGORIES),2):
        rows.append([InlineKeyboardButton(label,callback_data=f"c:{away}:{home}:{date_part}:{key}") for label,key in CATEGORIES[i:i+2]])
    rows.append([InlineKeyboardButton("📅 Autres matchs",callback_data="d:0")])
    return InlineKeyboardMarkup(rows)


def category_text(away,home):
    return (f"🏒 <b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>\n\nChoisis le module à analyser.\n\n"
            "🧬 Simulation Monte-Carlo des équipes et joueurs.\n📊 H2H domicile + extérieur.\n👤 TOP 5 avec une fiche par joueur.\n🔥 Forme générale sur 5 et 10 matchs dans chaque fiche.\n💰 VALUE avec tes cotes saisies.\n🔒 Confiance des données /100.")


async def send_category_menu(message,away,home,match_date=None):
    await message.reply_text(category_text(away,home),parse_mode=HTML,reply_markup=category_keyboard(away,home,match_date))


async def games_for(offset):
    date=(datetime.now(TZ)+timedelta(days=offset)).date().isoformat()
    async def build():
        data=await get_json(f"{API_WEB}/schedule/{date}")
        for day in data.get("gameWeek",[]):
            if day.get("date")!=date: continue
            output=[]
            for g in day.get("games",[]):
                start=datetime.fromisoformat(g["startTimeUTC"].replace("Z","+00:00")).astimezone(TZ)
                output.append((g["awayTeam"]["abbrev"],g["homeTeam"]["abbrev"],start.strftime("%H:%M"),(g.get("gameDate") or date)[:10]))
            return output
        return []
    return await cached(("schedule-day",date),600,build)


DAYS=("Aujourd'hui","Demain","Après-demain")


def games_view_keyboard(offset,games):
    rows=[[InlineKeyboardButton(f"{NAMES.get(a,a)} @ {NAMES.get(h,h)} · {hour}",callback_data=f"g:{a}:{h}:{date}")] for a,h,hour,date in games]
    rows.append([InlineKeyboardButton(("• " if i==offset else "")+label,callback_data=f"d:{i}") for i,label in enumerate(DAYS)])
    return InlineKeyboardMarkup(rows)


async def games_view(offset):
    offset=max(0,min(2,offset))
    games=await games_for(offset)
    text=(f"<b>Matchs : {DAYS[offset].lower()}</b>\nHoraires en heure de Paris 🇫🇷\nSélectionne un match." if games else f"Aucun match {DAYS[offset].lower()}.\nChoisis un autre jour ou écris deux équipes.")
    return text,games_view_keyboard(offset,games)


async def send_games(message,offset=0):
    try:
        text,markup=await games_view(offset)
        await message.reply_text(text,parse_mode=HTML,reply_markup=markup)
    except Exception as e:
        log.warning("Calendrier : %s",e)
        await message.reply_text("Calendrier indisponible. Écris deux équipes pour ouvrir leur analyse.")


async def find_upcoming_date(away,home):
    for offset in range(7):
        try:
            for a,h,_,date in await games_for(offset):
                if a==away and h==home: return date
        except Exception: continue
    return None

# =====================================================
# NAVIGATION CALLBACKS
# =====================================================
def split_telegram_text(text,limit=3900):
    if len(text)<=limit: return [text]
    chunks=[]
    current=""
    for line in text.split("\n"):
        candidate=current+"\n"+line if current else line
        if len(candidate)>limit and current:
            chunks.append(current)
            current=line
        else:
            current=candidate
    if current: chunks.append(current)
    return chunks


async def show_player_profile(query,ctx,index):
    state=ctx.user_data.get("analysis_state")
    if not state or state.get("kind")!="players":
        await query.answer("Cette analyse a expiré. Relance le marché.",show_alert=True)
        return
    ranked=state["ranked"]
    if index<0 or index>=len(ranked):
        await query.answer("Joueur introuvable.",show_alert=True)
        return
    p,_=ranked[index]
    text=format_player_profile(state["away"],state["home"],p,state["category"],state.get("match_date"),state["scores"])
    chunks=split_telegram_text(text)
    await query.edit_message_text(chunks[0],parse_mode=HTML,reply_markup=player_profile_keyboard(index,len(ranked)) if len(chunks)==1 else None)
    for i,chunk in enumerate(chunks[1:],1):
        await query.message.reply_text(chunk,parse_mode=HTML,reply_markup=player_profile_keyboard(index,len(ranked)) if i==len(chunks)-1 else None)


async def on_button(update,ctx):
    query=update.callback_query
    parts=query.data.split(":")
    if parts[0] in ("oa","om"):
        await handle_admin_callback(query,ctx,parts)
        return
    if not user_can_access(update,ctx):
        await query.answer("🔒 BLACK ICE est fermé.",show_alert=True)
        return
    await query.answer()
    try:
        action=parts[0]
        if action=="d":
            text,markup=await games_view(int(parts[1]))
            await query.edit_message_text(text,parse_mode=HTML,reply_markup=markup)
            return
        if action in ("g","m"):
            away,home=parts[1],parts[2]
            match_date=parts[3] if len(parts)>3 and parts[3]!="none" else None
            await query.edit_message_text(category_text(away,home),parse_mode=HTML,reply_markup=category_keyboard(away,home,match_date))
            return
        if action=="c":
            if len(parts)!=5: return
            away,home=parts[1],parts[2]
            match_date=None if parts[3]=="none" else parts[3]
            category=parts[4]
            if away not in NAMES or home not in NAMES or category not in {k for _,k in CATEGORIES}: return
            await query.edit_message_text(f"⏳ Analyse {NAMES[away]} @ {NAMES[home]}...\nCatégorie : {category}")
            try:
                state=await prepare_category(away,home,category,match_date)
            except Exception as e:
                log.exception("Analyse %s : %s",category,e)
                state={"kind":"text","text":"Les données de cette catégorie sont indisponibles pour le moment. Réessaie dans un instant."}
            state.update({"away":away,"home":home,"category":category,"match_date":match_date})
            ctx.user_data["analysis_state"]=state
            if state["kind"]=="players":
                await query.edit_message_text(state["text"],parse_mode=HTML,reply_markup=player_top_keyboard(state["ranked"]))
            else:
                kb=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Retour aux catégories",callback_data="backcat")],
                    [InlineKeyboardButton("📅 Autres matchs",callback_data="d:0")]
                ])
                chunks=split_telegram_text(state["text"])
                await query.edit_message_text(chunks[0],parse_mode=HTML,reply_markup=kb if len(chunks)==1 else None)
                for i,chunk in enumerate(chunks[1:],1):
                    await query.message.reply_text(chunk,parse_mode=HTML,reply_markup=kb if i==len(chunks)-1 else None)
            return
        if action=="p":
            await show_player_profile(query,ctx,int(parts[1]))
            return
        if action=="pn":
            await show_player_profile(query,ctx,int(parts[1]))
            return
        if action=="backtop":
            state=ctx.user_data.get("analysis_state")
            if not state or state.get("kind")!="players":
                await query.answer("Analyse expirée.",show_alert=True)
                return
            await query.edit_message_text(state["text"],parse_mode=HTML,reply_markup=player_top_keyboard(state["ranked"]))
            return
        if action=="backcat":
            state=ctx.user_data.get("analysis_state")
            if not state:
                await query.answer("Sélectionne à nouveau un match.",show_alert=True)
                return
            await query.edit_message_text(category_text(state["away"],state["home"]),parse_mode=HTML,reply_markup=category_keyboard(state["away"],state["home"],state.get("match_date")))
            return
    except Exception as e:
        log.exception("Erreur bouton Telegram : %s",e)

# =====================================================
# START / TEXTE / COMMANDES
# =====================================================
async def start(update,ctx):
    if not user_can_access(update,ctx):
        await update.message.reply_text("🔒 BLACK ICE est actuellement fermé.")
        return
    keyboard=ReplyKeyboardMarkup([[MENU_BTN]],resize_keyboard=True)
    await update.message.reply_text(
        "🏒 <b>BLACK ICE — ANALYSE NHL</b>\n\nSélectionne un match pour accéder aux statistiques.\n\n"
        "📊 H2H domicile + extérieur.\n👤 TOP 5 avec fiches individuelles.\n"
        "🔥 Forme récente sur 5 et 10 matchs.\n💰 VALUE automatique avec tes cotes.\n"
        "🧬 Match Simulator : équipes + joueurs.\n🔒 Confiance des données /100.",
        parse_mode=HTML,reply_markup=keyboard
    )
    await send_games(update.message,0)


async def on_text(update,ctx):
    message=update.message
    text=(message.text or "").strip()
    if ctx.user_data.get("await_admin_password"):
        ctx.user_data.pop("await_admin_password",None)
        try:
            await message.delete()
        except Exception:
            pass
        if ADMIN_PASSWORD and secrets.compare_digest(text,ADMIN_PASSWORD):
            ctx.user_data["admin_expiry"]=time.time()+ADMIN_SESSION_SECONDS
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
    flow=ctx.user_data.get("admin_flow")
    if flow and is_admin(update,ctx):
        step=flow.get("step")
        if step=="player":
            if len(text)<2 or len(text)>80:
                await message.reply_text("Nom de joueur invalide. Réessaie.")
                return
            flow["player"]=text
            flow["step"]="market"
            await message.reply_text(
                f"Joueur : <b>{esc(text)}</b>\n\nChoisis le marché :",
                parse_mode=HTML,reply_markup=odds_market_keyboard()
            )
            return
        if step=="odd":
            try:
                odd=float(text.replace(",","."))
            except ValueError:
                await message.reply_text("Cote invalide. Exemple : 2.05")
                return
            if not 1.01<=odd<=1000:
                await message.reply_text("La cote doit être comprise entre 1.01 et 1000.")
                return
            save_odd(flow["match_date"],flow["away"],flow["home"],flow["player"],flow["market"],odd)
            await message.reply_text(
                "✅ <b>COTE ENREGISTRÉE</b>\n\n"
                f"Match : {esc(NAMES[flow['away']])} @ {esc(NAMES[flow['home']])}\n"
                f"Date : {format_date(flow['match_date'])}\n"
                f"Joueur : {esc(flow['player'])}\n"
                f"Marché : {ODD_MARKETS[flow['market']]}\n"
                f"Cote : <b>{fmt(odd,2)}</b>\n\nCote sauvegardée dans SQLite.",
                parse_mode=HTML
            )
            ctx.user_data.pop("admin_flow",None)
            return
    if not user_can_access(update,ctx):
        await message.reply_text("🔒 BLACK ICE est actuellement fermé.")
        return
    if text==MENU_BTN or norm(text) in ("match","matchs","menu","ce soir","aujourdhui"):
        await send_games(message,0)
        return
    parts=[p.strip() for p in SPLIT.split(text) if p.strip()]
    if len(parts)==2:
        away,home=find_team(parts[0]),find_team(parts[1])
        if away and home and away!=home:
            date=await find_upcoming_date(away,home)
            await send_category_menu(message,away,home,date)
            return
        bad=parts[0] if not away else parts[1]
        await message.reply_text(f"Je ne reconnais pas « {bad} ». Essaie le nom de la ville, de l'équipe ou son abréviation.")
        return
    if len(parts)==1:
        team=find_team(parts[0])
        if team:
            for offset in range(7):
                try:
                    games=await games_for(offset)
                except Exception:
                    continue
                for away,home,_,date in games:
                    if team in (away,home):
                        await send_category_menu(message,away,home,date)
                        return
            await message.reply_text(f"Aucun match trouvé pour {NAMES[team]} dans les 7 prochains jours.")
            return
    await message.reply_text("Écris deux équipes, par exemple « Washington vs Montréal », ou touche « Matchs du jour ».")


async def shutdown(app):
    global _client
    if _client:
        await _client.aclose()
        _client=None


def main():
    init_db()
    app=Application.builder().token(TOKEN).post_shutdown(shutdown).build()
    app.add_handler(CommandHandler(["start","help"],start))
    app.add_handler(CommandHandler("admin",cmd_admin))
    app.add_handler(CommandHandler("logout",cmd_logout))
    app.add_handler(CommandHandler("cotes",cmd_cotes))
    app.add_handler(CommandHandler("fermer",cmd_fermer))
    app.add_handler(CommandHandler("ouvrir",cmd_ouvrir))
    app.add_handler(CommandHandler("statut",cmd_statut))
    app.add_handler(CommandHandler("annuler",cmd_annuler))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,on_text))
    log.info("BLACK ICE démarré — TOP 5 interactif — fiches joueurs — H2H — forme 5/10 — VALUE — Match Simulator Monte-Carlo — SQLite")
    app.run_polling()


if __name__=="__main__":
    main()