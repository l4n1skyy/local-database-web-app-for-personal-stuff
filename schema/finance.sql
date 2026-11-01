-- Finance: what you spend (expenses), your budget, how much is in your savings
-- accounts (balances), and your shares: the trades, the ringgit you converted to buy
-- them (conversions) and their prices over time. Amounts are in ringgit (RM) unless a
-- column says it's in the share's currency (usually US dollars).
-- `db` applies this file every time it runs, so it only creates what's missing;
-- views and triggers are rebuilt so edits here take effect.

-- Filled automatically from the expenses, the budget and the balances, so the
-- forms can suggest names
CREATE TABLE IF NOT EXISTS categories (name TEXT PRIMARY KEY COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS accounts (name TEXT PRIMARY KEY COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS expenses (
    id       INTEGER PRIMARY KEY,
    date     TEXT NOT NULL DEFAULT (date('now', 'localtime'))
             CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    category TEXT NOT NULL COLLATE NOCASE REFERENCES categories (name) CHECK (trim(category) <> ''),
    amount   REAL NOT NULL CHECK (amount > 0),
    notes    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS expenses_date ON expenses (date);
CREATE INDEX IF NOT EXISTS expenses_category ON expenses (category, date);

-- What you plan to spend per category: each day ("daily") or each month ("monthly")
CREATE TABLE IF NOT EXISTS budget (
    id       INTEGER PRIMARY KEY,
    period   TEXT NOT NULL CHECK (period IN ('daily', 'monthly')),
    position INTEGER NOT NULL DEFAULT 0,
    category TEXT NOT NULL COLLATE NOCASE REFERENCES categories (name) CHECK (trim(category) <> ''),
    amount   REAL NOT NULL CHECK (amount >= 0),
    UNIQUE (period, category)
);

-- How much was in a savings account on a date. The newest entry is its balance now.
CREATE TABLE IF NOT EXISTS balances (
    id      INTEGER PRIMARY KEY,
    date    TEXT NOT NULL DEFAULT (date('now', 'localtime'))
            CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    account TEXT NOT NULL COLLATE NOCASE REFERENCES accounts (name) CHECK (trim(account) <> ''),
    balance REAL NOT NULL,
    notes   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS balances_account ON balances (account, date);

-- Shares bought and sold. price and commission are in the share's currency (tickers.currency).
CREATE TABLE IF NOT EXISTS trades (
    id         INTEGER PRIMARY KEY,
    date       TEXT NOT NULL DEFAULT (date('now', 'localtime'))
               CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    ticker     TEXT NOT NULL CHECK (trim(ticker) <> ''),
    side       TEXT NOT NULL DEFAULT 'buy' CHECK (side IN ('buy', 'sell')),
    shares     REAL NOT NULL CHECK (shares > 0),
    price      REAL NOT NULL CHECK (price > 0),
    commission REAL NOT NULL DEFAULT 0 CHECK (commission >= 0),
    notes      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS trades_ticker ON trades (ticker, date);

-- Ringgit changed into another currency to buy shares with: myr is what left your
-- account for it (fee included), amount what arrived at the broker, fee the part of myr
-- that was a fee (Wise shows it). With these the app knows exactly how many ringgit went
-- into your shares.
CREATE TABLE IF NOT EXISTS conversions (
    id       INTEGER PRIMARY KEY,
    date     TEXT NOT NULL DEFAULT (date('now', 'localtime'))
             CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    myr      REAL NOT NULL CHECK (myr > 0),
    currency TEXT NOT NULL DEFAULT 'USD' CHECK (length(currency) = 3),
    amount   REAL NOT NULL CHECK (amount > 0),
    fee      REAL NOT NULL DEFAULT 0 CHECK (fee >= 0),
    notes    TEXT NOT NULL DEFAULT ''
);

-- Every ticker you traded: symbol is what it's called on Yahoo Finance, where the app
-- gets prices (CSPX on the London Stock Exchange is CSPX.L), currency what it trades in.
CREATE TABLE IF NOT EXISTS tickers (
    ticker   TEXT PRIMARY KEY,
    symbol   TEXT,
    currency TEXT NOT NULL DEFAULT 'USD' CHECK (length(currency) = 3),
    name     TEXT NOT NULL DEFAULT ''
);

-- Closing prices by day, filled from Yahoo Finance: a ticker's in its currency, and
-- exchange rates as "USD/MYR" (ringgit per dollar)
CREATE TABLE IF NOT EXISTS prices (
    ticker TEXT NOT NULL,
    date   TEXT NOT NULL CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    close  REAL NOT NULL CHECK (close > 0),
    PRIMARY KEY (ticker, date)
);

-- Money that arrived at (or left) the broker, as the broker reports it: IBKR's deposits
-- and withdrawals, from its Flex statement. A deposit that no conversion accounts for yet
-- waits on the Investments tab for the ringgit it cost.
CREATE TABLE IF NOT EXISTS deposits (
    id          TEXT PRIMARY KEY,
    date        TEXT NOT NULL CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    currency    TEXT NOT NULL,
    amount      REAL NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);

-- Bits the app remembers, like when prices were last updated, and the IBKR connection
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

-- Whether a month's expenses count in averages, when you want to decide it yourself.
-- Without a row it's automatic: a month counts once expenses were logged on at least
-- half its days, so a month you forgot to log doesn't pull the averages down.
CREATE TABLE IF NOT EXISTS month_settings (
    month   TEXT PRIMARY KEY CHECK (month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
    counted INTEGER NOT NULL CHECK (counted IN (0, 1))
);

-- The categories the Stats tab's "Spent per day" chart adds up, picked on that tab.
-- Without any, it uses the daily budget's categories.
CREATE TABLE IF NOT EXISTS daily_chart_categories (
    category TEXT PRIMARY KEY COLLATE NOCASE REFERENCES categories (name)
);

-- Remember each category and account for suggestions, and spell it the way it
-- was first entered ("food " becomes "Food")
DROP TRIGGER IF EXISTS expenses_category_insert;
CREATE TRIGGER expenses_category_insert AFTER INSERT ON expenses
BEGIN
    INSERT OR IGNORE INTO categories (name) VALUES (trim(NEW.category));
    UPDATE expenses SET category = (SELECT name FROM categories WHERE name = trim(NEW.category)) WHERE id = NEW.id;
END;
DROP TRIGGER IF EXISTS expenses_category_update;
CREATE TRIGGER expenses_category_update AFTER UPDATE OF category ON expenses
-- only when the spelling differs, so the trigger's own UPDATE doesn't loop
WHEN NEW.category COLLATE BINARY IS NOT (SELECT name FROM categories WHERE name = trim(NEW.category))
BEGIN
    INSERT OR IGNORE INTO categories (name) VALUES (trim(NEW.category));
    UPDATE expenses SET category = (SELECT name FROM categories WHERE name = trim(NEW.category)) WHERE id = NEW.id;
END;
DROP TRIGGER IF EXISTS budget_category_insert;
CREATE TRIGGER budget_category_insert AFTER INSERT ON budget
BEGIN
    INSERT OR IGNORE INTO categories (name) VALUES (trim(NEW.category));
    UPDATE budget SET category = (SELECT name FROM categories WHERE name = trim(NEW.category)) WHERE id = NEW.id;
END;
DROP TRIGGER IF EXISTS budget_category_update;
CREATE TRIGGER budget_category_update AFTER UPDATE OF category ON budget
WHEN NEW.category COLLATE BINARY IS NOT (SELECT name FROM categories WHERE name = trim(NEW.category))
BEGIN
    INSERT OR IGNORE INTO categories (name) VALUES (trim(NEW.category));
    UPDATE budget SET category = (SELECT name FROM categories WHERE name = trim(NEW.category)) WHERE id = NEW.id;
END;
DROP TRIGGER IF EXISTS balances_account_insert;
CREATE TRIGGER balances_account_insert AFTER INSERT ON balances
BEGIN
    INSERT OR IGNORE INTO accounts (name) VALUES (trim(NEW.account));
    UPDATE balances SET account = (SELECT name FROM accounts WHERE name = trim(NEW.account)) WHERE id = NEW.id;
END;
DROP TRIGGER IF EXISTS balances_account_update;
CREATE TRIGGER balances_account_update AFTER UPDATE OF account ON balances
WHEN NEW.account COLLATE BINARY IS NOT (SELECT name FROM accounts WHERE name = trim(NEW.account))
BEGIN
    INSERT OR IGNORE INTO accounts (name) VALUES (trim(NEW.account));
    UPDATE balances SET account = (SELECT name FROM accounts WHERE name = trim(NEW.account)) WHERE id = NEW.id;
END;

-- Tickers are upper case ("cspx" becomes "CSPX"), and each one gets a row in tickers
DROP TRIGGER IF EXISTS trades_ticker;
CREATE TRIGGER trades_ticker AFTER INSERT ON trades
BEGIN
    UPDATE trades SET ticker = upper(trim(NEW.ticker)) WHERE id = NEW.id AND ticker COLLATE BINARY IS NOT upper(trim(NEW.ticker));
    INSERT OR IGNORE INTO tickers (ticker, symbol) VALUES (upper(trim(NEW.ticker)), upper(trim(NEW.ticker)));
END;
DROP TRIGGER IF EXISTS trades_ticker_update;
CREATE TRIGGER trades_ticker_update AFTER UPDATE OF ticker ON trades
WHEN NEW.ticker COLLATE BINARY IS NOT upper(trim(NEW.ticker))
BEGIN
    UPDATE trades SET ticker = upper(trim(NEW.ticker)) WHERE id = NEW.id;
    INSERT OR IGNORE INTO tickers (ticker, symbol) VALUES (upper(trim(NEW.ticker)), upper(trim(NEW.ticker)));
END;

-- Spending per month and category
DROP VIEW IF EXISTS monthly_spending;
CREATE VIEW monthly_spending AS
SELECT substr(date, 1, 7) AS month, category, count(*) AS expenses, round(sum(amount), 2) AS spent
FROM expenses
GROUP BY month, category
ORDER BY month DESC, spent DESC;

-- One row per month with expenses: spent, the monthly budget and what's left of it, and on
-- how many days anything was logged
DROP VIEW IF EXISTS months;
CREATE VIEW months AS
SELECT substr(date, 1, 7) AS month,
       count(*) AS expenses,
       count(DISTINCT date) AS logged_days,
       round(sum(amount), 2) AS spent,
       b.budget,
       round(b.budget - sum(amount), 2) AS left
FROM expenses, (SELECT round(coalesce(sum(amount), 0), 2) AS budget FROM budget WHERE period = 'monthly') b
GROUP BY month
ORDER BY month DESC;

-- Each account's balance now: its newest entry
DROP VIEW IF EXISTS latest_balances;
CREATE VIEW latest_balances AS
SELECT account, balance, date AS as_of, notes
FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY account ORDER BY date DESC, id DESC) AS n FROM balances)
WHERE n = 1
ORDER BY account;

-- Shares held per ticker and what was bought and sold, in the share's currency. The
-- Savings tab adds what they're worth now and the gain.
DROP VIEW IF EXISTS holdings;
CREATE VIEW holdings AS
SELECT ticker,
       round(sum(CASE side WHEN 'buy' THEN shares ELSE -shares END), 6) AS shares,
       round(sum(CASE side WHEN 'buy' THEN shares * price + commission END), 2) AS bought_for,
       round(sum(CASE side WHEN 'sell' THEN shares * price - commission END), 2) AS sold_for,
       round(sum(commission), 2) AS commission,
       count(*) AS trades,
       min(date) AS first_trade,
       max(date) AS last_trade
FROM trades
GROUP BY ticker
ORDER BY bought_for DESC;
