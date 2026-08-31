"""Concurrency regression tests for durable web-viewer operation claims."""

import asyncio
import logging
import socket
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from configparser import ConfigParser
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from modules.db_migrations import MigrationRunner
from modules.scheduler import MessageScheduler


class _FileDBManager:
    def __init__(self, db_path):
        self.db_path = db_path

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


def _config() -> ConfigParser:
    config = ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "scheduled_message_max_stagger_seconds", "0")
    return config


def _make_scheduler(db_manager: _FileDBManager, **bot_values) -> MessageScheduler:
    values = {
        "logger": Mock(),
        "config": _config(),
        "db_manager": db_manager,
    }
    values.update(bot_values)
    return MessageScheduler(SimpleNamespace(**values))


def _operation(db_manager: _FileDBManager, op_id: int) -> sqlite3.Row:
    with db_manager.connection() as conn:
        return conn.execute(
            "SELECT * FROM channel_operations WHERE id = ?",
            (op_id,),
        ).fetchone()


def _insert_operation(
    db_manager: _FileDBManager,
    operation_type: str,
    *,
    status: str = "pending",
    channel_idx: int | None = None,
    owner_host: str | None = None,
    owner_pid: int | None = None,
    owner_boot_id: str | None = None,
) -> int:
    with db_manager.connection() as conn:
        cursor = conn.execute(
            """INSERT INTO channel_operations
               (operation_type, channel_idx, status, claimed_at,
                claim_owner_host, claim_owner_pid, claim_owner_boot_id)
               VALUES (?, ?, ?, CASE WHEN ? = 'processing' THEN CURRENT_TIMESTAMP END,
                       ?, ?, ?)""",
            (
                operation_type,
                channel_idx,
                status,
                status,
                owner_host,
                owner_pid,
                owner_boot_id,
            ),
        )
        conn.commit()
        return cursor.lastrowid


def _initialize_database(tmp_path) -> _FileDBManager:
    manager = _FileDBManager(tmp_path / "operations.db")
    with manager.connection() as conn:
        MigrationRunner(conn, logging.getLogger(__name__)).run()
    return manager


def test_claimed_at_migration_is_present(tmp_path):
    manager = _initialize_database(tmp_path)

    with manager.connection() as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(channel_operations)")
        }

    assert "claimed_at" in columns
    assert {
        "claim_owner_host",
        "claim_owner_pid",
        "claim_owner_boot_id",
    } <= columns


def test_simultaneous_database_claim_has_exactly_one_winner(tmp_path):
    manager = _initialize_database(tmp_path)
    op_id = _insert_operation(manager, "radio_reboot")
    first = _make_scheduler(manager)
    second = _make_scheduler(manager)
    barrier = threading.Barrier(2)

    def claim(scheduler):
        barrier.wait(timeout=1)
        return scheduler._claim_operation(("radio_reboot",))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (first, second)))

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0]["id"] == op_id
    assert _operation(manager, op_id)["status"] == "processing"


def test_two_workers_execute_slow_channel_operation_once(tmp_path):
    manager = _initialize_database(tmp_path)
    op_id = _insert_operation(manager, "remove", channel_idx=7)
    call_count = 0

    async def scenario():
        nonlocal call_count
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_remove(channel_idx):
            nonlocal call_count
            call_count += 1
            assert channel_idx == 7
            started.set()
            await release.wait()
            return True

        first = _make_scheduler(
            manager,
            channel_manager=SimpleNamespace(remove_channel=AsyncMock(side_effect=slow_remove)),
        )
        second = _make_scheduler(
            manager,
            channel_manager=SimpleNamespace(remove_channel=AsyncMock(side_effect=slow_remove)),
        )

        first_run = asyncio.create_task(first._process_channel_operations())
        await asyncio.wait_for(started.wait(), timeout=1)

        claimed = _operation(manager, op_id)
        assert claimed["status"] == "processing"
        assert claimed["claimed_at"] is not None

        # A second tick sees the durable claim and must not invoke the device.
        await asyncio.wait_for(second._process_channel_operations(), timeout=1)
        assert call_count == 1

        release.set()
        await first_run

    asyncio.run(scenario())

    finished = _operation(manager, op_id)
    assert finished["status"] == "completed"
    assert finished["processed_at"] is not None
    assert call_count == 1


def test_channel_group_preserves_order_while_first_operation_is_slow(tmp_path):
    manager = _initialize_database(tmp_path)
    first_id = _insert_operation(manager, "remove", channel_idx=1)
    second_id = _insert_operation(manager, "remove", channel_idx=2)
    calls: list[int] = []

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_remove(channel_idx):
            calls.append(channel_idx)
            if channel_idx == 1:
                started.set()
                await release.wait()
            return True

        worker_one = _make_scheduler(
            manager,
            channel_manager=SimpleNamespace(remove_channel=AsyncMock(side_effect=slow_remove)),
        )
        worker_two = _make_scheduler(
            manager,
            channel_manager=SimpleNamespace(remove_channel=AsyncMock(side_effect=slow_remove)),
        )

        first_run = asyncio.create_task(worker_one._process_channel_operations())
        await asyncio.wait_for(started.wait(), timeout=1)
        await worker_two._process_channel_operations()

        assert _operation(manager, first_id)["status"] == "processing"
        assert _operation(manager, second_id)["status"] == "pending"
        assert calls == [1]

        release.set()
        await first_run
        await worker_two._process_channel_operations()

    asyncio.run(scenario())

    assert calls == [1, 2]
    assert _operation(manager, first_id)["status"] == "completed"
    assert _operation(manager, second_id)["status"] == "completed"


def test_two_workers_execute_slow_radio_operation_once(tmp_path):
    manager = _initialize_database(tmp_path)
    op_id = _insert_operation(manager, "radio_reboot")
    call_count = 0

    async def scenario():
        nonlocal call_count
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_reboot():
            nonlocal call_count
            call_count += 1
            started.set()
            await release.wait()
            return True

        first = _make_scheduler(manager, reboot_radio=AsyncMock(side_effect=slow_reboot))

        first_run = asyncio.create_task(first._process_radio_operations())
        await asyncio.wait_for(started.wait(), timeout=1)

        claimed = _operation(manager, op_id)
        assert claimed["claim_owner_host"] == first._claim_owner_host
        assert claimed["claim_owner_pid"] == first._claim_owner_pid
        assert claimed["claim_owner_boot_id"] == first._claim_owner_boot_id

        # Constructing another scheduler while A holds a slow claim must not
        # treat A's live local PID as a dead process or clear its ownership.
        second = _make_scheduler(manager, reboot_radio=AsyncMock(side_effect=slow_reboot))
        assert second._claim_owner_boot_id != first._claim_owner_boot_id
        await asyncio.wait_for(second._process_radio_operations(), timeout=1)
        assert call_count == 1
        still_claimed = _operation(manager, op_id)
        assert still_claimed["status"] == "processing"
        assert still_claimed["claim_owner_boot_id"] == first._claim_owner_boot_id

        release.set()
        await first_run

    asyncio.run(scenario())

    assert _operation(manager, op_id)["status"] == "completed"
    assert call_count == 1


def test_config_reload_uses_same_claim_and_result_contract(tmp_path):
    manager = _initialize_database(tmp_path)
    op_id = _insert_operation(manager, "config_reload")
    reload_config = Mock(return_value=(True, "configuration reloaded"))
    first = _make_scheduler(manager, reload_config=reload_config)
    second = _make_scheduler(manager, reload_config=reload_config)

    async def scenario():
        await asyncio.gather(
            first._process_config_operations(),
            second._process_config_operations(),
        )

    asyncio.run(scenario())

    row = _operation(manager, op_id)
    assert row["status"] == "completed"
    assert "configuration reloaded" in row["result_data"]
    reload_config.assert_called_once_with()


def test_startup_interrupts_old_claim_without_replay_and_allows_pending_work(tmp_path):
    manager = _initialize_database(tmp_path)
    stale_id = _insert_operation(
        manager,
        "radio_disconnect",
        status="processing",
        owner_host=socket.gethostname(),
        owner_pid=987654321,
        owner_boot_id="dead-process-boot",
    )
    pending_id = _insert_operation(manager, "radio_reboot")
    disconnect_radio = AsyncMock(return_value=True)
    reboot_radio = AsyncMock(return_value=True)
    with patch.object(MessageScheduler, "_is_local_pid_alive", return_value=False):
        scheduler = _make_scheduler(
            manager,
            disconnect_radio=disconnect_radio,
            reboot_radio=reboot_radio,
        )

    interrupted = _operation(manager, stale_id)
    assert interrupted["status"] == "interrupted"
    assert interrupted["processed_at"] is not None
    assert "Automatic retry is disabled" in interrupted["error_message"]
    assert _operation(manager, pending_id)["status"] == "pending"

    asyncio.run(scheduler._process_radio_operations())

    disconnect_radio.assert_not_awaited()
    reboot_radio.assert_awaited_once_with()
    assert _operation(manager, stale_id)["status"] == "interrupted"
    assert _operation(manager, pending_id)["status"] == "completed"


def test_startup_recovery_is_idempotent(tmp_path):
    manager = _initialize_database(tmp_path)
    stale_id = _insert_operation(manager, "remove", status="processing", channel_idx=3)

    first = _make_scheduler(manager)
    after_first = dict(_operation(manager, stale_id))
    second = _make_scheduler(manager)
    after_second = dict(_operation(manager, stale_id))

    assert after_first["status"] == "interrupted"
    assert after_second == after_first
    assert first._recover_interrupted_operations() == 0
    assert second._recover_interrupted_operations() == 0


def test_processing_claim_created_after_startup_is_not_time_recovered(tmp_path):
    manager = _initialize_database(tmp_path)
    reboot_radio = AsyncMock(return_value=True)
    scheduler = _make_scheduler(manager, reboot_radio=reboot_radio)
    active_id = _insert_operation(manager, "radio_reboot", status="processing")
    pending_id = _insert_operation(manager, "radio_reboot")

    asyncio.run(scheduler._process_radio_operations())

    reboot_radio.assert_not_awaited()
    assert _operation(manager, active_id)["status"] == "processing"
    assert _operation(manager, pending_id)["status"] == "pending"


def test_other_host_processing_owner_stays_blocked_conservatively(tmp_path):
    manager = _initialize_database(tmp_path)
    active_id = _insert_operation(
        manager,
        "radio_reboot",
        status="processing",
        owner_host="another-host.example",
        owner_pid=1234,
        owner_boot_id="remote-boot",
    )
    pending_id = _insert_operation(manager, "radio_reboot")
    reboot_radio = AsyncMock(return_value=True)

    scheduler = _make_scheduler(manager, reboot_radio=reboot_radio)
    asyncio.run(scheduler._process_radio_operations())

    reboot_radio.assert_not_awaited()
    assert _operation(manager, active_id)["status"] == "processing"
    assert _operation(manager, pending_id)["status"] == "pending"
