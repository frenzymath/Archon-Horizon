import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { demoResponse } from '../src/showcase/transport';

async function main() {
  const account = await demoResponse('/api/v3/auth/me').json();
  assert.equal(account.permissions.write, false);
  assert.equal(account.permissions.admin, true);
  assert.equal(account.role, 'admin');
  const projects = await demoResponse('/api/v3/projects').json();
  assert.equal(projects.items.length, 1);
  const id = projects.items[0].id;
  const resources = await demoResponse('/api/v3/resources').json();
  assert.equal(resources.hosts.length, 2);
  assert.equal(resources.hosts[0].occupied_slots, 1);
  assert.equal(resources.hosts[0].slots, 4);
  assert.equal(resources.hosts[1].mode, 'draining');
  const hosts = await demoResponse('/api/v3/records/host').json();
  assert.equal(hosts.items[0].id, resources.hosts[0].id);
  const bindings = await demoResponse(`/api/v3/hosts/${hosts.items[0].id}/harnesses`).json();
  assert.equal(bindings.items.length, 2);
  assert.equal(bindings.items[0].credential_configured, false);
  const harnesses = await demoResponse('/api/v3/records/harness').json();
  assert.deepEqual(harnesses.items.map((item: {adapter: string}) => item.adapter), ['codex_exec', 'claude_exec']);
  const reviewers = await demoResponse(`/api/v3/records/reviewer_descriptor?project_id=${id}`).json();
  assert.equal(reviewers.items[0].slug, 'mathematical-fidelity');
  const otherReviewers = await demoResponse('/api/v3/records/reviewer_descriptor?project_id=unknown').json();
  assert.deepEqual(otherReviewers.items, []);
  const catalog = await demoResponse('/api/v3/instruction-catalog').json();
  assert.equal(catalog.skills.length, 6);
  assert.equal(catalog.subagents.length, 3);
  assert.equal(catalog.prompts.length, 1);
  for (const path of catalog.files) {
    const file = await demoResponse(`/api/v3/instruction-catalog/file?path=${encodeURIComponent(path)}`).json();
    assert.equal(file.path, path);
    assert.ok(file.content.length > 20);
    assert.equal(file.sha256, createHash('sha256').update(file.content).digest('hex'));
  }
  assert.equal(demoResponse('/api/v3/instruction-catalog/file?path=../../private').status, 404);
  const integrations = await demoResponse(`/api/v3/projects/${id}/integrations`).json();
  assert.deepEqual(integrations.items.map((item: {kind: string}) => item.kind), ['forge', 'zulip']);
  for (const item of integrations.items) {
    assert.equal(item.enabled, true);
    assert.equal(item.browser_session, false);
    assert.match(item.public_url, /^\.\/integrations\/(forge|zulip)\.html$/);
  }
  const nodes = await demoResponse(`/api/v3/projects/${id}/dashboard/nodes?search=empty`).json();
  assert.equal(nodes.total, 1);
  assert.equal(nodes.nodes[0].id, 'sum-zero');
  const milestones = await demoResponse(`/api/v3/projects/${id}/dashboard/nodes?milestone=true`).json();
  assert.equal(milestones.total, 1);
  assert.ok(milestones.nodes[0].labels.includes('milestone'));
  assert.deepEqual(milestones.types, ['claim']);
  const customType = await demoResponse(`/api/v3/projects/${id}/dashboard/nodes?node_type=definition`).json();
  assert.equal(customType.total, 0);
  assert.equal(demoResponse('/api/v3/commands', 'POST').status, 403);
  assert.equal(demoResponse(`/api/v3/projects/${id}/integrations/demo-forge/web-session`, 'POST').status, 403);
  assert.equal(demoResponse('/api/v3/records/host/example-host', 'PATCH').status, 403);
  assert.equal(demoResponse('https://external.invalid/private').status, 404);
  assert.equal(demoResponse('/api/v3/unknown').status, 404);
  const references = await demoResponse('/api/v3/references').json();
  const reference = references.items[0];
  assert.deepEqual(reference.urls, []);
  assert.deepEqual(reference.identifiers, {});
  assert.match(await demoResponse(`/api/v3/references/${reference.id}/bibtex`).text(), /Invented demonstration data/);
  console.log('Synthetic demo transport checks passed.');
}
void main();
