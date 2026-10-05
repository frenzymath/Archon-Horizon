import assert from "node:assert/strict";
import { test } from "node:test";
import { missionTree } from "../src/utils/missionTree";



const missions = [
  { id: "leaf", parent_id: "branch", status: "done" },
  { id: "root", parent_id: null, status: "pending" },
  { id: "other", parent_id: null, status: "open" },
  { id: "branch", parent_id: "root", status: "running" },
  { id: "sibling", parent_id: "root", status: "open" },
];
const ids = (result: ReturnType<typeof missionTree>) => result.rows.map(({ mission, depth }) => [mission.id, depth]);

test("missions form contiguous branches even when children arrive before parents", () => {
  assert.deepEqual(ids(missionTree(missions, () => true, new Set())), [
    ["root", 0], ["branch", 1], ["leaf", 2], ["sibling", 1], ["other", 0],
  ]);
});

test("roots and siblings sort newest first by creation date while keeping descendants together", () => {
  const items = [
    { id: "old-root", created_at: "2026-09-08T12:00:00Z", updated_at: "2026-09-12T12:00:00Z" },
    { id: "old-child", parent_id: "new-root", created_at: "2026-09-10T12:00:00+08:00" },
    { id: "new-root", created_at: "2026-09-09T12:00:00Z" },
    { id: "new-child", parent_id: "new-root", created_at: "2026-09-10T05:00:00Z" },
    { id: "grandchild", parent_id: "old-child", created_at: "2026-09-11T12:00:00Z" },
    { id: "undated-child", parent_id: "new-root" },
    { id: "invalid-date", created_at: "invalid" },
    { id: "undated" },
  ];
  const snapshot = structuredClone(items);
  assert.deepEqual(ids(missionTree(items, () => true, new Set())), [
    ["new-root", 0], ["new-child", 1], ["old-child", 1], ["grandchild", 2], ["undated-child", 1], ["old-root", 0], ["invalid-date", 0], ["undated", 0],
  ]);
  assert.deepEqual(items, snapshot, "Sorting must not mutate the API snapshot");
});



test("collapse survives refreshed objects and hides newly arriving descendants", () => {
  const collapsed = new Set(["branch"]);
  const refreshed = [...missions.map((item) => ({ ...item })), { id: "new", parent_id: "branch", status: "open" }];
  assert.deepEqual(ids(missionTree(refreshed, () => true, collapsed)), [
    ["root", 0], ["branch", 1], ["sibling", 1], ["other", 0],
  ]);
  assert.deepEqual(ids(missionTree(refreshed, () => true, new Set(["root"]))), [["root", 0], ["other", 0]]);
});

test("filters retain ancestors of a matching leaf but count only actual matches", () => {
  const result = missionTree(missions, (mission) => mission.status === "done", new Set());
  assert.deepEqual(ids(result), [["root", 0], ["branch", 1], ["leaf", 2]]);
  assert.equal(result.matchingCount, 1);
  assert.deepEqual(result.rows.map((row) => row.matches), [false, false, true]);
  assert.deepEqual(missionTree(missions, () => false, new Set()).rows, []);
});

test("partial and cyclic snapshots remain visible without repeating missions", () => {
  const items = [{ id: "orphan", parent_id: "missing" }, { id: "a", parent_id: "b" }, { id: "b", parent_id: "a" }];
  assert.deepEqual(ids(missionTree(items, () => true, new Set())), [["orphan", 0], ["a", 0], ["b", 1]]);
});

test("deep mission trees do not overflow the JavaScript call stack", () => {
  const items = Array.from({ length: 10000 }, (_, index) => ({ id: String(index), parent_id: index ? String(index - 1) : null }));
  const result = missionTree(items, (mission) => mission.id === "9999", new Set());
  assert.equal(result.rows.length, 10000);
  assert.equal(result.rows.at(-1)?.depth, 9999);
});
