STATUS = "partial"


def lookup_old(conn, old_store_id: str) -> dict:
    return {
        "old_store_id": old_store_id,
        "new_store_id": None,
        "run_id": None,
        "migrated_segments": 0,
        "indexed": False,
    }


def list_index(conn) -> dict:
    return {"entries": [], "indexed_n": 0}
