import base64
import hashlib
import json

import httpx
import pytest

from archon_horizon.pipeline.worker.skills import materialize_bundle
from archon_horizon.pipeline.worker.sandbox import SandboxPolicy, podman_command
from archon_horizon.pipeline.worker.transport import WorkerTransport


def bundle_file(content=b"Pinned instructions"):
    return {"content_base64": base64.b64encode(content).decode(),
            "sha256": hashlib.sha256(content).hexdigest(), "executable": False}


def digest(bundle):
    return hashlib.sha256(json.dumps(bundle, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def test_bundle_atomic_readonly_verified_reuse_and_narrow_mount(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    bundle = {"schema_version": 1, "files": {"horizon-pipeline/SKILL.md": bundle_file()},
              "entrypoints": ["horizon-pipeline/SKILL.md"]}
    path = materialize_bundle(state, digest(bundle), bundle)
    assert materialize_bundle(state, digest(bundle), bundle) == path
    assert (path / "horizon-pipeline/SKILL.md").read_bytes() == b"Pinned instructions"
    assert not path.stat().st_mode & 0o222
    for name in ("workspace", "home", "scratch"):
        (tmp_path / name).mkdir()
    args = podman_command(SandboxPolicy("image@sha256:" + "a" * 64), workspace=tmp_path / "workspace",
                          provider_home=tmp_path / "home", scratch=tmp_path / "scratch", protected_roots=(state,),
                          command=["true"], name="horizon-test", uid=1000, gid=1000, skill_bundle=path)
    assert f"type=bind,src={path},dst=/horizon-skills,ro" in args
    leaf = path / "horizon-pipeline/SKILL.md"
    leaf.chmod(0o644)
    leaf.write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        materialize_bundle(state, digest(bundle), bundle)


@pytest.mark.parametrize("name", ["../escape", "/escape", "a/../escape", "a//escape", "a\\escape"])
def test_bundle_rejects_noncanonical_paths(tmp_path, name):
    bundle = {"files": {name: bundle_file()}, "entrypoints": [name]}
    with pytest.raises(ValueError, match="path"):
        materialize_bundle(tmp_path, digest(bundle), bundle)


def test_bundle_rejects_symlinks_and_hash_mismatch(tmp_path):
    bundle = {"files": {"horizon/SKILL.md": bundle_file()}, "entrypoints": ["horizon/SKILL.md"]}
    with pytest.raises(ValueError, match="digest"):
        materialize_bundle(tmp_path, "a" * 64, bundle)
    path = materialize_bundle(tmp_path, digest(bundle), bundle)
    parent = path / "horizon"
    parent.chmod(0o755)
    leaf = parent / "SKILL.md"
    leaf.unlink()
    leaf.symlink_to(tmp_path / "private")
    parent.chmod(0o555)
    with pytest.raises(ValueError, match="unexpected"):
        materialize_bundle(tmp_path, digest(bundle), bundle)


def test_transport_loads_large_goal_and_skill_bundle_with_host_auth():
    from archon_horizon.pipeline.worker.contracts import ExecutionGrant

    grant = ExecutionGrant("execution", "assignment", 1, 60, "harness", "workspace", "/workspace", "repo", "placeholder")
    grant_data = {**grant.__dict__, "goal": "", "goal_artifact_id": "artifact"}
    seen = []

    def handler(request):
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer host-only"
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"execution": grant_data})
        if request.url.path.endswith("/content"):
            return httpx.Response(200, json={"goal": "G" * 20000})
        return httpx.Response(200, json={"files": {}})

    transport = WorkerTransport("http://testserver", "host-only", client=httpx.Client(transport=httpx.MockTransport(handler)))
    grant = transport.claim("host", ["harness"])
    assert grant.goal == "" and len(seen) == 1
    assert len(transport.goal(grant)) == 20000
    assert transport.skill_bundle("execution") == {"files": {}}
    assert len(seen) == 3
