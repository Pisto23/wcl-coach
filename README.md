# wcl-coach

CLI tool that pulls a Warcraft Logs report via the official v2 GraphQL API, analyzes **one character** and prints a prioritized list of what to improve.

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

# All pulls of one boss, or an ID range (see "Selecting fights")
wcl-coach analyze AbCdEf1234567890 -p <charname> -f sszorak
wcl-coach analyze AbCdEf1234567890 -p <charname> -f 23-41

# All kills of the evening + recurring patterns + Markdown export
# (a bare file name is saved to reports/, which is git-ignored)
wcl-coach analyze AbCdEf1234567890 -p <charname> -f kills --md raid-2026-10-17.md

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
| `all` | every boss pull, incl. wipes |
| `12` · `3,7,12` | specific fight IDs (`wcl-coach fights <report>` lists them) |
| `23-41` | ID range, inclusive |
| `sszorak` · `nek,sszorak` | every pull of bosses whose name contains the text (case-insensitive) |

Parts can be mixed with commas, e.g. `-f 5,29,40-41` or `-f kith,sszorak`. Notes:

- Quote names with apostrophes or spaces: `-f "kith'ix"`, `-f "lost explorers"` – or just use an unambiguous fragment like `kith`.
- Generic fragments match several bosses (`the` → *Nek'zali the Soulcoiler* and *The Lost Explorers*).
- A boss name selects kills **and** wipes. For just the kill, use its ID.
- `all`, `kills`, `last` and `last-kill` are keywords and are never matched as boss names.
- Many pulls cost many API points (each one fetches top players too) – check `wcl-coach quota` first. Very short wipes add little and skew the recurring-patterns summary; picking the longer attempts is usually more useful.

### Markdown reports

`--md <file>` writes the full result (incl. the recurring-patterns summary for multiple fights) as Markdown. A bare file name goes to `reports/` in the current directory (created if needed, git-ignored); a path with a directory, e.g. `--md ~/Desktop/raid.md`, is used as given.

Options: `--top N` benchmark players (default 5, `0` = only absolute checks), `--gap 2.5` downtime threshold in seconds, `--no-cache`.

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
pip install -e ".[dev]" && pytest
```
The suite runs fully offline: analyzers on synthetic data plus an end-to-end run against a fake API (including event pagination).

## Layout

```
wcl_coach/
  api.py       OAuth client-credentials, GraphQL, disk cache, retry/backoff
  fetch.py     queries → dataclasses (Fight, Player, PlayerFight), top-N benchmark
  analysis.py  pure analyzers → Findings, SPEC_RULES hook
  render.py    terminal + Markdown output
  cli.py       argparse entry point, multi-fight recurring summary
tests/
```
