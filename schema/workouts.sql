-- Workouts: one row per set. Sets of the same exercise on the same date are
-- one exercise's sets; everything on the same date is one workout.
-- `db` applies this file every time it runs, so it only creates what's missing;
-- views and triggers are rebuilt so edits here take effect.

-- Filled automatically from the sets, so the add/edit forms can suggest names
CREATE TABLE IF NOT EXISTS exercises (name TEXT PRIMARY KEY COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS sets (
    id       INTEGER PRIMARY KEY,
    date     TEXT NOT NULL DEFAULT (date('now', 'localtime'))
             CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    exercise TEXT NOT NULL COLLATE NOCASE REFERENCES exercises (name) CHECK (trim(exercise) <> ''),
    reps     INTEGER NOT NULL CHECK (reps > 0),
    weight   REAL NOT NULL DEFAULT 0 CHECK (weight >= 0)  -- kg, 0 = bodyweight
);
CREATE INDEX IF NOT EXISTS sets_date ON sets (date, exercise);
CREATE INDEX IF NOT EXISTS sets_exercise ON sets (exercise, date);

-- Remember each exercise for suggestions, and spell it the way it was first
-- entered ("bench press " becomes "Bench press")
DROP TRIGGER IF EXISTS sets_exercise_insert;
CREATE TRIGGER sets_exercise_insert AFTER INSERT ON sets
BEGIN
    INSERT OR IGNORE INTO exercises (name) VALUES (trim(NEW.exercise));
    UPDATE sets SET exercise = (SELECT name FROM exercises WHERE name = trim(NEW.exercise)) WHERE id = NEW.id;
END;
DROP TRIGGER IF EXISTS sets_exercise_update;
CREATE TRIGGER sets_exercise_update AFTER UPDATE OF exercise ON sets
-- only when the spelling differs, so the trigger's own UPDATE doesn't loop
WHEN NEW.exercise COLLATE BINARY IS NOT (SELECT name FROM exercises WHERE name = trim(NEW.exercise))
BEGIN
    INSERT OR IGNORE INTO exercises (name) VALUES (trim(NEW.exercise));
    UPDATE sets SET exercise = (SELECT name FROM exercises WHERE name = trim(NEW.exercise)) WHERE id = NEW.id;
END;

-- Every set with its set number (1st, 2nd, ... of that exercise that day)
DROP VIEW IF EXISTS set_log;
CREATE VIEW set_log AS
SELECT id, date, exercise,
       ROW_NUMBER() OVER (PARTITION BY date, exercise ORDER BY id) AS set_no,
       reps, weight, reps * weight AS volume
FROM sets
ORDER BY date DESC, exercise, set_no;

-- One row per workout (date)
DROP VIEW IF EXISTS workouts;
CREATE VIEW workouts AS
SELECT date,
       CASE strftime('%w', date) WHEN '0' THEN 'Sun' WHEN '1' THEN 'Mon' WHEN '2' THEN 'Tue'
            WHEN '3' THEN 'Wed' WHEN '4' THEN 'Thu' WHEN '5' THEN 'Fri' ELSE 'Sat' END AS day,
       replace(group_concat(DISTINCT exercise), ',', ', ') AS exercises,
       count(*) AS sets,
       sum(reps) AS reps,
       sum(reps * weight) AS volume
FROM sets
GROUP BY date
ORDER BY date DESC;

-- Best results per exercise. est_1rm uses the Epley formula: weight * (1 + reps / 30)
DROP VIEW IF EXISTS records;
CREATE VIEW records AS
SELECT exercise,
       count(DISTINCT date) AS workouts,
       count(*) AS sets,
       max(weight) AS heaviest,
       (SELECT max(reps) FROM sets s2 WHERE s2.exercise = s.exercise AND s2.weight = max(s.weight)) AS reps_at_heaviest,
       max(reps) AS most_reps,
       round(max(weight * (1 + reps / 30.0)), 1) AS est_1rm,
       max(date) AS last_done
FROM sets s
GROUP BY exercise
ORDER BY last_done DESC;
