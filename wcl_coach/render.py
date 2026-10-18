"""Output: colored terminal text and Markdown."""
from __future__ import annotations

import os
import sys
from collections import Counter

from .analysis import HIGH, INFO, LOW, MEDIUM, SEVERITY_LABEL, Finding
from .fetch import PlayerFight

_COLORS = {HIGH: "\033[91m", MEDIUM: "\033[93m", LOW: "\033[96m", INFO: "\033[90m"}
_RESET, _BOLD = "\033[0m", "\033[1m"


def _use_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _header(me: PlayerFight, n_tops: int) -> list[str]:
    p, f = me.player, me.fight
    result = "Kill" if f.kill else f"Wipe ({f.boss_pct:.0f}%)" if f.boss_pct is not None else "Wipe"
    return [
        f"{p.name} – {p.spec or '?'} {p.class_name or '?'} ({p.role or '?'})",
        f"{f.name} · {result} · {int(f.duration_s // 60)}:{int(f.duration_s % 60):02d} · Report {me.report_code}#{f.id}",
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
        md += ["## Überblick", ""] + [f"- **{f.category}:** {f.title}" for f in infos] + [""]
    actionable = [f for f in findings if f.severity > INFO]
    md += ["## Verbesserungspunkte", ""]
    if not actionable:
        md.append("Keine Auffälligkeiten gefunden.")
    for i, f in enumerate(actionable, 1):
        md.append(f"### {i}. [{SEVERITY_LABEL[f.severity]}] {f.category}: {f.title}")
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
