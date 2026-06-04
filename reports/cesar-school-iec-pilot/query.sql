-- Cesar School IEC Pilot — minimock practice extraction (one-time)
-- Run in the Neon web client, then Export -> CSV as `cohort_minimocks.csv`.
-- Window: sessions created on/after the 2026-05-28 workshop (BRT).
-- One row per minimock attempt; students with zero practices are retained (NULL attempt columns).

-- Step 0 — sanity check (run first, expect one row: "Cesar School IEC Pilot"):
-- SELECT id, name, created_at FROM cohorts
-- WHERE id = 'ae7f98cf-91fa-4169-b8b6-38c7c6b9f7c7';

WITH target_cohort AS (
  SELECT id, name FROM cohorts
  WHERE id = 'ae7f98cf-91fa-4169-b8b6-38c7c6b9f7c7'
),
students AS (
  SELECT cm.cohort_id, cm.email
  FROM cohort_members cm
  JOIN target_cohort tc ON tc.id = cm.cohort_id
  WHERE cm.role = 'STUDENT'
)
SELECT
  tc.name                        AS cohort_name,
  s.email,
  u.id                           AS user_id,
  u.name                         AS user_name,
  ms.id                          AS session_id,
  ms.status                      AS session_status,        -- created | in_progress | completed | abandoned
  ms.created_at                  AS session_created_at,
  ms.started_at                  AS session_started_at,
  ms.completed_at                AS session_completed_at,
  ma.id                          AS attempt_id,
  mm.minimock_type,
  mm.scenario_title,
  ms.language,
  ma.scoring_status,
  (ma.scoring_status = 'SCORED') AS is_scored,  -- enum stored by member name (uppercase)
  ma.final_score,
  ma.dimension_scores,           -- JSONB -> JSON text in CSV
  ma.feedback_items,             -- JSONB -> JSON text in CSV
  ma.duration_seconds,
  ma.started_at,
  ma.completed_at,
  ma.created_at                  AS attempt_created_at
FROM students s
CROSS JOIN target_cohort tc
LEFT JOIN users u              ON u.email = s.email
LEFT JOIN minimock_sessions ms ON ms.user_id = u.id
                              AND ms.created_at >= TIMESTAMPTZ '2026-05-28 00:00:00-03'
LEFT JOIN minimock_attempts ma ON ma.session_id = ms.id
LEFT JOIN minimocks mm        ON mm.id = ms.minimock_id
ORDER BY s.email, ma.created_at;
