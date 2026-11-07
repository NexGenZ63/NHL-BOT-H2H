"""Bot Telegram : face-à-face NHL (points, buts, passes)."""
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
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_USERS", "").replace(" ", "").split(",") if x}

API_WEB = "https://api-web.nhle.com/v1"
API_STATS = "https://api.nhle.com/stats/rest/en"
ET = ZoneInfo("America/Toronto")
PER_TEAM = 14
SHRINK = 3
TOP = 5
MENU_BTN = "📅 Matchs du jour"
HTML = ParseMode.HTML

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
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


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[.'’]", "", s)
    return re.sub(r"\s+", " ", s).strip()


NAMES, ALIASES = {}, {}
for _line in TEAM_DATA.splitlines():
    _abbr, _name, *_al = _line.split("|")
    NAMES[_abbr] = _name
    for _a in [_abbr, _name, *_al]:
        ALIASES[norm(_a)] = _abbr


def find_team(text: str):
    n = norm(text)
    if n in ALIASES:
        return ALIASES[n]
    close = difflib.get_close_matches(n, list(ALIASES), n=1, cutoff=0.75)
    return ALIASES[close[0]] if close else None


SPLIT = re.compile(r"\s+(?:vs\.?|versus|contre|at|c\.)\s+|\s*@\s*|\s+-\s+", re.I)

_client = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=25, follow_redirects=True,
                                    headers={"User-Agent": "nhl-h2h-bot/1.0"})
    return _client


async def get_json(url, params=None, tries=3):
    last = None
    for i in range(tries):
        try:
            r = await client().get(url, params=params)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            await asyncio.sleep(0.6 * (i + 1))
    raise last


_cache = {}


async def cached(key, ttl, factory):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = await factory()
    _cache[key] = (time.time(), val)
    return val


def seasons():
    n = datetime.now(ET)
    y = n.year if n.month >= 9 else n.year - 1
    return f"{y}{y + 1}", f"{y - 1}{y}", y


async def pool_by_team(sid):
    sort = json.dumps([{"property": "points", "direction": "DESC"},
                       {"property": "goals", "direction": "DESC"},
                       {"property": "playerId", "direction": "ASC"}])

    async def page(start):
        try:
            d = await get_json(f"{API_STATS}/skater/summary", params={
                "isAggregate": "false", "isGame": "false", "sort": sort, "start": start,
                "limit": 100, "cayenneExp": f"gameTypeId=2 and seasonId={sid}"})
            return d.get("data", [])
        except Exception:  # noqa: BLE001
            return []

    pages = await asyncio.gather(*(page(s) for s in range(0, 700, 100)))
    teams, seen = {}, set()
    for p in (x for pg in pages for x in pg):
        if p["playerId"] in seen:
            continue
        seen.add(p["playerId"])
        t = (p.get("teamAbbrevs") or "").split(",")[-1].strip()
        teams.setdefault(t, []).append(
            {"id": p["playerId"], "name": p["skaterFullName"], "points": p.get("points", 0)})
    return teams


async def roster(team):
    sid, _, _ = seasons()

    async def build():
        try:
            d = await get_json(f"{API_WEB}/club-stats/{team}/{sid}/2")
            sk = d.get("skaters") or []
            if sk:
                return [{"id": p["playerId"],
                         "name": f'{p["firstName"]["default"]} {p["lastName"]["default"]}',
                         "points": p.get("points", 0)} for p in sk]
        except Exception as e:  # noqa: BLE001
            log.warning("club-stats %s: %s", team, e)
        pool = await cached(("pool", sid), 3600, lambda: pool_by_team(sid))
        return pool.get(team, [])

    r = await cached(("roster", team, sid), 3600, build)
    return sorted(r, key=lambda p: -p["points"])[:PER_TEAM]


def _row(date, opp, home, g, a, pts):
    return {"date": (date or "")[:10], "opp": opp, "home": home,
            "goals": g or 0, "assists": a or 0, "points": pts or 0}


async def game_log(pid):
    async def build():
        sid, prev, y = seasons()
        try:
            d = await get_json(f"{API_STATS}/skater/summary", params={
                "isAggregate": "false", "isGame": "true",
                "sort": json.dumps([{"property": "gameDate", "direction": "DESC"}]),
                "start": 0, "limit": 200, "factCayenneExp": "gamesPlayed>=1",
                "cayenneExp": f'gameDate>="{y - 1}-08-01" and playerId={pid} and gameTypeId=2'})
            rows = [_row(g.get("gameDate"), g.get("opponentTeamAbbrev"), g.get("homeRoad") == "H",
                         g.get("goals"), g.get("assists"), g.get("points")) for g in d.get("data", [])]
            if rows:
                return rows
        except Exception as e:  # noqa: BLE001
            log.warning("game log stats %s: %s", pid, e)
        parts = await asyncio.gather(*(get_json(f"{API_WEB}/player/{pid}/game-log/{s}/2") for s in (prev, sid)))
        return [_row(g.get("gameDate"), g.get("opponentAbbrev"), g.get("homeRoadFlag") == "H",
                     g.get("goals"), g.get("assists"), g.get("points"))
                for d in parts for g in d.get("gameLog", [])]

    return await cached(("log", pid), 3 * 3600, build)


async def games_for(offset):
    d = (datetime.now(ET) + timedelta(days=offset)).date().isoformat()

    async def build():
        data = await get_json(f"{API_WEB}/schedule/{d}")
        for day in data.get("gameWeek", []):
            if day.get("date") == d:
                out = []
                for g in day.get("games", []):
                    t = datetime.fromisoformat(g["startTimeUTC"].replace("Z", "+00:00")).astimezone(ET)
                    out.append((g["awayTeam"]["abbrev"], g["homeTeam"]["abbrev"], t.strftime("%H:%M")))
                return out
        return []

    return await cached(("sched", d), 600, build)


def score(p, m):
    vs, allg = p["vs"], p["log"]
    n = len(vs)
    total = sum(g[m] for g in vs)
    rate = total / n if n else 0.0
    base = sum(g[m] for g in allg) / len(allg) if allg else 0.0
    w = n / (n + SHRINK)
    return {"n": n, "sum": total, "rate": rate, "base": base,
            "blend": w * rate + (1 - w) * base,
            "hit": (sum(1 for g in vs if g[m] >= 1) / n) if n else 0.0}


def fmt(x):
    return f"{x:.2f}".replace(".", ",")


async def analyze(away, home):
    rosters = await asyncio.gather(roster(away), roster(home))
    players = [{**p, "team": t, "opp": o}
               for t, o, r in ((away, home, rosters[0]), (home, away, rosters[1])) for p in r]
    if not players:
        raise RuntimeError("aucun joueur trouvé")
    sem = asyncio.Semaphore(6)

    async def load(p):
        async with sem:
            try:
                p["log"] = await game_log(p["id"])
            except Exception as e:  # noqa: BLE001
                log.warning("log %s: %s", p["id"], e)
                p["log"] = None
        if p["log"] is not None:
            p["vs"] = [g for g in p["log"] if g["opp"] == p["opp"]]

    await asyncio.gather(*(load(p) for p in players))
    ok = [p for p in players if p["log"] is not None]
    if not ok:
        raise RuntimeError("statistiques indisponibles")
    return format_result(away, home, ok, len(players) - len(ok))


def format_result(away, home, ok, failed):
    sid, prev, _ = seasons()
    out = [f"<b>{esc(NAMES[away])} @ {esc(NAMES[home])}</b>",
           f"<i>Face-à-face {prev[:4]}-{prev[6:]} et {sid[:4]}-{sid[6:]}, top {TOP} par catégorie</i>"]
    withvs = [p for p in ok if p["vs"]]
    if not withvs:
        out.append("\nAucun face-à-face trouvé sur ces deux saisons.")
        return "\n".join(out)
    for m, title, one, many in (("points", "Points", "pt", "pts"),
                                ("goals", "Buts", "but", "buts"),
                                ("assists", "Passes", "passe", "passes")):
        ranked = sorted(((p, score(p, m)) for p in withvs),
                        key=lambda x: (-x[1]["blend"], -x[1]["hit"]))[:TOP]
        out.append(f"\n<b>{title}</b>")
        for i, (p, s) in enumerate(ranked, 1):
            warn = " ⚠️" if s["n"] < 3 else ""
            unit = one if s["sum"] == 1 else many
            out.append(f"{i}. {esc(p['name'])} ({p['team']}) : <b>{round(s['hit'] * 100)} %</b>"
                       f" des matchs, {s['n']} MJ, {s['sum']} {unit}, moy. {fmt(s['rate'])}"
                       f" (saison {fmt(s['base'])}){warn}")
    out.append("\n⚠️ = moins de 3 matchs contre cet adversaire.")
    out.append("Le % indique la part des matchs avec au moins 1 point, but ou passe. "
               "Le classement ajuste la moyenne H2H selon la forme générale du joueur.")
    if failed:
        out.append(f"{failed} joueur(s) n'ont pas pu être chargés.")
    return "\n".join(out)


def allowed(update: Update) -> bool:
    u = update.effective_user
    return not ALLOWED or (u and u.id in ALLOWED)


DAYS = ("Aujourd'hui", "Demain", "Après-demain")
BACK = InlineKeyboardMarkup([[InlineKeyboardButton("📅 Autres matchs", callback_data="d:0")]])
ERR = "Les données de la NHL sont indisponibles pour l'instant. Réessaie dans un instant."


async def games_view(offset):
    games = await games_for(offset)
    rows = [[InlineKeyboardButton(f"{NAMES.get(a, a)} @ {NAMES.get(h, h)} · {t} HE",
                                  callback_data=f"g:{a}:{h}")] for a, h, t in games]
    rows.append([InlineKeyboardButton(("• " if i == offset else "") + lbl, callback_data=f"d:{i}")
                 for i, lbl in enumerate(DAYS)])
    text = (f"<b>Matchs : {DAYS[offset].lower()}</b>\nTouche un match, ou écris par exemple "
            "« Washington vs Montréal »." if games else
            f"Aucun match {DAYS[offset].lower()}. Choisis un autre jour, ou écris deux équipes "
            "(« Washington vs Montréal »).")
    return text, InlineKeyboardMarkup(rows)


async def send_games(message, offset=0):
    try:
        text, markup = await games_view(offset)
    except Exception as e:  # noqa: BLE001
        log.warning("calendrier: %s", e)
        await message.reply_text("Calendrier indisponible. Écris deux équipes, par exemple "
                                 "« Washington vs Montréal ».")
        return
    await message.reply_text(text, parse_mode=HTML, reply_markup=markup)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    kb = ReplyKeyboardMarkup([[MENU_BTN]], resize_keyboard=True)
    await update.message.reply_text(
        "🏒 <b>Face-à-face NHL</b>\nChoisis un match ci-dessous pour voir les joueurs avec le meilleur "
        "historique contre l'adversaire (points, buts, passes).", parse_mode=HTML, reply_markup=kb)
    await send_games(update.message, 0)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not allowed(update):
        return
    await q.answer()
    parts = q.data.split(":")
    try:
        if parts[0] == "d":
            text, markup = await games_view(int(parts[1]))
            await q.edit_message_text(text, parse_mode=HTML, reply_markup=markup)
        elif parts[0] == "g":
            a, h = parts[1], parts[2]
            await q.edit_message_text(f"⏳ Analyse de {NAMES[a]} @ {NAMES[h]}…")
            try:
                result = await analyze(a, h)
            except Exception as e:  # noqa: BLE001
                log.exception("analyse: %s", e)
                result = ERR
            await q.edit_message_text(result, parse_mode=HTML, reply_markup=BACK)
    except Exception as e:  # noqa: BLE001
        log.warning("bouton: %s", e)


async def run_analysis(message, a, h):
    wait = await message.reply_text(f"⏳ Analyse de {NAMES[a]} @ {NAMES[h]}…")
    try:
        result = await analyze(a, h)
    except Exception as e:  # noqa: BLE001
        log.exception("analyse: %s", e)
        result = ERR
    await wait.edit_text(result, parse_mode=HTML, reply_markup=BACK)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    msg = update.message
    text = (msg.text or "").strip()
    if text == MENU_BTN or norm(text) in ("match", "matchs", "menu", "ce soir", "aujourdhui"):
        await send_games(msg, 0)
        return
    parts = [x.strip() for x in SPLIT.split(text) if x.strip()]
    if len(parts) == 2:
        a, h = find_team(parts[0]), find_team(parts[1])
        if a and h and a != h:
            await run_analysis(msg, a, h)
            return
        bad = parts[0] if not a else parts[1]
        await msg.reply_text(f"Je ne reconnais pas « {bad} ». Essaie le nom de la ville, de l'équipe "
                             "ou son abréviation (MTL, WSH…).")
        return
    if len(parts) == 1:
        t = find_team(parts[0])
        if t:
            try:
                for off in range(7):
                    for a, h, _ in await games_for(off):
                        if t in (a, h):
                            await run_analysis(msg, a, h)
                            return
            except Exception as e:  # noqa: BLE001
                log.warning("recherche match: %s", e)
            await msg.reply_text(f"Aucun match trouvé pour {NAMES[t]} dans les 7 prochains jours.")
            return
    await msg.reply_text("Écris deux équipes, par exemple « Washington vs Montréal », "
                         "ou touche « Matchs du jour ».")


async def shutdown(app: Application):
    if _client:
        await _client.aclose()


def main():
    app = Application.builder().token(TOKEN).post_shutdown(shutdown).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Bot démarré")
    app.run_polling()


if __name__ == "__main__":
    main()
