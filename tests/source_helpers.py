"""Public synthetic source templates; production SQL remains private."""
from pathlib import Path


def write_mysql_sources(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for table in ("employee", "time_off_request", "time_off_balance", "department",
                  "expense", "labor_allocation", "new_entity"):
        (directory / f"{table}.txt").write_text(
            "SELECT * FROM {{SHARD_CATALOG}}.dbonline." + table
            + " WHERE client_code = SPLIT_PART(CURRENT_USER, '|', 2)"
        )
    return directory
