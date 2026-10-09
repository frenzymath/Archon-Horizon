import assert from 'node:assert/strict';
import { demoResponse } from '../src/showcase/transport';

async function main() {
  const account = await demoResponse('/api/v3/auth/me').json();
  assert.equal(account.permissions.write, false);
  assert.equal(account.permissions.admin, false);
  const projects = await demoResponse('/api/v3/projects').json();
  assert.equal(projects.items.length, 1);
  const id = projects.items[0].id;
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
