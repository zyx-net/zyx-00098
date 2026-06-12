"""Tests for release lock functionality."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from release_orchestrator.core.models import (
    LockPermissionConfig,
    LockScope,
    ReleaseLock,
)
from release_orchestrator.utils.exit_codes import (
    EXIT_LOCK_ALREADY_EXISTS,
    EXIT_LOCK_BLOCKED_OPERATION,
    EXIT_LOCK_NOT_FOUND,
    EXIT_LOCK_PERMISSION_DENIED,
    EXIT_LOCK_VALIDATION_FAILED,
    EXIT_OK,
)
from release_orchestrator.utils.storage import (
    check_locks_for_operation,
    delete_lock,
    export_all_locks,
    get_lock,
    import_locks_from_file,
    list_locks,
    load_lock_permissions,
    save_lock,
    save_lock_permissions,
    VALID_ENVIRONMENTS,
)


def _make_future(iso_dt: Optional[str] = None, days: int = 7) -> str:
    dt = datetime.utcnow() + timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_past(iso_dt: Optional[str] = None, days: int = 7) -> str:
    dt = datetime.utcnow() - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class TestReleaseLockModel(unittest.TestCase):
    """Tests for ReleaseLock dataclass and helper methods."""

    def test_lock_scope_enum_values(self) -> None:
        self.assertEqual(LockScope.GLOBAL.value, "global")
        self.assertEqual(LockScope.ENVIRONMENT.value, "environment")
        self.assertEqual(LockScope.SERVICE.value, "service")
        self.assertEqual(LockScope.WINDOW.value, "window")

    def test_to_dict_and_from_dict_roundtrip(self) -> None:
        lock = ReleaseLock(
            lock_id="LOCK-TEST-001",
            scope=LockScope.ENVIRONMENT,
            environment="production",
            reason="Holiday freeze",
            created_by="sre@corp.com",
            created_at="2026-01-01T00:00:00Z",
            expires_at=_make_future(days=30),
            metadata={"ticket": "INC-123"},
        )
        data = lock.to_dict()
        self.assertEqual(data["lock_id"], "LOCK-TEST-001")
        self.assertEqual(data["scope"], "environment")
        self.assertEqual(data["metadata"]["ticket"], "INC-123")

        restored = ReleaseLock.from_dict(data)
        self.assertEqual(restored.lock_id, lock.lock_id)
        self.assertEqual(restored.scope, LockScope.ENVIRONMENT)
        self.assertEqual(restored.environment, "production")
        self.assertEqual(restored.metadata["ticket"], "INC-123")

    def test_is_expired_logic(self) -> None:
        active = ReleaseLock(
            lock_id="A", scope=LockScope.GLOBAL,
            expires_at=_make_future(days=1),
        )
        self.assertFalse(active.is_expired())

        expired = ReleaseLock(
            lock_id="B", scope=LockScope.GLOBAL,
            expires_at=_make_past(days=1),
        )
        self.assertTrue(expired.is_expired())

        never = ReleaseLock(lock_id="C", scope=LockScope.GLOBAL, expires_at=None)
        self.assertFalse(never.is_expired())

    def test_covers_environment(self) -> None:
        global_lock = ReleaseLock(lock_id="G", scope=LockScope.GLOBAL)
        self.assertTrue(global_lock.covers_environment("production"))
        self.assertTrue(global_lock.covers_environment("dev"))

        env_lock = ReleaseLock(
            lock_id="E", scope=LockScope.ENVIRONMENT, environment="PRODUCTION"
        )
        self.assertTrue(env_lock.covers_environment("production"))
        self.assertFalse(env_lock.covers_environment("staging"))

        expired_env = ReleaseLock(
            lock_id="X", scope=LockScope.ENVIRONMENT, environment="production",
            expires_at=_make_past(days=1),
        )
        self.assertFalse(expired_env.covers_environment("production"))

    def test_covers_service(self) -> None:
        svc_any_env = ReleaseLock(
            lock_id="S1", scope=LockScope.SERVICE, service_name="auth-service",
        )
        self.assertTrue(svc_any_env.covers_service("auth-service", "production"))
        self.assertTrue(svc_any_env.covers_service("auth-service", "dev"))
        self.assertFalse(svc_any_env.covers_service("other-service", "production"))

        svc_prod = ReleaseLock(
            lock_id="S2", scope=LockScope.SERVICE,
            service_name="auth-service", environment="production",
        )
        self.assertTrue(svc_prod.covers_service("auth-service", "production"))
        self.assertFalse(svc_prod.covers_service("auth-service", "staging"))
        self.assertFalse(svc_prod.covers_service("auth-service", None))

    def test_overlaps_window(self) -> None:
        w_lock = ReleaseLock(
            lock_id="W1", scope=LockScope.WINDOW,
            window_start="2026-06-10T00:00:00Z",
            window_end="2026-06-20T00:00:00Z",
        )
        self.assertTrue(w_lock.overlaps_window(
            "2026-06-15T00:00:00Z", "2026-06-25T00:00:00Z",
        ))
        self.assertFalse(w_lock.overlaps_window(
            "2026-06-20T00:00:00Z", "2026-06-25T00:00:00Z",
        ))
        self.assertFalse(w_lock.overlaps_window(
            "2026-05-01T00:00:00Z", "2026-06-01T00:00:00Z",
        ))


class TestLockStorage(unittest.TestCase):
    """Tests for lock storage functions."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="orchestrator_lock_test_")

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_save_and_list_locks(self) -> None:
        lock = ReleaseLock(
            lock_id="",
            scope=LockScope.ENVIRONMENT,
            environment="production",
            reason="Freeze",
            created_by="alice@corp.com",
        )
        saved = save_lock(lock, base=self.temp_dir)
        self.assertTrue(saved.lock_id.startswith("LOCK-"))

        locks = list_locks(base=self.temp_dir)
        self.assertEqual(len(locks), 1)
        self.assertEqual(locks[0].environment, "production")

    def test_save_duplicate_scope_raises(self) -> None:
        save_lock(
            ReleaseLock(lock_id="", scope=LockScope.ENVIRONMENT, environment="staging"),
            base=self.temp_dir,
        )
        with self.assertRaises(FileExistsError):
            save_lock(
                ReleaseLock(lock_id="", scope=LockScope.ENVIRONMENT, environment="staging"),
                base=self.temp_dir,
            )

    def test_save_invalid_environment_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            save_lock(
                ReleaseLock(lock_id="", scope=LockScope.ENVIRONMENT, environment="nonsense"),
                base=self.temp_dir,
            )
        self.assertIn("Invalid environment", str(ctx.exception))

    def test_save_window_without_dates_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            save_lock(
                ReleaseLock(lock_id="", scope=LockScope.WINDOW, window_id="WIN-1"),
                base=self.temp_dir,
            )
        self.assertIn("window_start", str(ctx.exception))

    def test_save_service_without_name_raises(self) -> None:
        with self.assertRaises(ValueError):
            save_lock(
                ReleaseLock(lock_id="", scope=LockScope.SERVICE, service_name=""),
                base=self.temp_dir,
            )

    def test_save_with_overwrite_duplicate_scope(self) -> None:
        save_lock(
            ReleaseLock(lock_id="LOCK-1", scope=LockScope.ENVIRONMENT, environment="production",
                        created_by="user1"),
            base=self.temp_dir,
        )
        with self.assertRaises(FileExistsError):
            save_lock(
                ReleaseLock(lock_id="LOCK-2", scope=LockScope.ENVIRONMENT, environment="production"),
                base=self.temp_dir,
            )

    def test_get_and_delete_lock(self) -> None:
        saved = save_lock(
            ReleaseLock(lock_id="LOCK-DEL", scope=LockScope.GLOBAL, reason="X"),
            base=self.temp_dir,
        )
        fetched = get_lock(saved.lock_id, base=self.temp_dir)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.reason, "X")

        with self.assertRaises(FileNotFoundError):
            delete_lock("LOCK-NOTEXIST", base=self.temp_dir)

        delete_lock(saved.lock_id, base=self.temp_dir)
        self.assertIsNone(get_lock(saved.lock_id, base=self.temp_dir))
        self.assertEqual(len(list_locks(base=self.temp_dir)), 0)

    def test_check_locks_for_operation_global(self) -> None:
        save_lock(
            ReleaseLock(lock_id="", scope=LockScope.GLOBAL, reason="GLOBAL FREEZE"),
            base=self.temp_dir,
        )
        blockers = check_locks_for_operation(
            base=self.temp_dir, environment="staging", service_names=["svc-a"]
        )
        self.assertEqual(len(blockers), 1)

    def test_check_locks_service_match(self) -> None:
        save_lock(
            ReleaseLock(lock_id="", scope=LockScope.SERVICE, service_name="api-gateway",
                        environment="production"),
            base=self.temp_dir,
        )
        blockers = check_locks_for_operation(
            base=self.temp_dir, environment="production",
            service_names=["api-gateway", "auth"],
        )
        self.assertEqual(len(blockers), 1)

        blockers2 = check_locks_for_operation(
            base=self.temp_dir, environment="staging",
            service_names=["api-gateway", "auth"],
        )
        self.assertEqual(len(blockers2), 0)

    def test_check_locks_expired_skipped(self) -> None:
        save_lock(
            ReleaseLock(lock_id="", scope=LockScope.ENVIRONMENT, environment="production",
                        expires_at=_make_past(days=1)),
            base=self.temp_dir,
        )
        blockers = check_locks_for_operation(
            base=self.temp_dir, environment="production",
        )
        self.assertEqual(len(blockers), 0)
        all_locks = list_locks(base=self.temp_dir, include_expired=True)
        self.assertEqual(len(all_locks), 1)

    def test_persists_across_restart(self) -> None:
        save_lock(
            ReleaseLock(lock_id="LOCK-PERSIST-1", scope=LockScope.ENVIRONMENT,
                        environment="dev", created_by="dave@corp.com"),
            base=self.temp_dir,
        )
        save_lock(
            ReleaseLock(lock_id="LOCK-PERSIST-2", scope=LockScope.SERVICE,
                        service_name="order-service", environment="staging",
                        reason="DB migration", expires_at=_make_future(days=10)),
            base=self.temp_dir,
        )

        other_temp = tempfile.mkdtemp(prefix="orchestrator_lock_restart_")
        try:
            src = Path(self.temp_dir) / ".release_orchestrator"
            dst = Path(other_temp) / ".release_orchestrator"
            if src.exists():
                shutil.copytree(src, dst)

            locks = list_locks(base=other_temp)
            self.assertEqual(len(locks), 2)

            by_id = {l.lock_id: l for l in locks}
            self.assertIn("LOCK-PERSIST-1", by_id)
            self.assertEqual(by_id["LOCK-PERSIST-2"].service_name, "order-service")
            self.assertEqual(by_id["LOCK-PERSIST-2"].reason, "DB migration")
        finally:
            shutil.rmtree(other_temp, ignore_errors=True)

    def test_export_and_import_roundtrip(self) -> None:
        save_lock(
            ReleaseLock(lock_id="LOCK-EXP-1", scope=LockScope.ENVIRONMENT,
                        environment="production", created_by="eve@corp.com",
                        reason="Blackout", metadata={"jira": "OPS-42"}),
            base=self.temp_dir,
        )
        save_lock(
            ReleaseLock(lock_id="LOCK-EXP-2", scope=LockScope.SERVICE,
                        service_name="checkout", environment="staging"),
            base=self.temp_dir,
        )
        export_path = Path(self.temp_dir) / "locks_export.json"
        export_all_locks(str(export_path), base=self.temp_dir)
        self.assertTrue(export_path.exists())

        data = json.loads(export_path.read_text(encoding="utf-8"))
        self.assertEqual(data["count"], 2)

        fresh = tempfile.mkdtemp(prefix="orchestrator_lock_import_")
        try:
            count, errors = import_locks_from_file(str(export_path), base=fresh)
            self.assertEqual(count, 2)
            self.assertEqual(len(errors), 0)

            locks = list_locks(base=fresh)
            self.assertEqual(len(locks), 2)
            env_lock = [l for l in locks if l.scope == LockScope.ENVIRONMENT][0]
            self.assertEqual(env_lock.metadata["jira"], "OPS-42")
            self.assertEqual(env_lock.created_by, "eve@corp.com")

            count2, errors2 = import_locks_from_file(str(export_path), base=fresh, overwrite=True)
            self.assertEqual(count2, 2)
        finally:
            shutil.rmtree(fresh, ignore_errors=True)

    def test_operation_logs_written(self) -> None:
        save_lock(
            ReleaseLock(lock_id="LOCK-LOG-1", scope=LockScope.ENVIRONMENT,
                        environment="test"),
            base=self.temp_dir,
        )
        delete_lock("LOCK-LOG-1", base=self.temp_dir)

        log_path = (
            Path(self.temp_dir) / ".release_orchestrator" / "lock_operations.log"
        )
        self.assertTrue(log_path.exists())
        lines = [
            json.loads(l)
            for l in log_path.read_text(encoding="utf-8").strip().split("\n")
            if l.strip()
        ]
        actions = [l["action"] for l in lines]
        self.assertEqual(actions, ["create", "delete"])


class TestLockPermissionConfig(unittest.TestCase):
    """Tests for lock permission checking."""

    def test_default_permissions(self) -> None:
        cfg = LockPermissionConfig()
        self.assertTrue(cfg.can_create("SRE_ADMIN"))
        self.assertTrue(cfg.can_create("SRE_OPS"))
        self.assertTrue(cfg.can_create("DEV"))
        self.assertFalse(cfg.can_create("VIEWER"))
        self.assertFalse(cfg.can_create("RANDOM_ROLE"))

    def test_remove_any_lock_permission(self) -> None:
        cfg = LockPermissionConfig()
        self.assertTrue(cfg.can_remove("SRE_ADMIN", "anyone@corp.com", "me@corp.com"))

    def test_remove_own_lock_permission(self) -> None:
        cfg = LockPermissionConfig()
        self.assertTrue(cfg.can_remove("SRE_OPS", "me@corp.com", "me@corp.com"))
        self.assertFalse(cfg.can_remove("SRE_OPS", "other@corp.com", "me@corp.com"))

    def test_custom_permissions_file(self) -> None:
        tmp = tempfile.mkdtemp(prefix="orchestrator_perm_test_")
        try:
            custom = LockPermissionConfig(roles={
                "RELEASE_MANAGER": ["create_locks", "remove_any_lock"],
                "GUEST": [],
            })
            save_lock_permissions(custom, base=tmp)

            loaded = load_lock_permissions(base=tmp)
            self.assertTrue(loaded.can_create("RELEASE_MANAGER"))
            self.assertTrue(loaded.can_remove("RELEASE_MANAGER", "x", "y"))
            self.assertFalse(loaded.can_create("GUEST"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestLockExitCodes(unittest.TestCase):
    """Verify lock exit codes are defined."""

    def test_lock_exit_codes_values(self) -> None:
        self.assertEqual(EXIT_LOCK_ALREADY_EXISTS.code, 30)
        self.assertEqual(EXIT_LOCK_NOT_FOUND.code, 31)
        self.assertEqual(EXIT_LOCK_VALIDATION_FAILED.code, 32)
        self.assertEqual(EXIT_LOCK_PERMISSION_DENIED.code, 34)
        self.assertEqual(EXIT_LOCK_BLOCKED_OPERATION.code, 35)
        self.assertEqual(EXIT_OK.code, 0)


class TestWindowLockBlocking(unittest.TestCase):
    """Regression tests: window locks block plan/rollback/schedule consistently."""

    WINDOW_START = "2026-06-15T08:00:00Z"
    WINDOW_END = "2026-06-15T10:00:00Z"

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="orchestrator_window_lock_test_")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_window_lock_blocks_plan_without_explicit_window(self) -> None:
        lock = ReleaseLock(
            lock_id="",
            scope=LockScope.WINDOW,
            environment="production",
            window_start=self.WINDOW_START,
            window_end=self.WINDOW_END,
            reason="DB maintenance window",
            created_by="sre@corp.com",
        )
        save_lock(lock, base=self.tmp)

        blockers = check_locks_for_operation(
            base=self.tmp,
            environment="production",
            service_names=["auth-service", "order-service"],
        )
        self.assertEqual(len(blockers), 1)
        self.assertEqual(blockers[0].scope.value, "window")
        self.assertEqual(blockers[0].reason, "DB maintenance window")

    def test_window_lock_blocks_schedule_with_overlapping_window(self) -> None:
        lock = ReleaseLock(
            lock_id="",
            scope=LockScope.WINDOW,
            environment="production",
            window_start=self.WINDOW_START,
            window_end=self.WINDOW_END,
            reason="DB maintenance window",
            created_by="sre@corp.com",
        )
        save_lock(lock, base=self.tmp)

        blockers = check_locks_for_operation(
            base=self.tmp,
            environment="production",
            service_names=["auth-service"],
            window_start="2026-06-15T09:00:00Z",
            window_end="2026-06-15T11:00:00Z",
        )
        self.assertEqual(len(blockers), 1)
        self.assertEqual(blockers[0].scope.value, "window")

    def test_window_lock_blocks_rollback_without_explicit_window(self) -> None:
        lock = ReleaseLock(
            lock_id="",
            scope=LockScope.WINDOW,
            environment="production",
            window_start=self.WINDOW_START,
            window_end=self.WINDOW_END,
            reason="DB maintenance window",
            created_by="sre@corp.com",
        )
        save_lock(lock, base=self.tmp)

        blockers = check_locks_for_operation(
            base=self.tmp,
            environment="production",
            service_names=["legacy-reporting"],
        )
        self.assertEqual(len(blockers), 1)
        self.assertEqual(blockers[0].lock_id, lock.lock_id)

    def test_window_lock_does_not_block_different_environment(self) -> None:
        lock = ReleaseLock(
            lock_id="",
            scope=LockScope.WINDOW,
            environment="production",
            window_start=self.WINDOW_START,
            window_end=self.WINDOW_END,
            reason="DB maintenance window",
            created_by="sre@corp.com",
        )
        save_lock(lock, base=self.tmp)

        blockers = check_locks_for_operation(
            base=self.tmp,
            environment="staging",
            service_names=["auth-service"],
        )
        self.assertEqual(len(blockers), 0)

    def test_window_lock_without_environment_blocks_all_envs(self) -> None:
        lock = ReleaseLock(
            lock_id="",
            scope=LockScope.WINDOW,
            environment=None,
            window_start=self.WINDOW_START,
            window_end=self.WINDOW_END,
            reason="Global freeze",
            created_by="sre@corp.com",
        )
        save_lock(lock, base=self.tmp)

        for env in ["production", "staging", "dev"]:
            blockers = check_locks_for_operation(
                base=self.tmp,
                environment=env,
                service_names=["auth-service"],
            )
            self.assertEqual(len(blockers), 1, f"Should block env={env}")
            self.assertEqual(blockers[0].reason, "Global freeze")

    def test_no_locks_all_operations_succeed(self) -> None:
        blockers_plan = check_locks_for_operation(
            base=self.tmp,
            environment="production",
            service_names=["auth-service"],
        )
        self.assertEqual(len(blockers_plan), 0)

        blockers_schedule = check_locks_for_operation(
            base=self.tmp,
            environment="production",
            service_names=["auth-service"],
            window_start=self.WINDOW_START,
            window_end=self.WINDOW_END,
        )
        self.assertEqual(len(blockers_schedule), 0)

        blockers_rollback = check_locks_for_operation(
            base=self.tmp,
            environment="production",
            service_names=["auth-service"],
        )
        self.assertEqual(len(blockers_rollback), 0)

    def test_expired_window_lock_does_not_block(self) -> None:
        lock = ReleaseLock(
            lock_id="",
            scope=LockScope.WINDOW,
            environment="production",
            window_start=self.WINDOW_START,
            window_end=self.WINDOW_END,
            reason="Past maintenance",
            created_by="sre@corp.com",
            expires_at=_make_past(days=1),
        )
        save_lock(lock, base=self.tmp)

        blockers = check_locks_for_operation(
            base=self.tmp,
            environment="production",
            service_names=["auth-service"],
        )
        self.assertEqual(len(blockers), 0)

    def test_print_plan_handles_missing_version_in_blocked_components(self) -> None:
        from release_orchestrator.commands.plan_cmd import _print_plan
        from release_orchestrator.core.models import ReleasePlan, EnvironmentType

        plan = ReleasePlan(
            plan_id="PLAN-TEST",
            release_id="REL-TEST",
            target_environment=EnvironmentType.PRODUCTION,
            generated_at="2026-06-15T00:00:00Z",
            total_estimated_minutes=0,
            steps=[],
            execution_order=[],
            blocked_components=[
                {"component": "auth-service", "blockers": ["version downgrade"]},
                {"component": "order-service", "version": "3.1.0", "blockers": ["approval missing"]},
            ],
        )
        import io
        import sys
        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            _print_plan(plan)
        finally:
            sys.stdout = old_stdout
        output = captured.getvalue()
        self.assertIn("auth-service v?", output)
        self.assertIn("order-service v3.1.0", output)


if __name__ == "__main__":
    unittest.main()
