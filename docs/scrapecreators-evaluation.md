# ScrapeCreators: evaluated, not adopted (Aug 2026); adopted for Instagram, TikTok and Facebook (Oct 2026)

## Update, 2026-10-23: what changed

The August decision rested on one premise: the Apify Starter subscription was a sunk
$29/month, so a cheaper per-item price bought nothing. That premise is gone. Apify is
blocked on an unpaid invoice, and HarvestAPI's direct API now serves LinkedIn persons
AND company pages at a measured $0.04/1k (`corpus/adapters/linkedin_harvest.py`),
which is the route section 6 said would actually kill the $29. With LinkedIn off Apify,
Instagram is the only thing left on it, and that is exactly what ScrapeCreators does
well. The author also held ~7,100 free credits, so there is no $47 pack to buy yet.

Re-checked live and against `https://docs.scrapecreators.com/openapi.json` (196 paths):

- **Still no LinkedIn person post feed.** LinkedIn paths are `/v1/linkedin/profile`,
  `/company`, `/company/posts`, `/post`, `/post/transcript`, `/search/posts`, `/ad`,
  `/ads/search`: the same eight. `llms.txt` no longer even lists `/company/posts` or
  `/search/posts`, but both remain in the spec. `/company/posts` still has no
  engagement counts and stops at 7 pages, so HarvestAPI wins LinkedIn outright and no
  ScrapeCreators LinkedIn route is registered.
- **Credits per request, measured** (2026-10-23, `credits_charged` on every response):
  `/v2/instagram/user/posts` 1 credit for 12 posts, `/v3/tiktok/profile/videos` 1 for
  10, `/v1/facebook/profile/posts` 1 for 3. At $47/25k that is $0.16, $0.19 and $0.63
  per 1,000. `/v1/credit-balance` costs nothing.
- **`cache_max_age` is still absent from every feed endpoint** (it is on
  `/v1/instagram/profile`, `/v1/tiktok/profile`, `/v1/facebook/profile` and single-post
  endpoints). Deltas save credits by stopping at the watermark instead.
- **The pk finding in section 5 holds**: items carry both `id` (`<pk>_<owner>`) and a
  bare `pk`; the adapter stores `pk`, which is what `instagram_public` stores.
- The response is Instagram's own web-API shape, so the media/metric projection is
  shared with `instagram_client`.

Adopted as `scrapecreators_instagram` (first Instagram route), `scrapecreators_tiktok`
and `scrapecreators_facebook` (the only routes on those platforms). The August text
below is kept as written.

---

**Decision: do not build the adapter. Adopting ScrapeCreators raises the annual bill
by $47 and removes nothing.**

Date: 2026-08-06. Revisit only if the trigger in the last section fires.

The build was gated on one condition: the economics have to actually beat Apify for
this workload. They do not, and the reason is not price. ScrapeCreators is genuinely
cheaper per item and genuinely subscription-free. It cannot serve the one route that
forces the Apify subscription to exist, so the subscription stays and the cheaper
per-item price buys nothing.

---

## 1. What the target actually was

The Apify Starter subscription is 91-98% of the bill. Per-item usage is noise. So the
only move worth making is one that lets the subscription be cancelled. A route that
merely lowers per-item cost is worth $0, because per-item cost is already $0 at the
margin.

Verified live against the account on 2026-08-06, not read off the price card:

| Field | Value |
|---|---|
| `plan` | STARTER |
| `monthlyBasePriceUsd` | 29 |
| `monthlyUsageCreditsUsd` | 29 |
| `maxMonthlyUsageUsd` | 29 |

Included usage equals the base price, so **marginal usage cost is $0 until $29/month of
usage is consumed**. At the measured rates that is roughly 14,500 LinkedIn posts or
58,000 Instagram posts per month before a single extra dollar is billed. Nothing in
this project's realistic volume comes close.

Two adapters carry `needs=("APIFY_TOKEN",)`. Both have to move for the subscription to
die:

| Adapter | Route | `cost_per_1k` |
|---|---|---|
| `linkedin_posts` | `harvestapi/linkedin-profile-posts` | $2.00 |
| `instagram_public` | `apidojo/instagram-scraper-api` | $0.50 |
| `linkedin_export` | consented .xlsx, no vendor | $0.00 |
| `instagram_client` | session cookie, no vendor | $0.00 |

---

## 2. The blocking fact

**ScrapeCreators has no LinkedIn person post-history endpoint.** Confirmed from two
independent vendor artifacts, not from marketing copy:

1. Every LinkedIn path the vendor documents, enumerated exhaustively:
   `/v1/linkedin/profile`, `/company`, `/company/posts`, `/post`, `/post/transcript`,
   `/search/posts`, `/ad`, `/ads/search`. Eight paths. None is a person's post feed.
   Reproducing this needs one caveat: `docs.scrapecreators.com/llms-full.txt` serves an
   HTML application shell, not markdown, so grepping it for prose finds nothing. The
   route manifest embedded in that shell is what lists the endpoints. Each of the eight
   was then confirmed against its own reference page at `<path>.md`, which does return
   real markdown, and all eight resolve. `/v1/linkedin/company/posts` describes itself
   as posts "from a LinkedIn company page", capped at 7 pages.
2. The vendor's own first-party agent skill has a "posts by account" table. Instagram,
   YouTube, Facebook, Twitter/X and Reddit each get a row. LinkedIn's row is
   `/v1/linkedin/company/posts`. There is no person row.

A person's posts appear only as a `recentPosts` stub inside `/v1/linkedin/profile`:
truncated title, activity type, link, image. No body, no date, no engagement. Unusable
as a corpus source.

`/v1/linkedin/search/posts` is not an escape hatch, and its own reference page is the
proof. It takes exactly three parameters, `query`, `date_posted` and `cursor`: there is
no author, profile or handle filter to pin it to one person. It is also not LinkedIn
data. The vendor's description says it searches "by keyword using Google Search" and is
therefore "best-effort and not a complete LinkedIn-native search". Pagination is hard
capped at pages 1-11, with cursor 12 or higher returning 400. Keyword search over a
Google index, truncated at 11 pages, cannot enumerate one person's back-catalogue.

### Why that is fatal here specifically

The probe modelled a 50/50 LinkedIn-company / Instagram split. The live corpus is not
that. Every account currently synced:

| Platform | Handle | Relationship | LinkedIn route |
|---|---|---|---|
| linkedin | (owner) | own | `/in/` person |
| linkedin | (a client) | client | `/in/` person |
| instagram | (owner) | own | n/a |
| instagram | (a client) | client | n/a |

Zero company pages. **100% of LinkedIn demand hits the exact route ScrapeCreators does
not have.** The product is per-person content audits, so this is structural, not a
sampling accident.

---

## 3. The arithmetic, 12 months

Recorded spend across the entire populated corpus (125 posts, 4 accounts), read from
`scrape_run`:

| Adapter | Runs | Items | Cost |
|---|---|---|---|
| `linkedin_posts` | 2 | 53 | $0.1061 |
| `instagram_public` | 2 | 78 | $0.0390 |
| **Total** | | **131** | **$0.1451** |

That is 0.5% of one month's subscription. The subscription is the whole bill.

| Scenario | Apify | ScrapeCreators | Year 1 | vs today |
|---|---|---|---|---|
| **A. Status quo** | $348 | $0 | **$348** | baseline |
| **B. Migrate Instagram only** | $348 | $47 | **$395** | **+$47** |
| **C. Drop Apify entirely** | not available | | | route does not exist |

Scenario C is the only one that wins, and it is not purchasable. Scenario B is what the
probe recommended, and it is a **$47/year cost increase**: the Apify subscription is
unchanged because LinkedIn persons still need it, and the Instagram usage it replaces
was already costing $0 at the margin.

The probe's headline "$301/year saving, 86%" is arithmetically correct but conditioned
on cancelling Apify. Route coverage forbids that, so the saving is not available. The
probe said as much in its own text and then recommended migrating anyway. The
recommendation does not follow from its own finding.

---

## 4. The data-quality argument does not rescue it either

The secondary case for migrating Instagram was richer fields. Checked against the
recorded fixture, the current `apidojo` route already returns most of them:

| Field | apidojo (current) | ScrapeCreators |
|---|---|---|
| video duration | yes, `32.432` | yes |
| play count | yes, `5,682,845` | yes |
| MP4 URL (reels) | yes | yes |
| follower / following / post counts | yes, on every row | yes |
| carousel video slide MP4 | **no**, poster JPG only | yes |

One real gap, and `instagram_public.py` already documents a same-vendor fix for it:
`apify/instagram-post-scraper` is the only actor that emits `childPosts[].videoUrl`.
Closing one gap with an actor swap costs a line of config. Closing it with a second
vendor costs a new key, a new auth failure mode, a second ToS, and a second thing to
debug at 2am. Not worth it.

---

## 5. Findings worth keeping, if this is ever revisited

Recorded because they are expensive to rediscover and cheap to write down.

**"Credits never expire" is marketing, not contract.** Terms of Service §04 says
verbatim: *"Credits may expire or be discontinued at our discretion."* §10: unused
credits may be forfeited on termination. §05: refunds at sole discretion. §12 caps
liability at the preceding 12 months' spend. Entity is Web Scraping Guy LLC, Texas law,
founded June 2024, self-described team of 3. If it ever is adopted, buy the $47 pack
and never the $497 one: at this volume $47 is over three years of supply, and prepaying
$497 into a two-year-old LLC that can void the balance is a bad trade for a $0.89/1k
unit-price improvement.

**Verified cost conversions, ready to drop into `cost_per_1k` if the decision flips.**
Credits are charged per REQUEST, not per post, so the headline credit price is not the
per-1k cost. At the Freelance rate of $47 / 25,000 credits = $0.00188/credit:

- Instagram: `/v2/instagram/user/posts` returns 12 items per credit, so 1k posts is
  ~83 credits = **$0.16 per 1k**. Against apidojo's $0.50, a real but irrelevant win.
- LinkedIn company: the listing endpoint carries no engagement metrics at all, so
  engagement costs 1 credit per post via `/v1/linkedin/post`. That is ~1 credit/post =
  **$1.88 per 1k**, against Apify's $2.00. A 6% improvement on a line item already
  costing $0 at the margin.

Always read `credits_charged` off the response rather than assuming 1. The per-endpoint
exceptions are not fully tabulated anywhere: the changelog prices
`/v2/instagram/reels/search` at 1 credit per 10 reels, a per-result model absent from
the skill's exception table.

**Instagram `platform_item_id` would duplicate the entire back-catalogue.** The Apify
fixture uses the bare media pk (`3912345678901234567`). ScrapeCreators returns a
composite `pk_userid` (`3601234567890123456_51234567890`). `instagram_public._item()`
does `pid = row.get("id") or code or stable_id(...)`, so mapping the vendor `id`
straight through re-enters every existing row as new on first sync. Do not reach for
`id.split("_")[0]` to fix it: the same response already carries a sibling `pk` field
holding the bare media pk, so prefer that over parsing a composite, and prefer `code`
over both since it is byte-identical across the two vendors and needs no arithmetic to
stay stable.

**A bogus key and a missing key report inversely.** A bogus key returns HTTP 402
*"Looks like you're out of credits"*; a missing key returns HTTP 401 *"Invalid API
key"*. Any adapter reading 402 as "top up" will misreport a typo'd key as an empty
balance.

**Signup is Google or Microsoft OAuth only.** Email/password was removed. The account owner has to
click through it personally; it cannot be self-provisioned by an agent. This is why no
authenticated live call was made in either the probe or this evaluation. Everything
above the ToS line is from vendor-published artifacts plus the live Apify account; the
pricing and endpoint claims are vendor-published, the Apify and workload numbers are
measured.

**Caching does not help this workload.** `cache_max_age` exists on four Instagram
endpoints only. It is on no feed endpoint and no LinkedIn endpoint, so the "cached
requests are free" headline buys nothing on the pulls that dominate.

---

## 6. What would actually kill the $29

The target was never "replace Apify". It is **"replace
`harvestapi/linkedin-profile-posts` for `/in/` persons"**. That single actor is what
holds $348/year in place. Anything that does not address it is not a candidate,
regardless of price.

Direct public LinkedIn HTML was probed alongside this and is not the answer. Enumerated
across four public creator handles, the results were inconsistent per account rather than uniformly
open or uniformly blocked:

| Handle | `/in/<h>/` | `/recent-activity/all/` | Own posts recovered |
|---|---|---|---|
| creator A | 200 | 200 | 9 of 23 items on page |
| creator B | 200 | 999 authwall | 3 of 16 items on page |
| creator C | 999 authwall | 999 authwall | 0 |
| creator D | 999 authwall | 999 authwall | 0 |

Two failure modes make it unusable as a back-catalogue source, independent of legality
or blocking risk. It authwalls per handle with no way to predict which. And even when
it returns 200, most of what is on the page belongs to other authors, yielding under
ten of the target's own posts. A route that returns 9 posts for one client and 0 for
the next cannot back a content audit.

Treat that table as a snapshot, not a constant. `corpus/adapters/linkedin_public.py`
landed the same day off a larger sample, 10 accounts against these 4, and found
`/recent-activity/all/` returning 999 unconditionally and the bare profile page 999ing
for 5 of 10, including both accounts actually in this corpus. The authwall moves with
time, IP and handle, so the adapter's numbers supersede these where they disagree. Its
conclusion is the same one reached here and reached independently: public HTML cannot
discover a person's posts, and it is only safe as a refresh engine over post IDs the
paid actor already found.

So the open question stays open. The next probe should target LinkedIn person post
history with engagement counts specifically, and should treat any vendor without that
route as disqualified on the first check rather than after a full pricing workup.
