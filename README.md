# wcl-coach

CLI tool that pulls a Warcraft Logs report via the official v2 GraphQL API, analyzes **one character** and prints a prioritized list of what to improve. A separate [raid mode](#raid-mode) checks **every player** of a report for deaths, avoidable damage and missing consumables, and a local [UI](#ui) shows both – plus damage, healing, buffs, debuffs, kicks and dispels per character – with the original Wowhead tooltips.

The key idea: almost every check compares you against the **top parses of your own spec on the same boss and difficulty**. No hard-coded spell IDs that break every patch, and encounter quirks (forced downtime, intermissions, unavoidable raid damage) cancel out automatically.

## What it checks

| Area | How |
|---|---|
| Performance | Your DPS/HPS, parse %, ratio to top-N median |
| Deaths | Time, killing blow, combat time lost, whether a Healthstone/healing potion was used before dying |
| Activity (ABC) | Gaps between casts above `--gap` seconds → active %, longest gaps with timestamps, opener delay; time after death is excluded |
| Rotation / cooldowns | Casts per minute per ability vs. top median: abilities you never press, cooldowns you could have used more often, fillers you overuse |
| Avoidable damage | Damage taken per ability vs. top players – abilities they take ~0 from are flagged as very likely avoidable |
| Buffs / prep | Persistent self-applied buffs (flask, food, rune, …) missing, procs/self-buffs with clearly lower uptime. Buffs that only come from other players (raid buffs, healer HoTs) are not counted against you; a missing raid buff is only noted as low priority |
| Gear & secondary stats | Snapshot at pull (`combatantinfo`): secondary distribution (Crit/Haste/Mastery/Vers) vs. top median, missing enchants per slot, weapon oil, gem count, trinkets popular among top players (Wowhead link), avg item level |
| Consumables / utility | No combat potion detected, interrupts |
| Recurring patterns | With `--fight kills` or `all`: issues that repeat across pulls (your actual habits) |

Severity: **HOCH** → **MITTEL** → **NIEDRIG**, sorted by impact within each level.

## Setup

1. Create an API client at <https://www.warcraftlogs.com/api/clients/> (any name, redirect URL e.g. `http://localhost`). Copy client ID and secret.
2. Install:
   ```bash
   cd wcl-coach
   python -m venv .venv && source .venv/bin/activate
   pip install -e .
   cp .env.example .env   # fill in WCL_CLIENT_ID, WCL_CLIENT_SECRET, optionally WCL_COACH_PLAYER
   ```
   `.env` is read from the current directory or `~/.config/wcl-coach/.env`. Plain environment variables work too.

## Usage

```bash
# Last kill in the report
wcl-coach analyze https://www.warcraftlogs.com/reports/AbCdEf1234567890 -p <charname>

# Specific fight (also picked up from ?fight=12 in the URL)
wcl-coach analyze AbCdEf1234567890 -p <charname> -f 12

# All pulls of one boss, only its wipes, or an ID range (see "Selecting fights")
wcl-coach analyze AbCdEf1234567890 -p <charname> -f sszorak
wcl-coach analyze AbCdEf1234567890 -p <charname> -f "twin fangs:wipes"
wcl-coach analyze AbCdEf1234567890 -p <charname> -f 23-41

# All kills of the evening + recurring patterns + Markdown export
# (a bare file name is saved to reports/, which is git-ignored)
wcl-coach analyze AbCdEf1234567890 -p <charname> -f kills --md raid-2026-10-17.md

# Whole raid: who died to what, who stood in bad, who forgot flask/food
wcl-coach raid AbCdEf1234567890 --md raid-2026-10-17.md

# List boss pulls / check API budget
wcl-coach fights AbCdEf1234567890
wcl-coach quota

# Classic / SoD logs
wcl-coach --site classic analyze <code> -p Name
```

### Selecting fights (`-f` / `--fight`)

| Value | Selects |
|---|---|
| *(none)* | `?fight=` from the URL, otherwise `last-kill` |
| `last-kill` | last kill (falls back to the last pull) |
| `last` | last pull, kill or wipe |
| `kills` | every kill in the report |
| `wipes` | every wipe in the report |
| `all` | every boss pull, incl. wipes |
| `12` · `3,7,12` | specific fight IDs (`wcl-coach fights <report>` lists them) |
| `23-41` | ID range, inclusive |
| `sszorak` · `nek,sszorak` | every pull of bosses whose name contains the text (case-insensitive) |
| `sszorak:kills` · `sszorak:wipes` | only the kills / only the wipes – also for ranges, e.g. `23-41:wipes` |

Parts can be mixed with commas, e.g. `-f 5,29,40-41` or `-f "kith:kills,sszorak:wipes"`. Notes:

- Quote names with apostrophes or spaces: `-f "kith'ix"`, `-f "lost explorers"` – or just use an unambiguous fragment like `kith`.
- Generic fragments match several bosses (`the` → *Nek'zali the Soulcoiler* and *The Lost Explorers*). The full name of a boss only matches that boss.
- A boss name selects kills **and** wipes – add `:kills` or `:wipes` for only one of them.
- `all`, `kills`, `wipes`, `last` and `last-kill` are keywords and are never matched as boss names.
- Many pulls cost many API points (each one fetches top players too) – check `wcl-coach quota` first. Very short wipes add little and skew the recurring-patterns summary; picking the longer attempts is usually more useful.

### Markdown reports

`--md <file>` writes the full result (incl. the recurring-patterns summary for multiple fights) as Markdown. A bare file name goes to `reports/` in the current directory (created if needed, git-ignored); a path with a directory, e.g. `--md ~/Desktop/raid.md`, is used as given.

Options: `--top N` benchmark players (default 5, `0` = only absolute checks), `--gap 2.5` downtime threshold in seconds, `--no-cache`.

## Raid mode

```bash
wcl-coach raid <report>                          # every pull (default -f all)
wcl-coach raid <report> -f sszorak --md prog.md  # one boss, saved to reports/prog.md
wcl-coach raid <report> -p <char1>,<char2>       # show only these players
wcl-coach raid <report> --ignore "Mindsting,Tempest"
```

Damage and healing done never make something a fail – raid mode is about **who failed where**. The raid is its own benchmark, so no top logs are fetched. (Damage, healing, auras, kicks and dispels are still collected as plain numbers for the [UI](#ui).)

| Check | How |
|---|---|
| Deaths | Who died when and to what, first death of each pull. On a wipe, deaths after the pull was lost don't count as fails (see below) |
| Avoidable damage | Damage from an enemy ability that at most half of the non-tanks took. Shows hits, amount, share of the player's damage taken and how many players were hit |
| Preparation | No flask, no food buff, no combat potion – only flagged if at least half the raid had one (detection is name-based) |
| Interrupts | Kicks per player; kicked enemy spells that still went through |

The report has four parts: a **fail overview** (one row per player, worst first), the **most common causes** across pulls (killing blows and avoidable abilities with who got hit), every fail **per player** with boss and pull, and a short summary **per pull**.

How the heuristics decide:

- **When a wipe counts as lost:** once 20% of the raid (at least 3 players) dies within 10 s, half the raid is dead, or the pull ends within 15 s. Deaths from there on are listed as one "Wipe ab …" line with their causes, not as individual fails. Damage tables also stop at that point, so survivors aren't blamed for the collapse.
- **Tanks** are excluded from avoidable damage (busters, frontals and boss melee hit them by design).
- **Own and friendly damage** (Burning Rush, Refraction, Blessing of Dawn, …) is filtered by source – only `Boss`/`NPC` damage counts. `Melee` never counts: adds hitting a dps is a tanking question.
- **Limits:** mechanics that target a few random players, soaks and melee-only mechanics look like "most players took nothing". The output shows `x/y Spieler getroffen` so you can judge – known false positives go into `--ignore`.

**API cost:** roughly 75 points per pull with 20+ players (out of 3600 per hour), so a full evening of 25 pulls uses about half the hourly budget. Cached pulls are free on re-runs.

## UI

```bash
pip install -e ".[ui]"
wcl-coach ui                  # opens http://localhost:8080 in your browser
```

1. Paste the report URL → **Laden** → pick the pulls → **Auswerten**. The raid analysis runs once for the whole raid.
   - **Boss**: all bosses or one of them, with how often it was pulled and killed.
   - **Kills + Wipes / Kills / Wipes**: e.g. every wipe on one boss to see what keeps killing you in progress.
   - **Pull**: all matching pulls or a single one.
2. Pick a character in the **Charakter** dropdown (or click a row in the overview). The most important points come first: the same problem across pulls is grouped ("Tod durch 'Tempest' · 2× (#24, #27)") with every pull listed below.
3. **Priorität** filters HOCH / MITTEL / NIEDRIG.
4. **Statistik** shows what the character did, for all selected pulls together or one pull:

   | Tab | Shows |
   |---|---|
   | Schaden / Heilung | Total, DPS / HPS, per ability with share, hits and crit rate (healing: overheal); pets as their own rows |
   | Erhaltener Schaden | Per ability, abilities flagged as avoidable marked; own and friendly damage last |
   | Buffs / Debuffs | Uptime and applications; debuffs split into enemy and own/raid (Forbearance, Exhaustion) |
   | Zauber | Casts and casts per minute |
   | Kicks / Dispels | Which enemy spells were interrupted, which auras removed |
   | Tode | Time, killing blow, the last hits before each death |

   On a wipe the numbers stop when the pull was lost (see raid mode). Damage and healing per ability are loaded when you pick the character – about 2 API points per pull, once.
5. **Detail-Analyse** runs the single-player comparison against top parses (rotation, cooldowns, buffs, gear, stats) for that character – on demand, since it costs considerably more API points.

Every ability, buff and item is a Wowhead link: hovering shows the **original in-game tooltip** with icon and description. **DE / EN** in the header switches the tooltip language; with DE, ability names on the page are shown in German too.

- Without credentials, a dialog asks for the WCL client ID and secret and stores them in `~/.config/wcl-coach/.env`. The stored secret is never sent to the browser.
- Links like `http://localhost:8080/?report=<code>&fight=sszorak:wipes&char=<charname>` load straight into a result (`fight` takes the same values as `-f` and sets the selects).
- The UI listens on `127.0.0.1` only. `--host 0.0.0.0` makes it reachable from your network – anyone there could then spend your API budget. `--port`, `--no-browser` (server only), `--site` and `--no-cache` work as usual.
- Tooltips come from wowhead.com and need internet access; they assume retail (`--site retail`).

## Notes

- **API budget:** the public API has an hourly point budget. Each benchmark player costs a few queries; responses are cached under `~/.cache/wcl-coach` (reports are immutable), so re-runs are nearly free. Rankings are always fetched fresh.
- **Activity is relative:** channeled spells show up as "gaps" in cast events. That's why the verdict compares against top players of the same spec instead of an absolute target.
- **Secondary stats** are rating values at pull time (no procs). A deviation from top players is a hint to sim (Raidbots Top Gear / Droptimizer), not a rule – build and boss type shift weights. Field names are parsed by pattern, flat or nested under `stats`.
- **Buff comparisons** only judge buffs the player applies themselves (detected from buff events and the pull snapshot). Auras from other classes, e.g. a shaman's *Earthliving Weapon*, depend on raid composition and are ignored, or flagged as low-priority "Raid-Buff" if top players always have them.
- **Wipes:** analysis works, but there's no parse; benchmark comparisons still use kill logs from top players.

## Extending with spec-specific rules

Generic checks catch most things. For spec details (e.g. a proc that must never be wasted), register an analyzer in `wcl_coach/analysis.py`:

```python
def fire_mage_rules(me, tops, settings):
    # me.cast_events, me.buffs, me.casts ... → return [Finding(...)]
    return []

SPEC_RULES[("Mage", "Fire")] = [fire_mage_rules]
```

## Tests

```bash
pip install -e ".[dev,ui]" && pytest
```
The suite runs fully offline: analyzers on synthetic data plus an end-to-end run against a fake API (including event pagination). The UI tests use NiceGUI's simulated user (no browser) and are skipped without `.[ui]`.

## Layout

```
wcl_coach/
  api.py       OAuth client-credentials, GraphQL, disk cache, retry/backoff
  fetch.py     queries → dataclasses (Fight, Player, PlayerFight), top-N benchmark
  analysis.py  pure analyzers → Findings, SPEC_RULES hook
  raid.py      raid mode: deaths, avoidable damage, consumables, kicks for every player
  pipeline.py  fight selection and fetch→analyze loops shared by CLI and UI
  render.py    terminal + Markdown output
  ui.py        NiceGUI web UI with Wowhead tooltips
  cli.py       argparse entry point, multi-fight recurring summary
tests/
```
