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
  /* The launch's approval destination, read once from /session. Null until then, which is
   * why the packet withholds its controls rather than guessing a destination. */
  target: null,
  /* Whether this launch can reach that destination. False until /session says otherwise, so
   * the outward controls are withheld rather than offered on an assumption. */
  outward: false,
  /* Which sources this launch can take material in from, read once from /intake/sources.
   * Null until then, so the intake controls are withheld rather than guessed at -- and they
   * are decided by this answer alone, never inferred from whether drafting is configured.
   * Read authority and draft authority are different grants. */
  sources: null,
  /* The last intake report, exactly as the server wrote it, or null. Nothing here decides
   * what it meant. */
  intake: null,
  /* What the operator last submitted, so a rebuilt panel does not silently discard it. It is
   * what they typed, never anything derived from a message. */
  intakeForm: { namespace: "", query: "", labels: "", limit: 25 },
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

async function reloadAll() {
  /* Everything new evidence can move, which is more than a decision can.
   *
   * A message arriving can create an opportunity, produce a new current review, and make an
   * approval that already stood no longer bind -- so Inbox, Opportunities and Approvals are
   * all re-read from the server. None of that is worked out here: which review is current and
   * whether an approval still binds are the engine's answers, and a browser that patched its
   * own rows would be deciding them a second time, differently.
   */
  state.communications = await api.communications();
  await reload();
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
    /* Where an approval recorded from this page would say a draft may go. A launch fact,
     * read once at start and never from a control: the packet shows it so the operator can
     * see what they are authorizing, and the server uses its own copy regardless. */
    target: state.target,
    outward: state.outward,
    composing: false,
    refusal: null,
    decisionRefusal: null,
    attempt: null,
    ...extra,
  };
}

function decisionRefused(answer) {
  /* What the server found, in the operator's terms.
   *
   * `binding_conflict` is not a failed request. It says the packet moved between the
   * moment it was rendered and the moment the decision was recorded, and that nothing was
   * written -- no decision row, no audit event. The fields that moved are named, because
   * "something changed" is not enough to decide against.
   */
  if (answer.status === 409 && answer.body.error === "binding_conflict") {
    const moved = Object.keys(answer.body.observed || {})
      .filter((field) => answer.body.expected[field] !== answer.body.observed[field])
      .sort();
    return (
      "This packet changed since it was shown: " +
      moved.join(", ") +
      ". Nothing was written and the decision on record is unchanged. " +
      "Read the packet again and decide against what it says now."
    );
  }
  if (answer.status === 409) {
    return answer.body.detail || "The decision was refused by the current state.";
  }
  return answer.body.error || "The decision was refused.";
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
  decide(review, approved, expected) {
    /* Send, then re-read -- on success and on refusal alike. Nothing here patches the
     * approval text, the queue, a badge count, the stale/current state or the timeline:
     * what those become is the engine's to decide, and a local edit would be this browser
     * forming a second opinion about state it just asked the server to change.
     *
     * On a binding conflict the packet is re-read first and the refusal is shown against
     * the new one. The approval is never resubmitted with the newer expectation: that
     * would approve a packet the operator never saw, which is the entire failure the
     * expectation exists to prevent.
     */
    const id = state.selected;
    guard(async () => {
      const answer = await api.decide(review, approved ? { approved, expected } : { approved });
      await reload();
      state.detail = await opportunityDetail(id, {
        decisionRefusal: answer.ok ? null : decisionRefused(answer),
      });
    });
  },
  outward(review, command) {
    /* Create a draft, or reconcile one, then re-read -- on every outcome alike.
     *
     * Nothing here decides what the answer meant. The outcome name, the sentence describing
     * it and the sentence saying what to do next are all the server's, written by the same
     * code the command line reports from: an unknown outcome and a proven rejection are
     * different facts about a mailbox, and a browser paraphrasing either is a second opinion
     * about whether something exists out there.
     *
     * Nothing is ever retried here. A repeat is the operator asking again, deliberately,
     * which is the only way a second attempt may ever begin.
     */
    const id = state.selected;
    guard(async () => {
      const answer = command === "draft" ? await api.draft(review) : await api.reconcile(review);
      await reload();
      state.detail = await opportunityDetail(id, { attempt: answer.body });
    });
  },
  setIntakeForm(supplied) {
    /* Held only so a rebuilt panel can show what was last submitted. Nothing downstream reads
     * it: each command is sent the values read off the controls at the moment it was pressed. */
    state.intakeForm = { ...state.intakeForm, ...supplied };
  },
  importEml(namespace, message) {
    /* Send the bytes the operator chose, then re-read. Nothing here parses the message,
     * inspects it to decide whether it is a job, or reports anything the server did not: what
     * the extractor made of it is the extractor's answer, diagnostics included.
     *
     * A refusal is kept and shown rather than retried. Whether the material was unusable or
     * this launch cannot take anything in, asking again unchanged would only produce the same
     * answer -- and pressing the button again is the operator's to do.
     */
    guard(async () => {
      const answer = await api.ingestEml(namespace, message);
      if (answer.ok) {
        await reloadAll();
      }
      state.intake = { ok: answer.ok, status: answer.status, report: answer.body };
    });
  },
  importGmail(bounds) {
    /* The same shape, against a mailbox. The batch is read whole by the server before
     * anything is written, so a failure here means nothing from that attempt was stored --
     * which is the server's guarantee, reported, not one assumed on its behalf.
     */
    guard(async () => {
      const answer = await api.ingestGmail(bounds);
      if (answer.ok) {
        await reloadAll();
      }
      state.intake = { ok: answer.ok, status: answer.status, report: answer.body };
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
    const [session, communications, vocabulary, sources] = await Promise.all([
      api.session(),
      api.communications(),
      api.statuses(),
      api.intakeSources(),
    ]);
    renderSession(session);
    state.target = session.decision_target;
    /* Whether this launch can reach that destination at all. Approving never needed a
     * credential and still does not, so a launch may record decisions and not act on them;
     * the controls say so rather than offering a button that could only ever refuse. */
    state.outward = session.outward;
    state.communications = communications;
    state.statuses = vocabulary.statuses;
    /* Which intake controls exist at all. Decided by this one answer, so a launch that cannot
     * reach a mailbox is never offered a button that could only ever refuse. */
    state.sources = sources;
    await reload();
  });
}

start();
