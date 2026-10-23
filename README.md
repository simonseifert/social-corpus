# corpus

**Point it at a social profile. Get back everything that account has published, with
the numbers, the media, and an honest read on what is working.**

It pulls a LinkedIn, Instagram or YouTube Shorts back-catalogue into a database on your own machine,
transcribes the videos, and runs your prompts over the content to produce a
client-ready audit you can send.

It replaces the afternoon you would otherwise spend scrolling somebody's profile,
screenshotting posts into a doc, and guessing at what performed.

---

## What you get

One self-contained HTML file per subject. No login, no account, nothing to install to
read it — it opens in any browser and can be emailed as-is.

It contains:

- **Headline numbers** — posts, reactions, comments, shares, views, followers, posting
  rate. Every figure labelled in plain language, because a client should not have to
  ask what "cadence" means.
- **Platform comparison** when someone runs more than one account.
- **Top posts**, ranked, with the excerpt and a link to the original.
- **What formats work** — median engagement per format, so one viral post does not
  make a format look reliable.
- **When they post** — engagement by weekday on the audience's clock (`CORPUS_TZ`,
  e.g. `Europe/Zagreb`; default this machine's zone), with single-post days hatched
  and excluded from the scale.
- **Career history** — roles, dates, employer, education. Pulled from the profile, so
  a positioning claim can be checked against what the person actually does.
- **Every post**, as a scannable row that expands on click into the full breakdown:
  the image or video frame, all metrics, the caption, the transcript, the permalink,
  and the AI read on that specific post.
- **What they amplify** — reposts, credited to whoever actually wrote them.
- **What this cannot show** — named explicitly. See "Honest limits" below. On a
  client's brief this becomes the ask: send your Instagram / LinkedIn export.
- **When the data was captured**, at the top, in red once it is over 14 days old.
  Periods count back from the account's last sync, not from today.
- **The account summary first** when one has been run (`account-summary`): what is
  going on, three priorities, a four-week plan. Then every client-safe lens per
  post, in a fixed order; lenses under `templates/personal/` never appear unless
  asked for with `--template`.
- **Croatian** with `--lang hr` (headings, notes, dates); English is the default.

---

## What it costs

Fetch and comment costs measured over 125 real posts (August 2026). Analysis runs on
the local model by default and costs nothing; the cloud figures are for when it is
not available.

| Unit | Cost |
|---|---|
| Fetch one LinkedIn post | $0.0020 |
| Fetch one Instagram post | $0.00054 |
| Analyse one post (per lens) | **$0** on the local model; ~$0.0005 on Mistral Small, ~$0.0008 on Groq |
| Transcribe a reel | **$0**: yap on the Mac (English) or your own whisper.cpp server (any language); Groq ~$0.0005 as the fallback |
| One LinkedIn comment | $0.0020 |
| One Instagram comment | $0.0005 |
| **All-in per LinkedIn post**, 3 lenses | **~$0.002 local**, ~$0.0035 on Mistral |

The local model is slower than a cloud API (tens of seconds a post rather than a
few), which is the price of nothing leaving the machine.

Comment text is opt-in and priced per comment, so it scales with how much an audience
talks rather than with post count. Measured across five real accounts: **398 comments
for $0.71**, against a $2.95 worst-case estimate — most posts have fewer comments than
the 25-per-post cap. A busy LinkedIn creator (58 posts, 918 counted comments) came to
$0.48. Run `corpus comments --dry-run` for the ceiling before spending.

### Realistic scenarios (fetch + 3 lenses, priced at a cloud model's rates; on the local model roughly half of each figure, the lens share, drops to $0)

| Scenario | Cost |
|---|---|
| One person trying it (2 profiles × 100 posts) | ~$0.85 |
| A test week: 5 people, 2 profiles each | ~$4.30 |
| An agency: 10 clients, monthly refresh | ~$1.30 |
| Heavy: 50 competitor profiles × 200 posts | ~$35 |

### Apify: free plan or Starter

Every Apify actor corpus uses charges the **same per item on the free plan as on
Starter** (checked live from each actor's price card on 2026-10-22). The free plan
includes **$5 of usage a month**, enough for ~2,500 LinkedIn or ~9,000 Instagram
posts. Starter ($19/month, with $19 of usage included) is for when that runs out.

Tiered pricing does exist on the Store (one X actor billed 100x more on free in
August 2026), so before any run over 50 items `corpus` reads the actor's live price
card for your tier and refuses if it is more than 1.5x the paid price, and refuses a
run that would not fit what is left of your monthly budget.

Everything else is pay-as-you-go with no minimum:

| | |
|---|---|
| Local model via Ollama (the analysis) | $0, your own GPU |
| Mistral (EU cloud fallback) | pay per token, no minimum |
| Groq (public fallback + non-English audio) | free tier is enough |
| Transcription | $0 — your Mac or your own whisper server, nothing uploaded |
| Hosting, storage, database | $0 — it all lives on your laptop |

> If several people share one Apify token they share one monthly budget and one rate
> limit. Separate tokens per person is cleaner.

---

## Install

Needs macOS or Linux and Python 3.11+.

```bash
git clone <repo-url> corpus
cd corpus
./install.sh
# or by hand: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

That creates a virtualenv, installs the `corpus` command, and writes a key file at
`~/.reach/keys.env` explaining what each key unlocks. Put your keys in it:

```
HARVESTAPI_KEY=...     # LinkedIn persons and company pages (first LinkedIn route)
SCRAPECREATORS_API_KEY=...  # Instagram, TikTok, Facebook pages
BRIGHTDATA_API_KEY=... # optional: shallow LinkedIn/Instagram fallback
APIFY_TOKEN=...        # optional: Apify actor fallbacks. Use a PAID plan.
MISTRAL_API_KEY=...    # optional: EU-hosted analysis when the local model is down
GROQ_API_KEY=...       # optional: fallback for competitor posts, and non-English audio
```

The analysis runs on a local model through [Ollama](https://ollama.com) when one is
reachable (`OLLAMA_URL`, default `http://127.0.0.1:11434`; model `CORPUS_OLLAMA_MODEL`,
default `gemma4:12b`). See "Where the text goes" below for which provider may see what.

Two optional extras make it better and nothing breaks without them:

```bash
brew install ffmpeg    # video keyframes and audio
brew install yap       # free on-device transcription (macOS 26+)
# Linux: apt install ffmpeg tesseract-ocr tesseract-ocr-hrv   (on-screen text)
```

---

## Using it

```bash
# 1. track a profile
corpus add https://www.linkedin.com/in/someone/ --relationship competitor
corpus add https://instagram.com/someone --relationship client --client-ref acme

# 2. fetch their content  (--dry-run first: prices it, spends nothing)
corpus sync --dry-run
corpus sync

# 3. transcripts, keyframes and images        (optional)
corpus enrich

# 4. what the audience said back  (--dry-run prices it first)
corpus comments --dry-run
corpus comments someone

# 5. run a lens over it   (without --execute this is a dry run that spends nothing,
#    and lists where each post would be sent)
corpus analyze content-audit --execute
corpus analyze account-summary --account someone --lang hr --execute

# 6. the deliverable  (last 12 months to the last sync; --days 0 for everything)
corpus brief someone
corpus brief all --client-ref acme             # titled with the client's name
corpus brief all --client-ref acme --lang hr --csv   # Croatian chrome, posts as CSV

# ...or browse the whole thing in a browser
corpus dashboard
```

Do not know the URL:

```bash
corpus find "Their Name" --location "Their City" --company "Their Co"
```

That returns **ranked candidates for you to confirm, never an answer**. A name is a
search: the author's own full name returns four different people on LinkedIn and none
of them is the right one. Picking the top hit automatically would build a corpus of the wrong
person and every number after that would be confidently wrong.

### The rest

| Command | |
|---|---|
| `corpus dashboard` | browse profiles, search, and open briefs at localhost:8891 |
| `corpus search "cold outreach"` | full-text across every post and transcript |
| `corpus outliers` | competitor posts from the last 14 days at 3x+ their account's usual and 50+ interactions (`--min`, `--all` adds your own and clients'); `sync` lists new ones too |
| `corpus compare <client> <rival> <rival>` | side-by-side benchmark: rate, typical post, formats, best day, and each rival's best posts. `--client <ref>` matches every platform of a client with its same-platform rivals; skipped accounts are named with the reason |
| `corpus comments --dry-run` | price fetching comment text before spending |
| `corpus show 412` | one post in full, with its engagement over time |
| `corpus profile <url> --json` | the stored profile payload, read-only, so another tool can reuse it instead of paying for the same actor run |
| `corpus health` | per-account freshness, failed runs, backups, whisper, spend this month; exits 1 on any red line (`--json`) |
| `corpus fix-paths` | rewrite stored media paths relative to the media root (dry run; `--execute`) |
| `corpus add <url> --relationship <rel> --client-ref ""` | re-add to change an account's relationship, or clear its client |
| `corpus adapters` | every route, its price, and what it can and cannot get |
| `corpus backup` | snapshot, verify and prune (the nightly job runs it) |
| `corpus weekly` | what changed in the last 7 days, short enough for WhatsApp; `--send` delivers it, `--client-ref` scopes it, `--out x.html` saves an HTML copy |
| `corpus notify "text"` | send a message on the alert channel; `--unit <name>` reports a failed systemd unit |
| `corpus analyze account-summary --account <h>` | one read on the whole account: summary, 3 priorities, a 4-week plan (JSON + markdown); `--lang hr` for Croatian, works on every lens |
| `corpus retention` | age out comments on competitor/influencer posts (dry run; `--execute`) |

### `--relationship` is the important flag

It decides which routes are legitimate and keeps consented client data separate from
public competitor data.

| | Meaning |
|---|---|
| `own` | your own accounts |
| `client` | someone who has asked you to do this |
| `competitor` / `influencer` | public accounts you are studying |

`own` and `client` get the free first-party routes first (a LinkedIn analytics export
carries impressions and reach that no scraper can). `competitor` and `influencer` only
ever get public routes.

---

## Routes

`corpus adapters` prints this live, including what each one cannot get.

Cost / 1k is the **marginal** price, measured where it says so. Routes are tried
cheapest-first; a free allowance only breaks ties (corpus cannot see how much of it is
left), and a shallow route (one that cannot reach an account's old posts) goes last.

| Route | Platform | Cost / 1k | Free quota | For |
|---|---|---|---|---|
| `linkedin_export` | LinkedIn | free | | own / client — LinkedIn's own exports: personal (`AggregateAnalytics_*` / older `Content_*`), per-post (`SinglePostAnalytics_*`), company Page (`*_content_*.xls`, the only one with post text). `sync --file` takes the file or a folder such as `~/Downloads` |
| `linkedin_harvest` | LinkedIn | $0.04 (measured: $0.004 per 100-post page) | | anyone — person and company pages via HarvestAPI's direct API, real pagination |
| `linkedin_posts` | LinkedIn | $2.00 | | anyone — same data through the Apify actor; fallback |
| `brightdata_linkedin` | LinkedIn | $1.50 | 5,000 records/mo, shared | anyone — **shallow**: ~10 newest posts per profile, no shares |
| `scrapecreators_instagram` | Instagram | $0.16 (12 posts per credit) | ~7,100 prepaid credits | anyone — posts, reels, carousels, follower counts |
| `instagram_public` | Instagram | $0.50 | | anyone — public posts and reels via Apify |
| `brightdata_instagram` | Instagram | $1.50 | 5,000 records/mo, shared | anyone — async dataset, capped at 100 records a fetch |
| `instagram_free` | Instagram | free | | shallow: the newest dozen posts |
| `instagram_client` | Instagram | free | | own / client — session cookie, **opt-in only** (see below) |
| `scrapecreators_tiktok` | TikTok | $0.19 (10 videos per credit) | shared with the above | anyone — views, likes, comments, shares, saves |
| `scrapecreators_facebook` | Facebook | $0.63 (3 posts per credit) | shared with the above | public **pages** — reactions by type, comments, shares, video views |
| `youtube_shorts` | YouTube | free | | anyone — every public Short via yt-dlp, with views, likes, comments |

Order today: LinkedIn `linkedin_harvest` > `linkedin_posts` > `brightdata_linkedin`;
Instagram `scrapecreators_instagram` > `instagram_public` > `brightdata_instagram` >
`instagram_free`. The paid routes outside Apify each carry their own pre-run check
(HarvestAPI and ScrapeCreators read their balance, free; Bright Data refuses a sync
that would pull more than 1,000 of its 5,000 monthly records), and `--dry-run` prices
them the way they bill: per page or per request, not per post. Vendor details,
endpoints and error shapes: `docs/vendors.md`.

Bright Data is wired but **unverified**: on 2026-10-23 the account answered "Customer
is not active" to every trigger. Activate it in the control panel, then run one
`corpus sync <handle> --limit 10` on a throwaway DB before trusting it.

The cheapest usable route wins, and a route that cannot run right now (no export file,
no cookies, no key) falls through to the next instead of failing.

**No paid route uses your own social account.** The paid routes are vendor APIs
that need no cookie and no login, so nothing here can get a LinkedIn or Instagram
account restricted.

`instagram_client` is the exception and is **off unless `CORPUS_ALLOW_IG_SESSION=1`**
is set for that run. It calls Instagram's private web API with the account's own
session cookie, and an account was flagged for automation twice when a session
script like it ran on a timer. Without the switch, own and client Instagram
accounts use `instagram_public`. Never set it in a systemd unit or cron job.

---

## Running it on a schedule (Linux, systemd)

corpus is only as current as its last sync, and an unattended job that fails into
a log file nobody reads looks exactly like one that works: an earlier launchd setup
went 78 days without a scheduled sync before anyone noticed. On Linux it runs as
systemd **user** units, and every one of them reports failure to your phone.

| Unit | When | What |
|---|---|---|
| `corpus-nightly` | 01:30 daily | `deploy/corpus-nightly.sh`: sync, enrich, retention (once that command exists, and only on a night with no earlier failure), backup, copy the newest snapshots to `$CORPUS_OFFSITE_HOST:~/corpus-backups-offsite/` over ssh/rsync (skipped if unset, logged if the host is unreachable), health |
| `corpus-weekly` | Monday 08:00 | `corpus weekly --send` |
| `ytdlp-upgrade` | Sunday 23:00 | upgrades yt-dlp (pipx, plus any `pip --user` copy shadowing it on PATH) |
| `corpus-notify@` | on failure | `corpus notify --unit <failed unit>`: unit, exit status and its last 15 journal lines, secrets scrubbed |

If other GPU-heavy jobs share the box, move the timer so enrich (which transcribes
on the whisper server) does not queue behind them.

```bash
deploy/install-systemd.sh           # copy units, daemon-reload, enable the timers
deploy/install-systemd.sh --no-enable
systemctl --user start corpus-nightly.service   # run once now
journalctl --user -u corpus-nightly -n 100
```

The units run the checkout the installer lives in (or `$CORPUS_REPO`): the
`@CORPUS_REPO@` placeholder in `deploy/systemd/*.service` is filled in at install time,
and units are copied, not linked, so moving a checkout cannot silently disarm a timer.
Set `CORPUS_TZ` in the units to your audience's timezone (default `UTC`), and
`CORPUS_OFFSITE_HOST` (an ssh host or `~/.ssh/config` alias) in
`corpus-nightly.service` if you want an off-box copy. The installer warns when
lingering is off (`sudo loginctl enable-linger $USER`), without which nothing fires
after you log out.

**Alerts** go to a WhatsApp REST bridge you run yourself (any HTTP endpoint that takes
`POST {"recipient","message"}` with an `X-API-Key` header) and/or an
[ntfy](https://ntfy.sh) topic, configured in `~/.config/corpus/notify.json` (falls
back to a shared `~/.config/notify-bridge.json`):

```json
{"rest_url": "http://127.0.0.1:8083/api/send", "rest_env": "~/path/to/bridge.env",
 "recipient": "<number>@s.whatsapp.net",
 "ntfy_url": "https://ntfy.sh/<topic>", "unit_detail": "tail"}
```

`rest_env` is a file holding `API_KEY=...`; the key is read at send time and never
printed. `ntfy_url` is optional, a second path when the bridge is down.
`"unit_detail": "status"` sends only the unit name and exit status, no log lines.

**macOS** has no systemd; schedule `deploy/corpus-nightly.sh` with a launchd agent
(`StartCalendarInterval`, absolute paths, since launchd's `PATH` omits Homebrew and
the venv).

## The dashboard

```bash
corpus dashboard          # http://127.0.0.1:8891
```

Profiles and their counts, every post per account, full-text search across captions,
transcripts, on-screen text **and comments**, and one-click briefs. Sync, enrich and
comment fetches run as background jobs, one at a time, because SQLite takes exactly
one writer.

It binds `127.0.0.1` and **refuses to listen on a public interface without a token**.
The database holds comment text written by third parties who never agreed to any of
this. To reach it from another machine, tunnel rather than expose:

```bash
ssh -N -L 8891:127.0.0.1:8891 your-machine
```

## Honest limits

These are structural, not bugs, and the brief says so on the page.

- **Reach, impressions, watch time and saves are owner-only.** No API and no scraper
  produces them for an account you do not own. Any tool claiming competitor watch time
  is fabricating it. This is why a client's own export is worth more than any scrape.
- **Instagram Stories cannot be backfilled, ever.** There is no archive endpoint. A
  client you onboard today has zero story history available.
- **Comment text is capped and one-level.** `corpus comments` fetches the bodies, but
  at 25 per post by default and **without replies to comments**. So an author
  answering a commenter is invisible: the brief therefore reports what the audience
  said and refuses to report a reply rate, because the data cannot support one.
- **A LinkedIn Analytics export caps at ~50 posts** per ranking, 365 days.
- **Audience sentiment is not scored.** We hold the comments and quote them verbatim.
  Turning a few dozen into a "sentiment score" would be inventing a number, which is
  what the competitors printing one are doing.
- **Croatian and other Slavic audio needs the cloud fallback.** Apple's on-device
  engine ships no Slavic locales, and handed Croatian audio it does not fail — it
  returns fluent invented English. So the language is detected from the caption first
  and anything non-English routes to Whisper: your own whisper.cpp server when
  `WHISPER_SERVER_URL` is set ($0, nothing uploaded), Groq otherwise.
- **A sync is capped at `--limit` posts.** If an account posted more than that since
  the last sync, the run says so, and the next sync starts from the gap rather than
  from the newest post, until it gets all the way down. Raise `--limit` to close it in
  one go.
- **A collab post is stored once**, under whichever co-author synced first, and counts
  in every tracked co-author's brief. `forget` on one co-author leaves it with the
  other.
- **Music-only videos produce no transcript.** Whisper invents on them ("Субтитры
  создавал DimaTorzok" is a real example from a real reel), so those are detected and
  discarded rather than stored. What is written ON the frames is read instead, with
  Apple's Vision framework on a Mac and tesseract on Linux, and that is what carries
  the message on a music reel. Tesseract is the weaker reader of the two.
- **Machine transcripts are labelled as such.** A lens is told a transcript may
  contain errors, and one that looks like Whisper junk (one word on repeat, a chorus,
  "Thank you." over silence) is withheld from it rather than analysed.
- **Posts about grief, illness or loss are not assessed.** A lens once told a client
  to "convert grief into authority" on a post about their mother's death. Such posts
  are filtered before any model sees them (English and Croatian phrases) and every
  prompt carries a rule to decline the rest; the brief shows "personal post, not
  assessed".

---

## How it works, in one paragraph

Extraction is **rented**, not written. Scrapers rot, get accounts banned, and attract
legal attention — four products in this exact category died between 2025 and 2026, all
because they owned the scraping. So vendors do the fetching and absorb the breakage,
and what lives here is the part that compounds: the stored content and the prompts.
Every post is kept with its untouched vendor payload beside it, so when a projection
turns out wrong it is rebuilt from what is on disk rather than re-bought.

---

## Your data

Everything is a file on your machine.

```
~/.corpus/corpus.db      the content, metrics and history
~/.corpus/analysis.db    the AI output, separate on purpose
~/.corpus/media          keyframes and images
~/.corpus/backups        daily snapshots, verified, 14 kept
~/Downloads/*.html       the briefs
```

No server, no accounts, no shared state. Each person keeps their own database.

### Where the text goes

Every analysis payload is classified before anything is sent:

| Payload | May go to |
|---|---|
| **Private**: an `own`/`client` account's posts, a collab post shared with one, anything carrying audience comments, or any prompt with `--context` notes | only `CORPUS_PRIVATE_PROVIDERS`, default `ollama,mistral`: the local model, or Mistral (EU-hosted, DPA available; ask for zero data retention on the account) |
| **Public**: a competitor's or influencer's post text | the public chain, default `ollama,mistral,groq,gemini,cerebras,nvidia`, or whatever `CORPUS_PROVIDERS` says |

If no private provider is reachable, private items are **skipped with a message**,
never sent somewhere else instead. Commenter names are replaced with "Commenter A",
"Commenter B" before any prompt is built. `corpus analyze <lens>` without `--execute`
prints the destination of every item.

DeepSeek is no longer in any default chain. Its hosted API stores requests in China
and may use them for training, it offers no DPA or SCCs, and several EU data
protection authorities have acted against it; client text and third-party comments
must not go there. It still works for public competitor text if you name it in
`CORPUS_PROVIDERS`.

### Retention

Comments on competitor and influencer posts are written by people who never agreed to
be in this database. `corpus retention` deletes them after 12 months (`--months`) and
blanks the raw vendor payload beside them after 3 (`--raw-months`); comments on
`own`/`client` posts are never touched (`corpus forget` erases those on request). The
basis is legitimate interest, and the EDPB's draft guidelines 03/2026 on web scraping
(public consultation open until 30 October 2026) make minimisation and a defined
retention period central to that test: collect only what the analysis needs (no
commenter profiles, only the display name already public), keep it no longer than it
is useful, and be able to say when it goes. Run it monthly; dry run first.

The analysis lives in a **second file the fetch path never opens**, so you can delete
it, re-run with better prompts, and the content is untouched. Editing a prompt
automatically re-runs the work and keeps the old result alongside, which makes "did my
new prompt get better" a query rather than a feeling.

Backups run nightly (see "Running it on a schedule"), get copied off the box, verify themselves by opening the snapshot and counting rows, and
have a tested restore path. A run where any snapshot fails verification prunes
nothing, an unchanged database is not copied again (the newest snapshot is
re-verified instead), and `--restore` puts a snapshot back over the database its
name says it came from, refusing anything else.

Media paths are stored relative to the media root (`CORPUS_MEDIA`, default
`~/.corpus/media`), so the corpus can move between machines. After moving one, or
on a database from before this, run `corpus fix-paths` (dry run) then
`corpus fix-paths --execute`: legacy absolute paths are rewritten, and rows whose
file is gone are cleared so `enrich` fetches the image again.

---

## Cost safety

Three things stop a run costing more than you expected:

1. **`--dry-run` on `sync`** makes no network call at all and prints the estimate.
2. **`analyze` dry-runs by default.** You have to pass `--execute` to spend anything.
3. **The spend guard** runs before any Apify run over 50 items (sync and comments,
   CLI and dashboard): it refuses if the actor charges your tier more than 1.5x the
   paid price, if the actor's live price has risen more than 25% above what corpus
   expects (every run's spend cap is sized from that price, and a capped run ends
   "SUCCEEDED" with half the data), or if the run would not fit the remaining monthly
   budget. A probe is counted at its worst case (1 item + the full limit).
   Routes outside Apify run their own check instead (balance or record budget; see
   Routes).

A comment batch that ran out of items before reaching every post leaves the unreached
posts queued instead of filing them as empty, and a thread is only bought again when
its comment count grows, not whenever its likes move.

Every run records what it cost, comment batches included. `corpus health` shows
this month's total.

A run that pays for vendor records and cannot parse more than a fifth of them
(5 or more records) is marked failed with the reason, instead of completing "ok"
with fewer posts: that is what a vendor changing its output shape looks like.

### Running unattended

Built to run from a timer with nobody watching, so failures are loud:

- **Exit codes.** `sync`, `enrich`, `comments`, `analyze --execute`, `backup` and
  `health` exit non-zero on any failure; a run row is only `ok` when nothing failed.
- **One writer.** Every writing command (and the dashboard's jobs) takes a lock on
  `.lock` beside the database. A second run exits **75** immediately instead of
  racing the first.
- **SIGTERM** (`systemctl stop`, shutdown) closes open runs as `interrupted`; rows a
  killed process left `running` for over 2h are closed by the next run.
- **Transcriber outages are not the clip's fault.** A whisper server that is down,
  refusing or timing out (or Groq 5xx/429) does not use up a video's three enrich
  attempts. Servers are pinged once per run; three outages in a row stop the run.
  Self-hosted requests time out after 300s (`WHISPER_TIMEOUT_S`).
- Old temp dirs and brief thumbnails (60 days) are pruned; a re-keyframe deletes
  the frames it replaced.

---

## Development

```bash
.venv/bin/python -m pytest tests/ -q     # ~850 tests, ~30s, all offline
```

Adapter tests run against vendor payloads modelled on real recorded responses (the
shapes, quirks and edge cases are real; every account, name, text, id and URL in
`tests/fixtures/` is invented), so a vendor changing its output shape fails loudly
instead of writing nulls. There are also tests that assert on the
*rendered HTML* — for stringified Python objects, double-escaped entities, blank
cells, unexplained jargon — because every mistake worth catching in a document is
invisible in the source and obvious on screen.

```
corpus/
  store.py       SQLite: idempotent writes, change-only metric history, search
  adapters/      one file per route
  enrich/        transcription, keyframes, image fetch, frame selection
  analyze/       lens templates over stored content, in a separate database;
                 privacy.py routes each payload, sensitivity.py guards personal posts
  textq.py       is this machine transcript usable
  retention.py   ages out third-party comments
  brief.py       the deliverable
  backup.py      snapshot, verify, restore
  health.py      the checks behind `corpus health`
  ops.py         run lock, SIGTERM, temp and thumbnail pruning
  cli.py         the front door
templates/       client-facing lenses
templates/personal/   creator-side lenses, not for client documents
```

---

## Ethics and platform terms

corpus is built for content audits of your own accounts, of clients who asked you to
do one, and of public competitor or creator accounts you are studying. Please keep it
that way:

- **Public data only.** It reads what an account has published publicly (or, for
  `own`/`client`, what the account owner exported or consented to). It does not and
  should not be extended to read private profiles, DMs or anything behind someone
  else's login.
- **Respect each platform's Terms of Service** and each vendor's terms. Fetching is
  delegated to commercial data vendors who carry that relationship; you are still
  responsible for how you use what they return, including data protection law (GDPR
  and similar) where you and the people in the data live.
- **No session-cookie scraping by default.** The only route that uses a logged-in
  session (`instagram_client`) is off unless `CORPUS_ALLOW_IG_SESSION=1` is set for
  a single run, and should only ever be used on an account you own. Never put it on a
  timer: platforms read scheduled cookie traffic as a bot, and the penalty lands on
  the account.
- **Third-party comments are minimised.** Commenters are pseudonymised before any
  prompt, comments on competitor posts age out (`corpus retention`), and `corpus
  forget` erases an account on request.
- **Not for profiling individuals.** Do not use it to monitor, track or build dossiers
  on private people.

## License

MIT, see `LICENSE`.
