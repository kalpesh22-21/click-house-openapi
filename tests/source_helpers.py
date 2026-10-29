"""Public synthetic source templates; production SQL remains private."""
from pathlib import Path


def write_mysql_sources(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for table in ("employee", "time_off_request", "time_off_balance", "department",
                  "expense", "labor_allocation", "new_entity"):
        filename = {"time_off_request": "accrual_events", "time_off_balance": "accrual_balances"}.get(table, table)
        (directory / f"{filename}.txt").write_text(
            "SELECT * FROM {{SHARD_CATALOG}}.dbonline." + table
            + " WHERE client_code = SPLIT_PART(CURRENT_USER, '|', 2)"
        )
    return directory
