# Metaflow Metadata Service — Non-Breaking Pagination & Tag Filtering

**Google Summer of Code 2026 · Final Report**

| | |
|---|---|
| Contributor | Eunhye Jeong ([@guts27](https://github.com/guts27)) |
| Organization | [Metaflow](https://metaflow.org) |
| Project idea | Metadata service request improvements |
| Mentor | Sakari Ikonen ([@saikonen](https://github.com/saikonen)) |
| Repository | [Netflix/metaflow-service](https://github.com/Netflix/metaflow-service) |
| Scope | Server side (metadata service, UI backend) |

---

## Summary

I added cursor pagination to the list endpoints of the Metaflow metadata
service without breaking existing clients, and changed the tag autocomplete
cache from a timer to an event-driven refresh.

| | Before | After |
|---|---|---|
| `/flows/{id}/runs` (1.1M rows) | 4,459ms · about 190MB | **26ms** · 50 rows |
| New tag appears in autocomplete | 4m 15s | **96ms** |
| flows / steps / tasks / metadata / artifacts | 3.7 ~ 298ms | **0.07 ~ 20ms** |

All five PRs are merged
([#482](https://github.com/Netflix/metaflow-service/pull/482),
[#488](https://github.com/Netflix/metaflow-service/pull/488),
[#490](https://github.com/Netflix/metaflow-service/pull/490),
[#492](https://github.com/Netflix/metaflow-service/pull/492),
[#493](https://github.com/Netflix/metaflow-service/pull/493)).
Each one includes integration tests.

### What changed from the proposal

**I did not add a tag index.** I measured five index types with nine queries,
and the GIN index that was already there turned out to be the best option.
The conclusion of the benchmark was to change nothing.

---

## Background

Metaflow is a framework for building and running data science workflows.
When a flow runs, it records what happened — runs, steps, tasks, artifacts,
metadata — into PostgreSQL, and users read that back through the Metaflow UI
or the Python client. The service that answers those reads is
metaflow-service.

### The problems

**1. List endpoints returned everything.**
They returned every matching row at once, and there was no way to ask for a
page. On a flow with a million runs, one `GET /flows/{flow_id}/runs` meant
reading 1.1M rows and serializing about 190MB. The same was true for tasks,
artifacts and metadata. Responses grew heavier as a deployment aged, and
backfill jobs that iterate over existing resources had no predictable memory
ceiling.

**2. The tag autocomplete cache only refreshed on a five-minute timer.**
It is an in-memory cache behind the tag dropdown in the UI. A new tag could
take up to five minutes to show up, so a user who tagged a run and wanted to
filter by it right away could not find their own tag. In the other direction,
the service scanned 1.1M rows every five minutes even when nobody was
looking.

### The constraint

There is one deployed service but many client versions. Older clients that do
not know about the new parameters had to keep working.

---

## Goals

The project idea covered both the server and the client side, and two
contributors split it. This report covers the server side.

**From the project idea (server side)**

1. Return paginated responses from metadata-service
2. Stay backwards compatible with clients that do not support pagination

**Added in my proposal**

3. Improve tag filter query performance
4. Remove the five-minute delay in the tag autocomplete cache

---

## What I did

### 1. Cursor pagination

PR [#482](https://github.com/Netflix/metaflow-service/pull/482) ·
[#488](https://github.com/Netflix/metaflow-service/pull/488) ·
[#493](https://github.com/Netflix/metaflow-service/pull/493)

Ten GET endpoints now accept `_limit` and `_cursor`.

```
GET /flows/HelloFlow/runs?_limit=50
→ X-Next-Cursor: eyJ0c19lcG9jaCI6MTc4MDE1NTA1NjE3NywicnVuX251bWJlciI6MTAxMDA1fQ==

GET /flows/HelloFlow/runs?_limit=50&_cursor=eyJ0c19lcG9jaCI6...
```

#### Design decisions

**Keyset, not offset.**
`WHERE (ts_epoch, run_number) < (?, ?)` starts reading from that point in the
index. `OFFSET 100000` counts and throws away 100,000 rows first, so it gets
slower the deeper you page. Keyset also does not shift when rows are inserted
while someone is paging.

**The cursor goes in a header, not in the response body.**
With `X-Next-Cursor` the response body stays byte-for-byte the same, so a
client that does not know about pagination sees exactly what it saw before.

**No parameters means the old behavior.**
If neither `_limit` nor `_cursor` is present, the request takes the original
unpaginated path. These two decisions are how goal 2 (backwards
compatibility) is met.

#### Composite indexes

Each endpoint got an index that matches its sort key. I used
`CREATE INDEX CONCURRENTLY` so the table is not locked while the index is
built.

| Endpoint | Sort key |
|---|---|
| `/flows` | ts_epoch, flow_id |
| `/flows/{id}/runs` | ts_epoch, run_number |
| `/flows/{id}/runs/{n}/steps` | ts_epoch, step_name |
| `/tasks` | ts_epoch, task_id |
| `/metadata` | ts_epoch, id |
| `/artifacts` (3 routes) | ts_epoch, task_id, name |

#### For artifacts, the filter had to move into SQL

Artifacts have a "keep only the latest attempt of each artifact" filter, and
it used to run outside the query. The SQL returned every attempt and the
handler threw away the old ones.

Left as it was, pagination would work on the wrong set: a page of 50 rows
could shrink to 12 after filtering. I moved it into SQL with
`DISTINCT ON (task_id, name)`, so the cursor walks a set that is already
filtered.

---

### 2. Tag filter performance

The plan in my proposal was to add a GIN index to make tag filter queries
faster. I had not read all of the code when I wrote that proposal. When I
actually looked, there was already a GIN index on `(tags || system_tags)`,
and it was working. The question changed from "which index should I add" to
"is the current one the best choice".

My mentor suggested measuring size as well as speed.

> "if the difference in query speed is negligible but the index size is an order
> of magnitude larger, then it's a clear decision that we pick the smaller index."

**I measured five index types with nine queries.** Each query was mapped from
a real API request, and I used two selectivity points — a tag that matches 40%
of all rows, and a tag that matches 5 rows out of 1.1M.

The planner never chose either of the two GiST variants, so there was no
execution time to measure for them. Only their size is in the second table
below. `No index` in the first table means the index was dropped, and it is
there as a baseline.

#### Results

| Case | No index | GIN | GIN off | btree_gin |
|---|---|---|---|---|
| a. metadata, common tag | 0.60 | 676 | 730 | 809 |
| a-rare. rare tag | **1,180** | **0.07** | 0.11 | 0.24 |
| b. + status filter | 3,123 | 3,349 | 3,655 | 3,335 |
| c. ui_backend, flow scope | 0.63 | 37 | 42 | 98 |
| c-rare. rare tag | **943** | **0.16** | 0.22 | 0.32 |
| d. ui_backend, global | 0.26 | 55 | 44 | 48 |
| d-rare. rare tag | **948** | **0.33** | 0.16 | 0.28 |
| e. global + `_order=-ts_epoch` (rewrite ON) | 0.32 | **754** | 733 | 818 |
| f. global + `_order=-duration` (rewrite OFF) | 9,557 | **3,841** | 4,272 | 5,443 |

(ms / one value out of 3 runs)

`b` and `f` take over three seconds in every setup. The status filter in `b`
runs a LATERAL JOIN 400,000 times, and `duration` in `f` is computed rather
than stored, so no index can help. Neither is something a tag index can fix.

| Index | Size | Build time | Planner used it |
|---|---|---|---|
| **GIN (current)** | **6.0 MB** | 3–4s | Yes |
| GIN fastupdate=off | 6.0 MB | 3–4s | Yes |
| btree_gin | 7.2 MB | 3–4s | Yes |
| GiST (text cast) | 204 MB | 2m 31s | **No** |
| btree_gist | 103 MB | 59s | **No** |

#### Selectivity flips the result

Put `d` and `d-rare` side by side and the order reverses. With a common tag,
having no index is 0.26ms against 55ms for GIN — 210 times faster. With a rare
tag, GIN is 0.33ms against 948ms — 2,900 times faster.

With `LIMIT 10` and no index, PostgreSQL scans and stops the moment it has ten
matches. For a common tag it only has to read a few dozen rows. GIN has to
build the full bitmap first, so it cannot stop early: to return ten rows it
pays for finding all 441,840 matches. For a rare tag it is the other way
around, and the sequential scan reads all 1.1M rows to find five.

Size split them too. The GiST variants take 17 to 34 times the space of GIN
and the planner never picked either one, and btree_gin is 20% larger while
being slower in `c`, where 37ms became 98ms.

I wrote up the results and sent them to my mentor. My reading was that we
cannot know in advance which tag a user will search for, so covering the worst
case is the safer choice, and GIN also happens to be the smallest of the five.
I also noted a limit: I could not build the hundreds-of-millions scale he had
mentioned on my machine, so I measured with 1.1M rows and 18 distinct tags.
His answer was that we can keep the existing GIN index if it performs as
expected.

#### A side finding: a rewrite that only helps when the index is missing

The UI backend wraps the runs table in a subquery to get a sorted index path.

```sql
FROM (SELECT * FROM runs_v3 ORDER BY ts_epoch DESC) AS runs_v3
```

Without the GIN index this works as intended — 0.32ms, index scan, early exit,
no sort node. With the GIN index present, which is the case in production, the
planner picks GIN instead and the subquery's own `ORDER BY` turns into a 62MB
external merge sort.

| | Rewrite ON (`e`) | Rewrite OFF (`d`) |
|---|---|---|
| No index | 0.32ms | 0.26ms |
| **GIN present (production)** | **754ms** | **55ms** |

**That is 13.7 times worse.** The planner estimates about 1,000 matching rows
where there are actually 441,840. It cannot estimate JSONB selectivity.

I reported this and asked whether we should remove the rewrite. Since we
decided not to change the index, performance would not change either, so we
left it alone.

---

### 3. Tag autocomplete cache

PR [#490](https://github.com/Netflix/metaflow-service/pull/490)

The cache only refreshed on a five-minute timer. A new tag could take up to
five minutes to appear in the dropdown, and a full table scan ran every five
minutes whether anyone was looking or not.

```python
while True:
    await self.update_cached_tags()      # full scan of 1.1M rows, 2.9s
    await asyncio.sleep(300)
```

The service already has a PostgreSQL `LISTEN`/`NOTIFY` path. Triggers on the
tables emit events and other parts of the backend subscribe to them. The tag
cache was simply not connected to it.

#### Design — a third answer that was neither of mine

I read the PostgreSQL `LISTEN`/`NOTIFY` documentation and some material on
cache design, then checked how each point actually held in this codebase.
Three problems came out of that.

1. The refresh itself is heavy — the query that fills the cache has no
   `WHERE` clause, so no index applies
2. Events can arrive in bursts — if many runs are created at once, 2.9-second
   full scans overlap
3. The database will not hold them back, so I have to — PostgreSQL only folds
   duplicate notifications inside the same transaction, and Metaflow uses a
   separate transaction per run

With that in mind I drafted two designs and took them to my mentor.

```
A. Payload-based
   trigger payload includes tags -> incremental update, no full scan
   INSERT filter + hard TTL fallback
   no debounce needed
   cost: payload 2-3x, rides on every heartbeat UPDATE

B. Signal-based
   emit "runs changed" only -> full scan, debounced
   INSERT filter + trailing + maxWait + pending + hard TTL fallback
   cost: full scan stays, more moving parts
```

His answer was neither of them.

> "instead of a full scan for new tags, you could include the run id as part of
> the 'run changed' event, and then query that single run in order to add its tags."

`run_number` is in `primary_keys`, so it was already in the payload. Using it
avoids both the payload cost of A and the full scan of B. The debounce in B
rested on the assumption that a full scan is expensive, and a single-run lookup
removes that assumption; deletions can be left to the periodic refresh. The
trigger did not need to change either.

#### The cost difference is the whole argument

| Query | Time | Plan |
|---|---|---|
| `SELECT DISTINCT tag FROM (...)` (periodic refresh) | **2,920ms** | Parallel Seq Scan |
| `SELECT tags WHERE flow_id=? AND run_number=?` (event) | **3.3ms** | PK Index Scan |

#### A problem we created — polling and events write to the same cache

Adding the event path meant two places now write to the cache. While the
periodic refresh spends 2.9 seconds scanning, an event can add a tag, and when
the scan finishes it overwrites that tag. This did not happen when there was
only polling.

A lock would have to be held for the whole scan, and every event handler would
wait behind it. Instead the periodic refresh takes a snapshot of the cache
before scanning and merges back whatever appeared in the meantime.

```python
before = set(self.tags)
res, _ = await self.db.run_table_postgres.get_tags()
added_during_scan = set(self.tags) - before
self.tags = sorted(set(res.body) | added_during_scan)
```

#### The limit — tag deletion still takes up to five minutes

I kept the five-minute poll. It covers deletions, missed events, and the case
where `FEATURE_DB_LISTEN_DISABLE=1` turns the listener off. I checked that last
one: no listener starts, and a new tag shows up after about 295 seconds.

Handling deletion as an event would mean checking whether the tag still exists
on any other run, which is exactly the full scan we were avoiding. Measured at
126s / 183s / 286s. The UI does not remove deleted runs from its list in real
time either, and deleting runs is rare in practice, so I left it.
**Adding a tag went from minutes to 96ms, but deleting one did not change, and
that is the limit of this work.**

---

### 4. Cursor field validation

PR [#492](https://github.com/Netflix/metaflow-service/pull/492)

This follows a suggestion my mentor made while reviewing #488. `decode_cursor`
only checked that `ts_epoch` was present, and the remaining fields relied on a
`KeyError` in the handler.

```python
class AsyncRunTablePostgres(AsyncPostgresTable):
    primary_keys = ["flow_id", "run_number"]
    trigger_keys = primary_keys + ["last_heartbeat_ts"]
    cursor_keys  = ["ts_epoch", "run_number"]      # added
```

```python
def decode_cursor(cursor: str, cursor_keys: list) -> dict:
    ...
    if not all(key in decoded for key in cursor_keys):
        raise ValueError("invalid_cursor")
```

The check only looks for the fields it needs. An unknown key in the cursor
does no harm because the handler never reads it, and requiring an exact match
would invalidate every existing cursor the next time the cursor format
changes.

---

### Performance results

`EXPLAIN ANALYZE`, local PostgreSQL 11, `runs_v3` with 1,103,003 rows.

**Cursor pagination**

| Endpoint | Before | After | Rows read |
|---|---|---|---|
| `/flows/{id}/runs` (1.1M rows) | **4,459ms**, about 190MB | **26ms** | 50 |
| `/flows/{id}/runs` (100K rows) | 1,012ms | 4.3ms | 50 |
| `/flows` (about 100K) | 24.6ms | 0.07ms | — |
| `/tasks` (about 100K) | 28.4ms | 0.40ms | — |
| `/flows/{id}/runs/{n}/steps` (5,000) | 3.7ms | 0.07ms | — |
| `/metadata` per run | 56.5ms | 0.83ms | — |
| `/metadata` per task | 53ms | 0.28ms | — |
| `/artifacts` by run | 298ms | 20ms | — |

The index turns `Seq Scan + Sort` into `Index Scan`, and the sort node
disappears.

```
Limit  (cost=0.43..1.30 rows=6) (actual time=0.756..0.779 rows=6 loops=1)
  ->  Index Scan using runs_v3_idx_flow_ts_runnum_desc on runs_v3
        Index Cond: ((flow_id)::text = 'test_1m'
                     AND (ROW(ts_epoch, run_number) < ROW(1780155056177, 101005)))
Execution Time: 3.144 ms
```

**Tag autocomplete cache**

| | Before | After |
|---|---|---|
| New tag appears | **4m 15s** | **96ms** |
| Tag removal reflected | up to 5m (periodic) | up to 5m (unchanged) |

Measured as the gap between the insert time reported by the database's
`clock_timestamp()` and the log timestamp the backend writes when the cache
updates. Three runs: 73ms / 108ms / 107ms. Before was 228s / 257s / 266s /
271s — the spread comes from the poll interval, since where you land in the
five-minute window is arbitrary.

---

## Worth picking up next

This is not part of what I did, but it stood out while reading the code and
taking measurements. I am leaving it here because it is a good place for
someone else to continue.

### The subquery rewrite only helps when the index is missing

The runs query in the UI backend wraps the table in a subquery to get a sorted
index path, and there is a comment in the code:

```python
# JSONB tag filters combined with `ORDER BY` causes performance impact
# due to lack of pg statistics on JSONB fields. To battle this, first execute
# subquery of ordered list from runs_v3 table in and filter by tags on outer query.
# This needs more research in the future to further improve performance.
```

"needs more research" caught my eye, so I measured it. It works as intended
only when the GIN index is absent; with the index present it is 13.7 times
worse (details in the tag filter section above).

Removing the rewrite alone will not fix it. The root cause is that the planner
cannot estimate JSONB selectivity, so filtering and sorting would have to be
redesigned together. Since we decided not to change the index this time,
performance would not change either, and I left it as it was.

**It is measured but untouched.** The "needs more research" in that comment is
still true, and now there is a number attached to it.

---

## What I learned

### Tag cache — you do not know until you read the code

My first instinct was "refresh when an event arrives", and if I had built
that, it would have been slower than the five-minute poll it replaced. One
refresh is a full scan of 1.1M rows, and heartbeats fire the same trigger
about once every ten seconds per run. With only a hundred runs going, that is
ten events per second, each starting a 2.9-second scan on top of the last.

### Tag cache — I inserted ten tags and only nine survived

I knew the periodic refresh and the event handler both write to the same
variable, but I thought the odds were low and left it. In a load test with ten
consecutive inserts, only nine tags came back. The odds were higher than I had
assumed, so I fixed it right away.

### Measurement — benchmarking is hard

It took several attempts before I could trust a number. When I measured the
improvement and got 0ms I was briefly pleased, but that was not an improvement:
I had started the timer after the thing I was timing had already finished.
The poll interval also read as 275 seconds at one point. For a loop running
`sleep(300)` that is impossible, and the cause was not the code — `asyncio` and
the log were using two different clocks, and on that host the two had drifted
apart.

### Benchmark — the index I expected to win was 2.7 times slower

What was already there was a GIN index on `(tags || system_tags)`, and I
thought a `btree_gin` composite index with `flow_id` in front would be better,
since most queries narrow by `flow_id` first. Measuring said the opposite. It
was 20% larger and 2.7 times slower on the flow-scoped ui_backend query, where
37ms became 98ms. The GiST variants took 2m 31s to build 204MB that the planner
never chose. In the end, of all five, the GIN index that was already there was
both the smallest and the only one that worked properly. It reminded me how
much benchmarking matters.

### Indexes — changing an existing one

Adding an index turned out to be less simple than I expected. When I put
`step_name` into the artifact index, queries without a step filter could not
use it, and 20ms went back up to 245ms. Taking one column out covered three
endpoints at once. Adding an index also broke an existing test: a query with no
`ORDER BY` does not guarantee an order, and the new index changed what that
order actually was. An index has to match the query pattern, and changing one
shows up in places you were not looking at.

### git — fetch upstream, please

I work alone for long stretches, so I kept forgetting how much `pull` matters.
Without pulling the latest upstream, I reused one branch across three PRs, and
commits that were already merged kept following along until the diff contained
work that was not mine. Cleaning it up took half a day, mostly because my
`upstream` remote pointed at my own fork rather than the original, so every
comparison I made to check my work was against the wrong thing.

---

## Reproducing the measurements

Environment: PostgreSQL 11, `runs_v3` with 1,103,003 rows (1M of them under
`flow_id='test_1m'`), 18 distinct tags.

The same data can be created with `seed_runs.py` and `seed_data.py` in this
repository. The first creates runs at each scale, the second fills in the rest of
the hierarchy.

**Full scan vs single-run lookup** — the 880x gap the tag cache design rests
on:

```bash
# the periodic refresh query
psql -c "EXPLAIN ANALYZE
SELECT DISTINCT tag FROM (
    SELECT JSONB_ARRAY_ELEMENTS_TEXT(tags||system_tags) AS tag FROM runs_v3
) AS t;"
# → Parallel Seq Scan, about 2,920ms (discard the first run, the cache is not cold)

# the event handler query
psql -c "EXPLAIN ANALYZE
SELECT tags, system_tags FROM runs_v3
WHERE flow_id = 'test_1m' AND run_number = 500000;"
# → Index Scan using runs_v3_pkey, about 3.3ms
```

**Cache propagation delay** — both timestamps come from inside the system:

```bash
psql -t -c "
INSERT INTO runs_v3 (flow_id, run_number, user_name, ts_epoch, tags, system_tags)
VALUES ('test_1m', 9999001, 'eunhye', extract(epoch from now())*1000,
        '[\"probe:a\"]'::jsonb, '[]'::jsonb)
RETURNING to_char(clock_timestamp() AT TIME ZONE 'UTC', 'HH24:MI:SS.MS');"

sleep 2
docker logs ui_backend --timestamps 2>&1 | grep "9999001"
```

Use a new tag and a new `run_number` each time — an existing tag skips the
merge, and the primary key rejects a duplicate.

**Fallback** — start the backend with `FEATURE_DB_LISTEN_DISABLE=1` and no
listener appears in the log, while a new tag still shows up after roughly one
poll interval.

---

## Closing

Before GSoC I worked at a startup, and it was busy enough that I rarely got a
proper code review. After we brought in Claude Code I stopped getting reviews
altogether. This summer Sakari read my code closely, and I learned things I
would not have thought of on my own. It widened my view.

Sakari's reviews turned this project in a different direction more than once. Using
the run id instead of putting tags in the payload for the tag cache was one,
and measuring index size and not only speed was another. Both times his answer
was better than any of the options I had. These felt like things you can only
see with a full understanding of the project and a lot of experience behind
you. I would like to get there soon.

Looking back at the three months, I spent more time measuring than writing
code. The tag index work ended in "do not change this" — if I had gone by
guesswork instead, I would have found out only after it was in production.

I learned a lot this summer. Summer is usually a hard season for me, but this
one was a good one. Thank you for the opportunity.

---

## Links

**Cursor pagination**

https://github.com/Netflix/metaflow-service/pull/482

https://github.com/Netflix/metaflow-service/pull/488

https://github.com/Netflix/metaflow-service/pull/493

**Tag autocomplete cache**

https://github.com/Netflix/metaflow-service/pull/490

**Cursor field validation**

https://github.com/Netflix/metaflow-service/pull/492

**References**

Markus Winand — Pagination Done the PostgreSQL Way

https://wiki.postgresql.org/images/3/35/Pagination_Done_the_PostgreSQL_Way.pdf

PostgreSQL — GIN Indexes

https://www.postgresql.org/docs/current/gin.html

PostgreSQL — LISTEN

https://www.postgresql.org/docs/current/sql-listen.html

AWS — Caching challenges and strategies

https://aws.amazon.com/builders-library/caching-challenges-and-strategies/

AWS — Index types supported in Amazon Aurora PostgreSQL: btree_gin

https://aws.amazon.com/blogs/database/index-types-supported-in-amazon-aurora-postgresql-and-amazon-rds-for-postgresql-using-extensions-sp-gist-btree_gin-and-btree_gist/

Haki Benita — Fastest Way to Load Data Into PostgreSQL Using Python

https://hakibenita.com/fast-load-data-python-postgresql
