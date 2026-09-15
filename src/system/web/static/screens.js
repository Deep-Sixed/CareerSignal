/* The five frozen screens, built from values the server already decided.
 *
 * What this file is allowed to do is render, count, filter what it was given, and switch
 * panes. What it must never do is work out whether an approval is stale, whether something
 * needs reconciling, whether a draft could be created, what REFUSED means, whether an
 * opportunity advances, or which state outranks another. Every one of those is a rule, and
 * the rules live in Python: they arrive here as `presentation.coverage`, `presentation.queue`,
 * `presentation.approval` and `presentation.attempt`, already rendered.
 *
 * The tables below are display copy keyed by a name the server sent. They carry no
 * precedence, no fallthrough and no arithmetic: an unrecognised name renders verbatim
 * rather than being interpreted, so this file cannot quietly disagree with the engine.
 */

import {
  button,
  chip,
  clear,
  definitions,
  el,
  link,
  paragraphs,
  put,
  shorten,
  text,
  visible,
} from "./dom.js";

/* Display copy only. The queue itself is decided by system.views.queue(). */
const QUEUE_LABELS = {
  reconcile: "reconcile",
  created: "draft created",
  stale: "stale approval",
  draft: "ready to draft",
  decide: "awaiting decision",
  rejected: "rejected",
};
const QUEUE_HINTS = {
  reconcile: "a draft was attempted and the outcome is unknown",
  created: "a draft exists in the destination mailbox",
  stale: "the packet moved after the approval; it binds nothing until reapproved",
  draft: "approved, still binding, and not yet attempted",
  decide: "advances the threshold and is undecided",
  rejected: "declined; nothing was created",
};
/* Which queues make approval state worth showing on the Approvals screen. Fixed by the
 * PR contract rather than derived here, and exported because the nav badge and the queue
 * cards have to agree with the screen itself: a count the operator cannot then see is a
 * worse answer than no count at all. */
export const APPROVAL_QUEUES = ["decide", "stale", "draft", "reconcile", "created"];

export function inApprovals(row) {
  return APPROVAL_QUEUES.includes(row.presentation.queue);
}
/* Copy limited to what the queue itself guarantees.
 *
 * The queue is the only fact behind these panels, so the wording may not name a cause the
 * server did not report. `stale` means an approval no longer binds -- a later message can
 * do that, and so can a status event, and the packet does not say which. `reconcile` means
 * an attempt is unsettled; reconciliation does not create a second draft, but it does
 * record what it finds, so it is not true that it writes nothing. Where the operator needs
 * the cause, the before-and-now digests below say it exactly. */
const ATTENTION = {
  reconcile: [
    "action outcome",
    "The outcome of a draft attempt is unknown",
    "A draft-create request may have reached the provider. CareerSignal never retries it " +
      "automatically; reconciliation reads the destination for this review's intent key " +
      "and does not create another draft.",
  ],
  stale: [
    "approval stale",
    "The approval on record no longer matches this material",
    "The current packet differs from the one that was approved; compare the approved and " +
      "now values below. The record of who approved stays -- it authorizes nothing until " +
      "the current packet is approved.",
  ],
};

export function label(queue) {
  return QUEUE_LABELS[queue] || queue;
}

/* --- time ------------------------------------------------------------------------------- */

const MINUTE = 60;
const UNITS = [
  ["year", 365 * 24 * 60 * MINUTE],
  ["month", 30 * 24 * 60 * MINUTE],
  ["day", 24 * 60 * MINUTE],
  ["hour", 60 * MINUTE],
  ["minute", MINUTE],
];

export function ago(seconds) {
  /* Date formatting, which is the browser's own business. */
  if (seconds === null || seconds === undefined) {
    return "";
  }
  const elapsed = Math.max(0, Math.floor(Date.now() / 1000) - Number(seconds));
  for (const [unit, size] of UNITS) {
    if (elapsed >= size) {
      const count = Math.floor(elapsed / size);
      return count + " " + unit + (count === 1 ? "" : "s") + " ago";
    }
  }
  return "just now";
}

export function stamp(seconds) {
  if (seconds === null || seconds === undefined) {
    return "";
  }
  return new Date(Number(seconds) * 1000).toISOString().replace("T", " ").slice(0, 19);
}

/* --- small shared pieces ----------------------------------------------------------------- */

function address(value, modifier = "") {
  /* Keep the domain. It carries the trust in an untrusted From, and it is what tells two
   * messages from the same display name apart, so when a row is too narrow the local part
   * is what gives way -- the domain is never the part that disappears.
   */
  const text = String(value ?? "");
  const at = text.lastIndexOf("@");
  const holder = el("span", ("address " + modifier).trim());
  if (at === -1) {
    return put(holder, el("span", "address-local", text));
  }
  return put(
    holder,
    el("span", "address-local", text.slice(0, at)),
    el("span", "address-domain", text.slice(at)),
  );
}

function queueChip(row) {
  const queue = row.presentation.queue;
  if (queue === "none") {
    /* Nothing is waiting on the operator. `advances` is a stored boolean, rendered as it
     * arrived, exactly like `eligible`. */
    return row.advances === false ? chip("does not advance", "quiet") : null;
  }
  return chip(label(queue), "queue-" + queue);
}

function empty(message) {
  return el("p", "empty", message);
}

function section(title, ...children) {
  return put(el("section", "block"), el("h3", "block-title", title), ...children);
}

/* --- list rows ---------------------------------------------------------------------------- */

function opportunityRow(row, selected, onSelect) {
  const node = button("row" + (selected ? " selected" : ""), () => onSelect(row.id));
  node.setAttribute("aria-current", selected ? "true" : "false");
  const head = put(
    el("div", "row-head"),
    el("span", "row-company", row.company),
    el("span", "row-coverage", row.presentation.coverage),
  );
  const tags = put(el("div", "row-tags"), chip(row.status, "status"), queueChip(row));
  put(tags, el("span", "row-when", ago(row.status_changed_at)));
  return put(node, head, el("div", "row-title", row.title), tags);
}

function communicationRow(row, selected, onSelect) {
  const node = button("row" + (selected ? " selected" : ""), () => onSelect(row.message));
  node.setAttribute("aria-current", selected ? "true" : "false");
  const head = put(el("div", "row-head"), address(row.sender));
  put(head, el("span", "row-namespace", row.namespace));
  const tags = put(el("div", "row-tags"), chip(row.extracted + " extracted", "quiet"));
  if (row.unextracted) {
    put(tags, chip(row.unextracted + " not extracted", "warn"));
  }
  put(tags, el("span", "row-when", row.format));
  return put(node, head, el("div", "row-title", row.subject), tags);
}

/* --- screens ------------------------------------------------------------------------------ */

function tally(rows, key) {
  /* Counting facts, in the order the server returned them. The server sorts opportunities
   * by pipeline stage, so first appearance is pipeline order and nothing here re-sorts:
   * an order is a rule, and this screen does not own it.
   */
  const counts = new Map();
  for (const row of rows) {
    const name = key(row);
    counts.set(name, (counts.get(name) || 0) + 1);
  }
  return [...counts.entries()];
}

function dashboard(state, actions) {
  const body = el("div", "dashboard");
  const statuses = put(el("div", "cells"));
  for (const [status, count] of tally(state.opportunities, (row) => row.status)) {
    put(statuses, put(el("div", "cell"), el("div", "cell-n", count), el("div", "cell-label", status)));
  }
  put(body, section("Pipeline", state.opportunities.length ? statuses : empty("Nothing ingested yet.")));

  const queues = el("div", "queues");
  const counted = tally(state.opportunities, (row) => row.presentation.queue).filter(
    ([queue]) => queue !== "none",
  );
  for (const [queue, count] of counted) {
    /* Only a queue Approvals actually shows is a way in. A card for one it does not --
     * `rejected` today -- reports the count and goes nowhere, because sending the
     * operator to a screen the row is absent from is worse than not offering the trip. */
    const routes = APPROVAL_QUEUES.includes(queue);
    const card = routes
      ? button("queue-card", () => actions.showApprovals())
      : el("div", "queue-card quiet-card");
    put(
      card,
      el("span", "queue-n", count),
      put(
        el("span", "queue-text"),
        el("span", "queue-label", label(queue)),
        el("span", "queue-hint", QUEUE_HINTS[queue] || ""),
      ),
    );
    put(queues, card);
  }
  put(body, section("Work queues", counted.length ? queues : empty("No queue is waiting on you.")));

  const recent = el("div", "events");
  for (const event of state.timeline.slice(0, 8)) {
    put(recent, activityRow(event, state));
  }
  put(body, section("Recent activity", state.timeline.length ? recent : empty("No activity yet.")));
  return body;
}

function activityRow(event, state) {
  const node = el("div", "event");
  const head = put(
    el("div", "event-head"),
    el("span", "event-when", ago(event.created_at)),
    chip(event.kind, "kind-" + event.kind),
  );
  const named = state.byId.get(event.opportunity);
  const subject = named ? named.company + " — " + named.title : event.opportunity;
  put(node, head, el("div", "event-text", visible(subject) + " → " + visible(event.event)));
  const parts = [event.kind + " " + event.event_id];
  if (event.actor) {
    parts.push(event.actor);
  }
  put(node, el("div", "event-sub", parts.join(" · ")));
  if (event.reason) {
    put(node, paragraphs(event.reason, "event-reason"));
  }
  return node;
}

/* --- detail: one opportunity ---------------------------------------------------------------- */

function attention(record) {
  const copy = ATTENTION[record.presentation.queue];
  if (!copy) {
    return null;
  }
  const [kicker, title, body] = copy;
  return put(
    el("div", "attention"),
    el("div", "attention-kicker", kicker),
    el("div", "attention-title", title),
    el("div", "attention-body", body),
  );
}

function statusAction(record, extra, actions) {
  /* The one thing this surface can change, anchored to the opportunity rather than to the
   * approval packet below: a status is opportunity authority, and an approval is not.
   */
  const holder = el("div", "action");
  if (!extra.composing) {
    const open = button("primary", () => actions.composeStatus());
    text(open, "Change status");
    return put(holder, open);
  }
  const form = el("div", "action-form");
  put(
    form,
    el(
      "p",
      "action-note",
      `Recorded against event ${record.status_event} (${record.status}). This appends a ` +
        "history event; it does not edit what is already there. If the opportunity has " +
        "moved since this was read, nothing is written.",
    ),
  );

  const chooser = el("label", "field");
  put(chooser, el("span", "field-label", "Status"));
  const choices = el("select", "status-choice");
  for (const name of extra.statuses || []) {
    const option = el("option", null, name);
    option.value = name;
    if (name === record.status) {
      option.selected = true;
    }
    choices.append(option);
  }
  put(chooser, choices);

  const note = el("label", "field");
  put(note, el("span", "field-label", "Reason (optional, kept verbatim)"));
  const reason = el("textarea", "status-reason");
  reason.rows = 3;
  reason.value = extra.reason || "";
  put(note, reason);

  const cancel = button("secondary", () => actions.cancelStatus());
  text(cancel, "Cancel");
  const record_ = button("primary", () =>
    actions.recordStatus({
      status: choices.value,
      reason: reason.value,
      expected_event_id: record.status_event,
    }),
  );
  text(record_, "Record status");

  put(form, chooser, note, put(el("div", "action-buttons"), cancel, record_));
  if (extra.refusal) {
    put(form, el("p", "action-refusal", extra.refusal));
  }
  return put(holder, form);
}


function opportunityDetail(record, extra, actions) {
  const body = el("div", "detail-pane");
  put(
    body,
    el("div", "eyebrow", "Opportunity · " + shorten(record.id, 14)),
    el("h2", "detail-title", record.company + " — " + record.title),
  );
  const tags = put(
    el("div", "detail-tags"),
    chip(record.status, "status"),
    chip("event " + record.status_event, "quiet"),
    chip("coverage " + record.presentation.coverage, "quiet"),
    chip("eligible " + (record.eligible ? "yes" : "no"), "quiet"),
  );
  if (record.advances !== null) {
    put(tags, chip(record.advances ? "advances" : "does not advance", "quiet"));
  }
  put(body, tags, put(el("div", "detail-url"), link(record.url)));
  put(body, statusAction(record, extra, actions));
  put(body, attention(record));

  const packet = record.packet;
  const reasons = el("ul", "reasons");
  if (packet && packet.reasons) {
    for (const reason of packet.reasons) {
      put(reasons, el("li", null, reason));
    }
  }
  put(
    body,
    section(
      "Review evidence",
      packet ? reasons : empty("No current review — nothing an approval could bind."),
      definitions([
        ["skills stated", record.stated_skills],
        ["skills matched", record.matched_skills],
        ["location", record.location],
      ]),
    ),
  );

  const bound = record.bound;
  const packetBlock = bound
    ? definitions([
        ["review", shorten(bound.review, 14)],
        ["source", bound.source ? shorten(bound.source, 18) : "—"],
        ["recipient", bound.to || "—"],
        ["subject", bound.subject || "—"],
        ["destination", record.action.provider + " · " + record.action.provider_namespace],
      ])
    : empty("No current review — nothing an approval could bind.");
  const wording = bound ? paragraphs(bound.wording, "wording") : null;
  put(
    body,
    section(
      "Approval packet",
      el("p", "block-note", "What an approval binds."),
      packetBlock,
      wording,
      definitions(
        [
          ["approval", record.presentation.approval],
          ["draft state", record.presentation.attempt],
        ],
        "facts strong",
      ),
      el(
        "p",
        "block-note",
        "Reporting, not prediction — the write path re-checks every binding inside " +
          "its own transaction.",
      ),
    ),
  );

  if (extra.authorization) {
    put(body, section("Digests", digests(extra.authorization)));
  }

  const history = el("table", "history");
  const header = put(
    el("tr"),
    el("th", null, "event"),
    el("th", null, "when"),
    el("th", null, "status"),
    el("th", null, "actor"),
  );
  put(history, put(el("thead"), header));
  const rows = el("tbody");
  for (const event of record.history) {
    const line = put(
      el("tr"),
      el("td", null, event.event),
      el("td", "mono", stamp(event.created_at)),
      el("td", null, event.status),
      el("td", null, event.actor),
    );
    put(rows, line);
    if (event.reason) {
      const reason = el("tr", "reason-row");
      const cell = el("td");
      cell.colSpan = 4;
      put(cell, paragraphs(event.reason, "reason"));
      put(rows, put(reason, cell));
    }
  }
  put(body, section("Status history", el("p", "block-note", "Append-only."), put(history, rows)));

  const sources = el("div", "sources");
  for (const source of extra.sources || []) {
    const item = put(el("div", "source"), address(source.sender));
    put(
      item,
      el("div", "source-subject", source.subject),
      el("div", "source-sub", shorten(source.message, 18) + " · " + source.namespace),
    );
    put(sources, item);
  }
  put(
    body,
    section(
      "Provenance",
      el("p", "block-note", "Messages that carried it, oldest first."),
      (extra.sources || []).length
        ? sources
        : empty(
            "Structured intake — no message addressing. A draft would be refused for " +
              "want of a recipient.",
          ),
    ),
  );

  const audit = el("div", "audit");
  for (const event of extra.audit || []) {
    put(audit, chip(event.event, "quiet"));
  }
  put(
    body,
    section(
      "Audit",
      (extra.audit || []).length ? audit : empty("Nothing recorded against this review yet."),
    ),
  );
  return body;
}

function digests(authorization) {
  /* The approve dialog's before/now pairs, read-only: what an approval bound, beside what
   * is true now. Difference is shown by putting them side by side, not decided here.
   */
  const table = el("table", "digests");
  put(
    table,
    put(el("thead"), put(el("tr"), el("th", null, "field"), el("th", null, "approved"), el("th", null, "now"))),
  );
  const rows = el("tbody");
  const pairs = [
    ["content", authorization.bound_content, authorization.content_digest],
    ["wording", authorization.bound_draft, authorization.draft_digest],
    ["addressing", authorization.bound_addressing, authorization.addressing_digest],
    ["status event", authorization.bound_event, authorization.status_event_id],
  ];
  for (const [field, before, now] of pairs) {
    const differs = before !== null && before !== undefined && String(before) !== String(now);
    const line = put(
      el("tr", differs ? "differs" : null),
      el("td", null, field),
      el("td", "mono", before === null || before === undefined ? "—" : shorten(before, 14)),
      el("td", "mono", shorten(now, 14)),
    );
    put(rows, line);
  }
  return put(table, rows);
}

/* --- detail: one communication --------------------------------------------------------------- */

function communicationDetail(record, state, actions) {
  const body = el("div", "detail-pane");
  put(
    body,
    el("div", "eyebrow", "Recruiting message · " + shorten(record.message, 14)),
    put(el("h2", "detail-title"), address(record.sender, "full")),
    el("p", "detail-subject", record.subject),
  );
  const tags = put(
    el("div", "detail-tags"),
    chip(record.namespace, "quiet"),
    chip(record.format, "quiet"),
    chip("arrival #" + record.arrival, "quiet"),
    chip(record.extracted + " extracted", "quiet"),
  );
  if (record.unextracted) {
    put(tags, chip(record.unextracted + " not extracted", "warn"));
  }
  put(body, tags);

  const extracted = el("div", "extracted");
  for (const id of record.addresses) {
    const known = state.byId.get(id);
    if (!known) {
      put(extracted, el("div", "source", id));
      continue;
    }
    const card = button("mini", () => actions.openOpportunity(known.id));
    put(
      card,
      put(
        el("div", "row-head"),
        el("span", "row-company", known.company),
        el("span", "row-coverage", known.presentation.coverage),
      ),
      el("div", "row-title", known.title),
      put(el("div", "row-tags"), chip(known.status, "status"), queueChip(known), el("span", "row-when", "current source")),
    );
    put(extracted, card);
  }
  put(
    body,
    section(
      "Extracted opportunities",
      record.addresses.length
        ? extracted
        : empty(
            "This message addresses nothing right now. Either nothing was extracted, or a " +
              "later message became the current source.",
          ),
    ),
  );

  const items = el("div", "items");
  for (const item of evidence(record.items)) {
    const node = el("div", "item" + (item.reason ? " unextracted" : ""));
    put(
      node,
      put(
        el("div", "item-head"),
        el("span", "item-index", "item " + item.index),
        el("span", "item-label", item.reason ? "not extracted" : "extracted"),
      ),
    );
    if (item.reason) {
      put(node, el("div", "item-reason", item.reason));
    }
    put(node, paragraphs(item.excerpt, "excerpt"));
    put(items, node);
  }
  put(
    body,
    section(
      "Stored evidence",
      el(
        "p",
        "block-note",
        "The message body is not stored — only the labeled excerpt per item, exactly " +
          "as it arrived. Non-printing characters are shown, never removed.",
      ),
      record.items.length ? items : empty("No evidence rows for this message."),
    ),
  );

  put(
    body,
    section(
      "Provenance",
      definitions([
        ["message id", shorten(record.message, 20)],
        ["external id", record.external_id],
        ["namespace", record.namespace],
        ["sender (raw)", record.sender],
        ["subject (raw)", record.subject],
        ["format · parser", record.format + " · " + record.parser_version],
        ["arrival", "#" + record.arrival + " in ingest order"],
      ]),
      el(
        "p",
        "block-note",
        "Arrival is ingest order, not a timestamp: no clock is stored for a message today. " +
          "Nothing here can be edited — a misread message is corrected by re-ingesting " +
          "a corrected one under a new id, and the record of what arrived stays.",
      ),
    ),
  );
  return body;
}

function evidence(items) {
  /* extraction_evidence() rows arrive as tuples. Name the positions once, here, so a
   * column order is read in one place rather than indexed at each use.
   */
  return (items || []).map((item) => ({
    index: item[0],
    excerpt: item[1],
    reason: item[2],
    review: item[4],
  }));
}

/* --- assembly -------------------------------------------------------------------------------- */

function filtered(state) {
  const search = state.search.trim().toLowerCase();
  return state.opportunities.filter((row) => {
    if (state.activeOnly && row.presentation.queue === "none") {
      return false;
    }
    if (state.floor && !(Number(row.coverage) >= state.floor)) {
      return false;
    }
    if (!search) {
      return true;
    }
    return (
      String(row.company).toLowerCase().includes(search) ||
      String(row.title).toLowerCase().includes(search)
    );
  });
}

export const SCREENS = {
  dashboard: {
    title: "Dashboard",
    subtitle: "Every count is derived from status history, decisions and draft intents.",
  },
  inbox: { title: "Inbox", subtitle: "Recruiting messages as ingested. Read-only evidence." },
  opportunities: {
    title: "Opportunities",
    subtitle: "One row per opportunity; the review is evidence about it.",
  },
  approvals: {
    title: "Approvals",
    subtitle: "Reviews whose approval state is worth your attention.",
  },
  activity: {
    title: "Activity",
    subtitle: "Status events and decisions — append-only, newest first.",
  },
};

export function listFor(state, actions) {
  const body = el("div", "rows");
  if (state.screen === "dashboard") {
    return dashboard(state, actions);
  }
  if (state.screen === "activity") {
    if (!state.timeline.length) {
      return empty("No activity yet.");
    }
    for (const event of state.timeline) {
      put(body, activityRow(event, state));
    }
    return body;
  }
  if (state.screen === "inbox") {
    if (!state.communications.length) {
      return empty("No messages have been ingested.");
    }
    for (const row of state.communications) {
      put(body, communicationRow(row, state.selected === row.message, actions.openCommunication));
    }
    return body;
  }
  const rows =
    state.screen === "approvals"
      ? state.opportunities.filter(inApprovals)
      : filtered(state);
  if (!rows.length) {
    return empty("Nothing matches.");
  }
  for (const row of rows) {
    put(body, opportunityRow(row, state.selected === row.id, actions.openOpportunity));
  }
  return body;
}

export function detailFor(state, actions) {
  if (!state.detail) {
    return put(
      el("div", "detail-empty"),
      el("h3", null, "Nothing selected"),
      el(
        "p",
        null,
        "Choose a row to read what CareerSignal stored about it. This surface only reads: " +
          "approving, recording a status and creating a draft stay on the command line.",
      ),
    );
  }
  if (state.detail.kind === "communication") {
    return communicationDetail(state.detail.record, state, actions);
  }
  return opportunityDetail(state.detail.record, state.detail, actions);
}

export function controlsFor(state, actions) {
  const holder = clear(document.getElementById("list-controls"));
  if (state.screen !== "opportunities") {
    holder.hidden = true;
    return;
  }
  holder.hidden = false;
  const search = el("input", "search");
  search.type = "search";
  search.placeholder = "Search company or title";
  search.value = state.search;
  search.setAttribute("aria-label", "Search company or title");
  search.addEventListener("input", () => actions.setSearch(search.value));

  const active = button("toggle" + (state.activeOnly ? " on" : ""), () => actions.toggleActive());
  text(active, "Needs attention");
  active.setAttribute("aria-pressed", state.activeOnly ? "true" : "false");

  const coverage = el("select", "coverage-filter");
  coverage.setAttribute("aria-label", "Minimum coverage");
  for (const [value, text] of [
    ["0", "Any coverage"],
    ["70", "70% or more"],
    ["100", "100%"],
  ]) {
    const option = el("option", null, text);
    option.value = value;
    coverage.append(option);
  }
  coverage.value = String(state.floor || 0);
  coverage.addEventListener("change", () => actions.setFloor(Number(coverage.value)));
  put(holder, search, active, coverage);
}
