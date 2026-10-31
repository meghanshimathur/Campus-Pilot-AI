// Small observable store. All state lives in memory only: raw tool results can contain student records,
// so nothing is written to localStorage/sessionStorage. The sign-in TOKEN is not kept here at all (see api.js); the store
// holds only who is signed in, so a state dump can never leak a credential.

export function createStore() {
  const listeners = new Set();
  const state = {
    user: null,          // {user_id, role, student_id, display_name} from the sign-in response; never a token or password
    studentId: "",       // the signed-in student's own id (from the server), "" for staff or when signed out
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
    // Sign-in: only the fields the UI needs. The student id always comes from the server's answer, never from user input.
    setUser(user) {
      state.user = user ? { user_id: user.user_id, role: user.role, student_id: user.student_id ?? null, display_name: user.display_name ?? "" } : null;
      state.studentId = state.user?.student_id ?? "";
      emit("signin");
    },
    // Sign-out / expired session: forget the user AND everything that was shown to them (conversation, selection, session).
    signOut() {
      state.user = null;
      state.studentId = "";
      state.turns = [];
      state.selectedTurnId = null;
      state.sessionId = null;
      state.busy = false;
      emit("signout");
    },
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
