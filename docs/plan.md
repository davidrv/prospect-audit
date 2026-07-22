# Prospect Audit — Roadmap: Comparative Audit & Sales Report

> **Status update (2026-07-22): Phases 1-8 below are implemented and verified end-to-end** (Azure Maps, Google reviews, schema.org extraction, matching, inconsistencies, reputation, findings UI, PDF report). See `docs/context.md` "Status" section for what was tested and what's still deferred (opening-hours/category comparison rules, the couple of minor known-limitation caveats). This file is kept as the original design record — the "suggested build order" at the bottom reflects the plan as written, not necessarily the order things ended up shipping in.
>
> **Later divergence (same day):** official data was briefly made mandatory with every comparison anchored on it, then reverted — **Google Maps is now the anchor**, official data is optional again. A Firecrawl-based scraping fallback for official-data extraction was built, then removed (too fragile across real sites), then replaced with a lighter in-house heuristic HTML scraper + Playwright, deliberately scoped to skip bot-protected sites entirely. See `docs/context.md`'s "Why Google is the anchor" and "The in-house scraper" sections for the full reasoning and the real Zara/Movistar data that drove each change.

## Goal

The real purpose of this tool isn't just "search two maps APIs" — it's to **find inconsistencies and reputation problems** across a prospect's locations, package them into a compelling sales artifact, and use that to pitch Localistico's solution. Today it shows two raw tables side by side; this roadmap turns it into an actual audit tool that surfaces *findings*, not just data.

## Decisions already made (not open questions)

- **3rd data source: Azure Maps**, not Bing — Bing Maps API is being retired by Microsoft, Azure Maps is the supported successor. It has POI search but no ratings/reviews.
- **"Official" reference data**: extracted automatically from schema.org JSON-LD structured data on prospect-provided URLs (their website / store-locator pages). No generic visual scraper, no manual CSV entry in v1 — if a page has no structured data, the tool reports that clearly rather than failing silently.
- **Review history/velocity is out of scope for v1.** Google's API doesn't expose historical review timestamps in bulk; getting real "reviews/week" trends would require a paid third-party scraping service (SerpApi/Outscraper/DataForSEO). v1 only shows a current snapshot (rating, total review count, up to 5 sample reviews).
- **PDF report**: executive-style report (cover, exec summary, findings, appendix), generated server-side with **WeasyPrint** (HTML/CSS → PDF), reusing the existing visual language from `public/index.html`.

## Unified data model

Every source (`google`, `apple`, `azure`, `official`) gets normalized into the same shape before comparison:

```python
{
  "source": "google" | "apple" | "azure" | "official",
  "source_id": str,
  "name": str | None, "name_norm": str,
  "formatted_address": str | None, "address_norm": str,
  "lat": float | None, "lng": float | None,
  "phone": str | None,          # normalized digits/prefix
  "website": str | None,        # normalized domain
  "rating": float | None,       # Google only
  "review_count": int | None,   # Google only
  "opening_hours": list[str] | None,
  "category": str | None,
  "raw": dict,                  # original payload, for debugging/PDF appendix
}
```

**Field support matrix** (the inconsistency engine only compares fields both sides actually support — no field gets invented):

| field | google | apple | azure | official |
|---|---|---|---|---|
| name/address | ✓ | ✓ | ✓ | ✓ |
| lat/lng | ✓ (add `geometry` to `fields`) | ✓ (`coordinate`, needs propagating) | ✓ (`position`) | only if `geo` present (rare) |
| phone | ✓ | often null | ✓ | ✓ (`telephone`) |
| website | ✓ | ✓ | ✓ | ✓ |
| rating/reviews | ✓ | ✗ | ✗ | ✗ |
| opening_hours | ✓ | ✗ | ✗ | if `openingHoursSpecification` present |

---

## Phase 1 — Azure Maps as 3rd source

- New env var: `AZURE_MAPS_SUBSCRIPTION_KEY` in `.env` (**blocked on the user obtaining this from Azure**).
- New function `_search_azure(name, city)` in `app.py`, same shape as `_search_google`/`_search_apple`.
  - Endpoint: `GET https://atlas.microsoft.com/search/fuzzy/json?api-version=1.0`
  - Auth via `subscription-key` query param (no JWT flow, unlike Apple).
  - Params: `query=f'{name} {city}'`, `lat`/`lon` from the existing `_geocode_city(city)` cache, `radius=50000`, `countrySet=ES`, `language=es-ES`, `limit=100`.
  - Pagination via `ofs` offset (not a token) — loop until a page returns fewer than `limit` results, with a defensive cap (`ofs >= 200`).
  - Filter to `item.get('type') == 'POI'` only (Fuzzy Search also returns raw addresses/streets).
  - Map `poi.phone` → `phone_number`, `poi.url` → `url`, `poi.categories` (joined) → `category`, `position.lat/lon` → coords.
- Add a third `run_azure` thread in `/search`, following the existing `try/except` + `app.logger.error` pattern.
- **Verify**: same McDonald's/Barcelona smoke test used for Google/Apple; confirm POI-only filtering doesn't leak street/address results.

## Phase 2 — Reputation signal inputs (Google reviews sample)

- One-line change: add `reviews` (and optionally `reviews_sort=newest`, so the sample skews toward *recent* negativity rather than "most relevant") to the `fields` param in `_search_google`'s `_detail()` call. No other ingestion code needed — `reviews` just rides along in the existing response.
- Billing note: `rating`/`user_ratings_total`/`reviews` are all in Places API's "Atmosphere Data" tier — already being paid for today, `reviews` doesn't move to a new tier.
- **Verify**: confirm `reviews` array appears in `/search` output with `author_name`, `rating`, `text`, `relative_time_description`.

## Phase 3 — Official data via schema.org

- New function `_extract_official(urls: list[str]) -> {"locations": [...], "errors": [...]}`.
- Per URL (threaded, like Google details): `requests.get` with a realistic `User-Agent` (many store-locators block the default one) → `BeautifulSoup(html, 'html.parser')` → all `<script type="application/ld+json">` tags → `json.loads` per tag (skip individually on `JSONDecodeError`, don't abort the whole page).
- Normalize JSON-LD shape (single dict / list / `@graph`) via a `_flatten_jsonld()` helper.
- Accept nodes via a whitelist (`LocalBusiness`, `Restaurant`, `Store`, `FoodEstablishment`, ...) **plus** a duck-typing fallback (any node with `address` + `telephone`/`openingHoursSpecification`, regardless of `@type`) — schema.org has ~400 `LocalBusiness` subtypes, a strict whitelist alone will miss cases.
- Handle `ItemList` explicitly (`itemListElement` → `.item`).
- Map to the unified schema: `PostalAddress` → `formatted_address`, `telephone` → `phone`, `geo.latitude/longitude` → coords, `openingHoursSpecification` (day codes can be full schema.org URLs, short codes, or arrays) → the same `["Lunes: 09:00–22:00", ...]` shape Google uses.
- **If a URL yields nothing**: add to `errors`, surface prominently in the UI/report — this is the accepted v1 limitation, must never fail silently.
- Dedup identical `name_norm`+`address_norm` results (common when a store-locator index page and its individual store pages both embed the same `LocalBusiness`).
- **API contract change**: `/search` needs to accept 0..N URLs — switch it from `GET` to `POST` with JSON body `{"name", "city", "official_urls": [...]}`. Update the frontend `fetch` call accordingly.
- New deps: `beautifulsoup4` (stdlib `html.parser`, no `lxml` needed for this volume of pages).

## Phase 4 — Entity resolution (matching locations across sources)

- Normalize `name_norm` (strip accents/punctuation, lowercase) and `address_norm` (expand `c/`→`calle`, `av.`→`avenida`, etc.) for every record first.
- Compute, for every cross-source pair: `dist_m` (haversine, hand-written, no new dependency) when both have coordinates; `name_sim`/`addr_sim` via `rapidfuzz.fuzz.token_sort_ratio`.
- Match rule (tune via named constants at the top of `app.py`):
  - `dist_m <= 30m` → match regardless of name (same building).
  - `dist_m <= 120m` **and** `name_sim >= 60` → match.
  - no coordinates on either side: `name_sim >= 85` **and** `addr_sim >= 70` → match (typical for `official` records without `geo`).
- Union-Find to make matches transitive → clusters = "candidate location."
- Special cases:
  - A cluster containing 2 records from the *same* source → mark `ambiguous: true` for manual review (likely two real, nearby locations getting merged by the threshold) — don't silently merge their data.
  - A singleton cluster (1 record, 1 source) is itself a finding — "found in Google, absent everywhere else."
- New dep: `rapidfuzz` (faster and better-maintained than `difflib`/`fuzzywuzzy`, no compiled-dependency headaches).

## Phase 5 — Inconsistency detection engine

Per cluster, compare fields across the sources present (respecting the field-support matrix above) and emit flags:

**Critical**
- **R1** Single-source location — found in only 1 of 4 sources.
- **R2** Maps↔Official mismatch — present on ≥1 map provider but absent from official data (or vice versa) — the core sales hook ("customers find you on Maps but your own site doesn't list this location").
- **R3** Weak-match discrepancy — cluster only formed via the no-coordinates fallback (text similarity alone) → flag for manual review.
- **R4/R6** Missing phone/website where another source has one.
- **R5/R7** Conflicting phone / conflicting website domain across sources.

**Moderate**
- **R9** Notable name variation (`name_sim` 60–90 in the match).
- **R10** Different address formatting despite a solid geographic match (informational, shown side-by-side).
- **R11** Partial coverage — present in exactly 2 of 4 sources.
- **R14b** Contradictory open/closed status between sources that both support hours.

**Minor**
- **R13** Cosmetic name variation (`name_sim ≥ 90`).
- **R14** Small opening-hours differences (<30 min).

Output per cluster (feeds directly into both the UI and the PDF):
```python
{
  "cluster_id": str, "canonical_label": str, "ambiguous": bool,
  "sources_present": [...], "sources_missing": [...],
  "records": {"google": {...}, "apple": None, "azure": {...}, "official": {...}},
  "flags": [{"rule": "R1", "severity": "critical", "message": "...", "fields": [...], "sources": [...]}],
}
```

## Phase 6 — Reputation signals (Google-only)

Computed per matched location once Phase 2 + Phase 4 are in place:

| Flag | Condition | Weight |
|---|---|---|
| `NO_GOOGLE_PRESENCE` | Confirmed to exist via another source, no Google match | 100 (auto-critical) |
| `NO_REVIEWS` | Google entry exists, `user_ratings_total` is 0/null | 35 |
| `LOW_RATING` | `<3.5` → 40 · `3.5–3.99` → 25 · `4.0–4.29` → 10 · `≥4.3` → 0 | tiered |
| `FEW_REVIEWS_RELATIVE` | `review_count` < 20% of this chain's own median across its matched Google sedes (only when ≥3 have data) | up to 20 |
| `NEGATIVE_RECENT_SAMPLE` | Any of the ≤5 sample reviews has `rating ≤ 2` | 15 × count, capped at 30 |

`reputation_severity_score = min(100, sum of triggered weights)` — sort "highest risk locations" by this descending. Uses an **internal chain benchmark** (median across the prospect's own matched sedes) instead of an external "expected reviews for this business type" figure, since no such benchmark is available without a paid data source — also more persuasive ("your Diagonal location has 800 reviews, this one has 12").

## Phase 7 — Consolidated audit view (UI)

Before the raw per-source tables, add a findings-first view: KPI summary (locations with critical flags, locations below rating threshold, locations missing from each source) + a sorted flags table. The raw Google/Apple/Azure/Official tables remain as a lower "appendix" section, not the primary view — the whole point of this phase is that the analyst leads with findings, not raw data.

## Phase 8 — PDF executive report

- New route `GET /report?name=...&city=...` (POST if URLs are involved per Phase 3's contract change), reusing a shared `_run_all_sources()` helper so `/search` and `/report` don't duplicate the threaded-fetch logic.
- New template `templates/report.html` (Flask's default `templates/` folder — **not** `public/`, which is served statically and would leak unrendered `{{ }}` if hit directly).
- Structure: Cover (prospect, city, date, overall risk pill) → Executive summary (4-6 KPI tiles + 2-3 sentence narrative) → Quick recommendations (tied to flags actually present) → Top inconsistencies table (from Phase 5) → Reputation section (Phase 6 ranked table + 3-5 verbatim negative-review "quote cards") → Appendix (full per-source, per-location detail, page-broken).
- WeasyPrint adaptations vs. the live-browser CSS: light theme only (no dark-mode media query), no icon webfont (color-coded badges/plain glyphs instead), no `@keyframes`/CSS Grid (flex only), explicit font stack (`Helvetica Neue, Helvetica, Arial` — WeasyPrint's Pango backend doesn't resolve `-apple-system`), 1px borders instead of `0.5px`, `@page` rules for A4 size + running footer/page numbers, `page-break-before: always` before the appendix, base64-embedded logo.
- New dep: `weasyprint>=62,<64`. Needs native Pango/cairo/gdk-pixbuf via Homebrew (`brew install pango cairo gdk-pixbuf`) — already present on this machine. Known macOS gotcha if `import weasyprint` can't find the `.dylib`s: `export DYLD_FALLBACK_LIBRARY_PATH="$(brew --prefix)/lib:$DYLD_FALLBACK_LIBRARY_PATH"`.
- Frontend: new "Exportar informe PDF" button near the CSV buttons, disabled until a search completes, builds a temporary `<a>` and clicks it (same pattern as `downloadCSV`) — this one is a real server round-trip (re-runs the full search + PDF layout, expect up to a couple seconds), so show a loading state.

## Backlog (explicitly deferred)

- Review velocity / historical trend (reviews per week/month) — needs either a paid scraping service (SerpApi/Outscraper/DataForSEO) or building forward history via periodic snapshots starting from whenever this feature ships. Revisit once there's real usage data or budget for a third-party service.
- Chains with >60 Google results (pagination cap) — deprioritized by the user relative to this roadmap.
- Website-reachability check (`R8`, HEAD/GET per site) — extra HTTP round-trip per location, evaluate cost/latency before adding.
- `phonenumbers`-based phone normalization if the regex approach in Phase 4 produces too many false positives/negatives in practice.

## Suggested build order

1. Phase 2 (Google reviews field) — trivial, no new deps/keys, immediately useful.
2. Phase 3 (schema.org extraction) — no new keys needed, testable against any prospect's real site.
3. Phase 1 (Azure Maps) — **blocked until `AZURE_MAPS_SUBSCRIPTION_KEY` is obtained**; code can be written in parallel but won't be verifiable until then.
4. Phase 4 + 5 (matching + inconsistency engine) — the core value prop, depends on having ≥2 real sources with data to compare (Google + official is enough to start).
5. Phase 6 (reputation signals) — depends on Phase 2.
6. Phase 7 (UI findings view) — depends on 4/5/6.
7. Phase 8 (PDF report) — depends on 7, last because it's presentation-layer on top of everything else.
