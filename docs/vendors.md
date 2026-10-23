# Social data vendors: a reference for corpus

Compact facts, reusable outside corpus too. **Measured** means a live call on the date given;
everything else is from the vendor's docs on that date. Keys live in
`~/.reach/keys.env` (names below); never print values.

Last full check: 2026-10-23.

## Summary

| Vendor | Key name | Billing unit | Measured cost | Balance read | Status |
|---|---|---|---|---|---|
| HarvestAPI | `HARVESTAPI_KEY` | per request (USD) | $0.004 per posts page (~100 posts), $0.004 per profile (`main=true`) or company lookup | `GET /users/my-api-user` → `usage.balance`, free | live, routed first for LinkedIn |
| ScrapeCreators | `SCRAPECREATORS_API_KEY` | per request (credits) | 1 credit = 12 IG posts / 10 TikToks / 3 FB posts | `GET /v1/credit-balance` → `creditCount`, free | live, routed first for IG, only route for TikTok/FB |
| Bright Data | `BRIGHTDATA_API_KEY` | per delivered record | 1 error record billed on the one reviews test | not readable with this key (403) | account accepts jobs since 2026-10-23; reviews wired in a companion tool, unverified |

Spend in this check (2026-10-23): HarvestAPI $0.016 (balance $0.992 → $0.976),
ScrapeCreators 10 credits (7,100 → 7,090), Bright Data 0 records.
Other tools spent from the same balances at the same time, so balance deltas over
this window are not attributable to one tool.

---

## HarvestAPI (harvestapi.io)

- **Base URL** `https://api.harvestapi.io` **Auth** header `X-API-Key: <key>`
- **Docs** `https://docs.harvestapi.io/llms.txt` (per page: `<path>.md`)

| Endpoint | Params | Notes |
|---|---|---|
| `GET /linkedin/profile-posts` | `profile` (URL, public id or profile id), `page`, `paginationToken`, `postedLimit` (`24h`/`week`/`month`), `scrapePostedLimit` | ~100 posts/page, newest first. **$0.004/page measured** 2026-10-22 (2 pages, 200 posts; 60/60 ids identical to the Apify `harvestapi~linkedin-profile-posts` actor) |
| `GET /linkedin/company-posts` | `company` (URL, universal name or id), same paging | **$0.004/page measured** 2026-10-23 (a ferry company: 86 posts in one page) |
| `GET /linkedin/profile` | `query` (URL / public id / id), `main=true` (cheaper: only the unexpanded profile), `findEmail` | single profile lookup. **$0.004 with `main=true` measured** 2026-10-23 (one call, a public figure's profile): `payments: ["linkedinProfileShort"]`. Body: `element` = the profile (`firstName`, `lastName`, `headline`, `about`, `experience`, `education`, `followerCount`, `connectionsCount`, `location`, ...), same shape as the Apify `harvestapi~linkedin-profile-scraper` record. Without `main=true` is a pricier full-profile tier (not called) |
| `GET /linkedin/company` | `query` (URL / universal name / id) | single company lookup. **$0.004 measured** 2026-10-23 (a company page): `payments: ["linkedinCompany"]`. `element` has `name`, `universalName`, `followerCount`, `employeeCount`, `employeeCountRange`, `industries`, `locations`, `website`, `foundedOn`, `specialities`, `description` |
| `GET /users/my-api-user` | | free. `usage.balance` = USD left; `user.totalBalance` is the package size, NOT what is left; `usage.requestsConcurrency` |

- **Every response states its own price**: top-level `cost` (USD, e.g. `0.004`) and
  `payments` (e.g. `["linkedinPostSearch"]`). Use it instead of estimating.
- **Paging**: pass back `pagination.paginationToken` with `page+1`. `totalPages` can say
  1 while a token is still returned; an empty page ends the walk.
- **Payload** is identical to the Apify actor's records (`id` = activity id,
  `postedAt.timestamp` in ms, `engagement.{likes,comments,shares,reactions[]}`,
  `repostedBy`). No views, no saves.
- **Concurrency**: free 1, Starter 5, Basic 10, Pro 20, Business 40 concurrent
  requests; no per-minute rate limit; excess requests queue (queue size 10).
- **Errors**: invalid key → HTTP 401
  `{"status":401,"error":{"error":"Invalid API key","status":401},...}`. A 200 body can
  also carry `error`. 429 → back off (corpus retries 2/4/8/16 s).
- **Gotchas**: no `maxTotalChargeUsd`; bound pages yourself (corpus: 20 pages = $0.08
  max per fetch) and stop a delta at the first page older than the watermark.

## ScrapeCreators (scrapecreators.com)

- **Base URL** `https://api.scrapecreators.com` **Auth** header `x-api-key: <key>`
- **Docs** `https://docs.scrapecreators.com/llms.txt`; per endpoint `<path>.md`;
  full spec `https://docs.scrapecreators.com/openapi.json` (196 paths on 2026-10-23)
- **Price**: 1 credit per request unless the page says otherwise; cheapest pack $47 /
  25,000 credits = $0.00188/credit (Aug 2026). Read `credits_charged` /
  `credits_remaining` from every response.

| Endpoint | Params | Per credit (measured 2026-10-23) |
|---|---|---|
| `GET /v2/instagram/user/posts` | `handle`, `next_max_id`, `trim` | 12 posts. Instagram web-API shape (`pk`, `code`, `media_type` 1/2/8, `taken_at`, `like_count`, `comment_count`, `play_count`, `video_duration`, `carousel_media`, `image_versions2`, `video_versions`) + `user` block with `follower_count`, `following_count`, `media_count`. Cursor: `next_max_id` while `more_available` |
| `GET /v3/tiktok/profile/videos` | `handle` or `user_id`, `max_cursor`, `sort_by`, `include_pinned`, `region`, `trim` | 10 videos. `aweme_list[]`: `aweme_id`, `create_time`, `desc`, `statistics.{play_count,digg_count,comment_count,share_count,collect_count}`, `video.duration` (ms), `author.{follower_count,aweme_count}`, `is_top`. Cursor `max_cursor` while `has_more` |
| `GET /v1/facebook/profile/posts` | `url` or `pageId`, `cursor` | 3 posts. `posts[]`: `id`, `publishTime`, `text`, `reactionCount`, `reaction_counts{like,love,...}`, `commentCount`, `share_count`, `videoViewCount`, `videoDetails.{sdUrl,hdUrl}`, `topComments` (third-party commenters: do not store). No page follower count |
| `GET /v1/linkedin/profile` | `url` | 1 credit; person profile + `recentPosts` stub (no body/date/engagement). Not called |
| `GET /v1/linkedin/company` | `url` | 1 credit; company page. Not called |
| `GET /v1/linkedin/company/posts` | `url`, `page` (max 7) | URL, id, date, text only: **no engagement counts**. Not used |
| `GET /v1/credit-balance` | | free; `{"creditCount": 7090}` |

- **No LinkedIn person post feed** (re-checked 2026-10-23).
- **`cache_max_age`** (0 credits on a cache hit) exists on profile and single-post
  endpoints (`/v1/instagram/profile`, `/v1/tiktok/profile`, `/v1/facebook/profile`,
  `/v1/instagram/post`, ...), NOT on any feed endpoint above.
- **Errors**: invalid or missing key → HTTP 401 `{"success":false,"message":"Invalid
  API key"}` (2026-10-23; in Aug a bogus key was seen returning 402 "out of credits", so
  treat 402 as "out of credits or bad key"). Private / age-gated / unavailable profile
  → `success: true` with HTTP 403/404, `error`, `errorStatus`, `message` ("Profile is
  private", "Profile is age restricted", "Profile is unavailable on Instagram") and
  `credits_charged: 0`.
- **Gotchas**: IG `id` is `<pk>_<owner id>`; store `pk` (the bare media pk Apify and
  public IG surfaces use). Feeds include collabs owned by the other account (check
  `owner.username`). Pinned posts sit on top of IG/TikTok feeds whatever their age: a
  delta must test the page's LAST row against the watermark. Media URLs are signed
  and expire (IG ~1-4 days; TikTok session-bound).
- **Terms**: ToS §04 "Credits may expire or be discontinued at our discretion".

## Bright Data (brightdata.com), Scraper API

- **Base URL** `https://api.brightdata.com` **Auth** `Authorization: Bearer <key>`
- **Docs** `https://docs.brightdata.com/llms.txt`; every scraper per domain:
  `https://docs.brightdata.com/scrapers/<domain>.json`; inputs and outputs of all:
  `/scrapers-full.json`
- **Price**: 1 credit per delivered record, error records included. 5,000 free
  credits/month shared across Unlocker, SERP, Scraper API and Studio, reset on the 1st,
  no rollover; with no funds deposited the account **hard-stops** at the quota.
  After that $1.50/1k pay-as-you-go (Sept 2026 FAQ).

| Dataset | id | Discovery (`discover_by`) and inputs |
|---|---|---|
| LinkedIn posts | `gd_lyy3tktm25m4avu764` | `profile_url`: `url`, `start_date`/`end_date` (ISO `2018-04-25T00:00:00.000Z`), `only_authored_posts`; `company_url`: `url`, `start_date`, `end_date`; `url`: `url`, `limit` |
| Instagram posts | `gd_lk5ns7kz21pck8jpis` | `url` (profile URL): `num_of_posts`, `posts_to_not_include`, `start_date`/`end_date` (`MM-DD-YYYY`), `post_type` (`Post`/`Reel`) |
| Instagram reels | `gd_lyclm20il4r5helnj` | `url`, `num_of_posts`, dates; `url_all_reels` |
| Facebook page posts | `gd_lkaxegm826bjpoo9m5` | collect by page `url`: `num_of_posts`, dates, `include_profile_data` |
| TikTok posts | `gd_lu702nij2f790tmv9h` | `profile_url`: `url`, `num_of_posts`, dates, `post_type`, `sort_by`; fast variant `gd_m7n5v2gq296pex2f5m` |
| LinkedIn person profile | `gd_l1viktl72bvl7bjuj0` | collect by `url`; 1 record. `position` "restricted by LinkedIn since Nov 13, 2025" |
| LinkedIn company | `gd_l1vikfnt1wgvvqz95w` | collect by `url`; 1 record |
| Google Maps reviews | `gd_luzfs1dn2oa0teb81` | collect by `url` (a `google.com/maps/place/...` URL): `days_limit`, `sort_by`. Records: `review_id`, `reviewer_name`, `review_rating`, `review`, `review_date`, `response_of_owner`, `place_name`, `place_general_rating`, `overall_place_riviews`. Not wired in corpus (used from a companion fetch tool). **One live test 2026-10-23**: ~10 min from trigger to ready, 0 records + 1 billed error record `crawl_error` "Sign in modal hides the Reviews". Treat as unreliable until a run returns reviews |

**Async flow** (discovery always runs past the sync `/scrape` endpoint's 1-minute window):

1. `POST /datasets/v3/trigger?dataset_id=<id>&type=discover_new&discover_by=<method>&include_errors=true&limit_multiple_results=<cap>`
   body `{"input":[{...}], "limit_per_input": <cap>}` → `{"snapshot_id": "sd_..."}`.
   (A bare array body cannot carry `limit_per_input`.)
2. `GET /datasets/v3/progress/<snapshot_id>` → `status`: `starting` | `running` |
   `ready` | `failed` | `canceled`. Poll every ~10 s; Bright Data's example gives up
   at 10 min.
3. `GET /datasets/v3/snapshot/<snapshot_id>?format=json` → array of records (default
   format is NDJSON). A 202 means still building. Snapshots live 30 days.
4. On timeout: `POST /datasets/v3/snapshot/<id>/cancel` (best effort; not verified).
- `GET /datasets/v3/snapshots?dataset_id=<id>` lists snapshots (works on an inactive
  account, returned `[]`).

- **Records**: LinkedIn `id` (activity id, same as HarvestAPI), `url`, `date_posted`
  (ISO), `post_text`, `num_likes`, `num_comments` (no shares, no views), `images`,
  `videos`, `video_duration`, `document_page_count`, `user_followers`, `repost`.
  Instagram `pk` (bare media pk), `shortcode`, `date_posted`, `description`, `likes`,
  `num_comments`, `video_view_count`, `video_play_count`, `videos_duration`,
  `post_content[]`, `followers`, `posts_count`. No shares/saves.
- **Limits**: 5,000 running jobs; **25 HTTP 429s within 5 minutes blacklists the IP on
  every Bright Data API** until support clears it, so honour `Retry-After` and never
  retry a 429 immediately. Instagram media links die 24 h after collection. LinkedIn
  profile post discovery returns "typically about 10" posts: shallow.
- **Errors**: `400 Customer is not active` (account not activated: seen 2026-10-23 on
  every trigger, nothing billed; later on 2026-10-23 triggers were accepted, so the
  account is now active); `401 Invalid credentials` (bad key, plain text);
  `401 Credentials are missing`; `400 {"error":"Invalid input provided","code":
  "validation_error","errors":[[field, reason]]}`; `404 dataset does not exist`; `429
  You have too many running jobs ...`. Record-level errors arrive inside the snapshot
  as records with `error` / `error_code` (`dead_page`, `bucket_rate_limit`, ...) and are
  billed. `GET /customer/balance` → 403 "Your API key lacks the required permissions"
  with this key: no balance read.
- **Free liveness probe**: a trigger whose input lacks the required `url`
  (`{"input":[{}]}`) is rejected with `400 validation_error` before any job exists, so
  it proves key + active account at no cost (a cheap doctor/health check can use this). Unverified
  whether an inactive account answers `not active` before or after validation.
- **To verify now that the account is active**: one LinkedIn person, one company and one
  Instagram profile at `limit_per_input` 10; confirm `id`/`pk` parity with HarvestAPI
  and apidojo, and whether discovery really stops near 10 posts.

## Which to use (as routed in corpus, 2026-10-23)

| Need | First choice | Then |
|---|---|---|
| LinkedIn person posts | HarvestAPI `/linkedin/profile-posts` | Apify actor, Bright Data (shallow) |
| LinkedIn company posts | HarvestAPI `/linkedin/company-posts` | Apify actor, Bright Data (shallow) |
| Instagram posts | ScrapeCreators `/v2/instagram/user/posts` | Apify apidojo, Bright Data |
| TikTok videos | ScrapeCreators `/v3/tiktok/profile/videos` | Bright Data `gd_lu702nij2f790tmv9h` (not wired) |
| Facebook page posts | ScrapeCreators `/v1/facebook/profile/posts` | Bright Data `gd_lkaxegm826bjpoo9m5` (not wired) |
| One LinkedIn profile/company | HarvestAPI `/linkedin/profile?main=true`, `/linkedin/company` ($0.004 each, measured) | Apify actor; ScrapeCreators `/v1/linkedin/profile`, `/company` (1 credit, not wired) |
| Google Maps reviews | Bright Data `gd_luzfs1dn2oa0teb81` (async, unreliable so far; not wired in corpus) | none wired |

A target-level miss (private, not found) does not fall back to the next vendor: the
second vendor would bill for the same miss.
