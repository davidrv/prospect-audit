import os

# Keep the test suite hermetic and deterministic: never touch the persistent
# audit cache (cache.py reads this env var at call time). Otherwise a first
# run would populate SQLite and a second run would get cache hits.
os.environ['DISABLE_AUDIT_CACHE'] = '1'

# Same reasoning for the audit-history store (history.py) — off by default in
# tests so runs don't create/mutate a SQLite file; test_history.py re-enables
# it against a tmp path.
os.environ['DISABLE_AUDIT_HISTORY'] = '1'

# And the store-locator sitemap probe (official._analyze_sitemap) — off in
# tests so extract_official never makes a real network call for robots.txt /
# sitemap.xml; test_official.py re-enables it with a mocked requests.get.
os.environ['DISABLE_SITEMAP_FETCH'] = '1'
