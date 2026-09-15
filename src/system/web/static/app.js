/* The shell: load once, then switch panes and render what the server said.
 *
 * All state here is what came back from a read plus which row is selected. There is no
 * cache that could go stale against the engine and no local copy of a rule: a reload is a
 * re-read, and every screen renders from the same fetched rows, which is why a list and a
 * detail pane cannot disagree about the same opportunity.
 */

import { claim, connect } from "./api.js";
import { button, clear, definitions, el, put, text } from "./dom.js";
import { SCREENS, controlsFor, detailFor, inApprovals, listFor } from "./screens.js";

const ORDER = ["dashboard", "inbox", "opportunities", "approvals", "activity"];
const TIMELINE_LIMIT = 200;

const state = {
  screen: "dashboard",
  selected: null,
  detail: null,
  opportunities: [],
  communications: [],
  timeline: [],
  byId: new Map(),
  search: "",
  activeOnly: false,
  floor: 0,
  failure: null,
  statuses: [],
};

let api = null;

function counts() {
  return {
    dashboard: null,
    inbox: state.communications.length || null,
    opportunities: state.opportunities.length || null,
    /* The same predicate the Approvals screen filters by, so the badge can never promise
     * a row the screen will not show. */
    approvals: state.opportunities.filter(inApprovals).length || null,
    activity: state.timeline.length || null,
  };
}

function renderNav() {
  const nav = clear(document.getElementById("nav"));
  const sizes = counts();
  for (const name of ORDER) {
    const item = el("li");
    const control = button("nav-item" + (state.screen === name ? " current" : ""), () =>
      show(name),
    );
    control.setAttribute("aria-current", state.screen === name ? "page" : "false");
    put(control, el("span", "nav-label", SCREENS[name].title));
    if (sizes[name]) {
      put(control, el("span", "nav-count", sizes[name]));
    }
    put(nav, put(item, control));
  }
}

function renderSession(facts) {
  const pairs = [
    ["db", facts.database, "mono"],
    ["sqlite", facts.sqlite, "mono"],
    ["read token", facts.gmail_token ? "present" : "absent", "mono"],
    ["compose token", facts.gmail_compose_token ? "present" : "absent", "mono"],
  ];
  const holder = document.getElementById("session-facts");
  holder.replaceWith(Object.assign(definitions(pairs, "facts"), { id: "session-facts" }));
}

function render() {
  const screen = SCREENS[state.screen];
  const subtitle = document.getElementById("list-subtitle");
  text(document.getElementById("list-title"), screen.title);
  text(subtitle, state.failure || screen.subtitle);
  subtitle.classList.toggle("failed", Boolean(state.failure));
  renderNav();
  controlsFor(state, actions);
  const list = clear(document.getElementById("list-body"));
  put(list, listFor(state, actions));
  const detail = clear(document.getElementById("detail"));
  put(detail, detailFor(state, actions));
}

function show(name) {
  state.screen = name;
  render();
}

async function reload() {
  /* One re-read of everything a status change can move, so no screen is left describing a
   * state that no longer exists. */
  const [opportunities, timeline] = await Promise.all([
    api.opportunities(),
    api.timeline(TIMELINE_LIMIT),
  ]);
  state.opportunities = opportunities;
  state.timeline = timeline;
  state.byId = new Map(opportunities.map((row) => [row.id, row]));
}


async function opportunityDetail(id, extra = {}) {
  /* One assembler for the detail pane, so the pane after a command is built exactly the
   * way the pane after a click is. Two paths would be two chances to disagree. */
  const record = await api.opportunity(id);
  const sources = await api.sources(id);
  const audit = state.timeline.filter(
    (event) => event.kind === "audit" && event.review === record.review,
  );
  let authorization = null;
  if (record.review) {
    try {
      authorization = await api.authorization(record.review);
    } catch {
      /* A superseded review is refused by the engine in its own words. The rest of the
       * opportunity is still worth reading, so the digests block is simply absent rather
       * than the whole pane failing. */
      authorization = null;
    }
  }
  return {
    kind: "opportunity",
    record,
    sources,
    audit,
    authorization,
    statuses: state.statuses,
    composing: false,
    refusal: null,
    ...extra,
  };
}

function refused(answer) {
  /* What the server found, in the operator's terms. A conflict is not a failure of the
   * request: it says the thing they were looking at has moved. */
  if (answer.status === 409) {
    return (
      "This opportunity changed since it was shown: it is now " +
      `${answer.body.status} at event ${answer.body.observed_event_id}. ` +
      "Nothing was written. Decide again against what it says now."
    );
  }
  return answer.body.error || "The command was refused.";
}


async function guard(work) {
  try {
    await work();
    state.failure = null;
  } catch (failure) {
    state.failure = "The local surface refused this request: " + failure.message;
  }
  render();
}

const actions = {
  openOpportunity(id) {
    guard(async () => {
      state.selected = id;
      state.detail = await opportunityDetail(id);
      if (state.screen === "inbox" || state.screen === "activity") {
        state.screen = "opportunities";
      }
    });
  },
  composeStatus() {
    state.detail = { ...state.detail, composing: true, refusal: null };
    render();
  },
  cancelStatus() {
    state.detail = { ...state.detail, composing: false, refusal: null };
    render();
  },
  recordStatus(payload) {
    /* Send, then re-read. Nothing here patches the row, the status, the queue or a count:
     * what those become is the engine's to decide, and a local edit would be this browser
     * forming a second opinion about state it just asked the server to change.
     *
     * A refusal is re-read too, and the form stays open on the new state. The operator was
     * acting on something that has moved, so the first thing they need is what it moved
     * to -- and the command is never retried against the newer event, because deciding
     * again is theirs to do.
     */
    const id = state.selected;
    guard(async () => {
      const answer = await api.recordStatus(id, payload);
      await reload();
      state.detail = await opportunityDetail(id, {
        composing: !answer.ok,
        refusal: answer.ok ? null : refused(answer),
      });
    });
  },
  openCommunication(id) {
    guard(async () => {
      state.selected = id;
      state.detail = { kind: "communication", record: await api.communication(id) };
    });
  },
  showApprovals() {
    /* Only offered by a card whose queue Approvals actually lists; the screen and the
     * card share one definition of which those are. */
    state.screen = "approvals";
    state.selected = null;
    render();
  },
  setSearch(value) {
    state.search = value;
    render();
  },
  toggleActive() {
    state.activeOnly = !state.activeOnly;
    render();
  },
  setFloor(value) {
    state.floor = value;
    render();
  },
};

async function start() {
  const credential = claim();
  if (!credential) {
    const subtitle = document.getElementById("list-subtitle");
    text(
      subtitle,
      "Open the address CareerSignal printed when it started; this tab has no launch token.",
    );
    subtitle.classList.add("failed");
    return;
  }
  api = connect(credential);
  await guard(async () => {
    const [session, communications, vocabulary] = await Promise.all([
      api.session(),
      api.communications(),
      api.statuses(),
    ]);
    renderSession(session);
    state.communications = communications;
    state.statuses = vocabulary.statuses;
    await reload();
  });
}

start();
