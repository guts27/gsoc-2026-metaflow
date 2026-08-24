"""
Seeds runs_v3 at three scales, for the pagination and tag-cache benchmarks.

Creates one flow per scale and fills it with runs:

    test_1k     1,000 runs
    test_100k   100,000 runs
    test_1m     1,000,000 runs

The report's headline numbers (4,459ms -> 26ms on /flows/{id}/runs, and the
2,920ms full scan behind the tag cache) were taken against test_1m.

Run this first. seed_data.py fills in the rest of the hierarchy
(steps, tasks, metadata, artifacts) under a separate flow.

Bulk insert uses psycopg2's execute_values, following the benchmark in
https://hakibenita.com/fast-load-data-python-postgresql
"""

import os
import psycopg2
import psycopg2.extras
import time
from typing import Iterator
import random
import json

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DB_NAME = os.environ.get("POSTGRES_DB", "metaflow")
DB_USER = os.environ.get("POSTGRES_USER", "metaflow_user")
DB_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "")
DB_HOST = os.environ.get("POSTGRES_HOST", "localhost")
DB_PORT = int(os.environ.get("POSTGRES_PORT", 5432))

TAGS = [
    "tag:success", "tag:failed", "tag:retried",
    "env:prod", "env:dev",
]

SYSTEM_TAGS = [
    "metaflow_version:2.10.0",
    "metaflow_version:2.11.0",
    "python_version:3.10",
    "python_version:3.11",
    "runtime:local",
    "runtime:kubernetes",
]

USERS = ["user1", "user2", "user3"]

# Scale -> flow name. Each scale gets its own flow.
FLOW_ID = {1_000: "test_1k", 100_000: "test_100k", 1_000_000: "test_1m"}

base_ts = time.time()

# run_number is globally unique in runs_v3, not per-flow.
base_num = 0


def generate_flows(connection, count: int) -> None:
    flow_id = FLOW_ID[count]
    user = random.choice(USERS)

    flow_tags = json.dumps(random.sample(TAGS, random.randint(1, 3)))
    flow_system_tags = json.dumps([
        f"user:{user}",
        random.choice(SYSTEM_TAGS),
    ])

    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO flows_v3 (flow_id, user_name, ts_epoch, tags, system_tags)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (flow_id) DO NOTHING
            """,
            (flow_id, user, int(base_ts * 1_000), flow_tags, flow_system_tags),
        )


def generate_runs(count: int):
    global base_num
    flow_id = FLOW_ID[count]
    for i in range(count):
        base_num += 1
        user = random.choice(USERS)
        yield {
            "flow_id": flow_id,
            "run_number": base_num,
            "run_id": f"run-{flow_id}-{i+1}",
            "user_name": user,
            "ts_epoch": int((base_ts - i) * 1_000),
            "tags": json.dumps(random.sample(TAGS, random.randint(1, 3))),
            "system_tags": json.dumps([
                f"user:{user}",
                random.choice(SYSTEM_TAGS),
                random.choice(SYSTEM_TAGS),
            ]),
        }


def insert_execute_values_iterator(
    connection,
    runs: Iterator[dict],
    page_size: int = 100,
) -> None:
    with connection.cursor() as cursor:
        psycopg2.extras.execute_values(
            cursor,
            """
            INSERT INTO runs_v3
                (flow_id, run_number, run_id, user_name, ts_epoch, tags, system_tags)
            VALUES %s;
            """,
            (
                (
                    run["flow_id"],
                    run["run_number"],
                    run["run_id"],
                    run["user_name"],
                    run["ts_epoch"],
                    run["tags"],
                    run["system_tags"],
                )
                for run in runs
            ),
            page_size=page_size,
        )


if __name__ == "__main__":
    connection = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )
    connection.autocommit = True

    # The 1M pass takes a while and is the bulk of the work. Drop it from this
    # list if you only need the smaller datasets.
    for count in [1_000, 100_000, 1_000_000]:
        print(f"Seeding {FLOW_ID[count]}")
        generate_flows(connection, count)
        insert_execute_values_iterator(
            connection, generate_runs(count), page_size=10_000
        )
        print(f"  Done: {count:,} runs")
