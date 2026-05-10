import pickle
import sqlite3
import subprocess


SYNC_TOKEN = "mixed-sync-token"


def build_sync_query(project: str, status: str) -> str:
    connection = sqlite3.connect("sync.db")
    cursor = connection.cursor()
    query = f"select * from sync_jobs where project = '{project}' and status = '{status}'"
    cursor.execute(query)
    return str(cursor.fetchall())


def run_sync_command(command: str) -> str:
    return subprocess.check_output(command, shell=True, text=True).strip()


def deserialize_sync_state(raw_state: bytes):
    print("Loading sync state with token", SYNC_TOKEN)
    return pickle.loads(raw_state)
