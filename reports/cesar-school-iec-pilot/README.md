# Cesar School IEC Pilot — Minimock Report

One-time analysis of the cohort's mock interview (minimock) practice since the
2026-05-28 workshop. Design: `docs/plans/2026-06-04-cohort-minimock-report-design.md`.

Cohort: **Cesar School IEC Pilot** (`ae7f98cf-91fa-4169-b8b6-38c7c6b9f7c7`).

## Steps

1. **Sanity check** the cohort in the Neon web client (expect one row):

   ```sql
   SELECT id, name, created_at FROM cohorts
   WHERE id = 'ae7f98cf-91fa-4169-b8b6-38c7c6b9f7c7';
   ```

2. **Run `query.sql`** in the Neon web client, then **Export → CSV** and save it
   here as `cohort_minimocks.csv`.

3. **Deps** — this folder is a self-contained `uv` project (`pyproject.toml` +
   `uv.lock`) so it doesn't touch the app envs. Sync once: `uv sync`.

4. **Generate the report:**

   ```sh
   uv run python report.py cohort_minimocks.csv
   ```

   Outputs `report.md` and `charts/*.png` in this folder.

## What it computes

- **Overview** — students, who practiced (≥1 started session), practices started, graded attempts, in-progress, provisioned-but-never-started, avg practices/student, and engagement tiers (highly/regularly/not-started by started practices).
- **Scores** — cohort avg/median `final_score`, per-skill averages (clarity, confidence, eloquence, social_intelligence, storytelling), by minimock type.
- **Progress** — per-student linear trend (`numpy.polyfit` slope) over scored attempts; # improving/declining; mean slope.
- **Feedback themes** — `feedback_items` ranked by `skill` × `category` (strength/improvement), with sample quotes. Aggregates on stable English enums (text may be pt-BR).

## Notes

- A **practice = a minimock session the student STARTED** — i.e. `session_status`
  left the `CREATED` state (`IN_PROGRESS` or `COMPLETED`). `CREATED` sessions are
  provisioned scaffolding the student never began and are **excluded** from
  engagement. Grading can lag (`IN_PROGRESS`), so engagement counts *started*
  practices, not graded ones. If `session_status` isn't in the CSV, the script
  falls back to counting all sessions.
- **Scores, feedback, and trends** use only the **graded attempts** (`scoring_status = SCORED`).
- Scores are 0–100. `final_score` is the mean of the 5 skill dimensions.
- The `feedback` text column is always NULL for minimocks — only `feedback_items` is used.
- Students with no session at all count toward the roster as "not yet started".
- The engagement threshold (`HIGH_THRESHOLD`, default 7 practices) is a tunable
  constant at the top of `report.py` — adjust to taste.
