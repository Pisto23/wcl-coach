"""Output: colored terminal text and Markdown."""
from __future__ import annotations

import os
import sys
from collections import Counter

from .analysis import HIGH, INFO, LOW, MEDIUM, SEVERITY_LABEL, Finding, fmt_t
from .fetch import Fight, PlayerFight
from .raid import AVOIDABLE, DEATH, PREP, FightReport, summarize, top_causes

_COLORS = {HIGH: "\033[91m", MEDIUM: "\033[93m", LOW: "\033[96m", INFO: "\033[90m"}
_RESET, _BOLD = "\033[0m", "\033[1m"

# Markdown has no colors – inline HTML renders in VS Code, Obsidian, Typora etc.
SEVERITY_BG = {HIGH: "#f8a5a5", MEDIUM: "#ffe066", LOW: "#8ce99a"}
STATS_BG = "#a5d8ff"


def _md_hl(text: str, bg: str) -> str:
    return f'<span style="background:{bg};color:#000;padding:0 4px;border-radius:3px">{text}</span>'


def md_severity(sev: int) -> str:
    label = f"[{SEVERITY_LABEL[sev]}]"
    return _md_hl(label, SEVERITY_BG[sev]) if sev in SEVERITY_BG else label


def _use_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _fight_result(f: Fight) -> str:
    return "Kill" if f.kill else f"Wipe ({f.boss_pct:.0f}%)" if f.boss_pct is not None else "Wipe"


def _header(me: PlayerFight, n_tops: int) -> list[str]:
    p, f = me.player, me.fight
    return [
        f"{p.name} – {p.spec or '?'} {p.class_name or '?'} ({p.role or '?'})",
        f"{f.name} · {_fight_result(f)} · {int(f.duration_s // 60)}:{int(f.duration_s % 60):02d} · Report {me.report_code}#{f.id}",
        f"Vergleich mit {n_tops} Top-Parses derselben Spec" if n_tops else "Kein Top-Vergleich (nur absolute Checks)",
    ]


def to_terminal(me: PlayerFight, tops: list[PlayerFight], findings: list[Finding]) -> str:
    c = _use_color()
    b, r = (_BOLD, _RESET) if c else ("", "")
    lines = [b + h + r if i == 0 else h for i, h in enumerate(_header(me, len(tops)))]
    lines.append("")

    actionable = [f for f in findings if f.severity > INFO]
    infos = [f for f in findings if f.severity == INFO]
    for f in infos:
        lines.append(f"  · {f.category}: {f.title}")
    lines.append("")

    if not actionable:
        lines.append("Keine Auffälligkeiten gefunden – sauberer Pull. 👍")
        return "\n".join(lines)

    counts = Counter(f.severity for f in actionable)
    lines.append(
        b
        + "Verbesserungspunkte ("
        + ", ".join(f"{counts[s]}× {SEVERITY_LABEL[s]}" for s in (HIGH, MEDIUM, LOW) if counts[s])
        + ")"
        + r
    )
    for i, f in enumerate(actionable, 1):
        col = _COLORS[f.severity] if c else ""
        lines.append(f"\n{i:>2}. {col}[{SEVERITY_LABEL[f.severity]}]{r} {b}{f.category}{r}: {f.title}")
        if f.detail:
            lines.append(f"    {f.detail}")
        if f.tip:
            lines.append(f"    → {f.tip}")
    return "\n".join(lines)


def to_markdown(me: PlayerFight, tops: list[PlayerFight], findings: list[Finding]) -> str:
    h = _header(me, len(tops))
    md = [f"# {h[0]}", "", f"_{h[1]}_  ", f"_{h[2]}_", ""]
    infos = [f for f in findings if f.severity == INFO]
    if infos:
        md += ["## Überblick", ""] + [
            f"- **{f.category}:** " + (_md_hl(f.title, STATS_BG) if f.category == "Stats" else f.title)
            for f in infos
        ] + [""]
    actionable = [f for f in findings if f.severity > INFO]
    md += ["## Verbesserungspunkte", ""]
    if not actionable:
        md.append("Keine Auffälligkeiten gefunden.")
    for i, f in enumerate(actionable, 1):
        md.append(f"### {i}. {md_severity(f.severity)} {f.category}: {f.title}")
        if f.detail:
            md.append(f.detail + "  ")
        if f.tip:
            md.append(f"→ {f.tip}")
        md.append("")
    if tops:
        md += ["## Benchmark-Spieler", ""]
        for t in tops:
            amt = f"{t.amount_per_s:,.0f}" if t.amount_per_s else "?"
            md.append(f"- {t.player.name} – {amt} – report `{t.report_code}` fight {t.fight.id}")
    return "\n".join(md) + "\n"


# ---------------------------------------------------------------- raid mode


def _n(x: int) -> str:
    return str(x) if x else "–"


def _table(head: tuple[str, ...], rows: list[tuple[str, ...]], markdown: bool) -> list[str]:
    if markdown:
        return [f"| {' | '.join(head)} |", "|" + "---|" * len(head)] + [f"| {' | '.join(r)} |" for r in rows]
    widths = [max(len(x) for x in col) for col in zip(head, *rows)]
    return ["  " + "  ".join(x.ljust(w) for x, w in zip(row, widths)).rstrip() for row in (head, *rows)]


def raid_summary(title: str, reports: list[FightReport], only: set[str] | None = None, markdown: bool = False) -> str:
    """Who died, who stood in what, who came unprepared – overview, per player, per pull."""
    c = not markdown and _use_color()
    b, r = (_BOLD, _RESET) if c else ("", "")
    li = "- " if markdown else "  "

    def head(level: int, text: str) -> str:
        return f"{'#' * level} {text}" if markdown else b + text + r

    def badge(sev: int) -> str:
        if markdown:
            return md_severity(sev)
        return f"{_COLORS[sev] if c else ''}[{SEVERITY_LABEL[sev]}]{r}"

    players = summarize(reports, only)
    kills = sum(1 for x in reports if x.fight.kill)
    sub = (
        f"{len(reports)} {'Kampf' if len(reports) == 1 else 'Kämpfe'} ({kills} Kills, {len(reports) - kills} Wipes)"
        f" · {len(players)} Spieler · Vergleichsbasis: der eigene Raid"
    )
    lines = [head(1, f"Raid-Auswertung: {title}"), "", f"_{sub}_" if markdown else sub, ""]

    lines += [head(2, "Fail-Übersicht"), ""]
    rows = []
    for ps in players:
        prep = Counter(f.subject for f in ps.fails if f.category == PREP)
        rows.append((
            ps.player.name, str(ps.pulls), _n(ps.count(DEATH)), _n(ps.first_deaths), _n(ps.count(AVOIDABLE)),
            " · ".join(f"{k} {v}×" for k, v in sorted(prep.items())) or "–", _n(ps.kicks),
        ))
    lines += _table(("Spieler", "Pulls", "Tode", "davon erster", "Vermeidbarer Schaden", "Vorbereitung", "Kicks"),
                    rows, markdown)

    causes = [x for x in top_causes(reports, only) if sum(x[2].values()) >= 2]
    if causes:
        lines += ["", head(2, "Häufigste Ursachen"), ""]
        for cat, subject, who in causes:
            what = f"**'{subject}'**" if markdown else f"'{subject}'"
            kind = "Tode" if cat == DEATH else "vermeidbarer Schaden"
            people = ", ".join(f"{name} {k}×" for name, k in who.most_common())
            lines.append(f"{li}{what} – {sum(who.values())}× {kind}: {people}")

    lines += ["", head(2, "Pro Spieler")]
    clean = []
    for ps in players:
        if not ps.fails:
            clean.append(ps.player.name)
            continue
        p = ps.player
        lines += ["", head(3, f"{p.name} – {p.spec or '?'} {p.class_name or '?'}")]
        for f in ps.fails:
            fi = f.fight
            res = "Kill" if fi.kill else f"Wipe {fi.boss_pct:.0f}%" if fi.boss_pct is not None else "Wipe"
            lines.append(f"{li}{badge(f.severity)} {fi.name} #{fi.id} ({res}) · {f.category}: {f.title}")
    if clean:
        lines += ["", f"Ohne Fails: {', '.join(clean)}"]

    lines += ["", head(2, "Pro Kampf")]
    for rep in reports:
        f = rep.fight
        lines += ["", head(3, f"#{f.id} {f.name} · {_fight_result(f)} · {fmt_t(f.duration_s)}")]
        early = [d for d in rep.deaths if not d.wipe]
        wipe = [d for d in rep.deaths if d.wipe]
        if early:
            lines.append(
                f"{li}Tode{' vor dem Wipe' if wipe else ''}: "
                + ", ".join(
                    f"{d.player.name} {fmt_t(d.t)} '{d.killing_blow}'" + (" (erster)" if i == 0 else "")
                    for i, d in enumerate(early)
                )
            )
        if wipe:
            causes = Counter(d.killing_blow for d in wipe).most_common(3)
            lines.append(
                f"{li}Wipe ab {fmt_t(wipe[0].t)}: {len(wipe)} Tote – "
                + ", ".join(f"'{kb}' {n}×" for kb, n in causes)
                + f" · zuerst {wipe[0].player.name}"
            )
        if not rep.deaths:
            lines.append(f"{li}Keine Tode")
        if rep.kicks:
            lines.append(f"{li}Kicks: " + ", ".join(f"{name} {k}" for name, k in rep.kicks.most_common()))
        through = [x for x in rep.kicks_through if x[1]]
        if through:
            lines.append(f"{li}Durchgekommen: " + ", ".join(f"'{a}' {t}× ({k}× gekickt)" for a, t, k in through))
    return "\n".join(lines) + "\n"
