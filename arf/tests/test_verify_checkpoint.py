"""Tests for verify_checkpoint verificator."""

from pathlib import Path

import pytest

from arf.scripts.verificators import verify_checkpoint as verify_mod
from arf.scripts.verificators.common.types import VerificationResult
from arf.tests.fixtures.paths import configure_repo_paths
from arf.tests.fixtures.task_builder import build_step_tracker, build_task_folder
from arf.tests.fixtures.writers import write_text

TASK_ID: str = "t0001_checkpoint_test"

_VALID_CHECKPOINT: str = """\
---
spec_version: "1"
task_id: "t0001_checkpoint_test"
updated_at: "2026-06-23T14:00:00Z"
completed_steps: 1
next_step_number: 2
next_step_id: "research-papers"
---
# Task Objective
Build a test model for unit testing purposes.

---

## Step History

### Step 1 — create-branch
Branch task/t0001_checkpoint_test created.
Initial folder structure initialized in tasks/t0001_checkpoint_test/.
Step 1 is a mechanical setup step with no research output.

---

## Cross-Step Decisions

---

## Next Step Notes
Step 1 completed successfully. Proceed to step 2 (research-papers) per step_tracker.json.
"""

_STEP_COMPLETED: dict[str, object] = {
    "step": 1,
    "name": "create-branch",
    "status": "completed",
    "completed_at": "2026-06-23T13:55:00Z",
    "started_at": "2026-06-23T13:54:00Z",
    "last_heartbeat_at": "2026-06-23T13:55:00Z",
    "heartbeat_interval_seconds": 60,
    "expected_completion_at": "2026-06-23T14:00:00Z",
}

_STEP_PENDING: dict[str, object] = {
    "step": 2,
    "name": "research-papers",
    "status": "pending",
}


def _setup(*, monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
    configure_repo_paths(
        monkeypatch=monkeypatch,
        repo_root=repo_root,
        verificator_modules=[verify_mod],
    )


def _codes(result: VerificationResult) -> list[str]:
    return [d.code.text for d in result.diagnostics]


def _run(*, task_id: str = TASK_ID) -> VerificationResult:
    return verify_mod.verify_checkpoint(task_id=task_id)


def _checkpoint_path(*, repo_root: Path, task_id: str = TASK_ID) -> Path:
    return repo_root / "tasks" / task_id / "checkpoint.md"


class TestValidPasses:
    def test_valid_checkpoint_passes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
        build_task_folder(repo_root=tmp_path, task_id=TASK_ID)
        build_step_tracker(
            repo_root=tmp_path,
            task_id=TASK_ID,
            steps=[_STEP_COMPLETED, _STEP_PENDING],
        )
        write_text(
            path=_checkpoint_path(repo_root=tmp_path),
            content=_VALID_CHECKPOINT,
        )
        result: VerificationResult = _run()
        assert result.passed, _codes(result)
        assert len(result.diagnostics) == 0

    def test_no_completed_steps_no_checkpoint_required(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
        build_task_folder(repo_root=tmp_path, task_id=TASK_ID)
        build_step_tracker(
            repo_root=tmp_path,
            task_id=TASK_ID,
            steps=[_STEP_PENDING],
        )
        result: VerificationResult = _run()
        assert result.passed
        assert len(result.diagnostics) == 0

    def test_no_step_tracker_passes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
        build_task_folder(repo_root=tmp_path, task_id=TASK_ID)
        result: VerificationResult = _run()
        assert result.passed
        assert len(result.diagnostics) == 0


class TestCkE001MissingCheckpoint:
    def test_missing_checkpoint_with_completed_step(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
        build_task_folder(repo_root=tmp_path, task_id=TASK_ID)
        build_step_tracker(
            repo_root=tmp_path,
            task_id=TASK_ID,
            steps=[_STEP_COMPLETED, _STEP_PENDING],
        )
        result: VerificationResult = _run()
        assert not result.passed
        assert "CK-E001" in _codes(result)


class TestCkE002FrontmatterIssues:
    def test_missing_frontmatter(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
        build_task_folder(repo_root=tmp_path, task_id=TASK_ID)
        build_step_tracker(
            repo_root=tmp_path,
            task_id=TASK_ID,
            steps=[_STEP_COMPLETED],
        )
        write_text(
            path=_checkpoint_path(repo_root=tmp_path),
            content="# Task Objective\nNo frontmatter here.\n",
        )
        result: VerificationResult = _run()
        assert not result.passed
        assert "CK-E002" in _codes(result)

    def test_invalid_yaml_frontmatter(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
        build_task_folder(repo_root=tmp_path, task_id=TASK_ID)
        build_step_tracker(
            repo_root=tmp_path,
            task_id=TASK_ID,
            steps=[_STEP_COMPLETED],
        )
        write_text(
            path=_checkpoint_path(repo_root=tmp_path),
            content="---\n: invalid: yaml: [unclosed\n---\n# Task Objective\ntext\n",
        )
        result: VerificationResult = _run()
        assert not result.passed
        assert "CK-E002" in _codes(result)


class TestCkE003MissingFields:
    def test_missing_required_field(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
        build_task_folder(repo_root=tmp_path, task_id=TASK_ID)
        build_step_tracker(
            repo_root=tmp_path,
            task_id=TASK_ID,
            steps=[_STEP_COMPLETED],
        )
        content: str = (
            "---\n"
            'spec_version: "1"\n'
            f'task_id: "{TASK_ID}"\n'
            "# updated_at is missing\n"
            "completed_steps: 1\n"
            "next_step_number: 2\n"
            'next_step_id: "research-papers"\n'
            "---\n"
            "# Task Objective\nA test.\n\n"
            "## Step History\n\n### Step 1 — create-branch\nDone.\n\n"
            "## Cross-Step Decisions\n\n"
            "## Next Step Notes\nNext step.\n"
        )
        write_text(
            path=_checkpoint_path(repo_root=tmp_path),
            content=content,
        )
        result: VerificationResult = _run()
        assert not result.passed
        assert "CK-E003" in _codes(result)


class TestCkE004TaskIdMismatch:
    def test_task_id_mismatch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
        build_task_folder(repo_root=tmp_path, task_id=TASK_ID)
        build_step_tracker(
            repo_root=tmp_path,
            task_id=TASK_ID,
            steps=[_STEP_COMPLETED],
        )
        content: str = (
            "---\n"
            'spec_version: "1"\n'
            'task_id: "t9999_wrong_task_id"\n'
            'updated_at: "2026-06-23T14:00:00Z"\n'
            "completed_steps: 1\n"
            "next_step_number: 2\n"
            'next_step_id: "research-papers"\n'
            "---\n"
            "# Task Objective\nA test.\n\n"
            "## Step History\n\n### Step 1 — create-branch\nDone.\n\n"
            "## Cross-Step Decisions\n\n"
            "## Next Step Notes\nNext step.\n"
        )
        write_text(
            path=_checkpoint_path(repo_root=tmp_path),
            content=content,
        )
        result: VerificationResult = _run()
        assert not result.passed
        assert "CK-E004" in _codes(result)


class TestCkE007MissingStepHistory:
    def test_missing_step_history_entry(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
        build_task_folder(repo_root=tmp_path, task_id=TASK_ID)
        step_2_completed: dict[str, object] = {
            "step": 2,
            "name": "research-papers",
            "status": "completed",
            "completed_at": "2026-06-23T15:00:00Z",
            "started_at": "2026-06-23T14:30:00Z",
            "last_heartbeat_at": "2026-06-23T15:00:00Z",
            "heartbeat_interval_seconds": 60,
            "expected_completion_at": "2026-06-23T15:30:00Z",
        }
        build_step_tracker(
            repo_root=tmp_path,
            task_id=TASK_ID,
            steps=[_STEP_COMPLETED, step_2_completed],
        )
        # Only Step 1 in history — Step 2 is missing
        content: str = (
            "---\n"
            'spec_version: "1"\n'
            f'task_id: "{TASK_ID}"\n'
            'updated_at: "2026-06-23T15:00:00Z"\n'
            "completed_steps: 2\n"
            "next_step_number: 3\n"
            'next_step_id: "planning"\n'
            "---\n"
            "# Task Objective\nA test.\n\n"
            "## Step History\n\n### Step 1 — create-branch\nDone.\n\n"
            "## Cross-Step Decisions\n\n"
            "## Next Step Notes\nNext step.\n"
        )
        write_text(
            path=_checkpoint_path(repo_root=tmp_path),
            content=content,
        )
        result: VerificationResult = _run()
        assert not result.passed
        assert "CK-E007" in _codes(result)


class TestCurrentStepIdPromotion:
    def test_in_progress_step_promoted_passes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
        build_task_folder(repo_root=tmp_path, task_id=TASK_ID)
        step_2_in_progress: dict[str, object] = {
            "step": 2,
            "name": "research-papers",
            "status": "in_progress",
            "started_at": "2026-06-23T14:30:00Z",
            "last_heartbeat_at": "2026-06-23T14:45:00Z",
            "heartbeat_interval_seconds": 60,
            "expected_completion_at": "2026-06-23T15:30:00Z",
        }
        step_3_pending: dict[str, object] = {
            "step": 3,
            "name": "planning",
            "status": "pending",
        }
        build_step_tracker(
            repo_root=tmp_path,
            task_id=TASK_ID,
            steps=[_STEP_COMPLETED, step_2_in_progress, step_3_pending],
        )
        # checkpoint.md written by the step-executor — counts step 2 as completed already
        content: str = (
            "---\n"
            'spec_version: "1"\n'
            f'task_id: "{TASK_ID}"\n'
            'updated_at: "2026-06-23T15:00:00Z"\n'
            "completed_steps: 2\n"
            "next_step_number: 3\n"
            'next_step_id: "planning"\n'
            "---\n"
            "# Task Objective\nA test.\n\n"
            "## Step History\n\n"
            "### Step 1 — create-branch\nDone.\n\n"
            "### Step 2 — research-papers\nResearched all relevant papers.\n\n"
            "## Cross-Step Decisions\n\n"
            "## Next Step Notes\nNext step.\n"
        )
        write_text(path=_checkpoint_path(repo_root=tmp_path), content=content)
        # Without current_step_id: CK-E006 would fire
        result_no_id: VerificationResult = _run()
        assert "CK-E006" in _codes(result_no_id)
        # With current_step_id: passes
        result_with_id: VerificationResult = verify_mod.verify_checkpoint(
            task_id=TASK_ID, current_step_id="research-papers"
        )
        assert result_with_id.passed, _codes(result_with_id)


class TestCkE005NextStepFields:
    def test_null_next_step_number_with_pending_step_fires(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
        build_task_folder(repo_root=tmp_path, task_id=TASK_ID)
        build_step_tracker(
            repo_root=tmp_path,
            task_id=TASK_ID,
            steps=[_STEP_COMPLETED, _STEP_PENDING],
        )
        content: str = (
            "---\n"
            'spec_version: "1"\n'
            f'task_id: "{TASK_ID}"\n'
            'updated_at: "2026-06-23T14:00:00Z"\n'
            "completed_steps: 1\n"
            "next_step_number: null\n"
            'next_step_id: "research-papers"\n'
            "---\n"
            "# Task Objective\nA test.\n\n"
            "## Step History\n\n### Step 1 — create-branch\nDone.\n\n"
            "## Cross-Step Decisions\n\n"
            "## Next Step Notes\nNext step.\n"
        )
        write_text(path=_checkpoint_path(repo_root=tmp_path), content=content)
        result: VerificationResult = _run()
        assert not result.passed
        assert "CK-E005" in _codes(result)

    def test_nonnull_next_step_number_with_no_pending_fires(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
        build_task_folder(repo_root=tmp_path, task_id=TASK_ID)
        build_step_tracker(
            repo_root=tmp_path,
            task_id=TASK_ID,
            steps=[_STEP_COMPLETED],
        )
        content: str = (
            "---\n"
            'spec_version: "1"\n'
            f'task_id: "{TASK_ID}"\n'
            'updated_at: "2026-06-23T14:00:00Z"\n'
            "completed_steps: 1\n"
            "next_step_number: 2\n"
            "next_step_id: null\n"
            "---\n"
            "# Task Objective\nA test.\n\n"
            "## Step History\n\n### Step 1 — create-branch\nDone.\n\n"
            "## Cross-Step Decisions\n\n"
            "## Next Step Notes\nLast step done.\n"
        )
        write_text(path=_checkpoint_path(repo_root=tmp_path), content=content)
        result: VerificationResult = _run()
        assert not result.passed
        assert "CK-E005" in _codes(result)

    def test_next_step_id_mismatch_fires(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
        build_task_folder(repo_root=tmp_path, task_id=TASK_ID)
        build_step_tracker(
            repo_root=tmp_path,
            task_id=TASK_ID,
            steps=[_STEP_COMPLETED, _STEP_PENDING],
        )
        content: str = (
            "---\n"
            'spec_version: "1"\n'
            f'task_id: "{TASK_ID}"\n'
            'updated_at: "2026-06-23T14:00:00Z"\n'
            "completed_steps: 1\n"
            "next_step_number: 2\n"
            'next_step_id: "wrong-step-id"\n'
            "---\n"
            "# Task Objective\nA test.\n\n"
            "## Step History\n\n### Step 1 — create-branch\nDone.\n\n"
            "## Cross-Step Decisions\n\n"
            "## Next Step Notes\nNext step.\n"
        )
        write_text(path=_checkpoint_path(repo_root=tmp_path), content=content)
        result: VerificationResult = _run()
        assert not result.passed
        assert "CK-E005" in _codes(result)


class TestCkE006CompletedStepsType:
    def test_string_completed_steps_fires_type_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
        build_task_folder(repo_root=tmp_path, task_id=TASK_ID)
        build_step_tracker(
            repo_root=tmp_path,
            task_id=TASK_ID,
            steps=[_STEP_COMPLETED, _STEP_PENDING],
        )
        content: str = (
            "---\n"
            'spec_version: "1"\n'
            f'task_id: "{TASK_ID}"\n'
            'updated_at: "2026-06-23T14:00:00Z"\n'
            'completed_steps: "1"\n'
            "next_step_number: 2\n"
            'next_step_id: "research-papers"\n'
            "---\n"
            "# Task Objective\nA test.\n\n"
            "## Step History\n\n### Step 1 — create-branch\nDone.\n\n"
            "## Cross-Step Decisions\n\n"
            "## Next Step Notes\nNext step.\n"
        )
        write_text(path=_checkpoint_path(repo_root=tmp_path), content=content)
        result: VerificationResult = _run()
        assert not result.passed
        assert "CK-E006" in _codes(result)


class TestCkW004TaskObjective:
    def test_empty_objective_with_subsections_fires(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
        build_task_folder(repo_root=tmp_path, task_id=TASK_ID)
        build_step_tracker(
            repo_root=tmp_path,
            task_id=TASK_ID,
            steps=[_STEP_COMPLETED, _STEP_PENDING],
        )
        # Objective text is empty — only the divider and sub-sections follow
        content: str = (
            "---\n"
            'spec_version: "1"\n'
            f'task_id: "{TASK_ID}"\n'
            'updated_at: "2026-06-23T14:00:00Z"\n'
            "completed_steps: 1\n"
            "next_step_number: 2\n"
            'next_step_id: "research-papers"\n'
            "---\n"
            "# Task Objective\n\n"
            "---\n\n"
            "## Step History\n\n### Step 1 — create-branch\nDone.\n\n"
            "## Cross-Step Decisions\n\n"
            "## Next Step Notes\nNext step.\n"
        )
        write_text(path=_checkpoint_path(repo_root=tmp_path), content=content)
        result: VerificationResult = _run()
        assert "CK-W004" in _codes(result)


class TestCkW001SizeLimit:
    def test_checkpoint_over_10kb(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
        build_task_folder(repo_root=tmp_path, task_id=TASK_ID)
        build_step_tracker(
            repo_root=tmp_path,
            task_id=TASK_ID,
            steps=[_STEP_COMPLETED, _STEP_PENDING],
        )
        padding: str = "x" * 11000
        content: str = (
            "---\n"
            'spec_version: "1"\n'
            f'task_id: "{TASK_ID}"\n'
            'updated_at: "2026-06-23T14:00:00Z"\n'
            "completed_steps: 1\n"
            "next_step_number: 2\n"
            'next_step_id: "research-papers"\n'
            "---\n"
            "# Task Objective\nA test.\n\n"
            f"## Step History\n\n### Step 1 — create-branch\n{padding}\n\n"
            "## Cross-Step Decisions\n\n"
            "## Next Step Notes\nNext step.\n"
        )
        write_text(
            path=_checkpoint_path(repo_root=tmp_path),
            content=content,
        )
        result: VerificationResult = _run()
        codes: list[str] = _codes(result)
        assert "CK-W001" in codes


class TestCkW002OversizedStepHistoryEntry:
    def test_oversized_entry_fires(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
        build_task_folder(repo_root=tmp_path, task_id=TASK_ID)
        build_step_tracker(
            repo_root=tmp_path,
            task_id=TASK_ID,
            steps=[_STEP_COMPLETED, _STEP_PENDING],
        )
        # 110 words in the step history entry — over the 100-word limit
        long_entry: str = " ".join(["word"] * 110)
        content: str = (
            "---\n"
            'spec_version: "1"\n'
            f'task_id: "{TASK_ID}"\n'
            'updated_at: "2026-06-23T14:00:00Z"\n'
            "completed_steps: 1\n"
            "next_step_number: 2\n"
            'next_step_id: "research-papers"\n'
            "---\n"
            "# Task Objective\nA test.\n\n"
            f"## Step History\n\n### Step 1 — create-branch\n{long_entry}\n\n"
            "## Cross-Step Decisions\n\n"
            "## Next Step Notes\nNext step.\n"
        )
        write_text(path=_checkpoint_path(repo_root=tmp_path), content=content)
        result: VerificationResult = _run()
        assert "CK-W002" in _codes(result)


class TestCkW003UpdatedAtStaleness:
    def test_stale_updated_at_fires(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
        build_task_folder(repo_root=tmp_path, task_id=TASK_ID)
        # Step completed at 14:00; checkpoint updated_at is earlier (13:00)
        step_completed: dict[str, object] = {
            "step": 1,
            "name": "create-branch",
            "status": "completed",
            "completed_at": "2026-06-23T14:00:00Z",
            "started_at": "2026-06-23T13:54:00Z",
        }
        build_step_tracker(
            repo_root=tmp_path,
            task_id=TASK_ID,
            steps=[step_completed],
        )
        content: str = (
            "---\n"
            'spec_version: "1"\n'
            f'task_id: "{TASK_ID}"\n'
            'updated_at: "2026-06-23T13:00:00Z"\n'
            "completed_steps: 1\n"
            "next_step_number: null\n"
            "next_step_id: null\n"
            "---\n"
            "# Task Objective\nA test.\n\n"
            "## Step History\n\n### Step 1 — create-branch\nDone.\n\n"
            "## Cross-Step Decisions\n\n"
            "## Next Step Notes\nAll done.\n"
        )
        write_text(path=_checkpoint_path(repo_root=tmp_path), content=content)
        result: VerificationResult = _run()
        assert "CK-W003" in _codes(result)


class TestCkE003UpdatedAtTypeValidation:
    def _base_content(self, *, updated_at_yaml: str) -> str:
        return (
            "---\n"
            'spec_version: "1"\n'
            f'task_id: "{TASK_ID}"\n'
            f"{updated_at_yaml}\n"
            "completed_steps: 1\n"
            "next_step_number: null\n"
            "next_step_id: null\n"
            "---\n"
            "# Task Objective\nA test.\n\n"
            "## Step History\n\n### Step 1 — create-branch\nDone.\n\n"
            "## Cross-Step Decisions\n\n"
            "## Next Step Notes\nAll done.\n"
        )

    def test_null_updated_at_fires(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
        build_task_folder(repo_root=tmp_path, task_id=TASK_ID)
        build_step_tracker(
            repo_root=tmp_path,
            task_id=TASK_ID,
            steps=[{**_STEP_COMPLETED, "completed_at": "2026-06-23T14:00:00Z"}],
        )
        write_text(
            path=_checkpoint_path(repo_root=tmp_path),
            content=self._base_content(updated_at_yaml="updated_at: null"),
        )
        result: VerificationResult = _run()
        assert "CK-E003" in _codes(result)

    def test_wrong_type_updated_at_fires(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
        build_task_folder(repo_root=tmp_path, task_id=TASK_ID)
        build_step_tracker(
            repo_root=tmp_path,
            task_id=TASK_ID,
            steps=[{**_STEP_COMPLETED, "completed_at": "2026-06-23T14:00:00Z"}],
        )
        write_text(
            path=_checkpoint_path(repo_root=tmp_path),
            content=self._base_content(updated_at_yaml="updated_at: 20260623"),
        )
        result: VerificationResult = _run()
        assert "CK-E003" in _codes(result)

    def test_invalid_iso8601_updated_at_fires(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup(monkeypatch=monkeypatch, repo_root=tmp_path)
        build_task_folder(repo_root=tmp_path, task_id=TASK_ID)
        build_step_tracker(
            repo_root=tmp_path,
            task_id=TASK_ID,
            steps=[{**_STEP_COMPLETED, "completed_at": "2026-06-23T14:00:00Z"}],
        )
        write_text(
            path=_checkpoint_path(repo_root=tmp_path),
            content=self._base_content(updated_at_yaml='updated_at: "not-a-date"'),
        )
        result: VerificationResult = _run()
        assert "CK-E003" in _codes(result)
