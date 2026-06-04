#!/usr/bin/env python3
"""Cesar School IEC Pilot — minimock practice report (one-time).

Reads the CSV exported from query.sql and emits report.md + charts/*.png.

Usage:
    python report.py cohort_minimocks.csv [--outdir .]

Stack: polars (load/aggregate), numpy (trend fit), matplotlib (charts).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import polars as pl

SKILLS = ["clarity", "confidence", "eloquence", "social_intelligence", "storytelling"]
SKILL_LABEL = {s: s.replace("_", " ").title() for s in SKILLS}

HIGH_THRESHOLD = 4  # started practices per student: >= this = highly; 1..(n-1) = regularly; 0 = not started


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def parse_json(value):
    """Best-effort JSON parse; tolerate NULL/empty/garbage from CSV export."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "nan"}:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def load(csv_path: Path) -> pl.DataFrame:
    # Read JSON / id columns as strings so we can parse them ourselves.
    overrides = {
        "user_id": pl.Utf8,
        "session_id": pl.Utf8,
        "attempt_id": pl.Utf8,
        "dimension_scores": pl.Utf8,
        "feedback_items": pl.Utf8,
        "final_score": pl.Float64,
    }
    df = pl.read_csv(
        csv_path,
        schema_overrides=overrides,
        infer_schema_length=10_000,
        try_parse_dates=True,
    )
    return df


# --------------------------------------------------------------------------- #
# Derived frames
# --------------------------------------------------------------------------- #
def split(df: pl.DataFrame):
    roster = df.select("email").unique().sort("email")

    # Every distinct session (provisioned or not).
    sessions = df.filter(
        pl.col("session_id").is_not_null() & (pl.col("session_id") != "")
    ).unique(subset=["session_id"])

    # A "practice" = a session the student actually STARTED (left the CREATED state).
    # CREATED = provisioned scaffolding the student never began; excluded from engagement.
    # Grading may lag (IN_PROGRESS), so started practices is the engagement unit, not graded.
    # Fall back to all sessions if session_status wasn't exported.
    if "session_status" in sessions.columns:
        started = sessions.filter(
            pl.col("session_status").is_not_null()
            & (pl.col("session_status").str.to_uppercase() != "CREATED")
        )
    else:
        started = sessions

    attempts = df.filter(
        pl.col("attempt_id").is_not_null() & (pl.col("attempt_id") != "")
    )

    # Enum is stored by member name (uppercase "SCORED"); match case-insensitively.
    scored = attempts.filter(
        (pl.col("scoring_status").str.to_uppercase() == "SCORED")
        & pl.col("final_score").is_not_null()
    )
    return roster, sessions, started, attempts, scored


def dimension_frame(scored: pl.DataFrame) -> pl.DataFrame:
    """Long frame of per-attempt dimension scores: email, skill, score."""
    rows = []
    for rec in scored.iter_rows(named=True):
        dims = parse_json(rec["dimension_scores"]) or {}
        for skill in SKILLS:
            val = dims.get(skill)
            if isinstance(val, (int, float)):
                rows.append({"email": rec["email"], "skill": skill, "score": float(val)})
    return pl.DataFrame(rows, schema={"email": pl.Utf8, "skill": pl.Utf8, "score": pl.Float64})


def feedback_frame(scored: pl.DataFrame) -> pl.DataFrame:
    """Long frame of feedback items: email, category, skill, title, description."""
    rows = []
    for rec in scored.iter_rows(named=True):
        items = parse_json(rec["feedback_items"]) or []
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            rows.append(
                {
                    "email": rec["email"],
                    "category": it.get("category"),
                    "skill": it.get("skill"),
                    "title": it.get("title"),
                    "description": it.get("description"),
                }
            )
    return pl.DataFrame(
        rows,
        schema={
            "email": pl.Utf8,
            "category": pl.Utf8,
            "skill": pl.Utf8,
            "title": pl.Utf8,
            "description": pl.Utf8,
        },
    )


def engagement_tiers(roster: pl.DataFrame, started: pl.DataFrame) -> dict:
    """Bucket students by #started practices: highly (>=HIGH_THRESHOLD), regularly (1..), not started (0)."""
    counts = started.group_by("email").agg(pl.len().alias("n"))
    per_email = dict(zip(counts["email"].to_list(), counts["n"].to_list()))
    n_high = sum(1 for n in per_email.values() if n >= HIGH_THRESHOLD)
    n_regular = sum(1 for n in per_email.values() if 1 <= n < HIGH_THRESHOLD)
    n_practicers = len(per_email)
    return {
        "high": n_high,
        "regular": n_regular,
        "not_started": roster.height - n_practicers,
        "practicers": n_practicers,
    }


def trend_table(scored: pl.DataFrame) -> pl.DataFrame:
    """Per-student linear trend (slope) of final_score over attempt order."""
    rows = []
    for email, sub in scored.sort("attempt_created_at").group_by("email", maintain_order=True):
        key = email[0] if isinstance(email, tuple) else email
        ys = sub["final_score"].to_list()
        n = len(ys)
        if n >= 2:
            xs = np.arange(n, dtype=float)
            slope = float(np.polyfit(xs, np.array(ys, dtype=float), 1)[0])
            rows.append(
                {
                    "email": key,
                    "n_scored": n,
                    "first_score": ys[0],
                    "last_score": ys[-1],
                    "slope": slope,
                    "status": "improving" if slope > 0 else "declining" if slope < 0 else "flat",
                }
            )
        else:
            rows.append(
                {
                    "email": key,
                    "n_scored": n,
                    "first_score": ys[0] if ys else None,
                    "last_score": ys[-1] if ys else None,
                    "slope": None,
                    "status": "insufficient data",
                }
            )
    return pl.DataFrame(
        rows,
        schema={
            "email": pl.Utf8,
            "n_scored": pl.Int64,
            "first_score": pl.Float64,
            "last_score": pl.Float64,
            "slope": pl.Float64,
            "status": pl.Utf8,
        },
    )


# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #
def chart_skill_averages(dims: pl.DataFrame, path: Path):
    if dims.is_empty():
        return None
    avg = dims.group_by("skill").agg(pl.col("score").mean().alias("avg")).to_dicts()
    avg = sorted(avg, key=lambda r: SKILLS.index(r["skill"]))
    labels = [SKILL_LABEL[r["skill"]] for r in avg]
    values = [r["avg"] for r in avg]
    fig, ax = plt.subplots(figsize=(7, 4))
    bars = ax.bar(labels, values, color="#4C72B0")
    ax.set_ylim(0, 100)
    ax.set_ylabel("Average score (0–100)")
    ax.set_title("Cohort average by skill")
    for b, v in zip(bars, values):
        ax.text(b.get_x() + b.get_width() / 2, v + 1, f"{v:.0f}", ha="center", va="bottom")
    plt.xticks(rotation=20, ha="right")
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def chart_score_distribution(scored: pl.DataFrame, path: Path):
    if scored.is_empty():
        return None
    vals = scored["final_score"].to_list()
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.hist(vals, bins=min(10, max(3, len(vals))), color="#55A868", edgecolor="white")
    ax.set_xlabel("Final score (0–100)")
    ax.set_ylabel("Attempts")
    ax.set_title("Distribution of final scores")
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def chart_trends(scored: pl.DataFrame, path: Path):
    if scored.is_empty():
        return None
    fig, ax = plt.subplots(figsize=(7, 4))
    plotted = False
    for email, sub in scored.sort("attempt_created_at").group_by("email", maintain_order=True):
        key = email[0] if isinstance(email, tuple) else email
        ys = sub["final_score"].to_list()
        if len(ys) >= 1:
            ax.plot(range(1, len(ys) + 1), ys, marker="o", label=str(key).split("@")[0])
            plotted = True
    if not plotted:
        plt.close(fig)
        return None
    ax.set_xlabel("Attempt # (chronological)")
    ax.set_ylabel("Final score (0–100)")
    ax.set_ylim(0, 100)
    ax.set_title("Per-student score progression")
    ax.legend(fontsize=7, ncol=2)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def chart_participation(tiers: dict, path: Path):
    labels = [
        f"Highly-engaged\n(≥{HIGH_THRESHOLD})",
        f"Regularly-engaged\n(1–{HIGH_THRESHOLD - 1})",
        "Not yet started\n(0)",
    ]
    values = [tiers["high"], tiers["regular"], tiers["not_started"]]
    colors = ["#4C72B0", "#55A868", "#C44E52"]
    fig, ax = plt.subplots(figsize=(6, 4))
    bars = ax.bar(labels, values, color=colors)
    ax.set_ylabel("Students")
    ax.set_title("Engagement")
    for b, v in zip(bars, values):
        ax.text(b.get_x() + b.get_width() / 2, v + 0.05, str(v), ha="center", va="bottom")
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def fmt(x, nd=1):
    return "n/a" if x is None else f"{x:.{nd}f}"


def build_report(df, roster, sessions, started, attempts, scored, dims, fb, trends, charts, outdir: Path):
    cohort_name = df["cohort_name"].drop_nulls().to_list()
    cohort_name = cohort_name[0] if cohort_name else "Cohort"

    n_students = roster.height
    tiers = engagement_tiers(roster, started)
    n_practicers = tiers["practicers"]
    n_started = started.height
    n_scored = scored.height
    n_in_progress = n_started - n_scored          # started but not yet graded
    n_provisioned = sessions.height - n_started   # CREATED, never started

    avg_per_student = n_started / n_students if n_students else 0
    avg_per_practicing = n_started / n_practicers if n_practicers else 0

    avg_score = scored["final_score"].mean() if n_scored else None
    med_score = scored["final_score"].median() if n_scored else None

    lines = []
    w = lines.append
    w(f"# {cohort_name} — Minimock Practice Report")
    w("")
    w("_Window: sessions on/after the 2026-05-28 workshop. Minimocks only._")
    w("")

    # 1. Overview
    w("## Overview")
    w("")
    pct_practiced = f" ({n_practicers / n_students * 100:.0f}%)" if n_students else ""
    pct_graded = f" ({n_scored / n_started * 100:.0f}% of started)" if n_started else ""
    w(f"- **Students in cohort:** {n_students}")
    w(f"- **Practiced (≥1 started session):** {n_practicers}{pct_practiced}")
    w(f"- **Practices started:** {n_started}")
    w(f"- **Graded attempts:** {n_scored}{pct_graded}")
    w(f"- **In progress (started, not yet graded):** {n_in_progress}")
    w(f"- **Provisioned but never started:** {n_provisioned} sessions")
    w(f"- **Avg practices / student:** {avg_per_student:.2f} (per practicing student: {avg_per_practicing:.2f})")
    w("")
    w("**Engagement** — counts practices _started_ (graded or not):")
    w("")
    w(f"- **Highly-engaged (≥{HIGH_THRESHOLD}):** {tiers['high']}")
    w(f"- **Regularly-engaged (1–{HIGH_THRESHOLD - 1}):** {tiers['regular']}")
    w(f"- **Not yet started (0):** {tiers['not_started']}")
    w("")
    if charts.get("participation"):
        w(f"![Participation](charts/{charts['participation'].name})")
        w("")
    w(f"> Scores & feedback below are computed over the **{n_scored} graded attempts** only.")
    w("")

    # 2. Scores
    w("## Scores")
    w("")
    w(f"- **Average final score:** {fmt(avg_score)} / 100")
    w(f"- **Median final score:** {fmt(med_score)} / 100")
    w("")
    if not dims.is_empty():
        w("**Average by skill:**")
        w("")
        w("| Skill | Avg (0–100) |")
        w("| --- | --- |")
        avg_by_skill = dims.group_by("skill").agg(pl.col("score").mean().alias("avg")).to_dicts()
        for r in sorted(avg_by_skill, key=lambda r: SKILLS.index(r["skill"])):
            w(f"| {SKILL_LABEL[r['skill']]} | {r['avg']:.1f} |")
        w("")
    if charts.get("skills"):
        w(f"![Skill averages](charts/{charts['skills'].name})")
        w("")
    if charts.get("dist"):
        w(f"![Score distribution](charts/{charts['dist'].name})")
        w("")
    # Per minimock type
    if n_scored:
        by_type = (
            scored.group_by("minimock_type")
            .agg(pl.len().alias("attempts"), pl.col("final_score").mean().alias("avg"))
            .sort("attempts", descending=True)
            .to_dicts()
        )
        if by_type:
            w("**By minimock type:**")
            w("")
            w("| Type | Attempts | Avg score |")
            w("| --- | --- | --- |")
            for r in by_type:
                w(f"| {r['minimock_type'] or 'unknown'} | {r['attempts']} | {fmt(r['avg'])} |")
            w("")

    # 3. Progress
    w("## Progress (linear trend over attempts)")
    w("")
    measurable = trends.filter(pl.col("slope").is_not_null())
    n_improving = measurable.filter(pl.col("slope") > 0).height
    n_declining = measurable.filter(pl.col("slope") < 0).height
    n_flat = measurable.filter(pl.col("slope") == 0).height
    n_insuff = trends.filter(pl.col("slope").is_null()).height
    mean_slope = measurable["slope"].mean() if measurable.height else None
    w(f"- **Improving:** {n_improving}  ·  **Declining:** {n_declining}  ·  **Flat:** {n_flat}")
    w(f"- **Insufficient data (<2 scored):** {n_insuff}")
    w(f"- **Mean slope (pts / attempt):** {fmt(mean_slope, 2)}")
    w("")
    if measurable.height:
        w("| Student | Scored | First | Last | Slope | Trend |")
        w("| --- | --- | --- | --- | --- | --- |")
        for r in trends.sort("slope", descending=True, nulls_last=True).to_dicts():
            email = r["email"].split("@")[0]
            w(
                f"| {email} | {r['n_scored']} | {fmt(r['first_score'],0)} | "
                f"{fmt(r['last_score'],0)} | {fmt(r['slope'],2)} | {r['status']} |"
            )
        w("")
    if charts.get("trends"):
        w(f"![Score progression](charts/{charts['trends'].name})")
        w("")

    # 4. Feedback themes
    w("## Feedback themes")
    w("")
    if fb.is_empty():
        w("_No feedback items found in scored attempts._")
        w("")
    else:
        for cat, header in [("improvement", "Top areas to improve"), ("strength", "Top strengths")]:
            sub = fb.filter(pl.col("category") == cat)
            if sub.is_empty():
                continue
            counts = (
                sub.filter(pl.col("skill").is_not_null())
                .group_by("skill")
                .agg(pl.len().alias("mentions"))
                .sort("mentions", descending=True)
                .to_dicts()
            )
            w(f"**{header}** (by skill, mentions):")
            w("")
            for r in counts:
                w(f"- {SKILL_LABEL.get(r['skill'], r['skill'])}: {r['mentions']}")
            w("")
            # sample quotes from the top skill
            if counts:
                top_skill = counts[0]["skill"]
                samples = (
                    sub.filter(pl.col("skill") == top_skill)
                    .select("title", "description")
                    .unique()
                    .head(3)
                    .to_dicts()
                )
                if samples:
                    w(f"_Examples — {SKILL_LABEL.get(top_skill, top_skill)}:_")
                    w("")
                    for s in samples:
                        title = (s.get("title") or "").strip()
                        desc = (s.get("description") or "").strip()
                        w(f"> **{title}** — {desc}")
                    w("")

    w("---")
    w("")
    w("_Generated by `report.py`. Charts in `charts/`._")

    report_path = outdir / "report.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Cesar School IEC Pilot minimock report")
    ap.add_argument("csv", type=Path, help="Path to cohort_minimocks.csv")
    ap.add_argument("--outdir", type=Path, default=Path("."), help="Output directory")
    args = ap.parse_args()

    outdir = args.outdir
    charts_dir = outdir / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)

    df = load(args.csv)
    roster, sessions, started, attempts, scored = split(df)
    dims = dimension_frame(scored)
    fb = feedback_frame(scored)
    trends = trend_table(scored)

    tiers = engagement_tiers(roster, started)

    charts = {
        "skills": chart_skill_averages(dims, charts_dir / "skill_averages.png"),
        "dist": chart_score_distribution(scored, charts_dir / "score_distribution.png"),
        "trends": chart_trends(scored, charts_dir / "score_progression.png"),
        "participation": chart_participation(tiers, charts_dir / "participation.png"),
    }

    report_path = build_report(df, roster, sessions, started, attempts, scored, dims, fb, trends, charts, outdir)

    print(
        f"Students: {roster.height} | provisioned: {sessions.height} | started: {started.height} "
        f"| graded: {scored.height} | practiced: {tiers['practicers']} | highly: {tiers['high']} "
        f"| regularly: {tiers['regular']} | not started: {tiers['not_started']}"
    )
    print(f"Wrote {report_path}")
    print(f"Charts in {charts_dir}/")


if __name__ == "__main__":
    main()
