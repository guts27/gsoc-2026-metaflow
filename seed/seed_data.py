"""
Seed script for benchmarking the Metaflow metadata service.

Generates rows across the full FK chain (flows -> runs -> steps -> tasks ->
metadata / artifacts) so that each paginated endpoint has something to page
through.

Run seed_runs.py first — it creates the large runs_v3 datasets that the
headline numbers were measured against. This script fills in the rest of the
hierarchy under a separate flow.

Companion to the GSoC 2026 final report. See the report's "Reproducing the
measurements" section for how the numbers below were taken.
"""

import os
import psycopg2
import psycopg2.extras
import time
import random
import json

DB_NAME = os.environ.get("POSTGRES_DB", "metaflow")
DB_USER = os.environ.get("POSTGRES_USER", "metaflow_user")
DB_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "")
DB_HOST = os.environ.get("POSTGRES_HOST", "localhost")
DB_PORT = int(os.environ.get("POSTGRES_PORT", 5432))

TAGS = ["tag:success", "tag:failed", "tag:retried", "env:prod", "env:dev"]
SYSTEM_TAGS = [
    "metaflow_version:2.10.0", "metaflow_version:2.11.0",
    "python_version:3.10", "python_version:3.11",
    "runtime:local", "runtime:kubernetes",
]
USERS = ["user1", "user2", "user3"]
STEPS = ["start", "process", "end"]

# Milliseconds. Each row subtracts its index from this, so rows get distinct
# timestamps in descending order — which is what the cursor pages on.
base_ts = int(time.time() * 1000)


def rand_tags():
    return json.dumps(random.sample(TAGS, random.randint(1, 3)))


def rand_system_tags(user):
    return json.dumps([f"user:{user}", random.choice(SYSTEM_TAGS)])


# The goal is pagination testing, so each endpoint needs a lot of rows in the
# collection it actually lists:
#   runs      : many runs under one flow          -> get_all_runs
#   steps     : 3 steps under each run (few)      -> steps don't need pagination
#   tasks     : many tasks under (run 1, "start") -> get_tasks
#   metadata  : many rows under (task 1)          -> get_metadata
#   artifacts : 2 per task (A and B)              -> get_artifacts

FLOW_ID = "test_pagination"   # the deep flow: task / metadata / artifact tests


def generate_flows_many(count):
    """
    For paginating /flows. Real deployments have few flows (hundreds), so this
    doesn't need a large scale — more than one page (50) is enough.
    """
    for i in range(count):
        user = random.choice(USERS)
        yield {
            "flow_id": f"flow-{i+1}",
            "user_name": user,
            "ts_epoch": base_ts - i,   # larger i = further in the past
            "tags": rand_tags(),
            "system_tags": rand_system_tags(user),
        }


def generate_flow_deep():
    user = random.choice(USERS)
    yield {
        "flow_id": FLOW_ID,
        "user_name": user,
        "ts_epoch": base_ts,
        "tags": rand_tags(),
        "system_tags": rand_system_tags(user),
    }


def generate_runs(count):
    for i in range(count):
        user = random.choice(USERS)
        run_number = i + 1
        yield {
            "flow_id": FLOW_ID,
            "run_number": run_number,
            "run_id": f"run-{run_number}",
            "user_name": user,
            "ts_epoch": base_ts - i,
            "tags": rand_tags(),
            "system_tags": rand_system_tags(user),
        }


def generate_steps(run_count):
    # All three STEPS under every run — tasks and artifacts reference a
    # (run, step) pair, so every pair they might point at has to exist.
    for i in range(run_count):
        run_number = i + 1
        for step_name in STEPS:
            user = random.choice(USERS)
            yield {
                "flow_id": FLOW_ID,
                "run_number": run_number,
                "run_id": f"run-{run_number}",
                "step_name": step_name,
                "user_name": user,
                "ts_epoch": base_ts - i,
                "tags": rand_tags(),
                "system_tags": rand_system_tags(user),
            }


def generate_tasks(count):
    run_number = 1
    step_name = "start"
    for i in range(count):
        user = random.choice(USERS)
        yield {
            "flow_id": FLOW_ID,
            "run_number": run_number,
            "run_id": f"run-{run_number}",
            "step_name": step_name,
            "task_id": i + 1,
            # Left NULL about half the time — the API accepts either a task_id
            # or a task_name, so the null path needs coverage too.
            "task_name": None if random.random() < 0.5 else f"task-{i+1}",
            "user_name": user,
            "ts_epoch": base_ts - i,
            "tags": rand_tags(),
            "system_tags": rand_system_tags(user),
        }


def generate_metadatas(count):
    run_number = 1
    step_name = "start"
    task_id = 1
    for i in range(count):
        user = random.choice(USERS)
        yield {
            "flow_id": FLOW_ID,
            "run_number": run_number,
            "run_id": f"run-{run_number}",
            "step_name": step_name,
            "task_id": task_id,
            "task_name": f"task-{task_id}",
            # field_name is part of the primary key, so it has to be unique
            "field_name": f"field-{i+1}",
            "value": f"value-{i+1}",
            "type": "test-metadata",
            "user_name": user,
            "ts_epoch": base_ts - i,
            "tags": rand_tags(),
            "system_tags": rand_system_tags(user),
        }


def generate_artifacts(count):
    # attempt_id stays 0 here, so the "latest attempt per artifact"
    # (DISTINCT ON) filter has nothing to choose between. Testing that needs a
    # separate pass with attempts 0..3.
    run_number = 1
    step_name = "start"
    for i in range(count):
        user = random.choice(USERS)
        task_id = i + 1
        ts = base_ts - i
        common = {
            "flow_id": FLOW_ID,
            "run_number": run_number,
            "run_id": f"run-{run_number}",
            "step_name": step_name,
            "task_id": task_id,
            "task_name": f"task-{task_id}",
            "ds_type": "s3",
            "content_type": "gzip+pickle",
            "user_name": user,
            "attempt_id": 0,
            "ts_epoch": ts,
            "tags": rand_tags(),
            "system_tags": rand_system_tags(user),
        }
        yield {**common, "name": "artifact-A", "location": "/loc-a",
               "sha": "aaaa1111", "type": "metaflow.artifact"}
        yield {**common, "name": "artifact-B", "location": "/loc-b",
               "sha": "bbbb2222", "type": "metaflow.artifact"}


# Generic insert — takes a table name and a column list, works for any table.
# Method follows https://hakibenita.com/fast-load-data-python-postgresql
def insert_batch(connection, rows, table_name, columns, page_size=10_000):
    cols_str = ", ".join(columns)
    values = [[row[c] for c in columns] for row in rows]
    if not values:
        print(f"  {table_name}: no rows")
        return
    with connection.cursor() as cursor:
        psycopg2.extras.execute_values(
            cursor,
            f"INSERT INTO {table_name} ({cols_str}) VALUES %s ON CONFLICT DO NOTHING",
            values,
            page_size=page_size,
        )
    connection.commit()
    print(f"  {table_name}: inserted {len(values)} rows")


FLOW_COLS = ["flow_id", "user_name", "ts_epoch", "tags", "system_tags"]
RUN_COLS = ["flow_id", "run_number", "run_id", "user_name", "ts_epoch",
            "tags", "system_tags"]
STEP_COLS = ["flow_id", "run_number", "run_id", "step_name", "user_name",
             "ts_epoch", "tags", "system_tags"]
TASK_COLS = ["flow_id", "run_number", "run_id", "step_name", "task_id",
             "task_name", "user_name", "ts_epoch", "tags", "system_tags"]
META_COLS = ["flow_id", "run_number", "run_id", "step_name", "task_id",
             "task_name", "field_name", "value", "type", "user_name",
             "ts_epoch", "tags", "system_tags"]
ART_COLS = ["flow_id", "run_number", "run_id", "step_name", "task_id",
            "task_name", "name", "location", "ds_type", "sha", "type",
            "content_type", "user_name", "attempt_id", "ts_epoch",
            "tags", "system_tags"]


def main():
    FLOW_COUNT = 100_000     # standalone flows      -> /flows
    RUN_COUNT = 1_000        # runs under FLOW_ID    -> /flows/{id}/runs
    TASK_COUNT = 100_000     # tasks under (run 1, start) -> /tasks
    META_COUNT = 100_000     # metadata under task 1 -> /metadata
    ART_TASK_COUNT = 500     # 500 tasks x 2 = 1,000 artifacts

    connection = psycopg2.connect(
        dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD,
        host=DB_HOST, port=DB_PORT,
    )
    try:
        print("Seeding (FK order: flow -> run -> step -> task -> metadata/artifact)")

        insert_batch(connection, generate_flows_many(FLOW_COUNT),
                     "flows_v3", FLOW_COLS)

        insert_batch(connection, generate_flow_deep(),
                     "flows_v3", FLOW_COLS)

        insert_batch(connection, generate_runs(RUN_COUNT),
                     "runs_v3", RUN_COLS)

        insert_batch(connection, generate_steps(RUN_COUNT),
                     "steps_v3", STEP_COLS)

        insert_batch(connection, generate_tasks(TASK_COUNT),
                     "tasks_v3", TASK_COLS)

        insert_batch(connection, generate_metadatas(META_COUNT),
                     "metadata_v3", META_COLS)

        insert_batch(connection, generate_artifacts(ART_TASK_COUNT),
                     "artifact_v3", ART_COLS)

        print("Seed complete.")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
