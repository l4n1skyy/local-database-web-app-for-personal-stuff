-- Chinese vocabulary. `db` applies this file every time it runs, so it only
-- creates what's missing; views and triggers are rebuilt so edits here take effect.

-- A word's status is its list: "to learn" -> "learnt" -> (after N days) "to revise" -> "learnt" ...
CREATE TABLE IF NOT EXISTS statuses (
    name TEXT PRIMARY KEY
);
INSERT OR IGNORE INTO statuses (name) VALUES ('to learn'), ('to revise'), ('learnt');

-- Filled automatically from the words, so the edit form can suggest them
CREATE TABLE IF NOT EXISTS types (name TEXT PRIMARY KEY COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS categories (name TEXT PRIMARY KEY COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS words (
    id           INTEGER PRIMARY KEY,
    character    TEXT NOT NULL CHECK (trim(character) <> ''),
    pinyin       TEXT NOT NULL DEFAULT '',
    english      TEXT NOT NULL DEFAULT '',
    notes        TEXT NOT NULL DEFAULT '',
    type         TEXT REFERENCES types (name),
    category     TEXT REFERENCES categories (name),
    hsk          INTEGER CHECK (hsk IS NULL OR hsk = '' OR hsk BETWEEN 1 AND 9),
    status       TEXT NOT NULL DEFAULT 'to learn' REFERENCES statuses (name)
                 CHECK (status IN ('to learn', 'to revise', 'learnt')),
    last_studied TEXT CHECK (last_studied IS NULL OR last_studied = '' OR last_studied GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    date_added   TEXT NOT NULL DEFAULT (date('now', 'localtime'))
                 CHECK (date_added GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    UNIQUE (character, pinyin)
);
CREATE INDEX IF NOT EXISTS words_status ON words (status, last_studied);

-- Every time a word changes list, so the stats can show progress over time
CREATE TABLE IF NOT EXISTS study_log (
    id          INTEGER PRIMARY KEY,
    word_id     INTEGER NOT NULL REFERENCES words (id) ON DELETE CASCADE,
    date        TEXT NOT NULL DEFAULT (date('now', 'localtime')),
    from_status TEXT,
    to_status   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS study_log_date ON study_log (date);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    about TEXT NOT NULL DEFAULT ''
);
INSERT OR IGNORE INTO settings (key, value, about) VALUES
    ('revise_after_days', '7', 'A learnt word moves to "to revise" this many days after you last studied it');

-- Blank fields in the add/edit forms mean "no value"
DROP TRIGGER IF EXISTS words_blank_to_null;
CREATE TRIGGER words_blank_to_null AFTER UPDATE OF hsk, last_studied ON words
WHEN NEW.hsk = '' OR NEW.last_studied = ''
BEGIN
    UPDATE words SET hsk = NULLIF(hsk, ''), last_studied = NULLIF(last_studied, '') WHERE id = NEW.id;
END;
DROP TRIGGER IF EXISTS words_blank_to_null_insert;
CREATE TRIGGER words_blank_to_null_insert AFTER INSERT ON words
WHEN NEW.hsk = '' OR NEW.last_studied = ''
BEGIN
    UPDATE words SET hsk = NULLIF(hsk, ''), last_studied = NULLIF(last_studied, '') WHERE id = NEW.id;
END;

-- Remember each type/category for suggestions, and spell it the way it was
-- first entered ("verb " becomes "Verb"); blank becomes no value
DROP TRIGGER IF EXISTS words_lists_insert;
CREATE TRIGGER words_lists_insert AFTER INSERT ON words
BEGIN
    INSERT OR IGNORE INTO types (name) SELECT trim(NEW.type) WHERE trim(NEW.type) <> '';
    INSERT OR IGNORE INTO categories (name) SELECT trim(NEW.category) WHERE trim(NEW.category) <> '';
    UPDATE words SET type = (SELECT name FROM types WHERE name = trim(NEW.type)),
                     category = (SELECT name FROM categories WHERE name = trim(NEW.category))
    WHERE id = NEW.id;
END;
DROP TRIGGER IF EXISTS words_lists_update;
CREATE TRIGGER words_lists_update AFTER UPDATE OF type, category ON words
-- only when the spelling differs, so the trigger's own UPDATE doesn't loop
WHEN NEW.type COLLATE BINARY IS NOT (SELECT name FROM types WHERE name = trim(NEW.type))
  OR NEW.category COLLATE BINARY IS NOT (SELECT name FROM categories WHERE name = trim(NEW.category))
BEGIN
    INSERT OR IGNORE INTO types (name) SELECT trim(NEW.type) WHERE trim(NEW.type) <> '';
    INSERT OR IGNORE INTO categories (name) SELECT trim(NEW.category) WHERE trim(NEW.category) <> '';
    UPDATE words SET type = (SELECT name FROM types WHERE name = trim(NEW.type)),
                     category = (SELECT name FROM categories WHERE name = trim(NEW.category))
    WHERE id = NEW.id;
END;

-- Adding a word straight into "learnt" counts as studying it today
DROP TRIGGER IF EXISTS words_learnt_on_insert;
CREATE TRIGGER words_learnt_on_insert AFTER INSERT ON words
WHEN NEW.status <> 'to learn' AND (NEW.last_studied IS NULL OR NEW.last_studied = '')
BEGIN
    UPDATE words SET last_studied = date('now', 'localtime') WHERE id = NEW.id;
END;

-- Moving a word to another list: log it, and (unless the sync moved it to
-- "to revise", or you set the date yourself) count it as studied today
DROP TRIGGER IF EXISTS words_status_change;
CREATE TRIGGER words_status_change AFTER UPDATE OF status ON words
WHEN NEW.status IS NOT OLD.status
BEGIN
    INSERT INTO study_log (word_id, from_status, to_status) VALUES (NEW.id, OLD.status, NEW.status);
    UPDATE words SET last_studied = date('now', 'localtime')
    WHERE id = NEW.id AND NEW.status <> 'to revise' AND NEW.last_studied IS OLD.last_studied;
END;

-- The three lists
DROP VIEW IF EXISTS to_learn;
CREATE VIEW to_learn AS
SELECT id, character, pinyin, english, notes, type, category, hsk, date_added
FROM words WHERE status = 'to learn'
ORDER BY hsk, id;

DROP VIEW IF EXISTS to_revise;
CREATE VIEW to_revise AS
SELECT id, character, pinyin, english, notes, type, category, hsk, last_studied,
       CAST(julianday('now', 'localtime', 'start of day') - julianday(last_studied) AS INTEGER) AS days_since_studied
FROM words WHERE status = 'to revise'
ORDER BY last_studied, id;

DROP VIEW IF EXISTS learnt;
CREATE VIEW learnt AS
SELECT id, character, pinyin, english, notes, type, category, hsk, last_studied,
       date(last_studied, '+' || (SELECT value FROM settings WHERE key = 'revise_after_days') || ' days') AS revise_on
FROM words WHERE status = 'learnt'
ORDER BY last_studied, id;
