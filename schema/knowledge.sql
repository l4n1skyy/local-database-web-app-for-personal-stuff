-- Knowledge bits: little notes worth keeping, each with any number of tags. `db` applies
-- this file every time it runs, so it only creates what's missing; views and triggers are
-- rebuilt so edits here take effect. quotes.sql is the same thing for quotes.

CREATE TABLE IF NOT EXISTS bits (
    id     INTEGER PRIMARY KEY,
    title  TEXT NOT NULL DEFAULT '',
    text   TEXT NOT NULL CHECK (trim(text) <> ''),
    source TEXT NOT NULL DEFAULT '',  -- where you learnt it: a link, a book, a person
    added  TEXT NOT NULL DEFAULT (date('now', 'localtime'))
           CHECK (added GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')
);

CREATE TABLE IF NOT EXISTS tags (
    name TEXT PRIMARY KEY COLLATE NOCASE CHECK (trim(name) <> '')
);

CREATE TABLE IF NOT EXISTS bit_tags (
    bit_id INTEGER NOT NULL REFERENCES bits (id),
    tag    TEXT NOT NULL COLLATE NOCASE REFERENCES tags (name),
    PRIMARY KEY (bit_id, tag)
);
CREATE INDEX IF NOT EXISTS bit_tags_tag ON bit_tags (tag);

-- A tag given to a bit (in Datasette's row form, say) becomes a tag if it isn't one yet
DROP TRIGGER IF EXISTS bit_tags_new_tag;
CREATE TRIGGER bit_tags_new_tag BEFORE INSERT ON bit_tags
BEGIN
    INSERT OR IGNORE INTO tags (name) VALUES (NEW.tag);
END;

-- Renaming a tag renames it on its bits; deleting a tag or a bit removes their links
DROP TRIGGER IF EXISTS tags_rename;
CREATE TRIGGER tags_rename AFTER UPDATE OF name ON tags
BEGIN
    UPDATE bit_tags SET tag = NEW.name WHERE tag = OLD.name;
END;
DROP TRIGGER IF EXISTS tags_delete;
CREATE TRIGGER tags_delete AFTER DELETE ON tags
BEGIN
    DELETE FROM bit_tags WHERE tag = OLD.name;
END;
DROP TRIGGER IF EXISTS bits_delete;
CREATE TRIGGER bits_delete AFTER DELETE ON bits
BEGIN
    DELETE FROM bit_tags WHERE bit_id = OLD.id;
END;

-- Every bit with its tags
DROP VIEW IF EXISTS bits_tagged;
CREATE VIEW bits_tagged AS
SELECT b.id, b.title, b.text, b.source,
       (SELECT group_concat(tag, ', ') FROM (SELECT tag FROM bit_tags WHERE bit_id = b.id ORDER BY tag)) AS tags,
       b.added
FROM bits b
ORDER BY b.added DESC, b.id DESC;

-- How many bits each tag has
DROP VIEW IF EXISTS tag_counts;
CREATE VIEW tag_counts AS
SELECT t.name AS tag, count(l.bit_id) AS bits
FROM tags t LEFT JOIN bit_tags l ON l.tag = t.name
GROUP BY t.name
ORDER BY bits DESC, t.name;
