"""Analyzers. Pure functions: PlayerFight(s) in, Findings out.

Design principle: almost everything is *relative to top parses of the same
spec on the same boss and difficulty*. That avoids hard-coding spell IDs
that change every patch, and it automatically cancels out encounter-specific
effects (forced downtime, intermissions, unavoidable raid damage).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from statistics import median
from typing import Callable, Iterable

from .fetch import PlayerFight

HIGH, MEDIUM, LOW, INFO = 3, 2, 1, 0
SEVERITY_LABEL = {HIGH: "HOCH", MEDIUM: "MITTEL", LOW: "NIEDRIG", INFO: "INFO"}


@dataclass
class Settings:
    gap_threshold_s: float = 2.5  # gap between casts counted as downtime beyond this
    min_top: int = 2  # minimum benchmark players for comparisons
    cast_under_ratio: float = 0.75
    taken_over_ratio: float = 1.5
    taken_min_share: float = 0.03
    buff_uptime_gap_pp: float = 15.0


@dataclass
class Finding:
    severity: int
    category: str
    title: str
    detail: str = ""
    tip: str = ""
    impact: float = 0.0  # used to order findings within a severity
    rows: list[tuple] = field(default_factory=list)  # optional table rows


def fmt_t(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _norm_name(n: str) -> str:
    return re.sub(r"\s+", " ", (n or "").strip().lower())


def _per_minute(count: float, duration_s: float) -> float:
    return count / (duration_s / 60.0)


def _death_time(pf: PlayerFight) -> float | None:
    """Seconds into the fight of the first death, if any."""
    ts = [d.get("timestamp") for d in pf.deaths if d.get("timestamp") is not None]
    if not ts:
        return None
    return (min(ts) - pf.fight.start) / 1000.0


def _alive_s(pf: PlayerFight) -> float:
    dt = _death_time(pf)
    return max(1.0, dt if dt is not None else pf.duration_s)


# ============================================================ 1. performance


def analyze_performance(me: PlayerFight, tops: list[PlayerFight], s: Settings) -> list[Finding]:
    out: list[Finding] = []
    metric = "HPS" if me.player.role == "healer" else "DPS"
    total = me.healing_done_total if me.player.role == "healer" else me.damage_done_total
    mine = me.amount_per_s if me.amount_per_s else total / me.duration_s
    lines = [f"Dein {metric}: {mine:,.0f}"]
    if me.rank_percent is not None:
        lines.append(f"Parse: {me.rank_percent:.0f}%")
    top_amounts = [t.amount_per_s for t in tops if t.amount_per_s]
    if top_amounts:
        tm = median(top_amounts)
        lines.append(f"Median Top-{len(top_amounts)}: {tm:,.0f} ({mine / tm * 100:.0f}% davon)")
    out.append(Finding(INFO, "Performance", " · ".join(lines)))
    return out


# ================================================================ 2. deaths

HEAL_CONSUMABLE_RE = re.compile(r"healthstone|healing potion|heilstein|heiltrank", re.I)


def analyze_deaths(me: PlayerFight, tops: list[PlayerFight], s: Settings) -> list[Finding]:
    out: list[Finding] = []
    if not me.deaths:
        return out
    name_by_guid = {c.get("guid"): c.get("name", "") for c in me.casts}
    heal_guids = {g for g, n in name_by_guid.items() if HEAL_CONSUMABLE_RE.search(n or "")}

    for d in sorted(me.deaths, key=lambda x: x.get("timestamp", 0)):
        t = (d.get("timestamp", me.fight.start) - me.fight.start) / 1000.0
        pct = t / me.duration_s * 100
        kb = (d.get("killingBlow") or {}).get("name") or "unbekannt"
        used_heal = any(
            e.get("abilityGameID") in heal_guids and e.get("timestamp", 0) <= d.get("timestamp", 0)
            for e in me.cast_events
        )
        sev = HIGH if pct < 90 else MEDIUM
        lost = me.duration_s - t
        tip_parts = [
            "Öffne den Death Recap auf WCL: Welche Hits kamen in den letzten 5s?",
            "War ein Defensive verfügbar, der nicht genutzt wurde?",
        ]
        if not used_heal:
            tip_parts.append("Vor dem Tod wurde weder Healthstone noch Heiltrank benutzt.")
        out.append(
            Finding(
                sev,
                "Tod",
                f"Gestorben bei {fmt_t(t)} ({pct:.0f}% des Kampfes) durch '{kb}'",
                detail=f"{fmt_t(lost)} Kampfzeit verloren – das kostet meist mehr als jeder Rotationsfehler.",
                tip=" ".join(tip_parts),
                impact=lost,
            )
        )
    return out


# ============================================================== 3. activity


def activity_stats(pf: PlayerFight, gap_threshold_s: float) -> dict:
    """ABC ("always be casting") approximation from cast events."""
    alive = _alive_s(pf)
    end_ms = pf.fight.start + alive * 1000
    ts = sorted(
        e["timestamp"] for e in pf.cast_events if pf.fight.start <= e.get("timestamp", -1) <= end_ms
    )
    if not ts:
        return {"active_pct": 0.0, "first_cast_s": None, "gaps": [], "downtime_s": alive}
    points = [pf.fight.start] + ts + [end_ms]
    gaps = []
    downtime = 0.0
    for a, b in zip(points, points[1:]):
        g = (b - a) / 1000.0
        if g > gap_threshold_s:
            downtime += g - gap_threshold_s
            gaps.append(((a - pf.fight.start) / 1000.0, g))
    gaps.sort(key=lambda x: -x[1])
    return {
        "active_pct": max(0.0, 100.0 * (1 - downtime / alive)),
        "first_cast_s": (ts[0] - pf.fight.start) / 1000.0,
        "gaps": gaps,
        "downtime_s": downtime,
    }


def analyze_activity(me: PlayerFight, tops: list[PlayerFight], s: Settings) -> list[Finding]:
    out: list[Finding] = []
    st = activity_stats(me, s.gap_threshold_s)
    top_stats = [activity_stats(t, s.gap_threshold_s) for t in tops if t.cast_events]

    rows = [(fmt_t(start), f"{g:.1f}s") for start, g in st["gaps"][:5]]
    gap_text = ", ".join(f"{a} ({b})" for a, b in rows) or "–"

    if len(top_stats) >= s.min_top:
        top_med = median(x["active_pct"] for x in top_stats)
        diff = top_med - st["active_pct"]
        if diff > 3:
            sev = HIGH if diff > 10 else MEDIUM
            out.append(
                Finding(
                    sev,
                    "Aktivität",
                    f"Aktivität {st['active_pct']:.0f}% vs. {top_med:.0f}% bei Top-Spielern (−{diff:.0f} Pp.)",
                    detail=f"Längste Pausen (Zeitpunkt, Dauer): {gap_text}",
                    tip="Bei Bewegung Instant-Casts/Bewegungsfähigkeiten einplanen; "
                    "nach Mechaniken sofort weiter casten; Ziel-Swaps vorbereiten.",
                    impact=diff,
                )
            )
        else:
            out.append(
                Finding(INFO, "Aktivität", f"Aktivität {st['active_pct']:.0f}% – auf Top-Niveau ({top_med:.0f}%).")
            )
        fc_tops = [x["first_cast_s"] for x in top_stats if x["first_cast_s"] is not None]
        if st["first_cast_s"] is not None and fc_tops:
            fc_med = median(fc_tops)
            if st["first_cast_s"] - fc_med > 1.5:
                out.append(
                    Finding(
                        MEDIUM,
                        "Opener",
                        f"Erster Cast nach {st['first_cast_s']:.1f}s (Top: {fc_med:.1f}s)",
                        tip="Prepull-Cast/Prepot timen, sodass der erste Cast beim Pull landet.",
                        impact=st["first_cast_s"] - fc_med,
                    )
                )
    elif st["active_pct"] < 85:
        out.append(
            Finding(
                MEDIUM,
                "Aktivität",
                f"Aktivität nur {st['active_pct']:.0f}% (geschätzt, ohne Vergleichsdaten)",
                detail=f"Längste Pausen: {gap_text}",
                impact=100 - st["active_pct"],
            )
        )
    return out


# ================================================================= 4. casts

IGNORE_CAST_RE = re.compile(r"^(melee|auto shot|shoot)$", re.I)
# consumables are judged in analyze_utility / analyze_deaths, not as rotation
CONSUMABLE_RE = re.compile(r"potion|trank|healthstone|heilstein|phial|flask", re.I)


def _cast_index(pf: PlayerFight) -> dict[str, tuple[str, float]]:
    """key -> (display name, count). Keyed by normalized name so rank/variant IDs merge."""
    idx: dict[str, tuple[str, float]] = {}
    for c in pf.casts:
        name = c.get("name") or str(c.get("guid"))
        if IGNORE_CAST_RE.match(name) or CONSUMABLE_RE.search(name):
            continue
        k = _norm_name(name)
        prev = idx.get(k, (name, 0.0))[1]
        idx[k] = (name, prev + float(c.get("total", 0)))
    return idx


def analyze_casts(me: PlayerFight, tops: list[PlayerFight], s: Settings) -> list[Finding]:
    if len(tops) < s.min_top:
        return []
    mine = _cast_index(me)
    top_idx = [_cast_index(t) for t in tops]
    my_dur = _alive_s(me)
    keys = set(mine) | {k for ti in top_idx for k in ti}

    missing, under, over = [], [], []
    for k in keys:
        top_counts = [ti.get(k, ("", 0.0))[1] for ti in top_idx]
        top_cpm = [_per_minute(c, _alive_s(t)) for c, t in zip(top_counts, tops)]
        users = sum(1 for c in top_counts if c > 0)
        name = mine.get(k, (None, 0))[0] or next(ti[k][0] for ti in top_idx if k in ti)
        my_count = mine.get(k, (name, 0.0))[1]
        my_cpm = _per_minute(my_count, my_dur)
        med_cpm = median(top_cpm)
        med_count = median(top_counts)
        expected = med_cpm * my_dur / 60.0

        if my_count == 0 and users / len(tops) >= 0.6 and med_count >= 1:
            missing.append((name, med_count, med_cpm))
        elif med_count >= 1 and my_count < expected * s.cast_under_ratio and expected - my_count >= 1:
            under.append((name, my_count, expected, my_cpm, med_cpm))
        elif med_cpm >= 0.5 and my_cpm > med_cpm * 1.5 and users / len(tops) >= 0.6:
            over.append((name, my_cpm, med_cpm))

    out: list[Finding] = []
    for name, med_count, med_cpm in sorted(missing, key=lambda x: -x[1]):
        core = med_cpm >= 1.0
        out.append(
            Finding(
                HIGH if core else MEDIUM,
                "Rotation" if core else "Cooldowns",
                f"'{name}' nie benutzt – Top-Spieler: ~{med_count:.0f}× pro Kampf",
                tip="Talent/Build prüfen (evtl. anders geskillt) – sonst fest in Rotation/CD-Plan aufnehmen.",
                impact=med_count,
            )
        )
    for name, mc, exp, my_cpm, med_cpm in sorted(under, key=lambda x: -(x[2] - x[1]))[:8]:
        is_cd = med_cpm < 1.0  # used at most ~once per minute
        out.append(
            Finding(
                MEDIUM if (exp - mc) >= 2 or not is_cd else LOW,
                "Cooldowns" if is_cd else "Rotation",
                f"'{name}': {mc:.0f}× gecastet, bei deiner Kampfdauer wären ~{exp:.0f}× möglich",
                detail=f"{my_cpm:.1f}/min vs. {med_cpm:.1f}/min (Top-Median)",
                tip=(
                    "Cooldown direkt bei Verfügbarkeit nutzen oder bewusst auf Burst-Phasen planen – "
                    "Hauptsache keine Ladung verfallen lassen."
                    if is_cd
                    else "Priorität prüfen: diese Fähigkeit wird zu selten gedrückt."
                ),
                impact=exp - mc,
            )
        )
    for name, my_cpm, med_cpm in sorted(over, key=lambda x: -(x[1] / x[2]))[:5]:
        out.append(
            Finding(
                LOW,
                "Rotation",
                f"'{name}' deutlich öfter als Top-Spieler ({my_cpm:.1f}/min vs. {med_cpm:.1f}/min)",
                tip="Oft ein Filler, der stärkere Fähigkeiten verdrängt – Prioritätsliste prüfen.",
                impact=my_cpm / med_cpm,
            )
        )
    return out


# ========================================================== 5. damage taken


def _taken_index(pf: PlayerFight) -> dict[str, tuple[str, float]]:
    idx: dict[str, tuple[str, float]] = {}
    for e in pf.damage_taken:
        name = e.get("name") or str(e.get("guid"))
        k = _norm_name(name)
        prev = idx.get(k, (name, 0.0))[1]
        idx[k] = (name, prev + float(e.get("total", 0)))
    return idx


def analyze_damage_taken(me: PlayerFight, tops: list[PlayerFight], s: Settings) -> list[Finding]:
    mine = _taken_index(me)
    total = sum(v for _, v in mine.values()) or 1.0
    my_alive = _alive_s(me)
    out: list[Finding] = []

    if len(tops) < s.min_top:
        rows = sorted(mine.values(), key=lambda x: -x[1])[:5]
        if rows:
            out.append(
                Finding(
                    INFO,
                    "Schaden erhalten",
                    "Größte Schadensquellen: "
                    + ", ".join(f"{n} ({v / total * 100:.0f}%)" for n, v in rows),
                )
            )
        return out

    top_idx = [_taken_index(t) for t in tops]
    for k, (name, amount) in mine.items():
        share = amount / total
        if share < s.taken_min_share:
            continue
        my_ps = amount / my_alive
        top_ps = [ti.get(k, ("", 0.0))[1] / _alive_s(t) for ti, t in zip(top_idx, tops)]
        med = median(top_ps)
        zero_takers = sum(1 for v in top_ps if v == 0)
        if med == 0 and zero_takers / len(tops) >= 0.6:
            out.append(
                Finding(
                    HIGH if share >= 0.08 else MEDIUM,
                    "Vermeidbarer Schaden",
                    f"'{name}': {share * 100:.0f}% deines erhaltenen Schadens – die meisten Top-Spieler nehmen 0",
                    tip="Sehr wahrscheinlich vermeidbar: Mechanik im Dungeon-/Raid-Guide nachlesen "
                    "(Positionierung, Ausweichen, Timing).",
                    impact=share * 100 + 10,
                )
            )
        elif med > 0 and my_ps > med * s.taken_over_ratio:
            out.append(
                Finding(
                    MEDIUM if my_ps > med * 2 else LOW,
                    "Vermeidbarer Schaden",
                    f"'{name}': {my_ps / med:.1f}× so viel Schaden pro Sekunde wie Top-Spieler",
                    detail=f"{share * 100:.0f}% deines erhaltenen Schadens",
                    tip="Teilweise vermeidbar oder per Defensive/Positionierung reduzierbar.",
                    impact=share * 100 * (my_ps / med),
                )
            )
    return out


# ================================================================== 6. buffs


def _uptimes(pf: PlayerFight) -> dict[str, tuple[str, float, bool]]:
    """name-key -> (display name, uptime %, applied by the player themselves)."""
    tt = pf.buffs_total_time or (pf.fight.end - pf.fight.start)
    idx: dict[str, tuple[str, float, bool]] = {}
    for a in pf.buffs:
        name = a.get("name") or str(a.get("guid"))
        k = _norm_name(name)
        up = min(100.0, 100.0 * float(a.get("totalUptime", 0)) / tt)
        own = pf.self_buff_ids is None or a.get("guid") in pf.self_buff_ids
        prev = idx.get(k, (name, 0.0, False))
        idx[k] = (name, max(up, prev[1]), own or prev[2])
    return idx


def analyze_buffs(me: PlayerFight, tops: list[PlayerFight], s: Settings) -> list[Finding]:
    if len(tops) < s.min_top:
        return []
    mine = _uptimes(me)
    top_idx = [_uptimes(t) for t in tops]
    keys = {k for ti in top_idx for k in ti}
    out: list[Finding] = []
    for k in keys:
        vals = [ti[k][1] if k in ti else 0.0 for ti in top_idx]
        have = sum(1 for v in vals if v > 0)
        if have / len(tops) < 0.6:
            continue
        med = median(vals)
        if med < 30:
            continue
        name = next(ti[k][0] for ti in top_idx if k in ti)
        my = mine[k][1] if k in mine else 0.0
        # buffs that only ever come from other players (raid buffs, healer HoTs, ...)
        # depend on raid composition, not on the player's own play
        external = not any(ti[k][2] for ti in top_idx if k in ti)
        if external:
            if my == 0 and med >= 95:
                out.append(
                    Finding(
                        LOW,
                        "Raid-Buff",
                        f"Raid-Buff '{name}' fehlt (Top: {med:.0f}% Uptime)",
                        tip="Kommt von einer anderen Klasse – liegt an der Raid-Zusammensetzung, "
                        "nicht an deinem Spiel.",
                        impact=med / 10,
                    )
                )
            continue
        if my == 0 and med >= 95:
            out.append(
                Finding(
                    MEDIUM,
                    "Vorbereitung",
                    f"Dauerbuff '{name}' fehlt komplett (Top: {med:.0f}% Uptime)",
                    tip="Flask/Food/Augment Rune/Waffenöl?",
                    impact=med,
                )
            )
        elif med - my > s.buff_uptime_gap_pp:
            out.append(
                Finding(
                    MEDIUM if med - my > 30 else LOW,
                    "Buff-Uptime",
                    f"'{name}': {my:.0f}% Uptime vs. {med:.0f}% bei Top-Spielern",
                    tip="Selbst erzeugter Buff/Proc: Rotation darauf ausrichten, "
                    "ihn nicht auslaufen zu lassen.",
                    impact=med - my,
                )
            )
    return out


# ============================================================ 7. utility

COMBAT_POT_RE = re.compile(r"potion|trank", re.I)
NON_COMBAT_POT_RE = re.compile(r"heal|mana|heil", re.I)


def analyze_utility(me: PlayerFight, tops: list[PlayerFight], s: Settings) -> list[Finding]:
    out: list[Finding] = []
    def pots(pf: PlayerFight) -> float:
        return sum(
            c.get("total", 0)
            for c in pf.casts
            if COMBAT_POT_RE.search(c.get("name", "")) and not NON_COMBAT_POT_RE.search(c.get("name", ""))
        )

    top_pots = [pots(t) for t in tops]
    hint = f" (Top-Spieler: ~{median(top_pots):.0f})" if len(top_pots) >= s.min_top else ""
    if pots(me) == 0 and me.duration_s > 60:
        out.append(
            Finding(
                MEDIUM,
                "Consumables",
                "Kein Kampftrank im Kampf erkannt" + hint,
                tip="Mindestens einen Trank pro Pull, idealerweise synchron mit deinen Burst-Cooldowns/Bloodlust.",
                impact=5,
            )
        )
    top_kicks = [t.interrupts for t in tops]
    if len(top_kicks) >= s.min_top and median(top_kicks) >= 1 and me.interrupts == 0:
        out.append(
            Finding(
                LOW,
                "Utility",
                f"Keine Unterbrechungen (Top-Spieler: ~{median(top_kicks):.0f})",
                tip="Falls nicht eingeteilt: ok. Sonst Kick-Rotation mit dem Raid absprechen.",
            )
        )
    elif me.interrupts:
        out.append(Finding(INFO, "Utility", f"{me.interrupts} Unterbrechung(en)"))
    return out


# ======================================================== 8. gear & stats

SECONDARIES = ("Crit", "Haste", "Mastery", "Vers")
_STAT_PATTERNS = {
    "Crit": re.compile(r"^crit", re.I),
    "Haste": re.compile(r"^haste", re.I),
    "Mastery": re.compile(r"^mastery", re.I),
    "Vers": re.compile(r"^versatility(damagedone)?$", re.I),
}
SLOT_NAMES = {
    0: "Kopf", 1: "Hals", 2: "Schulter", 4: "Brust", 5: "Taille", 6: "Beine", 7: "Füße",
    8: "Handgelenke", 9: "Hände", 10: "Ring 1", 11: "Ring 2", 12: "Trinket 1", 13: "Trinket 2",
    14: "Umhang", 15: "Haupthand", 16: "Nebenhand",
}
TRINKET_SLOTS = {12, 13}


def secondary_ratings(ci: dict | None) -> dict[str, float]:
    """Secondary stat ratings from a combatantinfo event.

    Tolerant to field naming: stats may be flat (critMelee, hasteSpell, versatilityDamageDone…)
    or nested under "stats". For variants (melee/ranged/spell) the highest value wins.
    """
    if not ci:
        return {}
    pools = [ci] + ([ci["stats"]] if isinstance(ci.get("stats"), dict) else [])
    out: dict[str, float] = {}
    for pool in pools:
        for key, val in pool.items():
            if isinstance(val, dict):  # e.g. {"min": x, "max": y}
                val = val.get("max", val.get("value"))
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                continue
            for stat, pat in _STAT_PATTERNS.items():
                if pat.match(key):
                    out[stat] = max(out.get(stat, 0.0), float(val))
    return out if sum(out.values()) > 0 else {}


def stat_shares(ci: dict | None) -> dict[str, float]:
    r = secondary_ratings(ci)
    total = sum(r.values())
    return {k: 100.0 * r.get(k, 0.0) / total for k in SECONDARIES} if total else {}


def _gear(ci: dict | None) -> dict[int, dict]:
    gear = (ci or {}).get("gear") or []
    out = {}
    for i, item in enumerate(gear):
        if not isinstance(item, dict) or not item.get("id"):
            continue
        out[int(item.get("slot", i))] = item
    return out


def avg_item_level(ci: dict | None) -> float | None:
    lv = [g.get("itemLevel", 0) for g in _gear(ci).values() if (g.get("itemLevel") or 0) > 1]
    return sum(lv) / len(lv) if lv else None


def analyze_gear(me: PlayerFight, tops: list[PlayerFight], s: Settings) -> list[Finding]:
    out: list[Finding] = []
    if not me.combatant_info:
        return out
    top_ci = [t.combatant_info for t in tops if t.combatant_info]
    enough = len(top_ci) >= s.min_top

    # --- item level (context, not an action item)
    my_il = avg_item_level(me.combatant_info)
    top_il = [x for x in (avg_item_level(c) for c in top_ci) if x]
    if my_il:
        txt = f"Ø Itemlevel {my_il:.1f}"
        if top_il:
            txt += f" (Top-Median {median(top_il):.1f})"
        out.append(Finding(INFO, "Gear", txt))

    # --- secondary distribution
    mine = stat_shares(me.combatant_info)
    top_shares = [x for x in (stat_shares(c) for c in top_ci) if x]
    if mine:
        line = " · ".join(f"{k} {mine[k]:.0f}%" for k in SECONDARIES)
        if len(top_shares) >= s.min_top:
            med = {k: median(t[k] for t in top_shares) for k in SECONDARIES}
            line += "  |  Top: " + " · ".join(f"{k} {med[k]:.0f}%" for k in SECONDARIES)
            out.append(Finding(INFO, "Stats", "Verteilung Sekundärwerte: " + line))
            devs = sorted(((k, mine[k] - med[k]) for k in SECONDARIES), key=lambda x: -abs(x[1]))
            big = [(k, d) for k, d in devs if abs(d) >= 8]
            if big:
                worst = abs(big[0][1])
                desc = ", ".join(f"{k} {'+' if d > 0 else '−'}{abs(d):.0f} Pp." for k, d in big)
                out.append(
                    Finding(
                        MEDIUM if worst >= 15 else LOW,
                        "Stats",
                        f"Deine Stat-Verteilung weicht von den Top-Spielern ab: {desc}",
                        detail="Top-Spieler sind kein Gesetz – Build, Hero-Talente und Bosstyp (ST/AoE) "
                        "verschieben die Gewichte.",
                        tip="Charakter in Raidbots (Top Gear / Droptimizer) mit deinem Build simmen, "
                        "statt nach fester Stat-Priorität umzuschmieden oder Items zu tauschen.",
                        impact=worst,
                    )
                )
        else:
            out.append(Finding(INFO, "Stats", "Verteilung Sekundärwerte: " + line))

    if not enough:
        return out

    # --- enchants per slot
    my_gear = _gear(me.combatant_info)
    top_gears = [_gear(c) for c in top_ci]
    missing_ench = []
    for slot, sname in SLOT_NAMES.items():
        if slot not in my_gear:
            continue
        enchanted = [bool(g.get(slot, {}).get("permanentEnchant")) for g in top_gears if slot in g]
        if enchanted and sum(enchanted) / len(enchanted) >= 0.6 and not my_gear[slot].get("permanentEnchant"):
            missing_ench.append(sname)
    if missing_ench:
        out.append(
            Finding(
                HIGH if len(missing_ench) >= 2 else MEDIUM,
                "Vorbereitung",
                f"Enchant fehlt: {', '.join(missing_ench)}",
                detail="Top-Spieler deiner Spec haben diese Slots verzaubert.",
                tip="Günstigster DPS-Gewinn überhaupt – vor dem nächsten Raid nachholen.",
                impact=10 * len(missing_ench),
            )
        )

    # --- weapon oil / sharpening stone
    mh = my_gear.get(15)
    top_temp = [bool(g[15].get("temporaryEnchant")) for g in top_gears if 15 in g]
    if mh and top_temp and sum(top_temp) / len(top_temp) >= 0.6 and not mh.get("temporaryEnchant"):
        out.append(
            Finding(
                MEDIUM,
                "Vorbereitung",
                "Kein Waffenöl/Wetzstein auf der Haupthand",
                tip="Temporäre Waffenverzauberung vor dem Pull auftragen (hält 1–2 Stunden).",
                impact=8,
            )
        )

    # --- gems
    def gem_count(g: dict[int, dict]) -> int:
        return sum(len([x for x in (it.get("gems") or []) if x and x.get("id")]) for it in g.values())

    my_gems = gem_count(my_gear)
    med_gems = median(gem_count(g) for g in top_gears)
    if med_gems - my_gems >= 1:
        out.append(
            Finding(
                LOW,
                "Vorbereitung",
                f"{my_gems} Edelsteine gesockelt, Top-Spieler: ~{med_gems:.0f}",
                tip="Leere Sockel füllen; fehlende Sockel ggf. per Sockel-Item nachrüsten.",
                impact=med_gems - my_gems,
            )
        )

    # --- trinkets popular among top players
    my_ids = {g.get("id") for g in my_gear.values()}
    counts: dict[int, int] = {}
    for g in top_gears:
        for tid in {g[sl].get("id") for sl in TRINKET_SLOTS if sl in g}:
            counts[tid] = counts.get(tid, 0) + 1
    for tid, n in sorted(counts.items(), key=lambda x: -x[1]):
        if n >= 2 and n / len(top_gears) >= 0.5 and tid not in my_ids:
            out.append(
                Finding(
                    LOW,
                    "Gear",
                    f"Beliebtes Trinket bei Top-Spielern ({n}/{len(top_gears)}): "
                    f"https://www.wowhead.com/item={tid}",
                    tip="Kein Muss – aber ein Kandidat für Droptimizer/Vault, wenn erreichbar.",
                    impact=n,
                )
            )
    return out


# =============================================================== registry

Analyzer = Callable[[PlayerFight, list[PlayerFight], Settings], list[Finding]]

ANALYZERS: list[Analyzer] = [
    analyze_performance,
    analyze_deaths,
    analyze_activity,
    analyze_casts,
    analyze_damage_taken,
    analyze_buffs,
    analyze_utility,
    analyze_gear,
]

# Spec-specific rules: key is (class, spec) as WCL names them, e.g. ("Mage", "Fire").
# Add your own analyzers here, e.g. checking a specific proc is never wasted.
SPEC_RULES: dict[tuple[str, str], list[Analyzer]] = {}


def run_all(me: PlayerFight, tops: Iterable[PlayerFight], settings: Settings | None = None) -> list[Finding]:
    s = settings or Settings()
    tops = list(tops)
    findings: list[Finding] = []
    analyzers = ANALYZERS + SPEC_RULES.get((me.player.class_name or "", me.player.spec or ""), [])
    for fn in analyzers:
        findings.extend(fn(me, tops, s))
    findings.sort(key=lambda f: (-f.severity, -f.impact))
    return findings
