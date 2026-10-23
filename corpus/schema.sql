-- corpus.db — social content back-catalog store
--
-- LAYOUT MATTERS: every CREATE TABLE comes first, then the @@POST@@ marker, then
-- indexes, FTS and triggers. store.connect() runs the halves separately with the
-- column migration in between, because an index or trigger that references a
-- newly-added column fails on an existing database if it runs before the ALTER.
-- Put new tables above the marker and everything else below it.
--
-- Design notes worth knowing before you change anything here:
--
--  * Thin typed core + raw_json on every row. When you discover in six months
--    that a field was normalised wrong, raw_json is what saves the corpus.
--  * Metrics are NOT on item, except a small denormalised cache for ORDER BY.
--    The truth is metric_snapshot, inserted change-only (see store.record_metrics).
--  * transcript lives on BOTH item and media. A carousel with two video slides
--    produces two transcripts; one column on item would silently squash them.
--    item.transcript is the primary/joined text (what FTS searches), media.transcript
--    is per-asset.
--  * No vector table, no generated columns, no lease queue. All were specified in
--    the first draft and none was earned by a real query. Add when something asks.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

-- ─────────────────────────────────────────────────────────────
-- ACCOUNTS
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS account (
  id              INTEGER PRIMARY KEY,
  platform        TEXT    NOT NULL,          -- linkedin | instagram
  platform_uid    TEXT,                      -- stable opaque id; handles change, this does not
  handle          TEXT    NOT NULL,
  display_name    TEXT,
  bio             TEXT,
  url             TEXT,

  -- relationship drives the legal path, not just reporting. client/own data comes
  -- from consented native exports; competitor/influencer comes from public routes.
  -- Required, never NULL, so the two classes stay separable in any deliverable.
  relationship    TEXT    NOT NULL CHECK (relationship IN
                    ('own','client','competitor','influencer')),
  client_ref      TEXT,                      -- which client this belongs to, if any

  follower_count  INTEGER,
  raw_json        TEXT,
  first_seen_at   INTEGER NOT NULL,
  last_scraped_at INTEGER,
  oldest_item_at  INTEGER,                   -- backfill watermark: how far back we've reached
  sync_cursor     TEXT,                      -- opaque vendor pagination cursor
  -- Set when a delta came back truncated: posts between this and the oldest one
  -- returned may be missing. Deltas keep starting here until a run reaches it,
  -- instead of the watermark jumping past the hole for good.
  delta_floor     INTEGER,
  UNIQUE (platform, handle)
);

-- ─────────────────────────────────────────────────────────────
-- ITEMS — the one canonical content table
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS item (
  id               INTEGER PRIMARY KEY,
  account_id       INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
  platform         TEXT    NOT NULL,
  platform_item_id TEXT    NOT NULL,

  kind             TEXT    NOT NULL,   -- text | image | carousel | short_video
                                       -- | long_video | article | comment
  parent_id        INTEGER REFERENCES item(id),

  -- A repost is somebody else's writing appearing on this account's feed. Counting
  -- it as the account's own content inflates every number and, worse, points a
  -- content audit at a stranger's post while addressing the account owner. So the
  -- true author is stored, and "is this theirs" is a question the brief can ask.
  is_repost        INTEGER NOT NULL DEFAULT 0,
  author_name      TEXT,        -- who actually wrote it
  author_handle    TEXT,

  -- same creative cross-posted to two platforms gets the same work_key, so
  -- "how did this piece do across channels" is answerable without double-counting.
  -- Nullable and unpopulated for now; the column is here to avoid a later migration.
  work_key         TEXT,

  -- four text layers, never squashed together. screen_text is what is BURNED INTO
  -- the video rather than spoken in it, and on a music-bed reel it is the entire
  -- argument: one post's caption read "Trust yourself, you did it once" while the
  -- screen said "You worry a lot for someone who figures it out every time." Without
  -- this column that post is unanalysable and the lens correctly says so.
  title            TEXT,
  body             TEXT,
  transcript       TEXT,
  screen_text      TEXT,
  lang             TEXT,

  -- How many times enrichment tried and failed on this item. A failure writes no
  -- transcript, so without a counter the work-list (derived from the data, not a
  -- queue) offers the same broken clip back on every run: re-download, re-transcribe,
  -- re-fail, forever. lang='none' already records "we looked, there was no speech";
  -- this records "we looked and could not tell".
  enrich_attempts  INTEGER NOT NULL DEFAULT 0,

  published_at     INTEGER NOT NULL,   -- unix seconds UTC
  url              TEXT,
  duration_s       REAL,

  -- latest-metric cache for sorting only; metric_snapshot is the truth
  m_reactions      INTEGER,
  m_comments       INTEGER,
  m_shares         INTEGER,
  m_views          INTEGER,
  metrics_at       INTEGER,

  -- When comment BODIES were last fetched for this post. Not the same question as
  -- how many comments it has: a thread routinely returns fewer rows than the count
  -- claims (the count includes replies, the vendor caps, some are deleted), so
  -- "have < count" is never satisfied and the post is re-fetched and re-BILLED on
  -- every run. With this, a post is only revisited when its metrics moved since.
  comments_at      INTEGER,
  -- m_comments at that fetch. A thread is re-fetched only when the COUNT grew past
  -- this, not whenever any metric moved: keying it on metrics_at re-bought every
  -- short thread each time its likes ticked up.
  comments_n       INTEGER,

  raw_json         TEXT    NOT NULL,
  source           TEXT,               -- apify:harvestapi/linkedin-profile-posts | li-export | ...
  content_hash     TEXT    NOT NULL,   -- sha256(title||body||transcript) — detects edits
  first_seen_at    INTEGER NOT NULL,
  last_seen_at     INTEGER NOT NULL,
  deleted_at       INTEGER,            -- set when a previously-seen item stops appearing

  UNIQUE (platform, platform_item_id)
);

-- ─────────────────────────────────────────────────────────────
-- MEDIA
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS media (
  id          INTEGER PRIMARY KEY,
  item_id     INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
  role        TEXT    NOT NULL,        -- original | poster | keyframe | slide | thumbnail
  seq         INTEGER NOT NULL DEFAULT 0,
  mime        TEXT,
  width       INTEGER,
  height      INTEGER,
  bytes       INTEGER,

  -- CDN URLs are signed and expire (IG video ~34h, images ~106h). Never treat this
  -- as durable: re-resolve from the item's platform id on demand. Kept only so a
  -- fetch inside the window can skip a lookup.
  remote_url  TEXT,
  fetched_at  INTEGER,

  local_path  TEXT,                    -- content-addressed: media/ab/cd/<sha256>.webp
  sha256      TEXT,
  transcript  TEXT,                    -- per-asset; see header note
  t_offset_s  REAL,                    -- keyframe offset within the video
  state       TEXT    NOT NULL DEFAULT 'referenced',  -- referenced|stored|discarded|failed
  created_at  INTEGER NOT NULL
);

-- ─────────────────────────────────────────────────────────────
-- METRICS AS TIME SERIES
-- ─────────────────────────────────────────────────────────────
-- Inserted change-only. Feed posts flatline within ~48h so most old rows never
-- move again; evergreen surfaces (articles, long video) keep accruing for months,
-- so expect meaningfully more rows there than a naive feed-only model predicts.
CREATE TABLE IF NOT EXISTS metric_snapshot (
  item_id     INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
  captured_at INTEGER NOT NULL,
  reactions   INTEGER,
  comments    INTEGER,
  shares      INTEGER,
  saves       INTEGER,
  views       INTEGER,
  -- platform-native names that do not fit the columns above: LinkedIn's per-type
  -- reaction breakdown, IG's separate play/view counters, etc.
  metrics_raw TEXT,
  PRIMARY KEY (item_id, captured_at)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS account_metric_snapshot (
  account_id  INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
  captured_at INTEGER NOT NULL,
  followers   INTEGER,
  following   INTEGER,
  posts_total INTEGER,
  PRIMARY KEY (account_id, captured_at)
) WITHOUT ROWID;

-- What the audience said back.
--
-- Deliberately NOT rows in `item`. A comment is written by a third party who never
-- consented to anything, while every row in `item` belongs to an account someone
-- chose to track. Keeping them apart means comments can never leak into post counts,
-- engagement math or the "every post" list by accident, and erasure can drop them
-- without touching authored content.
--
-- Author identity is stored as the display name the platform already shows publicly,
-- and nothing else: no profile URL, no headline, no follower count. The vendor will
-- sell all of that for roughly twice the price; mining objections does not need it,
-- and not holding it is the difference between reading a public reply and profiling
-- a stranger.
CREATE TABLE IF NOT EXISTS comment (
  id                  INTEGER PRIMARY KEY,
  item_id             INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
  platform_comment_id TEXT    NOT NULL,
  published_at        INTEGER,
  body                TEXT,
  author_name         TEXT,
  -- Replies by the post's own author are the subject talking, not the audience.
  -- Counted separately: a creator who answers every comment is doing something
  -- different from one who never returns, and averaging the two hides it.
  is_author_reply     INTEGER NOT NULL DEFAULT 0,
  reactions           INTEGER,
  lang                TEXT,
  fetched_at          INTEGER NOT NULL,
  raw_json            TEXT,
  UNIQUE (item_id, platform_comment_id)
);

-- A collab post appears on every co-author's feed but is stored once (one row per
-- platform id), owned by whichever account synced it first. This records the
-- others, so each co-author's audit still counts it instead of the second account
-- silently losing every post it shares.
CREATE TABLE IF NOT EXISTS item_account (
  item_id     INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
  account_id  INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
  PRIMARY KEY (item_id, account_id)
) WITHOUT ROWID;

-- ─────────────────────────────────────────────────────────────
-- PROVENANCE
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scrape_run (
  id            INTEGER PRIMARY KEY,
  account_id    INTEGER REFERENCES account(id),
  adapter       TEXT    NOT NULL,
  mode          TEXT    NOT NULL,      -- backfill | delta | metrics_only
  started_at    INTEGER NOT NULL,
  finished_at   INTEGER,
  status        TEXT    NOT NULL DEFAULT 'running',  -- running|ok|error
  items_seen    INTEGER NOT NULL DEFAULT 0,
  items_new     INTEGER NOT NULL DEFAULT 0,
  items_changed INTEGER NOT NULL DEFAULT 0,
  cost_usd      REAL,
  error         TEXT
);

-- @@POST@@ ---------------------------------------------------------------
-- Everything below runs AFTER column migration. See the header.

CREATE INDEX IF NOT EXISTS ix_account_rel     ON account(relationship, platform);
CREATE INDEX IF NOT EXISTS ix_item_account_pub ON item(account_id, published_at DESC);
CREATE INDEX IF NOT EXISTS ix_item_kind_pub    ON item(kind, published_at DESC);
CREATE INDEX IF NOT EXISTS ix_item_work        ON item(work_key) WHERE work_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_media_item       ON media(item_id, role, seq);
CREATE INDEX IF NOT EXISTS ix_media_sha        ON media(sha256) WHERE sha256 IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_comment_item   ON comment(item_id, published_at);
CREATE INDEX IF NOT EXISTS ix_item_account   ON item_account(account_id);
CREATE INDEX IF NOT EXISTS ix_run_adapter      ON scrape_run(adapter, started_at DESC);

-- SEARCH — FTS5 external-content (index only, no text duplication)
CREATE VIRTUAL TABLE IF NOT EXISTS item_fts USING fts5(
  title, body, transcript, screen_text,
  content='item', content_rowid='id',
  tokenize='unicode61 remove_diacritics 2'
);

-- External-content FTS does not track the base table by itself; these triggers are
-- the contract. The UPDATE trigger matters more than it looks: enrichment fills
-- transcript long after insert, and without it those rows stay unsearchable.
CREATE TRIGGER IF NOT EXISTS item_ai AFTER INSERT ON item BEGIN
  INSERT INTO item_fts(rowid, title, body, transcript, screen_text)
  VALUES (new.id, new.title, new.body, new.transcript, new.screen_text);
END;

CREATE TRIGGER IF NOT EXISTS item_ad AFTER DELETE ON item BEGIN
  INSERT INTO item_fts(item_fts, rowid, title, body, transcript, screen_text)
  VALUES ('delete', old.id, old.title, old.body, old.transcript, old.screen_text);
END;

CREATE TRIGGER IF NOT EXISTS item_au AFTER UPDATE ON item BEGIN
  INSERT INTO item_fts(item_fts, rowid, title, body, transcript, screen_text)
  VALUES ('delete', old.id, old.title, old.body, old.transcript, old.screen_text);
  INSERT INTO item_fts(rowid, title, body, transcript, screen_text)
  VALUES (new.id, new.title, new.body, new.transcript, new.screen_text);
END;
