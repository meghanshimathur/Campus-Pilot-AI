// Small observable store. All state lives in memory only: raw tool results can contain student records,
// so nothing is written to localStorage/sessionStorage.

export function createStore() {
  const listeners = new Set();
  const state = {
    studentId: "",
    sessionId: null,     // assigned by the server on the first reply of a session
    turns: [],           // {id, userText, response|null, error|null}
    selectedTurnId: null,
    busy: false,
  };
  let nextId = 1;

  const emit = (what) => listeners.forEach((fn) => fn(what, state));

  return {
    state,
    subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); },
    setStudentId(id) {
      if (id === state.studentId) return false;
      state.studentId = id;
      emit("student");
      return true;
    },
    resetConversation() {
      state.turns = [];
      state.selectedTurnId = null;
      state.sessionId = null;
      emit("reset");
    },
    setBusy(busy) { state.busy = busy; emit("busy"); },
    addTurn(userText) {
      const turn = { id: nextId++, userText, response: null, error: null };
      state.turns.push(turn);
      emit("turn-added");
      return turn;
    },
    completeTurn(turn, response) {
      turn.response = response;
      if (response.session_id) state.sessionId = response.session_id;
      state.selectedTurnId = turn.id;
      emit("turn-completed");
    },
    failTurn(turn, error) {
      turn.error = error;
      emit("turn-failed");
    },
    removeTurn(turn) {
      state.turns = state.turns.filter((t) => t.id !== turn.id);
      if (state.selectedTurnId === turn.id) state.selectedTurnId = null;
      emit("turn-removed");
    },
    selectTurn(id) { state.selectedTurnId = id; emit("selection"); },
    selectedTurn() { return state.turns.find((t) => t.id === state.selectedTurnId) ?? null; },
  };
}
