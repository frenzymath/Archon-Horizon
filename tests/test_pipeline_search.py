from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from archon_horizon.pipeline.search import SearchManager, SourceSpec, _tree_bytes


def git(root: Path, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.run(["git", "-c", "user.name=Search test", "-c", "user.email=test@example.invalid",
                           "-c", "commit.gpgsign=false", *args], cwd=root, env=env,
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def source(tmp_path: Path) -> SourceSpec:
    repository = tmp_path / "source"
    repository.mkdir()
    git(repository, "init", "--quiet")
    (repository / "Proof.lean").write_text("/-! Geometry convexity -/\nnamespace Geometry\n/-- Reflexivity of equality. -/\ntheorem convexity (n : Nat) : n = n := rfl\nend Geometry\n")
    git(repository, "add", ".")
    git(repository, "commit", "--quiet", "-m", "Initial source")
    return SourceSpec("project-1", "workspace", repository.as_uri(), git(repository, "rev-parse", "HEAD"))


def manager(tmp_path: Path, **options) -> SearchManager:
    return SearchManager(tmp_path / "cache", local_source_root=tmp_path,
                         max_generation_bytes=1024**2, **options)


def wait_for(manager: SearchManager, source: SourceSpec, timeout: float = 15) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = manager.prepare(source)
        if state["status"] != "preparing":
            return state
        time.sleep(0.02)
    raise AssertionError(f"Search generation never settled: {manager.stats()}")


def next_commit(source: SourceSpec, tmp_path: Path) -> SourceSpec:
    repository = tmp_path / "source"
    file = repository / "Proof.lean"
    file.write_text(file.read_text().replace("convexity", "concavity"))
    git(repository, "add", ".")
    git(repository, "commit", "--quiet", "-m", "Second source")
    return replace(source, commit=git(repository, "rev-parse", "HEAD"))


def test_sources_are_pinned_and_origin_scoped(tmp_path: Path, source: SourceSpec) -> None:
    with manager(tmp_path) as search:
        first = search.generation_key(source)
        assert first != search.generation_key(replace(source, project_id="another-project"))
        for changed in (replace(source, commit="main"), replace(source, subdir="../secret"),
                        replace(source, subdir="Proof//Nested"),
                        replace(source, url="https://example.invalid/repo.git"),
                        replace(source, url="https://user:secret@example.invalid/repo.git"),
                        replace(source, url="file:///etc")):
            with pytest.raises(ValueError):
                search.prepare(changed)
        with pytest.raises(RuntimeError, match="already owns"):
            manager(tmp_path)


def test_cold_search_is_nonblocking_and_concurrent_preparation_coalesces(tmp_path: Path, source: SourceSpec, monkeypatch) -> None:
    with manager(tmp_path, max_pending=1) as search:
        started, release = threading.Event(), threading.Event()
        clone = search._clone
        calls = []
        def slow_clone(*args):
            calls.append(args[0].commit)
            started.set()
            assert release.wait(5)
            return clone(*args)
        monkeypatch.setattr(search, "_clone", slow_clone)
        before = time.monotonic()
        assert search.search(source, "convexity", mode="name")["status"] == "preparing"
        assert time.monotonic() - before < 0.5
        assert started.wait(2)
        try:
            with ThreadPoolExecutor(max_workers=8) as callers:
                states = list(callers.map(lambda _: search.prepare(source), range(20)))
            assert all(state["status"] == "preparing" for state in states)
            assert search.prepare(replace(source, project_id="other"))["reason"] == "background_capacity"
            assert calls == [source.commit]
        finally:
            release.set()
        assert wait_for(search, source)["status"] == "ready"
        for mode, query in (("name", "convexity"), ("text", "reflexivity"), ("type", "Nat"), ("header", "geometry")):
            result = search.search(source, query, mode=mode)
            assert result["indexed_commit"] == source.commit
            assert result["items"][0]["name"] == "Geometry.convexity"
            assert result["items"][0]["file"] == "Proof.lean"


def test_generation_reader_pin_prevents_memory_eviction(tmp_path: Path, source: SourceSpec) -> None:
    with manager(tmp_path) as search:
        assert wait_for(search, source)["status"] == "ready"
        newer = next_commit(source, tmp_path)
        with search.pin(source) as pinned:
            assert pinned is not None
            search.memory_budget_bytes = pinned.memory_bytes + 256
            state = wait_for(search, newer)
            assert state["status"] == "failed"
            assert "pin" in state["error"]
            assert search.search(source, "convexity", mode="name")["items"]
            assert search.stats()["pinned_generations"] == 1
        search.prepare(newer, retry=True)
        assert wait_for(search, newer)["status"] == "ready"
        assert search.search(newer, "concavity", mode="name")["items"]
        assert search.stats()["memory_bytes"] <= search.memory_budget_bytes
        assert (search.generations / search.generation_key(source)).is_dir(), "Memory LRU should preserve reusable disk cache"


def test_disk_eviction_preserves_readers_then_reclaims_only_owned_generation(tmp_path: Path, source: SourceSpec) -> None:
    with manager(tmp_path) as search:
        assert wait_for(search, source)["status"] == "ready"
        newer = next_commit(source, tmp_path)
        old_path = search.generations / search.generation_key(source)
        search.disk_budget_bytes = search.max_generation_bytes
        with search.pin(source):
            assert wait_for(search, newer)["status"] == "failed"
            assert old_path.is_dir()
        search.prepare(newer, retry=True)
        assert wait_for(search, newer)["status"] == "ready"
        assert not old_path.exists()
        assert search.stats()["disk_bytes"] <= search.disk_budget_bytes
        assert _tree_bytes(search.generations) <= search.disk_budget_bytes
        assert not list(search.staging.iterdir())


def test_persistent_generation_reuses_index_and_rebuilds_corruption(tmp_path: Path, source: SourceSpec, monkeypatch) -> None:
    with manager(tmp_path) as search:
        assert wait_for(search, source)["status"] == "ready"
        key = search.generation_key(source)
    with manager(tmp_path) as search:
        def no_clone(*_):
            raise AssertionError("Immutable cache was unnecessarily recloned")
        monkeypatch.setattr(search, "_clone", no_clone)
        assert wait_for(search, source)["status"] == "ready"
        assert search.search(source, "convexity", mode="name")["items"]
    (tmp_path / "cache" / "generations" / key / "index" / "declarations.jsonl").write_text("corrupt cache")
    with manager(tmp_path) as search:
        assert wait_for(search, source)["status"] == "ready"
        assert search.search(source, "convexity", mode="name")["items"]


def test_source_symlink_cannot_read_outside_clone(tmp_path: Path, source: SourceSpec) -> None:
    repository = tmp_path / "source"
    (repository / "Outside.lean").symlink_to("../../private.lean")
    git(repository, "add", ".")
    git(repository, "commit", "--quiet", "-m", "External link")
    linked = replace(source, commit=git(repository, "rev-parse", "HEAD"))
    with manager(tmp_path) as search:
        result = wait_for(search, linked)
        assert result["status"] == "failed"
        assert "symlink" in result["error"]
        assert not list(search.staging.iterdir())


def test_budget_failure_is_visible_not_empty_success(tmp_path: Path, source: SourceSpec) -> None:
    with SearchManager(tmp_path / "small-cache", local_source_root=tmp_path, max_generation_bytes=100,
                       disk_budget_bytes=100, memory_budget_bytes=100) as search:
        result = wait_for(search, source)
        assert result["status"] == "failed"
        assert search.search(source, "convexity", mode="name")["status"] == "failed"
        assert search.stats()["ready_generations"] == 0
        assert not list(search.staging.iterdir())


def test_private_fetch_credentials_are_ephemeral_and_not_part_of_generations(tmp_path: Path, source: SourceSpec, monkeypatch) -> None:
    observed = []
    popen = subprocess.Popen
    secret = "Bearer test-only-ephemeral-secret"
    def observe(command, **kwargs):
        assert not any(secret in str(argument) for argument in command)
        if "fetch" in command:
            observed.append(kwargs["env"].get("GIT_CONFIG_VALUE_0"))
        return popen(command, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", observe)
    with manager(tmp_path, credential_resolver=lambda _: secret) as search:
        assert wait_for(search, source)["status"] == "ready"
        assert observed == [f"Authorization: {secret}"]
        for file in (search.generations / search.generation_key(source)).rglob("*"):
            if file.is_file():
                assert secret.encode() not in file.read_bytes()


def test_shutdown_interrupts_git_fetch_and_releases_cache_owner(tmp_path, source, monkeypatch):
    started = threading.Event()
    processes = []
    popen = subprocess.Popen

    def slow_fetch(command, **kwargs):
        if "fetch" in command:
            process = popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
            processes.append(process)
            started.set()
            return process
        return popen(command, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", slow_fetch)
    search = manager(tmp_path)
    search.prepare(source)
    assert started.wait(5)
    closing = threading.Thread(target=search.close)
    closing.start()
    try:
        closing.join(3)
        assert not closing.is_alive(), "shutdown must not wait for the Git fetch deadline"
        assert processes[0].poll() is not None
        assert not list(search.staging.iterdir())
        assert search.stats()["preparing"] == 0
    finally:
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
        closing.join(5)
    with manager(tmp_path):
        pass


def test_shutdown_while_reopening_cache_preserves_published_generation(tmp_path, source, monkeypatch):
    with manager(tmp_path) as previous:
        assert wait_for(previous, source)["status"] == "ready"
        target = previous.generations / previous.generation_key(source)
    loaded = threading.Event()
    search = manager(tmp_path)
    load_generation = search._load_generation

    def load_then_stop(*args):
        index = load_generation(*args)
        loaded.set()
        assert search._stopping.wait(5)
        return index

    monkeypatch.setattr(search, "_load_generation", load_then_stop)
    search.prepare(source)
    assert loaded.wait(5)
    search.close()
    assert (target / "generation.json").is_file()
    assert (target / "index" / "declarations.jsonl").is_file()
    with manager(tmp_path) as reopened:
        monkeypatch.setattr(reopened, "_clone", lambda *_: pytest.fail("cancelled reload must preserve valid cache"))
        assert wait_for(reopened, source)["status"] == "ready"


def test_index_build_cancellation_skips_remaining_extraction(tmp_path, monkeypatch):
    from archon_horizon.search import index

    for number in range(50):
        (tmp_path / f"Proof{number}.lean").write_text("theorem proof : True := trivial\n")
    cancelled = threading.Event()
    calls = []

    def extract(job):
        calls.append(job)
        cancelled.set()
        return []

    def check():
        if cancelled.is_set():
            raise RuntimeError("cancelled test build")

    monkeypatch.setattr(index, "_extract_source_file", extract)
    with pytest.raises(RuntimeError, match="cancelled test build"):
        index.LeanSearchIndex.build({"source": tmp_path}, check_cancelled=check)
    assert len(calls) < 50
