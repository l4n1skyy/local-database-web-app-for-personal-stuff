-- Spaced repetition: a learnt word moves to "to revise" once
-- settings.revise_after_days have passed since you last studied it.
-- Run by the app every few minutes and on every page load, and by `db sync`.
UPDATE words SET status = 'to revise'
WHERE status = 'learnt'
  AND last_studied <= date('now', 'localtime',
        '-' || (SELECT value FROM settings WHERE key = 'revise_after_days') || ' days')
