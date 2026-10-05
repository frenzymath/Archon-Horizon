from archon_horizon.pipeline.instruction_catalog import read_catalog
from test_pipeline_api import api, api_database, auth  # noqa: F401


def test_reviewer_skills_resolve_and_catalog_is_lazy():
    catalog = read_catalog()
    names = {row["name"] for row in catalog["skills"]}
    assert {"horizon-workspace", "horizon-zulip", "source-research", "lean-performance", "statement-alignment"} <= names
    assert len(catalog["reviewers"]) == 11
    assert len(catalog["subagents"]) == 21
    assert {row["slug"] for row in catalog["subagents"] if row["category"] != "reviewers"} == {
        "lean-worker", "refactorer", "forge-integrator", "source-researcher", "page-transcriber",
        "build-checker", "debug", "library-auditor", "orchestration-auditor", "graph-planner"}
    for reviewer in catalog["subagents"]:
        assert set(reviewer["skills"]) <= names
        assert "instructions" not in reviewer
        file = read_catalog(path=reviewer["source_path"])
        assert file["content"].startswith("---\n")
        assert "SKILLS.md" in file["instructions"]
        assert reviewer["slug"] in file["content"]
    for skill in catalog["skills"]:
        assert skill["path"].startswith(skill["category"] + "/")
        assert "content" not in skill
        for resource in skill["resources"]:
            assert resource.startswith(skill["path"].removesuffix("SKILL.md"))


def test_catalog_api_authentication_cache_and_bounded_files(api):
    client, _, _, _, token, host_token = api
    path = "/api/v3/instruction-catalog"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=auth(host_token)).status_code == 403
    response = client.get(path, headers=auth(token))
    assert response.status_code == 200, response.text
    assert "private" in response.headers["cache-control"]
    assert client.get(path, headers={**auth(token), "If-None-Match": response.headers["etag"]}).status_code == 304
    skill = response.json()["skills"][0]
    file = client.get(path + "/file", params={"path": skill["path"]}, headers=auth(token))
    assert file.status_code == 200
    assert "SKILL.md" in file.json()["path"]
    for forbidden in ("../../private/server.json", "/etc/passwd", "missing/SKILL.md"):
        assert client.get(path + "/file", params={"path": forbidden}, headers=auth(token)).status_code == 404
    assert client.get(path + "/file", params={"path": skill["path"]}, headers=auth(host_token)).status_code == 403
