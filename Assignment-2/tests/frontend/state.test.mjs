import test from "node:test";
import assert from "node:assert/strict";
import { createStore } from "../../frontend/js/state.js";

test("turn lifecycle and selection", () => {
  const s = createStore();
  const events = []; s.subscribe((w) => events.push(w));
  s.setStudentId("STU001");
  const t1 = s.addTurn("hello");
  s.completeTurn(t1, { session_id: "abc", response: "hi" });
  assert.equal(s.state.sessionId, "abc");
  assert.equal(s.selectedTurn().id, t1.id);
  const t2 = s.addTurn("again");
  s.failTurn(t2, new Error("boom"));
  assert.equal(t2.error.message, "boom");
  s.selectTurn(t2.id);
  assert.equal(s.selectedTurn().id, t2.id);
  s.removeTurn(t2);
  assert.equal(s.selectedTurn(), null);
  assert.deepEqual(events, ["student", "turn-added", "turn-completed", "turn-added", "turn-failed", "selection", "turn-removed"]);
});

test("reset clears conversation and session but keeps student id", () => {
  const s = createStore();
  s.setStudentId("STU002");
  s.completeTurn(s.addTurn("q"), { session_id: "abc" });
  s.resetConversation();
  assert.deepEqual([s.state.turns.length, s.state.sessionId, s.state.selectedTurnId, s.state.studentId], [0, null, null, "STU002"]);
});

test("setStudentId reports whether it changed", () => {
  const s = createStore();
  assert.equal(s.setStudentId("A"), true);
  assert.equal(s.setStudentId("A"), false);
});

test("state is kept in memory only (nothing written to browser storage)", () => {
  globalThis.localStorage = { setItem() { throw new Error("must not be used"); } };
  globalThis.sessionStorage = { setItem() { throw new Error("must not be used"); } };
  const s = createStore();
  s.completeTurn(s.addTurn("q"), { session_id: "x" });
  delete globalThis.localStorage; delete globalThis.sessionStorage;
});
