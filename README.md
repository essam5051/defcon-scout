# DEFCON Scout

An unofficial FPL tool: which defenders and defensive midfielders hit DEFCON, which teams allow it, transfer picks for the coming gameweeks, and a player comparison.

## How it updates

- `.github/workflows/update-data.yml` runs every 6 hours. It downloads FPL's public data, runs `scripts/build_data.py`, and saves the result as `data.json` on the `data` branch.
- `site/index.html` is the page. Netlify serves the `site` folder from the `main` branch and only redeploys when `main` changes.
- The page loads `data.json` from `https://raw.githubusercontent.com/<user>/<repo>/data/data.json`, so data updates never need a Netlify deploy.

## Running an update by hand

Actions tab → **Update DEFCON data** → **Run workflow**.

## Method, in short

- A DEFCON hit is a game where FPL awarded the DEFCON points (defenders 10 actions, midfielders 12 including recoveries).
- Only games of 60+ minutes count, for players and for the teams they face.
- Midfielders are included once they have 2+ DEFCON hits this season.
- Each team's "allowed" numbers compare the hits it gave up with what the players it faced usually manage, pulled toward average while samples are small.

Not affiliated with Fantasy Premier League or the Premier League.
