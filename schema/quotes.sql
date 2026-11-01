-- Quotes, each with any number of tags. `db` applies this file every time it runs, so it
-- only creates what's missing; views and triggers are rebuilt so edits here take effect.
-- knowledge.sql is the same thing for knowledge bits; plugins/app.py serves both.

CREATE TABLE IF NOT EXISTS quotes (
    id     INTEGER PRIMARY KEY,
    text   TEXT NOT NULL CHECK (trim(text) <> ''),
    author TEXT NOT NULL DEFAULT '',  -- who said or wrote it
    source TEXT NOT NULL DEFAULT '',  -- where it's from: a book, a film, a show
    notes  TEXT NOT NULL DEFAULT '',
    added  TEXT NOT NULL DEFAULT (date('now', 'localtime'))
           CHECK (added GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')
);

CREATE TABLE IF NOT EXISTS tags (
    name TEXT PRIMARY KEY COLLATE NOCASE CHECK (trim(name) <> '')
);

CREATE TABLE IF NOT EXISTS quote_tags (
    quote_id INTEGER NOT NULL REFERENCES quotes (id),
    tag      TEXT NOT NULL COLLATE NOCASE REFERENCES tags (name),
    PRIMARY KEY (quote_id, tag)
);
CREATE INDEX IF NOT EXISTS quote_tags_tag ON quote_tags (tag);

-- A tag given to a quote (in Datasette's row form, say) becomes a tag if it isn't one yet
DROP TRIGGER IF EXISTS quote_tags_new_tag;
CREATE TRIGGER quote_tags_new_tag BEFORE INSERT ON quote_tags
BEGIN
    INSERT OR IGNORE INTO tags (name) VALUES (NEW.tag);
END;

-- Renaming a tag renames it on its quotes; deleting a tag or a quote removes their links
DROP TRIGGER IF EXISTS tags_rename;
CREATE TRIGGER tags_rename AFTER UPDATE OF name ON tags
BEGIN
    UPDATE quote_tags SET tag = NEW.name WHERE tag = OLD.name;
END;
DROP TRIGGER IF EXISTS tags_delete;
CREATE TRIGGER tags_delete AFTER DELETE ON tags
BEGIN
    DELETE FROM quote_tags WHERE tag = OLD.name;
END;
DROP TRIGGER IF EXISTS quotes_delete;
CREATE TRIGGER quotes_delete AFTER DELETE ON quotes
BEGIN
    DELETE FROM quote_tags WHERE quote_id = OLD.id;
END;

-- Every quote with its tags
DROP VIEW IF EXISTS quotes_tagged;
CREATE VIEW quotes_tagged AS
SELECT q.id, q.text, q.author, q.source,
       (SELECT group_concat(tag, ', ') FROM (SELECT tag FROM quote_tags WHERE quote_id = q.id ORDER BY tag)) AS tags,
       q.notes, q.added
FROM quotes q
ORDER BY q.added DESC, q.id DESC;

-- How many quotes each tag has
DROP VIEW IF EXISTS tag_counts;
CREATE VIEW tag_counts AS
SELECT t.name AS tag, count(l.quote_id) AS quotes
FROM tags t LEFT JOIN quote_tags l ON l.tag = t.name
GROUP BY t.name
ORDER BY quotes DESC, t.name;
