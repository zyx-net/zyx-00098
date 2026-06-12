"""Unit tests for release scheduling schemes functionality."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from release_orchestrator.core.models import (
    EnvironmentType,
    FreezePeriod,
    ReleaseScheme,
    ReleaseWindow,
    Severity,
    Wave,
    now_iso,
)
from release_orchestrator.core.scheme_validator import (
    SchemeValidationError,
    SchemeValidator,
    validate_scheme,
)
from release_orchestrator.utils.exit_codes import (
    EXIT_SCHEME_ALREADY_EXISTS,
    EXIT_SCHEME_IO_ERROR,
    EXIT_SCHEME_NOT_FOUND,
    EXIT_SCHEME_VALIDATION_FAILED,
)
from release_orchestrator.utils.storage import (
    clone_scheme,
    delete_scheme,
    export_scheme_to_file,
    import_scheme_from_file,
    list_schemes,
    load_scheme,
    save_scheme,
    scheme_exists,
)


def _make_test_window(
    window_id: str,
    name: str,
    start: str = "2026-06-15T09:00:00Z",
    end: str = "2026-06-15T17:00:00Z",
    capacity: int = 3,
    locked: bool = False,
) -> ReleaseWindow:
    return ReleaseWindow(
        window_id=window_id,
        name=name,
        start_time=start,
        end_time=end,
        capacity_max=capacity,
        allowed_environments=[EnvironmentType.PRODUCTION, EnvironmentType.STAGING],
        locked=locked,
        freeze_periods=[
            FreezePeriod(
                name="Morning Freeze",
                start="2026-06-15T07:00:00Z",
                end="2026-06-15T09:00:00Z",
            ),
        ],
    )


def _make_test_wave(wave_id: str, name: str, order: int) -> Wave:
    return Wave(
        wave_id=wave_id,
        name=name,
        order=order,
        description=f"Wave {name}",
    )


def _make_test_scheme(
    name: str = "test-scheme",
    with_windows: bool = True,
    with_waves: bool = True,
    with_manifest: bool = True,
    created_by: str = "test-user@corp.com",
) -> ReleaseScheme:
    windows = []
    waves = []
    manifest = None

    if with_windows:
        windows = [
            _make_test_window("WIN-1", "Window 1", "2026-06-15T09:00:00Z", "2026-06-15T17:00:00Z"),
            _make_test_window("WIN-2", "Window 2", "2026-06-16T09:00:00Z", "2026-06-16T17:00:00Z"),
            _make_test_window("WIN-3", "Window 3 Dev", "2026-06-01T00:00:00Z", "2026-06-30T23:59:59Z"),
        ]
        windows[2].allowed_environments = [EnvironmentType.DEV, EnvironmentType.TEST]
        windows[2].capacity_max = 0

    if with_waves:
        waves = [
            _make_test_wave("WAVE-1", "Canary", 1),
            _make_test_wave("WAVE-2", "Partial", 2),
            _make_test_wave("WAVE-3", "Full", 3),
        ]

    if with_manifest:
        manifest = {
            "manifest_version": "1.0",
            "release_id": "RL-TEST-001",
            "title": "Test Release",
            "target_environment": "production",
            "components": [
                {
                    "name": "common-lib",
                    "version": "2.1.0",
                    "environment": "production",
                },
                {
                    "name": "auth-service",
                    "version": "1.5.0",
                    "environment": "production",
                    "dependencies": ["common-lib"],
                },
            ],
        }

    return ReleaseScheme(
        scheme_name=name,
        created_at=now_iso(),
        created_by=created_by,
        description="Test scheme for unit tests",
        manifest=manifest,
        manifest_path="examples/clean_manifest.json",
        release_windows=windows,
        waves=waves,
        tags=["test", "unit"],
        metadata={"source": "pytest"},
    )


class TestReleaseSchemeModel(unittest.TestCase):
    """Tests for the ReleaseScheme data model."""

    def test_scheme_serialization_roundtrip(self) -> None:
        scheme = _make_test_scheme("roundtrip-test")
        data = scheme.to_dict()
        restored = ReleaseScheme.from_dict(data)
        self.assertEqual(restored.scheme_name, "roundtrip-test")
        self.assertEqual(len(restored.release_windows), 3)
        self.assertEqual(len(restored.waves), 3)
        self.assertEqual(restored.manifest["release_id"], "RL-TEST-001")

    def test_scheme_json_roundtrip(self) -> None:
        scheme = _make_test_scheme("json-test")
        json_str = scheme.to_json()
        restored = ReleaseScheme.from_json(json_str)
        self.assertEqual(restored.scheme_name, "json-test")
        self.assertEqual(restored.created_by, "test-user@corp.com")
        self.assertEqual(restored.description, "Test scheme for unit tests")

    def test_scheme_minimal(self) -> None:
        scheme = ReleaseScheme(
            scheme_name="minimal",
            created_at=now_iso(),
            created_by="anon",
            release_windows=[_make_test_window("WIN-1", "Minimal Window")],
        )
        data = scheme.to_dict()
        restored = ReleaseScheme.from_dict(data)
        self.assertEqual(restored.scheme_name, "minimal")
        self.assertEqual(len(restored.release_windows), 1)
        self.assertIsNone(restored.manifest)
        self.assertEqual(len(restored.waves), 0)

    def test_release_window_from_dict_missing_fields(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            ReleaseWindow.from_dict({"window_id": "WIN-1"})
        self.assertIn("missing required field(s)", str(ctx.exception))
        self.assertIn("name", str(ctx.exception))
        self.assertIn("start_time", str(ctx.exception))
        self.assertIn("end_time", str(ctx.exception))

    def test_release_window_from_dict_partial_missing(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            ReleaseWindow.from_dict({
                "window_id": "WIN-1",
                "name": "Partial",
                "start_time": "2026-06-15T09:00:00Z",
            })
        self.assertIn("missing required field(s)", str(ctx.exception))
        self.assertIn("end_time", str(ctx.exception))
        self.assertNotIn("name", str(ctx.exception))

    def test_wave_from_dict_missing_fields(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            Wave.from_dict({"wave_id": "WAVE-1"})
        self.assertIn("missing required field(s)", str(ctx.exception))
        self.assertIn("name", str(ctx.exception))

    def test_load_windows_from_json_missing_fields(self) -> None:
        from release_orchestrator.core.scheduler import load_windows_from_json

        with tempfile.TemporaryDirectory() as tmp:
            bad_path = os.path.join(tmp, "bad_windows.json")
            with open(bad_path, "w", encoding="utf-8") as f:
                json.dump({"windows": [
                    {"window_id": "WIN-1", "name": "Only Name"},
                    {"window_id": "WIN-2"},
                ]}, f)
            with self.assertRaises(ValueError) as ctx:
                load_windows_from_json(bad_path)
            self.assertIn("Invalid window", str(ctx.exception))
            self.assertIn("index 0", str(ctx.exception))
            self.assertIn("missing required field(s)", str(ctx.exception))

    def test_load_waves_from_json_missing_fields(self) -> None:
        from release_orchestrator.core.scheduler import load_waves_from_json

        with tempfile.TemporaryDirectory() as tmp:
            bad_path = os.path.join(tmp, "bad_waves.json")
            with open(bad_path, "w", encoding="utf-8") as f:
                json.dump({"waves": [
                    {"wave_id": "WAVE-1"},
                ]}, f)
            with self.assertRaises(ValueError) as ctx:
                load_waves_from_json(bad_path)
            self.assertIn("Invalid wave", str(ctx.exception))
            self.assertIn("index 0", str(ctx.exception))
            self.assertIn("missing required field(s)", str(ctx.exception))


class TestSchemeValidator(unittest.TestCase):
    """Tests for scheme validation logic."""

    def test_valid_scheme_passes(self) -> None:
        scheme = _make_test_scheme("valid-test")
        validator = SchemeValidator(scheme)
        issues = validator.validate()
        errors = [i for i in issues if i.get("severity") == "error"]
        self.assertEqual(len(errors), 0, f"Unexpected errors: {errors}")

    def test_missing_scheme_name(self) -> None:
        scheme = _make_test_scheme("")
        scheme.scheme_name = ""
        passed, issues = validate_scheme(scheme)
        self.assertFalse(passed)
        codes = [i["issue_code"] for i in issues]
        self.assertIn("SCHEME_NAME_MISSING", codes)

    def test_no_windows_or_manifest(self) -> None:
        scheme = _make_test_scheme("empty", with_windows=False, with_manifest=False)
        scheme.release_windows = []
        scheme.manifest = None
        passed, issues = validate_scheme(scheme)
        self.assertFalse(passed)
        codes = [i["issue_code"] for i in issues]
        self.assertIn("NO_WINDOWS_OR_MANIFEST", codes)

    def test_invalid_window_time(self) -> None:
        scheme = _make_test_scheme("bad-time")
        bad_window = _make_test_window("WIN-BAD", "Bad Time", "2026-06-15T17:00:00Z", "2026-06-15T09:00:00Z")
        scheme.release_windows = [bad_window]
        passed, issues = validate_scheme(scheme)
        self.assertFalse(passed)
        codes = [i["issue_code"] for i in issues]
        self.assertIn("INVALID_WINDOW_TIME", codes)

    def test_invalid_freeze_period(self) -> None:
        scheme = _make_test_scheme("bad-freeze")
        window = _make_test_window("WIN-1", "Window 1")
        window.freeze_periods = [
            FreezePeriod(
                name="Bad Freeze",
                start="2026-06-15T17:00:00Z",
                end="2026-06-15T07:00:00Z",
            ),
        ]
        scheme.release_windows = [window]
        passed, issues = validate_scheme(scheme)
        self.assertFalse(passed)
        codes = [i["issue_code"] for i in issues]
        self.assertIn("INVALID_FREEZE_PERIOD", codes)

    def test_duplicate_window_id(self) -> None:
        scheme = _make_test_scheme("dup-win")
        w1 = _make_test_window("WIN-DUP", "Window A")
        w2 = _make_test_window("WIN-DUP", "Window B", "2026-06-16T09:00:00Z", "2026-06-16T17:00:00Z")
        scheme.release_windows = [w1, w2]
        passed, issues = validate_scheme(scheme)
        self.assertFalse(passed)
        codes = [i["issue_code"] for i in issues]
        self.assertIn("DUPLICATE_WINDOW_ID", codes)

    def test_duplicate_wave_id(self) -> None:
        scheme = _make_test_scheme("dup-wave")
        w1 = _make_test_wave("WAVE-DUP", "Wave A", 1)
        w2 = _make_test_wave("WAVE-DUP", "Wave B", 2)
        scheme.waves = [w1, w2]
        passed, issues = validate_scheme(scheme)
        self.assertFalse(passed)
        codes = [i["issue_code"] for i in issues]
        self.assertIn("DUPLICATE_WAVE_ID", codes)

    def test_window_overlap_warning(self) -> None:
        scheme = _make_test_scheme("overlap", with_manifest=False)
        w1 = _make_test_window("WIN-1", "Window A", "2026-06-15T09:00:00Z", "2026-06-15T17:00:00Z")
        w2 = _make_test_window("WIN-2", "Window B", "2026-06-15T10:00:00Z", "2026-06-15T18:00:00Z")
        scheme.release_windows = [w1, w2]
        passed, issues = validate_scheme(scheme)
        self.assertTrue(passed)
        warnings = [i for i in issues if i.get("severity") == "warning"]
        self.assertTrue(
            any(i["issue_code"] == "WINDOW_TIME_OVERLAP" for i in warnings),
            f"Expected WINDOW_TIME_OVERLAP warning, got: {[i['issue_code'] for i in warnings]}",
        )

    def test_locked_window_reuse(self) -> None:
        scheme = _make_test_scheme("locked-test")
        scheme.release_windows[0].locked = False
        existing_locks = {
            "WIN-1": {
                "locked": True,
                "locked_by": "manager@corp.com",
                "locked_at": "2026-06-10T00:00:00Z",
            },
        }
        passed, issues = validate_scheme(scheme, existing_locks)
        self.assertFalse(passed)
        codes = [i["issue_code"] for i in issues]
        self.assertIn("LOCKED_WINDOW_REUSE", codes)

    def test_manifest_invalid_type(self) -> None:
        scheme = _make_test_scheme("bad-manifest")
        scheme.manifest = "not a dict"
        passed, issues = validate_scheme(scheme)
        self.assertFalse(passed)
        codes = [i["issue_code"] for i in issues]
        self.assertIn("MANIFEST_INVALID_TYPE", codes)

    def test_manifest_missing_fields(self) -> None:
        scheme = _make_test_scheme("missing-fields")
        scheme.manifest = {"manifest_version": "1.0"}
        passed, issues = validate_scheme(scheme)
        self.assertFalse(passed)
        codes = [i["issue_code"] for i in issues]
        self.assertIn("MANIFEST_MISSING_RELEASE_ID", codes)
        self.assertIn("MANIFEST_MISSING_TITLE", codes)
        self.assertIn("MANIFEST_MISSING_TARGET_ENVIRONMENT", codes)

    def test_validate_and_raise_on_errors(self) -> None:
        scheme = _make_test_scheme("raise-test")
        scheme.scheme_name = ""
        with self.assertRaises(SchemeValidationError):
            SchemeValidator(scheme).validate_and_raise()


class TestSchemeStorage(unittest.TestCase):
    """Tests for saving, loading, listing, and deleting schemes on disk."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="orchestrator_scheme_test_")
        self.work_dir = self.temp_dir

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_save_and_load_scheme(self) -> None:
        scheme = _make_test_scheme("save-load-test")
        path = save_scheme(scheme, base=self.work_dir)
        self.assertTrue(path.exists())
        loaded = load_scheme("save-load-test", base=self.work_dir)
        self.assertEqual(loaded.scheme_name, "save-load-test")
        self.assertEqual(len(loaded.release_windows), 3)
        self.assertEqual(loaded.manifest["release_id"], "RL-TEST-001")

    def test_scheme_exists(self) -> None:
        self.assertFalse(scheme_exists("exists-test", base=self.work_dir))
        scheme = _make_test_scheme("exists-test")
        save_scheme(scheme, base=self.work_dir)
        self.assertTrue(scheme_exists("exists-test", base=self.work_dir))

    def test_save_duplicate_without_overwrite(self) -> None:
        scheme = _make_test_scheme("dup-save")
        save_scheme(scheme, base=self.work_dir)
        with self.assertRaises(FileExistsError) as ctx:
            save_scheme(scheme, base=self.work_dir, overwrite=False)
        self.assertIn("already exists", str(ctx.exception))

    def test_save_duplicate_with_overwrite(self) -> None:
        scheme = _make_test_scheme("overwrite-save")
        save_scheme(scheme, base=self.work_dir)
        scheme.description = "UPDATED DESCRIPTION"
        save_scheme(scheme, base=self.work_dir, overwrite=True)
        loaded = load_scheme("overwrite-save", base=self.work_dir)
        self.assertEqual(loaded.description, "UPDATED DESCRIPTION")
        self.assertIsNotNone(loaded.updated_at)

    def test_list_schemes(self) -> None:
        names = ["scheme-a", "scheme-b", "scheme-c"]
        for name in names:
            s = _make_test_scheme(name)
            save_scheme(s, base=self.work_dir)
        listed = list_schemes(base=self.work_dir)
        listed_names = sorted([s["name"] for s in listed])
        self.assertEqual(listed_names, sorted(names))
        for entry in listed:
            self.assertIn("created_at", entry)
            self.assertIn("created_by", entry)
            self.assertIn("windows_count", entry)
            self.assertEqual(entry["windows_count"], 3)

    def test_delete_scheme(self) -> None:
        scheme = _make_test_scheme("delete-me")
        save_scheme(scheme, base=self.work_dir)
        self.assertTrue(scheme_exists("delete-me", base=self.work_dir))
        ok = delete_scheme("delete-me", base=self.work_dir)
        self.assertTrue(ok)
        self.assertFalse(scheme_exists("delete-me", base=self.work_dir))

    def test_delete_nonexistent_scheme(self) -> None:
        ok = delete_scheme("never-existed", base=self.work_dir)
        self.assertFalse(ok)

    def test_load_nonexistent_scheme(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_scheme("never-existed", base=self.work_dir)

    def test_scheme_persists_across_processes(self) -> None:
        """Simulate a process restart by deleting and recreating state."""
        scheme = _make_test_scheme("persistent-scheme")
        save_scheme(scheme, base=self.work_dir)
        self.assertTrue(scheme_exists("persistent-scheme", base=self.work_dir))

        other_temp = tempfile.mkdtemp(prefix="orchestrator_scheme_test2_")
        try:
            shutil.copytree(
                Path(self.work_dir) / ".release_orchestrator",
                Path(other_temp) / ".release_orchestrator",
            )
            self.assertTrue(scheme_exists("persistent-scheme", base=other_temp))
            loaded = load_scheme("persistent-scheme", base=other_temp)
            self.assertEqual(loaded.scheme_name, "persistent-scheme")
        finally:
            shutil.rmtree(other_temp, ignore_errors=True)


class TestSchemeImportExport(unittest.TestCase):
    """Tests for importing and exporting schemes as JSON files."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="orchestrator_ie_test_")
        self.work_dir = self.temp_dir
        self.ie_dir = Path(self.temp_dir) / "exports"
        self.ie_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_export_scheme(self) -> None:
        scheme = _make_test_scheme("export-test")
        save_scheme(scheme, base=self.work_dir)
        out_path = self.ie_dir / "exported.json"
        export_path = export_scheme_to_file("export-test", str(out_path), base=self.work_dir)
        self.assertEqual(export_path, out_path)
        self.assertTrue(out_path.exists())
        with out_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["scheme_name"], "export-test")
        self.assertEqual(len(data["release_windows"]), 3)

    def test_import_scheme(self) -> None:
        scheme = _make_test_scheme("source-scheme")
        src_path = self.ie_dir / "source.json"
        with src_path.open("w", encoding="utf-8") as f:
            json.dump(scheme.to_dict(), f, indent=2, ensure_ascii=False)
        imported = import_scheme_from_file(str(src_path), base=self.work_dir)
        self.assertEqual(imported.scheme_name, "source-scheme")
        self.assertTrue(scheme_exists("source-scheme", base=self.work_dir))
        loaded = load_scheme("source-scheme", base=self.work_dir)
        self.assertEqual(loaded.manifest["release_id"], "RL-TEST-001")

    def test_import_with_name_override(self) -> None:
        scheme = _make_test_scheme("original-name")
        src_path = self.ie_dir / "override.json"
        with src_path.open("w", encoding="utf-8") as f:
            json.dump(scheme.to_dict(), f, indent=2, ensure_ascii=False)
        import_scheme_from_file(str(src_path), scheme_name="new-name", base=self.work_dir)
        self.assertTrue(scheme_exists("new-name", base=self.work_dir))
        self.assertFalse(scheme_exists("original-name", base=self.work_dir))

    def test_import_overwrite_flag(self) -> None:
        scheme1 = _make_test_scheme("overwrite-import")
        scheme1.description = "ORIGINAL"
        save_scheme(scheme1, base=self.work_dir)

        scheme2 = _make_test_scheme("overwrite-import")
        scheme2.description = "UPDATED"
        src_path = self.ie_dir / "updated.json"
        with src_path.open("w", encoding="utf-8") as f:
            json.dump(scheme2.to_dict(), f, indent=2, ensure_ascii=False)

        with self.assertRaises(FileExistsError):
            import_scheme_from_file(str(src_path), base=self.work_dir)

        import_scheme_from_file(str(src_path), base=self.work_dir, overwrite=True)
        loaded = load_scheme("overwrite-import", base=self.work_dir)
        self.assertEqual(loaded.description, "UPDATED")

    def test_import_invalid_json(self) -> None:
        bad_path = self.ie_dir / "bad.json"
        with bad_path.open("w", encoding="utf-8") as f:
            f.write("{this is not valid json}")
        with self.assertRaises(ValueError) as ctx:
            import_scheme_from_file(str(bad_path), base=self.work_dir)
        self.assertIn("Invalid JSON", str(ctx.exception))

    def test_import_missing_file(self) -> None:
        with self.assertRaises(FileNotFoundError):
            import_scheme_from_file("/nonexistent/path.json", base=self.work_dir)

    def test_import_missing_required_fields(self) -> None:
        bad_data = {"created_at": "2026-01-01T00:00:00Z", "created_by": "test"}
        src_path = self.ie_dir / "missing.json"
        with src_path.open("w", encoding="utf-8") as f:
            json.dump(bad_data, f)
        with self.assertRaises(ValueError) as ctx:
            import_scheme_from_file(str(src_path), base=self.work_dir)
        self.assertIn("missing or invalid fields", str(ctx.exception))

    def test_export_import_roundtrip(self) -> None:
        scheme = _make_test_scheme("roundtrip-ie", created_by="roundtrip@test.com")
        save_scheme(scheme, base=self.work_dir)
        export_path = self.ie_dir / "rt.json"
        export_scheme_to_file("roundtrip-ie", str(export_path), base=self.work_dir)

        shutil.rmtree(Path(self.work_dir) / ".release_orchestrator")

        import_scheme_from_file(str(export_path), scheme_name="roundtrip-restored", base=self.work_dir)
        loaded = load_scheme("roundtrip-restored", base=self.work_dir)
        self.assertEqual(loaded.created_by, "roundtrip@test.com")
        self.assertEqual(len(loaded.release_windows), 3)
        self.assertEqual(len(loaded.waves), 3)
        self.assertEqual(loaded.manifest["release_id"], "RL-TEST-001")


class TestSchemeOperationsLog(unittest.TestCase):
    """Tests for the scheme operation history/tracking log."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="orchestrator_log_test_")
        self.work_dir = self.temp_dir

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_save_logs_operation(self) -> None:
        scheme = _make_test_scheme("log-test")
        save_scheme(scheme, base=self.work_dir)
        log_path = Path(self.work_dir) / ".release_orchestrator" / "scheme_operations.log"
        self.assertTrue(log_path.exists())
        lines = log_path.read_text(encoding="utf-8").strip().split("\n")
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["action"], "save")
        self.assertEqual(entry["scheme_name"], "log-test")
        self.assertIn("timestamp", entry)
        self.assertIn("user", entry)

    def test_delete_logs_operation(self) -> None:
        scheme = _make_test_scheme("log-delete")
        save_scheme(scheme, base=self.work_dir)
        delete_scheme("log-delete", base=self.work_dir)
        log_path = Path(self.work_dir) / ".release_orchestrator" / "scheme_operations.log"
        lines = [json.loads(l) for l in log_path.read_text(encoding="utf-8").strip().split("\n") if l.strip()]
        actions = [l["action"] for l in lines]
        self.assertIn("save", actions)
        self.assertIn("delete", actions)

    def test_import_export_logs_operations(self) -> None:
        scheme = _make_test_scheme("log-ie")
        save_scheme(scheme, base=self.work_dir)
        export_path = Path(self.temp_dir) / "exported.json"
        export_scheme_to_file("log-ie", str(export_path), base=self.work_dir)
        delete_scheme("log-ie", base=self.work_dir)
        import_scheme_from_file(str(export_path), base=self.work_dir)

        log_path = Path(self.work_dir) / ".release_orchestrator" / "scheme_operations.log"
        lines = [json.loads(l) for l in log_path.read_text(encoding="utf-8").strip().split("\n") if l.strip()]
        actions = [l["action"] for l in lines]
        self.assertEqual(actions, ["save", "export", "delete", "save", "import"])


class TestSchemeClone(unittest.TestCase):
    """Tests for scheme cloning functionality."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="orchestrator_clone_test_")
        self.work_dir = self.temp_dir

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_clone_scheme_success(self) -> None:
        import time

        source = _make_test_scheme("source-scheme", created_by="alice@corp.com")
        source.description = "Original source scheme"
        source.policy = {"max_capacity": 10, "strict": True}
        source_created_at = "2026-01-01T00:00:00Z"
        source.created_at = source_created_at
        save_scheme(source, base=self.work_dir)

        time.sleep(1)

        cloned = clone_scheme(
            "source-scheme",
            "cloned-scheme",
            base=self.work_dir,
            created_by="bob@corp.com",
        )

        self.assertEqual(cloned.scheme_name, "cloned-scheme")
        self.assertEqual(cloned.created_by, "bob@corp.com")
        self.assertNotEqual(cloned.created_at, source_created_at)
        self.assertEqual(len(cloned.release_windows), len(source.release_windows))
        self.assertEqual(len(cloned.waves), len(source.waves))
        self.assertEqual(cloned.manifest["release_id"], source.manifest["release_id"])
        self.assertEqual(cloned.tags, source.tags)
        self.assertEqual(cloned.policy, source.policy)
        self.assertEqual(cloned.metadata.get("cloned_from"), "source-scheme")
        self.assertEqual(cloned.metadata.get("source"), "scheme_clone")
        self.assertIsNone(cloned.updated_at)

        loaded = load_scheme("cloned-scheme", base=self.work_dir)
        self.assertEqual(loaded.scheme_name, "cloned-scheme")
        self.assertEqual(loaded.metadata.get("cloned_from"), "source-scheme")

    def test_clone_source_not_found(self) -> None:
        with self.assertRaises(FileNotFoundError) as ctx:
            clone_scheme("never-existed", "new-scheme", base=self.work_dir)
        self.assertIn("Source scheme not found", str(ctx.exception))

    def test_clone_target_already_exists_no_force(self) -> None:
        source = _make_test_scheme("clone-source")
        save_scheme(source, base=self.work_dir)
        target = _make_test_scheme("clone-target")
        save_scheme(target, base=self.work_dir)

        with self.assertRaises(FileExistsError) as ctx:
            clone_scheme("clone-source", "clone-target", base=self.work_dir)
        self.assertIn("already exists", str(ctx.exception))

    def test_clone_target_already_exists_with_force(self) -> None:
        source = _make_test_scheme("clone-source-2")
        source.description = "SOURCE DESCRIPTION"
        source.policy = {"key": "value"}
        save_scheme(source, base=self.work_dir)

        target = _make_test_scheme("clone-target-2")
        target.description = "OLD DESCRIPTION"
        save_scheme(target, base=self.work_dir)

        cloned = clone_scheme(
            "clone-source-2",
            "clone-target-2",
            base=self.work_dir,
            overwrite=True,
            created_by="charlie@corp.com",
        )

        self.assertEqual(cloned.scheme_name, "clone-target-2")
        self.assertEqual(cloned.description, "SOURCE DESCRIPTION")
        self.assertEqual(cloned.created_by, "charlie@corp.com")
        self.assertIsNotNone(cloned.updated_at)
        self.assertEqual(cloned.policy, {"key": "value"})

        loaded = load_scheme("clone-target-2", base=self.work_dir)
        self.assertEqual(loaded.description, "SOURCE DESCRIPTION")

    def test_clone_invalid_target_name(self) -> None:
        source = _make_test_scheme("valid-source")
        save_scheme(source, base=self.work_dir)

        with self.assertRaises(ValueError) as ctx:
            clone_scheme("valid-source", "   ", base=self.work_dir)
        self.assertIn("cannot be empty", str(ctx.exception))

    def test_clone_persists_across_process_restart(self) -> None:
        source = _make_test_scheme("persist-source", created_by="dave@corp.com")
        save_scheme(source, base=self.work_dir)
        clone_scheme(
            "persist-source",
            "persist-clone",
            base=self.work_dir,
            created_by="eve@corp.com",
        )

        other_temp = tempfile.mkdtemp(prefix="orchestrator_clone_restart_")
        try:
            shutil.copytree(
                Path(self.work_dir) / ".release_orchestrator",
                Path(other_temp) / ".release_orchestrator",
            )

            self.assertTrue(scheme_exists("persist-clone", base=other_temp))
            loaded = load_scheme("persist-clone", base=other_temp)
            self.assertEqual(loaded.scheme_name, "persist-clone")
            self.assertEqual(loaded.created_by, "eve@corp.com")
            self.assertEqual(loaded.metadata.get("cloned_from"), "persist-source")
            self.assertEqual(len(loaded.release_windows), 3)
            self.assertEqual(len(loaded.waves), 3)
        finally:
            shutil.rmtree(other_temp, ignore_errors=True)

    def test_clone_then_export_import_preserves_metadata(self) -> None:
        source = _make_test_scheme("export-source")
        save_scheme(source, base=self.work_dir)
        clone_scheme(
            "export-source",
            "export-clone",
            base=self.work_dir,
            created_by="frank@corp.com",
        )

        export_path = Path(self.temp_dir) / "clone_export.json"
        export_scheme_to_file("export-clone", str(export_path), base=self.work_dir)

        shutil.rmtree(Path(self.work_dir) / ".release_orchestrator")

        import_scheme_from_file(
            str(export_path),
            scheme_name="imported-clone",
            base=self.work_dir,
        )

        loaded = load_scheme("imported-clone", base=self.work_dir)
        self.assertEqual(loaded.scheme_name, "imported-clone")
        self.assertEqual(loaded.created_by, "frank@corp.com")
        self.assertEqual(loaded.metadata.get("cloned_from"), "export-source")
        self.assertEqual(len(loaded.release_windows), 3)
        self.assertEqual(len(loaded.waves), 3)
        self.assertEqual(loaded.manifest["release_id"], "RL-TEST-001")
        self.assertEqual(loaded.tags, ["test", "unit"])

    def test_clone_logs_operation(self) -> None:
        source = _make_test_scheme("log-clone-source")
        save_scheme(source, base=self.work_dir)
        clone_scheme(
            "log-clone-source",
            "log-clone-target",
            base=self.work_dir,
        )

        log_path = Path(self.work_dir) / ".release_orchestrator" / "scheme_operations.log"
        lines = [
            json.loads(l)
            for l in log_path.read_text(encoding="utf-8").strip().split("\n")
            if l.strip()
        ]
        actions = [l["action"] for l in lines]
        self.assertEqual(actions, ["save", "save", "clone"])

        clone_entry = lines[-1]
        self.assertEqual(clone_entry["action"], "clone")
        self.assertEqual(clone_entry["scheme_name"], "log-clone-target")
        self.assertEqual(clone_entry["extra"]["source"], "log-clone-source")
        self.assertFalse(clone_entry["extra"]["overwrite"])

    def test_clone_force_logs_overwrite(self) -> None:
        source = _make_test_scheme("force-clone-source")
        save_scheme(source, base=self.work_dir)
        target = _make_test_scheme("force-clone-target")
        save_scheme(target, base=self.work_dir)

        clone_scheme(
            "force-clone-source",
            "force-clone-target",
            base=self.work_dir,
            overwrite=True,
        )

        log_path = Path(self.work_dir) / ".release_orchestrator" / "scheme_operations.log"
        lines = [
            json.loads(l)
            for l in log_path.read_text(encoding="utf-8").strip().split("\n")
            if l.strip()
        ]
        clone_entries = [l for l in lines if l["action"] == "clone"]
        self.assertEqual(len(clone_entries), 1)
        self.assertTrue(clone_entries[0]["extra"]["overwrite"])

    def test_clone_preserves_policy_and_manifest(self) -> None:
        source = _make_test_scheme("policy-source")
        source.policy = {
            "rules": ["rule1", "rule2"],
            "threshold": 0.95,
            "nested": {"a": 1, "b": [2, 3]},
        }
        source.manifest["custom_field"] = "custom_value"
        save_scheme(source, base=self.work_dir)

        cloned = clone_scheme(
            "policy-source",
            "policy-clone",
            base=self.work_dir,
        )

        self.assertEqual(cloned.policy, source.policy)
        self.assertEqual(cloned.policy["nested"]["b"], [2, 3])
        self.assertEqual(cloned.manifest["custom_field"], "custom_value")

    def test_clone_preserves_windows_details(self) -> None:
        source = _make_test_scheme("window-detail-source")
        for w in source.release_windows:
            w.tags = ["region:us-east", "env:prod"]
            w.description = f"Window {w.name}"
        save_scheme(source, base=self.work_dir)

        cloned = clone_scheme(
            "window-detail-source",
            "window-detail-clone",
            base=self.work_dir,
        )

        self.assertEqual(len(cloned.release_windows), len(source.release_windows))
        for i, w in enumerate(cloned.release_windows):
            self.assertEqual(w.tags, source.release_windows[i].tags)
            self.assertEqual(w.description, source.release_windows[i].description)
            self.assertEqual(w.window_id, source.release_windows[i].window_id)


class TestSchemeExitCodes(unittest.TestCase):
    """Tests that exit codes are correctly defined."""

    def test_scheme_exit_codes_are_defined(self) -> None:
        self.assertEqual(EXIT_SCHEME_ALREADY_EXISTS.code, 21)
        self.assertEqual(EXIT_SCHEME_NOT_FOUND.code, 22)
        self.assertEqual(EXIT_SCHEME_VALIDATION_FAILED.code, 23)
        self.assertEqual(EXIT_SCHEME_IO_ERROR.code, 24)

    def test_scheme_exit_codes_have_names_and_descriptions(self) -> None:
        for code in [
            EXIT_SCHEME_ALREADY_EXISTS,
            EXIT_SCHEME_NOT_FOUND,
            EXIT_SCHEME_VALIDATION_FAILED,
            EXIT_SCHEME_IO_ERROR,
        ]:
            self.assertTrue(hasattr(code, "name"))
            self.assertTrue(hasattr(code, "description"))
            self.assertIsNotNone(code.name)
            self.assertIsNotNone(code.description)


if __name__ == "__main__":
    unittest.main()
