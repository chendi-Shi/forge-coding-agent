"""Initialize metadata, checkpoints and store once before replicas start."""

import asyncio

from aegra_api.core.database import db_manager
from aegra_api.core.migrations import run_migrations_async
from sqlalchemy import text

from agent_backend.coding.models import CodingBase
from agent_backend.coding.verification import AcceptanceSuite  # noqa: F401 - register suite metadata
from agent_backend.concurrency import ACTIVE_RUN_DDL


async def main():
    await run_migrations_async()
    try:
        await db_manager.initialize()
        async with db_manager.get_engine().begin() as connection:
            await connection.execute(text(ACTIVE_RUN_DDL))
            await connection.run_sync(CodingBase.metadata.create_all)
            for ddl in [
                "ALTER TABLE coding_tasks ADD COLUMN IF NOT EXISTS engine VARCHAR(32) NOT NULL DEFAULT 'codex'",
                "ALTER TABLE coding_tasks ADD COLUMN IF NOT EXISTS runtime_state JSONB",
                "ALTER TABLE coding_tasks ADD COLUMN IF NOT EXISTS checkpoint_revision INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE coding_projects ADD COLUMN IF NOT EXISTS acceptance_suite_id VARCHAR(36)",
                "ALTER TABLE coding_tasks ADD COLUMN IF NOT EXISTS acceptance_suite_id VARCHAR(36)",
                "ALTER TABLE coding_tasks ADD COLUMN IF NOT EXISTS acceptance_suite_digest VARCHAR(64)",
                "ALTER TABLE coding_executors ADD COLUMN IF NOT EXISTS engine VARCHAR(32) NOT NULL DEFAULT 'codex'",
                "CREATE INDEX IF NOT EXISTS ix_coding_tasks_engine ON coding_tasks(engine)",
            ]:
                await connection.execute(text(ddl))
    finally:
        await db_manager.close()


if __name__ == "__main__":
    asyncio.run(main())
