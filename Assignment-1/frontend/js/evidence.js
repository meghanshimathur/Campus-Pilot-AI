// Evidence view. Shows ONLY what the /api/chat response for the selected turn contains
// (handbook chunks from the search_handbook tool result, plus memory items used/saved).
import { badge, clear, emptyBlock, h } from "./dom.js";
import { extractHandbookSearches, formatCoverage, formatScore, matchHelp } from "./format.js";

function chunkCard(chunk) {
  return h("article", { class: "card evidence-card" },
    h("header", { class: "card-head" },
      h("code", { class: "chunk-id" }, chunk.chunk_id),
      h("span", { class: "card-title" }, chunk.section, chunk.clauses ? ` · clauses ${chunk.clauses}` : "")),
    h("div", { class: "badges" },
      badge(`similarity ${formatScore(chunk.score)}`, "info", "Embedding similarity between the question and this chunk (0–1)"),
      badge(`keyword coverage ${formatCoverage(chunk.keyword_coverage)}`, "neutral", "Share of the question's key terms found in this chunk"),
      chunk.match ? badge(chunk.match, "success", matchHelp(chunk.match)) : null),
    h("blockquote", { class: "excerpt" }, chunk.text),
    h("p", { class: "source" }, `Source: ${chunk.source}`));
}

function searchBlock(search) {
  const heading = h("h4", { class: "block-title" }, "Query sent to the handbook: ", h("q", {}, search.query ?? "—"));
  if (search.state === "found") {
    return h("section", { class: "block" }, heading,
      h("p", { class: "muted" }, `${search.chunks.length} relevant chunk${search.chunks.length === 1 ? "" : "s"} retrieved — only these were sent to the model, not the whole handbook.`),
      ...search.chunks.map(chunkCard));
  }
  if (search.state === "not_found") {
    return h("section", { class: "block" }, heading,
      h("div", { class: "alert alert-info", role: "status" },
        h("strong", {}, "Nothing relevant found in the handbook. "), h("span", {}, "The agent was told not to guess a policy."),
        search.message ? h("p", { class: "muted small" }, search.message) : null));
  }
  return h("section", { class: "block" }, heading,
    h("div", { class: "alert alert-danger", role: "alert" }, `Handbook search failed: ${search.message}`));
}

export function initEvidence({ store }) {
  const root = document.getElementById("panel-evidence");

  function render() {
    clear(root);
    const turn = store.selectedTurn();
    if (!turn) {
      root.append(emptyBlock("No turn selected", "Send a message, then choose a turn to see which handbook excerpts and memory items the agent used."));
      return;
    }
    if (!turn.response) {
      root.append(emptyBlock("No reply for this turn", turn.error ? turn.error.message : "Waiting for a reply…"));
      return;
    }
    const r = turn.response;
    root.append(h("p", { class: "muted small" }, "Shown exactly as returned by POST /api/chat for this turn."));
    const searches = extractHandbookSearches(r);
    if (searches.length === 0) {
      root.append(h("section", { class: "block" }, h("h4", { class: "block-title" }, "Handbook"),
        h("p", { class: "muted" }, "The agent did not search the handbook for this question.")));
    } else {
      searches.forEach((s) => root.append(searchBlock(s)));
    }
    const used = r.memories_used || [];
    const saved = r.memories_saved || [];
    root.append(h("section", { class: "block" }, h("h4", { class: "block-title" }, "Memory"),
      used.length
        ? h("ul", { class: "chips" }, used.map((m) => h("li", {}, badge("recalled", "neutral"), " ", h("code", {}, m.key), ": ", m.value)))
        : h("p", { class: "muted" }, "No remembered preferences were used for this reply."),
      saved.length
        ? h("ul", { class: "chips" }, saved.map((m) => h("li", {}, badge("saved this turn", "success"), " ", h("code", {}, m.key), ": ", m.value)))
        : null));
  }

  store.subscribe((what) => { if (["selection", "turn-completed", "turn-failed", "turn-removed", "reset"].includes(what)) render(); });
  render();
  return { render };
}
