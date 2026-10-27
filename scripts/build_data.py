"""Build data.json for DEFCON Scout from FPL's public API.

Usage:
  python3 build_data.py --fetch                    # download fresh data from FPL, then build
  python3 build_data.py                            # build from files already downloaded
  python3 build_data.py --fetch --out out/data.json  # choose where data.json goes

Method:
  * A DEFCON "hit" = FPL actually awarded defensive_contribution points in that
    fixture (read from the per-fixture points breakdown, so it matches FPL exactly).
    Thresholds: defenders 10 actions (CBIT), midfielders 12 (CBIT + recoveries).
  * Only appearances of 60+ minutes count, for players and for opponents.
  * Midfielders are only included once they have MID_MIN_HITS DEFCON hits this
    season (any minutes). This keeps wingers and No.10s out of the player lists
    AND out of the "chances" counted against teams.
  * Every qualifying appearance is credited to the opponent as a "chance faced";
    a hit is credited as "allowed". Defenders and midfielders are kept separate.
  * Expected allowed = sum of each faced player's own hit rate (leave-one-out,
    shrunk), so a team isn't punished for facing elite DEFCON players.
  * Small samples are shrunk toward the league average for that position.
"""
import json, os, sys, time, urllib.request
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(HERE, "site", "data.json")
for _i, _a in enumerate(sys.argv):
    if _a == "--out" and _i + 1 < len(sys.argv):
        OUT = os.path.abspath(sys.argv[_i + 1])
API = "https://fantasy.premierleague.com/api/"
UA = {"User-Agent": "Mozilla/5.0 (DEFCON Scout)"}

MIN_MINUTES = 60
POSITIONS = {2: "DEF", 3: "MID"}  # element_type -> label
THRESHOLD = {"DEF": 10, "MID": 12}  # FPL actions needed (near-miss display only)
MID_MIN_HITS = 2                  # midfielders need this many season hits to be included
PLAYER_PRIOR = 3                  # pseudo-appearances pulling players to league avg
TEAM_PRIOR = 25                   # pseudo-chances pulling teams to league avg
VENUE_PRIOR = 25                  # pulls a venue split toward that team's overall
HORIZON_MAX = 8


def get(path, name):
    req = urllib.request.Request(API + path, headers=UA)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                body = r.read()
            json.loads(body)
            with open(os.path.join(DATA, name), "wb") as f:
                f.write(body)
            return
        except Exception:
            if attempt == 3:
                raise
            time.sleep(3 * (attempt + 1))


def load(name):
    with open(os.path.join(DATA, name)) as f:
        return json.load(f)


def fetch():
    os.makedirs(DATA, exist_ok=True)
    get("bootstrap-static/", "bootstrap.json")
    get("fixtures/", "fixtures.json")
    boot = load("bootstrap.json")
    for e in boot["events"]:
        if e["finished"]:
            get(f"event/{e['id']}/live/", f"live{e['id']}.json")


def team_from_history(pid, fx):
    """For a player who changed clubs: read his FPL history to see which side he played for."""
    name = f"summary{pid}.json"
    if not os.path.exists(os.path.join(DATA, name)):
        try:
            get(f"element-summary/{pid}/", name)
        except Exception:
            return None
    for r in load(name).get("history", []):
        if r["fixture"] == fx["id"]:
            return fx["team_h"] if r["was_home"] else fx["team_a"]
    return None


def odds(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return p / (1 - p)


def prob(o):
    return o / (1 + o)


def build():
    boot = load("bootstrap.json")
    fixtures = load("fixtures.json")
    teams = {t["id"]: {"id": t["id"], "name": t["name"], "short": t["short_name"]} for t in boot["teams"]}
    players = {p["id"]: p for p in boot["elements"]}
    fx_by_id = {f["id"]: f for f in fixtures}

    finished_gws = [e["id"] for e in boot["events"] if e["finished"] and e["data_checked"]]
    last_gw = max(finished_gws) if finished_gws else 0
    next_ev = next((e for e in boot["events"] if e["is_next"]), None)
    next_gw = next_ev["id"] if next_ev else last_gw + 1

    # raw action counts and side, per fixture, from the fixture stats
    raw, side = {}, {}
    for f in fixtures:
        for s in f["stats"]:
            if s["identifier"] != "defensive_contribution":
                continue
            for k, tid in (("h", f["team_h"]), ("a", f["team_a"])):
                for e in s[k]:
                    raw[(f["id"], e["element"])] = e["value"]
                    side[(f["id"], e["element"])] = tid

    # appearances, one per player per fixture
    apps, unresolved = [], 0
    for gw in finished_gws:
        live = load(f"live{gw}.json")
        for el in live["elements"]:
            p = players.get(el["id"])
            if not p or p["element_type"] not in POSITIONS:
                continue
            for ex in el["explain"]:
                fx = fx_by_id.get(ex["fixture"])
                if not fx or not fx["finished"]:
                    continue
                stats = {s["identifier"]: s for s in ex["stats"]}
                minutes = stats.get("minutes", {}).get("value", 0)
                if minutes <= 0:
                    continue
                hit = stats.get("defensive_contribution", {}).get("points", 0) > 0
                key = (fx["id"], el["id"])
                team = side.get(key)
                if team is None:
                    team = p["team"] if p["team"] in (fx["team_h"], fx["team_a"]) else None
                if team is None and minutes >= MIN_MINUTES:
                    team = team_from_history(el["id"], fx)
                if team is None:
                    unresolved += 1
                    continue
                home = team == fx["team_h"]
                actions = raw.get(key, 0)
                if hit and actions == 0:
                    actions = stats["defensive_contribution"].get("value", 0)
                apps.append({
                    "pid": el["id"], "pos": POSITIONS[p["element_type"]], "gw": fx["event"],
                    "fixture": fx["id"], "team": team, "opp": fx["team_a"] if home else fx["team_h"],
                    "home": home, "min": minutes, "actions": actions, "hit": hit,
                    "q": minutes >= MIN_MINUTES,
                })

    # which players are included
    season_hits = defaultdict(int)
    for a in apps:
        season_hits[a["pid"]] += a["hit"]

    def included(pid):
        pos = POSITIONS[players[pid]["element_type"]]
        return pos == "DEF" or season_hits[pid] >= MID_MIN_HITS

    by_player = defaultdict(list)
    for a in apps:
        by_player[a["pid"]].append(a)

    team_games = defaultdict(set)
    for f in fixtures:
        if f["finished"] and f["event"] in finished_gws:
            team_games[f["team_h"]].add(f["id"]); team_games[f["team_a"]].add(f["id"])

    upcoming = defaultdict(list)
    for f in sorted(fixtures, key=lambda f: (f["event"] or 99, f["kickoff_time"] or "")):
        if f["finished"] or f["event"] is None or f["event"] < next_gw or f["event"] >= next_gw + HORIZON_MAX:
            continue
        upcoming[f["team_h"]].append({"gw": f["event"], "opp": f["team_a"], "home": True})
        upcoming[f["team_a"]].append({"gw": f["event"], "opp": f["team_h"], "home": False})

    league = {}
    team_out = {tid: dict(teams[tid]) for tid in teams}
    player_out = []

    for pos in ("DEF", "MID"):
        q = [a for a in apps if a["pos"] == pos and a["q"] and included(a["pid"])]
        lr = sum(a["hit"] for a in q) / len(q) if q else 0.3
        qh = [a for a in q if a["home"]]; qa = [a for a in q if not a["home"]]
        lr_h = sum(a["hit"] for a in qh) / max(len(qh), 1)
        lr_a = sum(a["hit"] for a in qa) / max(len(qa), 1)
        league[pos] = {"rate": round(lr, 4), "home": round(lr_h, 4), "away": round(lr_a, 4),
                       "apps": len(q), "hits": sum(a["hit"] for a in q)}

        def shrunk(h, n):
            return (h + PLAYER_PRIOR * lr) / (n + PLAYER_PRIOR)

        prate = defaultdict(lambda: [0, 0])
        for a in q:
            prate[a["pid"]][0] += a["hit"]; prate[a["pid"]][1] += 1

        T = {tid: {"chances": 0, "hits": 0, "exp": 0.0, "h": [0, 0, 0.0], "a": [0, 0, 0.0]} for tid in teams}
        for a in q:
            h, n = prate[a["pid"]]
            expected = shrunk(h - a["hit"], n - 1)   # leave this game out
            t = T[a["opp"]]
            t["chances"] += 1; t["hits"] += a["hit"]; t["exp"] += expected
            v = t["a"] if a["home"] else t["h"]      # attacker home -> opponent away
            v[0] += 1; v[1] += a["hit"]; v[2] += expected

        for tid, t in T.items():
            idx = (t["hits"] + TEAM_PRIOR * lr) / (t["exp"] + TEAM_PRIOR * lr)

            def venue(v):
                if v[0] == 0:
                    return {"chances": 0, "hits": 0, "exp": 0, "index": round(idx, 3)}
                vi = (v[1] + VENUE_PRIOR * lr * idx) / (v[2] + VENUE_PRIOR * lr)
                return {"chances": v[0], "hits": v[1], "exp": round(v[2], 2), "index": round(vi, 3)}

            games = len(team_games[tid])
            team_out[tid][pos.lower()] = {
                "games": games, "chances": t["chances"], "hits": t["hits"],
                "expected": round(t["exp"], 2), "index": round(idx, 3),
                "home": venue(t["h"]), "away": venue(t["a"]),
            }

        def matchup(base, opp, home):
            tp = team_out[opp][pos.lower()]
            idx = (tp["away"] if home else tp["home"])["index"]  # player at home -> opponent away
            venue_adj = odds(lr_h if home else lr_a) / odds(lr)
            return prob(odds(base) * idx * venue_adj)

        for pid, p in players.items():
            if POSITIONS.get(p["element_type"]) != pos or not included(pid):
                continue
            lst = sorted(by_player.get(pid, []), key=lambda a: a["gw"])
            hits60, n60 = prate[pid] if pid in prate else (0, 0)
            base = shrunk(hits60, n60)
            tg = len(team_games[p["team"]])
            q_cur = sum(1 for a in lst if a["q"] and a["team"] == p["team"])
            reliability = q_cur / tg if tg else 0
            avail = p["chance_of_playing_next_round"]
            fx_out = []
            for u in upcoming.get(p["team"], []):
                ph = matchup(base, u["opp"], u["home"])
                start = reliability
                if u["gw"] == next_gw and avail is not None:
                    start *= avail / 100
                if p["status"] in ("u", "n"):
                    start = 0
                fx_out.append({"gw": u["gw"], "opp": u["opp"], "home": u["home"],
                               "p": round(ph, 3), "e": round(ph * start, 3)})
            q_actions = [a["actions"] for a in lst if a["q"]]
            player_out.append({
                "id": pid, "name": p["web_name"], "full": f"{p['first_name']} {p['second_name']}",
                "team": p["team"], "pos": pos, "price": p["now_cost"] / 10,
                "status": p["status"], "news": p["news"], "chance": avail,
                "apps60": n60, "hits60": hits60, "rate": hits60 / n60 if n60 else None,
                "hitsAll": season_hits[pid], "base": round(base, 3), "reliability": round(reliability, 3),
                "avgActions": round(sum(q_actions) / len(q_actions), 1) if q_actions else None,
                "nearMiss": sum(1 for a in lst if a["q"] and not a["hit"] and a["actions"] >= THRESHOLD[pos] - 2),
                "selected": p["selected_by_percent"],
                "games": [{"gw": a["gw"], "opp": a["opp"], "home": a["home"], "min": a["min"],
                           "act": a["actions"], "hit": a["hit"]} for a in lst],
                "fixtures": fx_out,
            })

    out = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lastGw": last_gw, "nextGw": next_gw, "finishedGws": finished_gws,
        "minMinutes": MIN_MINUTES, "midMinHits": MID_MIN_HITS, "threshold": THRESHOLD,
        "league": league, "unresolved": unresolved,
        "teams": [team_out[t] for t in sorted(team_out)], "players": player_out,
    }
    # sanity checks: refuse to publish something obviously broken
    problems = []
    if len(out["teams"]) != 20: problems.append(f"{len(out['teams'])} teams")
    if last_gw and sum(1 for p in player_out if p["pos"] == "DEF") < 100: problems.append("too few defenders")
    if last_gw and league["DEF"]["apps"] < 50 * len(finished_gws): problems.append("too few defender games")
    if problems:
        sys.exit("Not publishing, the data looks wrong: " + ", ".join(problems))

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    for pos, l in league.items():
        n = sum(1 for p in player_out if p["pos"] == pos)
        print(f"{pos}: {n} players, {l['apps']} games of 60+ min, {l['hits']} hits, "
              f"league rate {l['rate']:.1%} (home {l['home']:.1%}, away {l['away']:.1%})")
    print(f"GW{finished_gws[0] if finished_gws else '-'}-{last_gw}, next GW{next_gw}, unresolved {unresolved}")


if __name__ == "__main__":
    if "--fetch" in sys.argv:
        fetch()
    build()
