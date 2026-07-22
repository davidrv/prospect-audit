# Prospect Audit Tool — Context

Internal tool for Localistico sales team. Given a prospect's business name, searches **Google Maps**, **Apple Maps** and **Bing Maps** (underlying API: Azure Maps Fuzzy Search — renamed to "Bing" only in user-facing text, see the venue-table Status entry below) in parallel and optionally compares against the prospect's **own official data** (a store-locator URL with schema.org markup, or an uploaded CSV). Every comparison is anchored on **Google Maps** — never platform vs. platform — and surfaces **inconsistencies and reputation problems** in a single worst-to-best **venue table**, packaged into a sales-ready PDF report with clickable per-source verification links, used to pitch Localistico's solution.

`docs/plan.md` is the original roadmap this was built from. **This file supersedes it on points that changed after real-world testing**: (1) official data is optional, not mandatory — Google is the anchor instead; (2) there is no *paid third-party* scraping fallback (Firecrawl was tried and removed) — but there **is** an in-house fallback (heuristic HTML parsing + Playwright) for sites that are reachable but lack schema.org, see "Why Google is the anchor" below; (3) the primary output is a single venue table (presence/accuracy/rating per location), not a rule-flags table + a separate reputation table — see the latest Status entry. Bot-protected sites remain explicitly out of scope either way.

## What it does

1. User enters a prospect name (e.g. "Zara"), a city (defaults to "Barcelona"), and *optionally* official data: a list of store-locator URLs, and/or an uploaded CSV (template downloadable from the UI).
2. Backend searches Google, Apple, and Azure in parallel, and — if given — extracts official locations from the URLs (schema.org only) and/or CSV.
3. Official/CSV records (which never come with coordinates) get geocoded via the Google Geocoding API before matching, since coordinate-based matching is far more reliable than fuzzy text matching (see below).
4. Every location across all sources is matched into a single "candidate location" via geographic + name/address similarity (`matching.py`), each getting a stable `cluster_id` (`L1`, `L2`, ...) and a `canonical_address` for identification in the UI/PDF.
5. Two engines run over the matched clusters: `inconsistencies.py` (Apple/Azure/Official vs. **Google** — missing/conflicting phone/website/hours, name variation, coverage gaps) and `reputation.py` (Google rating/review-based risk signals).
6. UI shows a findings-first view (KPIs + flags table, each row showing the location ID/address and clickable "verify live" links per source) above tabbed per-source tables (Google/Apple/Azure/Official, each still exportable as CSV).
7. "Exportar informe PDF" generates an executive report (cover, KPIs, recommendations, top findings with the same ID/address/links, reputation ranking with quoted negative reviews, full appendix) via WeasyPrint.

**Google Maps data:** name, address, phone, website, rating, review count, opening hours, up to 5 sample reviews, AI review summary (Gemini) when available
**Apple Maps data:** name, address, phone, website, category, coordinates (no ratings/reviews — Apple doesn't expose them)
**Azure Maps data:** name, address, phone, website, category, coordinates (no ratings/reviews either)
**Official data:** name, address, phone, website, opening hours — from schema.org JSON-LD, a heuristic HTML scraper, Playwright-rendered pages, or a CSV upload; geocoded after extraction since none of these reliably provide coordinates

## Why Google is the anchor (not official data)

Two different designs were tried and rejected before this one, both from real user feedback while testing against real prospects (Zara, Movistar):

1. **All 4 sources compared symmetrically** (original plan.md design) — rejected because Google/Apple/Azure disagreeing with each other, with no ground truth, just tells you three platforms disagree, not which one (if any) is right. Pure noise.
2. **Official data as the mandatory anchor** — rejected after discovering how unreliable getting *good* official data actually is in practice. A Firecrawl-based scraping fallback was built first (multi-page crawl to find individual store pages, following links, judging index-vs-detail pages) to rescue sites without schema.org markup — but it kept breaking in new ways per real site: Zara's index page has ~150 stores nationwide with no addresses (LLM extraction on it either times out or hallucinates city names as "addresses"); Movistar's markdown links had a title attribute my regex didn't handle, silently returning zero links; even after fixing that, "nearby store" links on real detail pages defeated a simple link-count heuristic for telling a leaf page from an index. Each fix surfaced a new edge case. **Decision: cut the paid scraping fallback entirely** and, separately, **build a lighter in-house one** (see below) — official data stopped being mandatory either way, since even a reliable extractor doesn't fix the fundamental "no ground truth" problem in design #1.

So now: **Google is the anchor** (fetched via a reliable direct API call every time), and Apple/Azure/Official are each compared against it, independently. Official data is a nice-to-have extra check when it's available, not a prerequisite for the whole audit.

### The in-house scraper (`official.py`, added after cutting Firecrawl)

Once Google became the anchor (removing the pressure for official-data extraction to be perfect), a **self-built, no-third-party-service** fallback became worth adding for the specific case Firecrawl was originally meant to handle: sites that are perfectly reachable (no bot protection) but simply don't mark up their store locator as schema.org — tiendas.movistar.es is the real, motivating example. Deliberately scoped to **not** chase anti-bot bypass (Zara-style 403s stay `'inaccessible'`, no fallback attempted at all — that's a different, harder problem this tool doesn't try to solve). For any URL where the direct request succeeds but has no schema.org, `_extract_one()` in `official.py` now tries, in order:

1. **Heuristic HTML parsing** (`_extract_heuristic`) on the raw HTML already downloaded — no extra request, no browser, no LLM. Anchors on a Spanish postal-code + locality pattern (e.g. "08002 Barcelona"), climbs the DOM from each anchor until the subtree would absorb a *second* one (the natural boundary between neighboring store blocks on a listing page), then pulls a name from the nearest heading/bold/link within that block. `<script>`/`<style>` contents are stripped before scanning — found via testing that a JS-injected page's own `<script>` source (containing the address as a string literal, used to build the DOM) otherwise gets matched as a second, bogus location.
2. **Playwright** (headless Chromium), only if step 1 found nothing — for pages whose real content only exists after JS runs. Renders the page, then retries both schema.org detection and the same heuristic parser on the rendered HTML.

New status `'found_heuristic'` (vs. `'found'` for real schema.org) — **the "no schema.org" moderate finding is kept even when this recovers real data**, including when Playwright reveals JSON-LD injected via JS: that markup isn't reliably seen by every crawler/AI assistant, so its absence from the server-rendered HTML is still worth flagging. Verified end-to-end against the real Movistar Barcelona store-locator: 83 real stores extracted via the heuristic pass alone (no Playwright needed — the HTML already has everything), 57 of them successfully matched against Google/Azure after geocoding, in ~7 seconds total.

Also fixed along the way: `truststore` was added (`app.py`, called once at import time via `truststore.inject_into_ssl()`) after discovering `requests.get('https://tiendas.movistar.es/...')` failed with `SSLCertVerificationError` — the server doesn't send its full certificate chain, which browsers/`curl` silently tolerate (OS-level chain-building) but plain `certifi`-based Python doesn't. `truststore` delegates to the OS-native trust store instead, fixing this without weakening verification (`verify=False` was never considered).

### Two distinct official-data findings (site-level, not tied to any location)

- **moderate** — the store locator page was reachable, but genuinely has no schema.org markup. This is a real, actionable finding: it's a local-SEO/GEO gap worth pointing out to the prospect (Localistico's own persona expertise), not a limitation of this tool.
- **minor** — couldn't even access/read the page (blocked, timeout, HTTP error). This is a limitation of the scan, not a proven fact about the prospect's site — downgraded accordingly, and suggests uploading a CSV instead.

---

## Stack

- **Backend:** Python 3 + Flask (`app.py`), split into focused modules (see Project structure)
- **Frontend:** Single static HTML page (`public/index.html`) — vanilla JS, no framework, Tabler Icons via CDN
- **APIs:** Google Places API + Google Geocoding API, Apple Maps Server API, Azure Maps (Fuzzy Search) — all HTTP REST
- **PDF:** WeasyPrint (HTML/CSS → PDF), template in `templates/report.html`
- **Matching:** `rapidfuzz` for name/address similarity, hand-written haversine for geo distance
- **Official data extraction:** `beautifulsoup4` for schema.org JSON-LD (tried first, free) → an in-house heuristic HTML parser (no dependency beyond `beautifulsoup4`+`re`) → `playwright` (headless Chromium) as a last resort for JS-only pages — see "The in-house scraper" above; a CSV upload is the alternative when a site has none of the above
- **TLS:** `truststore` — delegates certificate verification to the OS-native trust store instead of `certifi`'s bundled roots, needed because some real store-locators (e.g. tiendas.movistar.es) don't send their full cert chain
- **Verification links:** `normalize.py` builds a maps deep-link per record (`google_maps_url`/`apple_maps_url`/`bing_maps_url` — Azure has no consumer map site of its own, Bing is used as a same-Microsoft-stack stand-in) so every finding can be checked live
- **Branding:** `assets/images/logo.png` / `icon.png` — served at `/assets/...` for the web UI (favicon + header), base64-embedded into the PDF cover
- **Auth:** API keys stored in `.env`, never exposed to the browser

---

## Project structure

```
prospect-audit/
├── app.py                          ← Flask routes + Google/Apple/Azure fetchers + audit orchestration + geocoding
├── normalize.py                    ← unified location schema + per-source adapters + verify_url builders + hours parsing
├── official.py                     ← official data extraction: schema.org → heuristic HTML scraper → Playwright → CSV parsing
├── matching.py                     ← entity resolution: clusters records from different sources into "locations" (+ cluster_id, canonical_address)
├── inconsistencies.py              ← rule engine (R1/R3/R4-R9/R11/R14/R14b) — always vs. Google, never platform vs. platform
├── reputation.py                   ← Google-only reputation signals (rating/reviews snapshot, no history)
├── venue_metrics.py                ← per-venue table columns (presence/accuracy/rating) + worst-to-best venue_score
├── report.py                       ← assembles PDF context + calls WeasyPrint
├── templates/
│   └── report.html                 ← Jinja2 template for the PDF report (light theme, print-safe CSS)
├── tests/                          ← pytest suite (109 tests)
├── pytest.ini
├── requirements.txt
├── requirements-dev.txt            ← requirements.txt + pytest
├── Dockerfile / .dockerignore
├── run.sh                          ← one-command start (creates venv, installs deps, runs)
├── .env                            ← secrets (gitignored)
├── .gitignore
├── config/
│   └── AuthKey_8UCGRX4UFW.p8      ← Apple MapKit private key (gitignored)
├── assets/
│   └── images/
│       ├── logo.png                ← Localistico wordmark — web header + PDF cover
│       └── icon.png                ← favicon
├── docs/
│   ├── context.md                  ← this file (current state)
│   └── plan.md                     ← original roadmap (mostly implemented, since diverged in places — see Status)
└── public/
    ├── index.html                  ← the UI
    └── template_oficial.csv        ← downloadable CSV template for the official-data upload path
```

---

## Running it

```bash
./run.sh
# → http://localhost:5050
```

`run.sh` creates a `.venv`, installs `requirements.txt`, and starts Flask on port 5050.

WeasyPrint needs native Pango/cairo/gdk-pixbuf, normally via Homebrew (`brew install pango cairo gdk-pixbuf`) — already present and working on the dev machine this was built on, no extra `DYLD_FALLBACK_LIBRARY_PATH` workaround was needed here, but that's the first thing to check if `import weasyprint` fails elsewhere.

The Playwright fallback in `official.py` needs Chromium installed separately from `pip install`: run `playwright install chromium` once after `pip install -r requirements.txt` (locally; the `Dockerfile` does this automatically via `playwright install --with-deps chromium`). Without it, `_render_with_playwright()` just degrades gracefully (`ImportError`/missing-binary → `(None, message)`) — schema.org and the heuristic HTML pass still work fine, only the last-resort JS-rendering step is unavailable.

---

## Configuration (`.env`)

```
GOOGLE_PLACES_API_KEY="..."
APPLE_MAPS_TEAM_ID="6M3D35DUTC"
APPLE_MAPS_KEY_ID="8UCGRX4UFW"
APPLE_MAPS_PRIVATE_KEY_PATH="config/AuthKey_8UCGRX4UFW.p8"
AZURE_MAPS_SUBSCRIPTION_KEY="..."
```

- The `.p8` file is Apple's EC private key (PKCS#8 PEM format). The backend generates a short-lived JWT from it, then exchanges that JWT for an access token (see below) — the raw JWT alone isn't accepted by the search endpoint.
- The Azure key is a plain subscription key from an Azure Maps account's "Authentication" blade in the Azure Portal — no JWT flow needed.
- `reviewSummary` (Gemini-generated review summaries, see below) needs **Places API (New)** enabled separately from the legacy Places API in the same Google Cloud project — same `GOOGLE_PLACES_API_KEY` works for both once enabled.

---

## Backend logic

### `POST /search` (also accepts `GET` for URL-only, bookmarkable use)

Fields (query string for `GET`, form fields for `POST`): `name` (required), `city` (defaults to `Barcelona`), `official_url` (repeatable, optional), `official_csv` (file upload, multipart, optional). No official-data requirement — Google/Apple/Azure run regardless.

`_parse_request_params()` reads via `request.values` (covers both query args and POST form fields with the same code) and `request.files` for the CSV. `_run_audit()` runs 4 threads in parallel:

- **`_search_google(name, city)`** — `textsearch/json` (paginated, 3 pages max / 60 results) biased toward the geocoded city center, `language=es`, then `details/json` per result (thread pool) for phone/website/hours/rating/**reviews** (`reviews`+`reviews_sort=newest`). Each detail call also fires `_google_review_summary()` — Gemini-generated review summary via **Places API (New)**, best-effort, currently returns empty for Spain (Google's GA rollout doesn't cover it yet as of testing; not a bug here).
- **`_search_apple(name, city)`** — JWT → access-token exchange → `/v1/search`, paginated via `pageToken`, propagates `id`/`lat`/`lng`.
- **`_search_azure(name, city)`** — `GET https://atlas.microsoft.com/search/fuzzy/json`, paginated via `ofs` offset (capped at 200), filtered to `type == 'POI'`.
- **Official data** (`official.py`), only if given — two independent, additive ways in:
  1. **CSV upload** (`parse_official_csv()`) — columns `name, address, phone, opening_hours, url` (case-insensitive, `formatted_address` also accepted for address; `url` is optional, used as the row's verify link). Template at `public/template_oficial.csv`, downloadable from the UI.
  2. **schema.org JSON-LD** on each given URL (`_extract_one()`) — parses `<script type="application/ld+json">`, accepts a `LocalBusiness`-ish type whitelist + a duck-typing fallback, handles `ItemList`-wrapped listings. **No scraping fallback of any kind** — if a URL has no markup or can't be reached, that's reported as a site-level finding (see below), not worked around. (A Firecrawl-based fallback with a multi-page crawler was built and then removed — see "Why Google is the anchor" above for why.)
- **`_fill_missing_coords(official_locations)`** (in `app.py`, runs after extraction, before matching) — geocodes any official/CSV record lacking lat/lng via the Google Geocoding API (parallelized, reuses the same cache as city geocoding). Neither schema.org nor CSV rows reliably provide coordinates, and matching without them relies on a much more fragile fuzzy name/address fallback — this fixed a real case where only 2 of 21 real Zara stores matched anything (Google returns Catalan street names like "Carrer de Pelai" while other sources say "Calle Pelai", and a branch name like "Zara Pelayo" doesn't fuzzy-match Google's generic "Zara" listing) — geocoding brought that up to 12/21 via the far more reliable ≤30m/≤120m coordinate rule.

Then `_build_audit(results)`: normalize (via `normalize.from_google`/`from_apple`/`from_azure`, official records already normalized) → `matching.cluster_records()` (Union-Find, assigns `cluster_id`/`canonical_address`/`canonical_label` per cluster) → `inconsistencies.detect_inconsistencies()` → `reputation.compute_reputation()`.

### `POST /report` (also accepts `GET`)

Same params as `/search`, no official-data requirement either. Re-runs `_run_audit()`, then `report.render_report_pdf()` renders `templates/report.html` → PDF bytes, returned as a file download.

### `GET /template_oficial.csv`

Serves the downloadable CSV template (`public/template_oficial.csv`) as an attachment.

### Inconsistency rules implemented (`inconsistencies.py`) — always vs. Google (`ANCHOR = 'google'`)

| Rule | Meaning | Severity |
|---|---|---|
| R1 | No Google match for an Apple/Azure/Official listing ("stale/duplicate/closed?"), or (rarer) a Google-only location with no other source checked produces **no** flag at all — see note below | critical |
| R3 | Ambiguous match (2 records of the same source merged) | critical |
| R4/R6 | Missing phone/website — Google has it, the other source doesn't (or vice versa) | critical |
| R5/R7 | Conflicting phone/website — Google's value vs. that source's | critical |
| R9 | Name varies vs. Google (moderate if similarity 60-90, critical below 60) — compared on `name_norm`, not raw strings, so a pure case difference like "ZARA" vs. "Zara" (rapidfuzz's raw ratio: 25, would look critical) doesn't false-positive | moderate/critical |
| R11 | Google + ≥1 other source matched, but missing from the rest | moderate |
| R14b | Contradictory open/closed status vs. Google on the same day | moderate |
| R14 | Opening/closing time differs from Google by ≥30 min (minor) / ≥60 min (moderate) | minor/moderate |

Note on R1: a cluster with *only* a Google record and nothing else present produces **no flag** — that's not "found only in Google," it's just "no other source was checked against it," and flagging it would reintroduce the platform-noise problem this design avoids. `FIELD_SUPPORT` is keyed by source (`google: {phone, website, opening_hours}`, `apple`/`azure: {phone, website}`, `official: {phone, website, opening_hours}`), so a field only gets compared when *both* Google and that specific source support it. Hours comparison (`normalize.parse_hours`) keys by **day index** (0=Monday) rather than localized label, needed because Google can return English day names while official data is Spanish.

Every flag carries a `links` array (one entry per involved source, with `verify_url` + `formatted_address`) built by `_flag()` from the cluster's `by_source` — this is what powers the "Verificar" column in both the UI findings table and the PDF.

Verified against real data (Zara, Movistar) and synthetic cases: a platform-vs-platform disagreement with no Google match produces the R1 "no Google match" finding but never a phone/website/name conflict flag between those two — conflicts are only ever computed against Google.

**Still deferred (see plan.md backlog):** category comparison (R12), website-reachability checks (R8).

### Reputation signals implemented (`reputation.py`)

Always was Google-only, independent of the anchor-source question. `NO_GOOGLE_PRESENCE` (auto-critical, weight 100), `NO_REVIEWS`, `LOW_RATING` (tiered), `FEW_REVIEWS_RELATIVE` (vs. this chain's own median, needs ≥3 datapoints), `NEGATIVE_RECENT_SAMPLE` (any of ≤5 sampled reviews with rating ≤2). Carries `ai_summary` (Gemini review summary) through to the PDF when available (must show "Summarized with Gemini" disclosure — already in the template). No historical trend — snapshot only, as decided.

### Verification links (`normalize.py`)

Every normalized record gets a `verify_url` (distinct from `website`, the business's own site used for R6/R7 comparisons) so a human can open the exact record and check a finding live during a sales call:
- **Google**: `google_maps_url(place_id, name)` — `https://www.google.com/maps/search/?api=1&query=...&query_place_id=...`, Google's documented deep-link format for a specific place.
- **Apple**: `apple_maps_url(lat, lng, name)` — `https://maps.apple.com/?ll=...&q=...`. Needs coordinates (Apple's search results provide them).
- **Azure**: `bing_maps_url(lat, lng, name)` — Azure Maps has no consumer-facing web map of its own to link into, so Bing Maps (same Microsoft mapping stack) is used as a stand-in.
- **Official**: the actual scraped page URL (schema.org) or the CSV row's `url` column if provided — `None` otherwise (a CSV with no url column has nothing to link to, which is fine, the person who uploaded it already has the data).

---

## Frontend logic (`public/index.html`)

- Prospect name + city inputs, plus an **optional** "Datos oficiales del prospect" box: a URL-per-line textarea and/or a CSV file upload, with a "Descargar plantilla CSV" link. No client-side requirement — the audit runs fine with just a name.
- Search and PDF export both POST as `multipart/form-data` (`buildFormData()`) — needed for the CSV file; PDF export fetches the response as a `blob()` and triggers download via a temporary object-URL `<a>`, rather than a plain `<a href>` click (which can't carry a file upload).
- Progress card with 4 step indicators (Google/Apple/Azure/Official).
- **Findings card**: risk pill, KPI tiles, and a flags table — each row shows the location's `cluster_id` + `canonical_address` (so multiple "ZARA" rows are distinguishable) and a "Verificar" column with clickable per-source links (`escapeHtml`/`safeHref`-sanitized, since this text ultimately comes from scraped/third-party data). Also renders `official_findings` (the site-level "no schema markup" / "couldn't access store locator" notices, severity-badged) above the flags table.
- **Tabbed results** (Google/Apple/Azure/Official) in one card instead of 4 stacked cards — the page was getting very long with everything shown at once. Each tab keeps its own sort/CSV-export; badges show per-source counts.
- No API keys, no localStorage — all secrets stay on the server. Basic Auth (see below) protects the whole app when configured.

---

## Auth & deployment prep (see also "Deployment readiness" further below if present, or ask — this was done in an earlier session)

HTTP Basic Auth (`_require_basic_auth()`), enforced only when `BASIC_AUTH_USERNAME`/`BASIC_AUTH_PASSWORD` are both set — unset for frictionless local dev, required in any real deployment. `Dockerfile`/`.dockerignore` prepared and tested locally (Google+Azure+PDF generation all confirmed inside a container); Apple fails inside the container by design (the `.p8` isn't baked into the image — must be mounted as a secret at deploy time). Actual deployment (GCP project, CI/CD) is intentionally deferred.

---

## Known limitations / next steps

- Google Places API caps at 60 results (3 pages × 20) — for very large chains, Azure (up to 200) often surfaces additional locations Google's cap misses.
- Apple Maps' own search is still less complete than Google's for the same query, even after the per-location-anchor fix (see the later Status entry) — that fix addresses the "city-wide query only checks near downtown" gap, but Apple's POI database itself may simply lack a listing Google has, or file it under a different/rebranded name a text search can't find. A `missing_apple`-style gap can still partly reflect Apple's own data/search limitations, not necessarily a real gap in the prospect's presence there — just a much smaller gap than before the fix.
- **Azure Maps' fuzzy search can return unrelated POIs that merely mention the brand name** (observed once: a bike-sharing station named "... - McDonald's", category `company`). Rare (~1%), no filter added here — though a `token_set_ratio` name filter was later added for Google's Text Search (see the Status entry above) and worked cleanly there without rejecting real branch-suffixed names (`fuzz.token_set_ratio` is subset/order-tolerant); worth reusing the same approach for Azure if this turns out to be more than a ~1% problem in practice.
- No category comparison yet (R12, deferred).
- `reviewSummary` coverage for Spain isn't confirmed live yet — expect empty results until Google expands rollout; not a bug.
- **Bot-protected sites remain unsupported by design** — if the initial request itself fails (Zara-style 403 via Akamai, or any other block), the tool reports that as a `minor` finding and suggests a CSV upload; it does not attempt Playwright or anything else against a blocked URL. This is deliberate, not a gap to fix.
- **The heuristic HTML scraper is inherently approximate** — it's tuned around Spanish postal-code patterns and common name-holding tags (headings/bold/links), verified against one real site (Movistar) plus synthetic fixtures. Expect it to need iteration (like the schema.org whitelist needed a duck-typing fallback) as it meets more real, differently-structured sites — verify its output before using it in a report, don't assume it's exhaustive.
- **Playwright adds real weight**: the Docker image grew from ~407MB to **~2.4GB** after adding Chromium (`playwright install --with-deps chromium` pulls in a full GTK/X11 dependency stack on top of the ~150-250MB browser binaries themselves) — noticeably more than a rough estimate would suggest. Worth factoring into deploy-time decisions (image pull time, Cloud Run cold starts, registry storage) whenever deployment is revisited.
- No category comparison yet (R12, deferred).
- `reviewSummary` coverage for Spain isn't confirmed live yet — expect empty results until Google expands rollout; not a bug.
- CSV-uploaded opening hours are free text and generally won't feed into R14/R14b unless they happen to match the `"Día: HH:MM–HH:MM"` format the schema.org/Google paths produce.
- Geocoding cache (city names and official/CSV addresses) is in-memory only, per process.
- Review history/velocity is out of scope — would need a paid third-party scraper (SerpApi/Outscraper/DataForSEO).
- **Action Links (Google/Apple), Google Posts, Google Products, and Reply rate are all `N/D` in the venue table** — confirmed against official API docs that none are obtainable without the prospect's own OAuth grant to their Business Profile/Business Connect account (see venue-table Status entry). TODO: revisit via a paid provider (e.g. SerpApi) or an in-house review/reply scraper if this becomes a priority. `review_rate_3m` is a partial exception — approximated over the ≤5 most recent Google reviews already fetched, always flagged as a sample-based approximation, not a real rate.
- No authentication enforced unless `BASIC_AUTH_USERNAME`/`PASSWORD` are explicitly set — fine for local dev, must be set before any real deployment.

## Status (2026-07-22, live progress polling)

Prompted by realizing the audit's actual duration had quietly grown well past what the old static "Buscando en Google Maps..." UI implied — review scraping (`_attach_scraped_reviews`, added earlier the same day) alone can take minutes for a large chain (up to 45s per location, 5 in parallel) and previously ran silently *inside* what the frontend showed as one unchanging label with zero visibility.

- **`/search` and `/report` are unchanged** (still synchronous, still there for backward-compat/scripting). New alongside them: `POST /search/start` / `POST /report/start` kick off `_run_audit_job` in a background thread and return `{job_id}` immediately; `GET /jobs/<job_id>/status?since=N` returns `{status, progress, progress_count, result?}` — `progress` is only the messages after index `N`, so the frontend polls incrementally instead of re-fetching the whole history every time. In-memory job store (`_jobs` dict + lock) — fine for a single-process internal tool; would need a real queue behind more than one process. Finished jobs older than an hour are pruned opportunistically whenever a new one is created.
- **`_run_audit()` gained a `progress` callback** threaded through to `_search_google()` and `_attach_scraped_reviews()` — real messages now fire at every phase transition ("Buscando en Google Maps…", "Google Maps: 34 sede(s) encontradas.", then per-location "Analizando reseñas recientes: 12/34 sede(s) — Foo (3 reseñas)." as review scraping actually progresses). The callback is just a thread-safe list-append closure per job, called concurrently from whichever `ThreadPoolExecutor` pool happens to be active (Google Details, review scraping, Apple's per-anchor searches) — no contextvar/thread-local propagation needed since it's passed explicitly rather than inferred from calling-thread context.
- **PDF export uses the same job/poll flow** — `/report/start` runs the full audit + `render_report_pdf()` in the background and returns the PDF as base64 in the final `result.pdf_base64` (small enough for this tool's PDF sizes, ~650KB → <1MB base64, to stay in a plain JSON response); the frontend decodes it into a Blob client-side, same download mechanism as before.
- **Frontend**: the static step dots (Google/Apple/Bing/Official) are kept as a coarse "these 4 things are happening" indicator, unchanged; the status-bar text now updates live to the latest real backend message instead of a fixed string, and a new collapsible "Ver registro detallado" panel under the progress card lists the full message history as it streams in (auto-scrolling), addressing the ask to surface real logs/progress in the app, not just relabel the static text.
- Verified end-to-end against the real running server (not just mocks): started a real "McDonald's, Barcelona" job, polled `/jobs/<id>/status` repeatedly — watched real progress messages arrive in the correct order (Google/Bing found concurrently, Apple only starting after Google's coordinates were available, then the final comparison step), confirmed `since=N` returns only new messages, and confirmed the completed job's `result` matches the audit shape the frontend already knows how to render.
- Test suite grew to cover the job endpoints: starting a job runs the audit in the background and records progress messages, `since`-based incremental polling, unknown job id → 404, an audit exception surfaces as `status: 'error'` with the message, and the PDF job path returns valid base64-decodable bytes with the right filename.

## Status (2026-07-22, latest)

- **Anchor redesigned from "official data" to "Google Maps"** after two real-world test cycles (Zara, Movistar) exposed how unreliable official-data *extraction* is in practice, independent of the comparison logic. Official data is optional again; Apple/Azure/Official are each compared against Google, never against each other.
- **Firecrawl scraping fallback built, then removed.** It required increasingly complex fixes to keep working (multi-page BFS crawl for hierarchical sites like Movistar's region→province→city→store structure; a markdown-link regex that silently found zero links because it didn't handle title attributes; distinguishing a genuine leaf/detail page from one that merely has few "nearby store" links) — each fix surfaced a new failure mode on a new real site. Decision: cut the paid service. (A lighter, in-house replacement was added in a later session — see the next Status entry below.)
- **Verification links added** (`normalize.google_maps_url`/`apple_maps_url`/`bing_maps_url`) — every flag now carries a `links` array so a finding ("phone differs between Google and the official site") can be clicked through and checked live, per source, during a sales call.
- **Location identification added** — `matching.py` assigns a `cluster_id` (`L1`, `L2`, ...) and `canonical_address` per matched location, shown in both the UI findings table and the PDF, so multiple same-named locations ("ZARA" × 50) are distinguishable.
- **Real bug fixed**: name-variation comparison (R9) used raw strings, so "ZARA" vs. "Zara" (pure capitalization) scored 25 via rapidfuzz — looked like a critical mismatch. Fixed to compare on `name_norm`.
- Results UI switched to tabs (Google/Apple/Azure/Official in one card) instead of 4 stacked cards.
- Test suite: 83 tests, all passing, covering the full Google-anchored redesign.
- Fixed Google search missing `language=es` — was silently returning English weekday names, which would have broken hours comparison against Spanish-language official data.
- Localistico branding (`assets/images/logo.png`/`icon.png`) wired into both the web UI (favicon + header, with a light-background fallback for dark mode since the wordmark has no dark variant) and the PDF cover (base64-embedded).
- PDF report renders correctly (cover with logo, KPI tiles, recommendations, findings table, reputation ranking with quoted negative reviews, appendix) — verified visually.
- Remaining from the original plan: category comparison (R12) — deferred, not blocking for v1 use.

## Status (2026-07-22, in-house scraper)

- **Built a self-hosted replacement for the Firecrawl fallback** — see "The in-house scraper" above. Deliberately scoped to skip bot-protected sites entirely (no attempt to bypass, unlike Firecrawl's whole value proposition) and cover only "reachable but no schema.org," which is what the real Movistar case actually needed.
- **Real bug found via manual end-to-end testing with an actual browser** (not just mocks): a JS-rendered fixture page's `<script>` tag — containing the JS source that builds the address string — got matched by the heuristic parser as a second, bogus location, since `page.content()` includes script/style tags verbatim in the DOM after rendering. Fixed by stripping `script`/`style` before scanning; added as a regression test since this is a systematic risk for any JS-injected-content site, not a one-off.
- **Real bug found independently while first testing Movistar**: `requests.get('https://tiendas.movistar.es/...')` failed with `SSLCertVerificationError` — confirmed via `openssl s_client` that the server sends an incomplete certificate chain (missing intermediate), which `curl`/browsers tolerate via OS-level chain-building but plain `certifi`-based Python doesn't. Fixed with `truststore` (delegates to the OS-native trust store) rather than disabling verification.
- Verified end-to-end against the real Movistar Barcelona store locator: 83 stores extracted via the heuristic HTML pass alone (Playwright never needed — the case that motivated Playwright's inclusion turned out not to require it in practice), 57 successfully matched against Google/Azure after geocoding, real R6/R9/R11 findings generated, full PDF generated and visually verified.
- Verified no regression on the already-working paths: Zara (bot-protected) still short-circuits to `'inaccessible'` without any heuristic/Playwright attempt.
- Playwright's real Docker image cost (~2GB, see Known limitations) was measured, not estimated — built and ran the actual image, confirmed Chromium launches correctly inside the container with `--disable-dev-shm-usage`.
- Test suite: 95 tests, all passing (up from 83) — all mock `_render_with_playwright`/`requests.get`, none require a real browser or network to run.

## Deployment readiness (2026-07-22)

**Tests:** `pytest` suite added (`tests/`, run with `pytest` from repo root — `pytest.ini` sets `pythonpath = .`). 59 tests covering `normalize`, `matching`, `inconsistencies`, `reputation`, `official`, and the Basic Auth gate in `app.py`. All passing. One real bug caught and fixed by writing these: `address_norm`'s abbreviation expansion (`C/` → `calle`) ran *after* punctuation stripping, so `\bc/\b` could never match (`/` is itself non-word, breaking the trailing `\b`) — fixed by expanding abbreviations before stripping punctuation.

**Auth:** HTTP Basic Auth added (`app.py`, `_require_basic_auth`), gated on `BASIC_AUTH_USERNAME`/`BASIC_AUTH_PASSWORD` env vars — enforced only when both are set, so local dev without them stays frictionless while any real deployment must set them. Verified: 401 with no/wrong credentials, 200 with correct ones, still open when unset.

**Docker:** `Dockerfile` (Python 3.13-slim + Pango/cairo/gdk-pixbuf for WeasyPrint + `playwright install --with-deps chromium` for the official-data scraper fallback + gunicorn) and `.dockerignore` (excludes `.env` and the Apple `.p8` key — never bake secrets into an image). Built and run locally end-to-end: Google (60 results) + Azure (98 results) + PDF generation + a real headless-Chromium launch all confirmed working inside the container. Apple Maps fails inside the container by design (`.p8` isn't in the image) — **at real deploy time, the `.p8` file must be mounted as a file-based secret (e.g. GCP Secret Manager volume mount), not baked in or passed as a plain env var.** Image size: ~407MB without Chromium, **~2.4GB with it** — measured by building both versions, not estimated.

**Known deploy-time gotcha:** if you pass secrets via a raw `.env` file to a container runtime (e.g. `docker run --env-file .env`), the quotes around values (`KEY="value"`) are *not* stripped the way `python-dotenv` strips them when parsing `.env` directly — you get `"value"` (with literal quotes) as the env var, breaking every key. Real deploy targets (Cloud Run env vars, Secret Manager, CI/CD secret injection) take the raw value with no quotes, so this only bit the local `docker run` test methodology — but worth remembering when wiring up whatever deploys this.

**Deploy target:** deliberately not decided yet — evaluated Netlify (poor fit: no native-lib support for WeasyPrint, function time limits) and DigitalOcean (App Platform would work, needs a Dockerfile either way) against Google Cloud Run (same Docker path, but serverless/scale-to-zero and no new account needed). Actual deployment (project choice, CI/CD wiring, Secret Manager setup) is intentionally deferred — to be revisited once ready, likely via CI/CD rather than a manual `gcloud run deploy`.

## Status (2026-07-22, one-level store-page crawling)

Reopens a design question this session had previously closed (see "The in-house scraper" / "Why Google is the anchor" above): a real store-locator index page is often just a directory of links to individual store pages, with no address of its own on the index page (e.g. Zara's nationwide list of city links — the exact case that made the earlier Firecrawl-based multi-level crawler so fragile it got cut entirely). Per explicit product direction, `official.py` now crawls **one level** from a given URL when it has no extractable data of its own — deliberately scoped far below what Firecrawl attempted:

- **`_extract_one()` gained a 4th tier**, between the raw-HTML heuristic and Playwright: `_crawl_candidate_links()` discovers same-domain links on the page (`_discover_links()` — filters obvious nav/legal/login text, dedupes, caps at `_MAX_CANDIDATE_LINKS=80`, no URL-pattern guessing since store-locator URL shapes vary too much per site to guess reliably), then tries schema.org then the heuristic scraper against each one (parallelized, no Playwright at this nested level, to bound cost). A discovered link is only ever reported if it actually resolved to a real location — irrelevant links (the large majority, on a real page) are silently discarded rather than reported as "missing schema," which would just be noise about pages that were never store pages. This is the key difference from the old Firecrawl approach: no attempt to classify "is this an index or a leaf" ahead of time, no multi-level BFS, no site-hierarchy understanding needed — try every same-domain link once, keep only what resolves.
- `_extract_one()`'s return signature grew from a 3-tuple to a 4-tuple, `(locations, error, status, sub_analyses)` — `sub_analyses` carries one `{'url', 'status', 'location_count', 'page_type': 'store_page'}` entry per crawled page that resolved, so `extract_official()` can report each discovered store page's own schema.org status individually (not just the index page's), addressing the ask to "validar si tiene schema markup correcto" for the actual store pages, not just the URL the user pasted in.
- **The extracted locations already flow into the "Datos oficiales" tab with no extra wiring needed** — `extract_official()`'s `locations` list (now including everything found via crawling) was already merged into `results['official']` in `app.py`'s `run_official()`, which was already serialized to the `/search` JSON response's `official` key and rendered by the existing official-data tab in `public/index.html`. The ask to route extracted data there was already satisfied by the existing plumbing once the crawl step started actually populating `locations` for index-only pages.
- Test suite grew to cover the new tiers: `_discover_links` (nav/off-domain filtering, dedup, cap), `_crawl_candidate_links` (merges resolved pages, discards irrelevant ones silently), `_extract_one`'s crawl tier end-to-end (index page with zero direct data → 2 store pages resolved via crawl, Playwright never invoked), and `extract_official()` correctly surfacing per-crawled-page findings (a `no_schema` sub-page gets its own moderate finding; a `found` one doesn't).
- Known scope limits, stated explicitly rather than left implicit: only one level deep (an index → region → city → store hierarchy like Zara's isn't followed further); link discovery only runs against the raw HTML fetched via plain `requests.get`, not retried against Playwright-rendered HTML (a JS-only index page that needs a browser just to reveal its own store links is an edge case left for later); still no attempt at bypassing bot protection, unchanged from before.

## Status (2026-07-22, Apple proximity fix + Google competitor filter)

Two more real bugs found via user-reported symptoms and confirmed live against the actual APIs before fixing (not guessed at).

- **Apple returned far too few results for chain prospects.** Confirmed live: querying `"NH"` anchored at Barcelona's city center returned only 2 hotels; the identical query anchored at one specific hotel's own coordinates returned 10, including the one missing from the city-wide search — and the first request came back with no `pageToken` at all, so there was nothing to paginate through either. Apple's `/v1/search` is a proximity-biased "closest matches to this point" search, not an exhaustive chain search like Google's Text Search. **Fix**: `_search_apple()` in `app.py` now takes `extra_anchors` — after the original city-wide query, it re-runs the same query anchored at each of Google's confirmed location coordinates (parallelized, `ThreadPoolExecutor(10)`) and merges everything by POI id. `_run_audit()` restructured so Apple waits for Google to finish (needed for the anchors) while Azure/official extraction still run fully in parallel with Google. Verified end-to-end: "NH" in Barcelona went from 2 → 10 Apple results, including the specific hotel (place-id confirmed) the user had found manually but the tool reported missing; the real audit's wall-clock time barely moved (~3s total). A related, separate finding while investigating: that specific hotel's Apple record is now named "Anantara," not "NH ..." — a real brand rebrand Apple's own data already reflects, not a bug (a text search for the old brand name will never match a POI now filed under a different one).
- **Google Text Search returns real competitors, not just noise.** Confirmed live: searching `"Movistar"` in Barcelona returned 44 results, including "Tienda Orange," three separate "Vodafone Barcelona - ..." branches, "Phone House," "Rogent Telefonia," and other unrelated phone shops — not rare mismatches, a large fraction of the result set. Google's Text Search is relevance-based (anything "related" to the query text near that location), not an exact-brand filter. **Fix**: `_search_google()` now filters the text-search results by `rapidfuzz.fuzz.token_set_ratio` between the query name and each result's name (`normalize.name_norm`'d on both sides) before the (quota-costing) Details + review-summary calls — threshold 60, chosen because real matches score 100 while every real competitor example scored 16-35, comfortably separated. Verified live: 44 → 31 results for "Movistar," every Vodafone/Orange/unrelated-shop entry gone. Known accepted edge case: a multi-carrier SIM-card kiosk whose listing name literally strings together "Vodafone Movistar O2 Jazztel" still scores 100 (the query token really is a substring) and slips through — rare, and arguably still a legitimate finding (a reseller carrying the prospect's SIMs) rather than a clean false positive.

## Status (2026-07-22, venue table + Bing rename)

Follow-up session. Replaced the rule-flags table + separate reputation-ranking table with a single **venue table**, per explicit product direction: one row per matched location, sorted worst-to-best data quality, columns in priority order (presence dominates accuracy dominates rating/reviews dominates everything else).

- **New module `venue_metrics.py`**: `compute_venue_metrics(clusters, has_official_data)` adds a `venue_metrics` dict to each cluster and sorts `clusters` in place by a weighted `venue_score` (wide order-of-magnitude gaps between presence/accuracy/rating tiers, so a full swing in a lower tier can never outrank a difference in a higher one — a heuristic, not a formally-proven lexicographic sort). `presence_pct`'s denominator only includes `official` when official data was actually provided for that audit — otherwise an audit with no store locator would be permanently capped below 100%. `accuracy_hours`/`accuracy_phone` weight Apple + official data 3x over Bing/Azure's 1x; `accuracy_name` inverts that (Apple/Bing 3x, official 1x) — both per explicit spec. `inconsistencies.py` gained three small pure "verdict" helpers (`compare_field`, `name_similarity`, `hours_agreement_pct`) so flag generation and this new accuracy scoring share one source of truth for "does this agree with Google or not," instead of duplicating the comparison logic.
- **Feasibility research done before implementing** (Google Places API (New)/Business Profile API docs, Apple Maps Server API/Business Connect docs) confirmed 5 of the requested metrics are **not available via any public API**: Action Links (Google + Apple) and Google Posts/Products all require the business itself to grant OAuth access to its own Business Profile/Business Connect account — infeasible when auditing a prospect that hasn't onboarded. Reply rate has no `owner_response` field anywhere in the Places API (New) `Review` resource (confirmed against the official field reference) — the third-party services that do expose it (e.g. lobstr.io) get it via their own paid scraping, not Google's API. **Decision (user-confirmed): show all 5 as `N/D` for now**, each carrying a fixed reason string, plus a TODO note pointing at a future paid provider (SerpApi) or an in-house review/reply scraper as the eventual fix. `review_rate_3m` is the one partially-computable metric — approximated over the ≤5 most recent Google reviews already fetched by `_search_google`, always tagged `approx: True`, never presented as a real rate.
- **`bing_maps_url()` fixed** (`normalize.py`): was `cp={lat}~{lng}&lvl=18&q={name}`, which triggers a Bing *text search* (generic results list, the reported bug) rather than anchoring the specific venue. Now uses Bing's `sp=point.{lat}_{lng}_{name}` pushpin syntax, matching how `google_maps_url`/`apple_maps_url` already deep-link to one exact point. Confirmed this bug was link-construction only — `_search_azure()` in `app.py` already returns one distinct record per POI (own id/name/address/phone/lat/lng), so `matching.cluster_records()` and all comparisons were unaffected; only the "Verificar" link was broken.
- **"Azure" renamed to "Bing" in all user-facing text** (UI labels/tooltips/progress steps, PDF cover/KPIs/appendix) — scoped deliberately to display strings only; internal identifiers (`_search_azure`, `from_azure`, the `'azure'` dict/JS key, `AZURE_MAPS_SUBSCRIPTION_KEY`, the `audit_azure_*.csv` export filename) were left as-is since they're invisible to the user and renaming them added risk with no visible benefit. `inconsistencies._LABELS['azure']` is the single choke point that drives the rename across dynamic flag messages and link labels; `report.py`'s new `SOURCE_LABELS` dict does the same for the PDF appendix, which previously leaked the raw lowercase `azure:` key into the rendered PDF (a preexisting cosmetic bug, fixed as a side effect).
- **Site-level "store locator optimization" check + free-text comment field added**. `official.py`'s `extract_official()` now exposes `site_analysis` (per-URL `status`/`location_count`/inferred `page_type`) — previously computed internally but discarded after building `findings`. `page_type` is inferred purely from result count (`'index'` when a URL yields >1 location, `'store_page'` otherwise) since there's no crawling; this only distinguishes an index page from a store page well when the user pastes both kinds of URL into the same multi-URL box. A new optional `official_comment` form field (sales rep's free text, e.g. "le faltan fotos") is read in `_parse_request_params()` and passed through untouched to both the UI and the PDF's new "Análisis del store locator" section — this section (and the schema-optimization verdict) only renders when at least one `official_url` was provided, since CSV-only audits have no page to check schema on.
- Test suite grew from 96 → 109 tests: new `tests/test_venue_metrics.py` (presence/accuracy weighting, the "complete-but-mediocre-rating still outranks missing-a-source" ordering invariant, N/D placeholders, review-rate approximation), a `site_analysis` test in `test_official.py`, an updated `bing_maps_url` format test, and `_parse_request_params`'s new 6-tuple return (added `official_comment`) updated across `test_app_validation.py`.

## Status (2026-07-22, venue table follow-ups) — presence popover + real Azure/Bing hours

Follow-up session, refining the venue table from the previous entry.

- **Presence is now a click-to-open popover, not a hover tooltip.** The `%` cell in `public/index.html` is a `<button class="presence-btn">`; clicking it renders a floating `.popover` (viewport-clamped positioning) listing every checked platform with a status dot and a link — "ver" (its `verify_url`) when present, "buscar" (a text-search deep-link) when absent — so the absence can be confirmed live with one click. Click-outside/Escape/opening another row's popover all close it (`closePresencePopover()`); also called on re-render and `resetForm()` so a stale popover never points at a removed trigger element. PDF still shows the equivalent info inline (no hover/click in print), unchanged from the previous entry.
- **Real bug found and fixed: Bing/Azure hours were never being compared, at all.** Root cause — `_search_azure()` in `app.py` never requested or mapped any opening-hours field, and `FIELD_SUPPORT['azure']` didn't include `'opening_hours'`, so `accuracy_hours`/R14/R14b could only ever come from official data. Researched both APIs directly against their official docs before touching code: **Apple's Maps Server API genuinely has no hours field anywhere** (confirmed against Apple's own `Place` object reference — a hard, permanent limitation, correctly left out of `FIELD_SUPPORT['apple']`). **Azure DOES support hours**, but only when the fuzzy-search request opts in via `openingHours=nextSevenDays` — undocumented-by-omission unless you read the `Get Search Fuzzy` reference closely, since the base response silently omits hours without it.
  - Fix: `_search_azure()` now passes `openingHours=nextSevenDays`; new `_azure_opening_hours(poi)` helper converts Azure's rolling 7-day window of absolute date+time ranges into the same `["Día: HH:MM–HH:MM", ...]` shape `normalize.parse_hours` already expects everywhere else (bucketing each range by `startTime.date`'s weekday, combining split ranges on the same day, Spanish day names) — a day with no range in the window is left out entirely rather than marked "closed" (Azure's coverage isn't guaranteed per-POI, and guessing "closed" would manufacture false conflicts). `from_azure()` and `FIELD_SUPPORT['azure']` updated to carry it through.
  - **Verified against real data**: 69 of 83 real Bing/Azure results for a live "McDonald's, Barcelona" audit came back with real opening hours, and `accuracy_hours` correctly flagged a genuine Google-vs-Bing hours conflict that was completely invisible before this fix (previously always `sin_dato`/`na`, indistinguishable from "we can't tell").
  - Known follow-on nuance, not fixed here (out of scope for this bug report): Azure hours crossing midnight (e.g. `"06:00–03:00"`) parse into an inverted minute range (close < open) via the existing `normalize.parse_hours`/`_to_minutes` — same pre-existing behavior Google's hours would hit too, not something introduced by this fix, but worth a follow-up if it produces noisy diffs in practice.
- Test suite grew from 116 → 123 tests: new `tests/test_azure_hours.py` (weekday bucketing, split-range combining, multi-day sorting, feeds real `parse_hours` output), renamed/split `test_apple_has_no_hours_support_so_no_flag` + new `test_azure_hours_are_compared_when_present` in `test_inconsistencies.py`, and `test_venue_metrics.py`'s hours tests updated to reflect Azure's now-real (if per-record) hours support vs. Apple's structural absence.

## Status (2026-07-22, later same day) — official-anchored redesign

Follow-up session. Note: R2 from the earlier status entries above no longer exists as a separate rule — its two cases ("on maps, not official" / "on official, not maps") got folded into a broadened R1, since official presence is now mandatory rather than one-of-four-symmetric-sources.

- **Firecrawl fallback fixed**: it wasn't triggering at all for sites that block the direct request outright (Zara returns 403 via Akamai) — the old code only fell back to Firecrawl when the direct fetch *succeeded* but found no JSON-LD. Now it falls back on any failure of the schema.org path.
- **Firecrawl multi-page crawl added** for store-locator index pages (large chains list all stores nationwide with no per-store address — e.g. Zara's ~150 stores). Direct JSON-schema extraction on such a page either times out or has the LLM hallucinate low-quality results. Fix: markdown pass first (no LLM) to discover per-store links scoped to the target city via header-matching, then extract each linked page individually. Verified end-to-end against the real Zara Barcelona store-locator: 21 real stores extracted with address/phone/hours, capped at `_MAX_DETAIL_PAGES=40`.
- **Geocoding fix for official records**: Firecrawl/CSV-sourced official locations essentially never come with lat/lng, which broke the reliable coordinate-based matching and fell back to a fragile fuzzy name/address comparison. Diagnosed on real data: only 2 of 21 Zara stores matched anything (Google's Catalan "Carrer de Pelai" vs. official Spanish "Calle Pelai"; "Zara" vs. branch-specific "Zara Pelayo" both scored far below the fuzzy-match thresholds). Fixed by geocoding official addresses via the Google Geocoding API post-extraction (`_fill_missing_coords()`), plus adding Catalan street-type words to `normalize.py`'s abbreviation table as a complementary fix. Result: 12 of 21 matched correctly after the fix.
- **Official data made mandatory** (`_require_official_source()`, 400 without it) and **CSV upload added** as a third way in (`parse_official_csv()`, template at `public/template_oficial.csv`) alongside URL-based extraction — per explicit product direction: comparing platforms against each other with no ground truth is noise, not a finding.
- **`inconsistencies.py` rewritten** to always compare official vs. each platform, never platform vs. platform — verified this actually changes behavior (a synthetic test confirms Google/Apple disagreeing on phone number produces *no* flag when there's no official record, but the identical disagreement against an official record does).
- **`/search` and `/report` switched to POST** (`multipart/form-data`, via `request.values`/`request.files`) to support the CSV upload; still accept GET for URL-only/bookmarkable use. Frontend's PDF export switched from a plain `<a href>` click to `fetch` + `blob()` + object-URL, since a file upload can't ride along a plain link click.
- **Results UI switched to tabs** (Google/Apple/Azure/Official in one card) instead of 4 stacked cards — the page had gotten very long.
- Test suite grew from 59 → 71 tests covering all of the above; all passing.
