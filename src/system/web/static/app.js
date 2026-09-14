/* The shell: load once, then switch panes and render what the server said.
 *
 * All state here is what came back from a read plus which row is selected. There is no
 * cache that could go stale against the engine and no local copy of a rule: a reload is a
 * re-read, and every screen renders from the same fetched rows, which is why a list and a
 * detail pane cannot disagree about the same opportunity.
 */

import { claim, connect } from "./api.js";
import { button, clear, definitions, el, put, text } from "./dom.js";
import { SCREENS, controlsFor, detailFor, listFor } from "./screens.js";

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
};

let api = null;

function counts() {
  return {
    dashboard: null,
    inbox: state.communications.length || null,
    opportunities: state.opportunities.length || null,
    approvals:
      state.opportunities.filter((row) => row.presentation.queue !== "none").length || null,
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
          /* A superseded review is refused by the engine in its own words. The rest of
           * the opportunity is still worth reading, so the digests block is simply absent
           * rather than the whole pane failing. */
          authorization = null;
        }
      }
      state.detail = { kind: "opportunity", record, sources, audit, authorization };
      if (state.screen === "inbox" || state.screen === "activity") {
        state.screen = "opportunities";
      }
    });
  },
  openCommunication(id) {
    guard(async () => {
      state.selected = id;
      state.detail = { kind: "communication", record: await api.communication(id) };
    });
  },
  showQueue() {
    /* Every queue card leads to the same screen: Approvals is where approval state is
     * read. Which rows belong there is the server's queue, not a filter invented here. */
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
    const [session, opportunities, communications, timeline] = await Promise.all([
      api.session(),
      api.opportunities(),
      api.communications(),
      api.timeline(TIMELINE_LIMIT),
    ]);
    renderSession(session);
    state.opportunities = opportunities;
    state.communications = communications;
    state.timeline = timeline;
    state.byId = new Map(opportunities.map((row) => [row.id, row]));
  });
}

start();
