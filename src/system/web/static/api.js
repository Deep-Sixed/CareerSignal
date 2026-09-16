/* The launch credential, and every request this application is able to make.
 *
 * Reads go through one helper and commands through another, and neither takes a method: the
 * verb is written once, in `deliver` below, so a screen cannot acquire a different one by
 * passing it. Every request this application can make is a named function in this file, which
 * is what makes adding one an edit a reader notices rather than a parameter.
 */

const HEADER = "X-CareerSignal-Token";
const NAMESPACE = "X-CareerSignal-Namespace";
const JSON_MEDIA = "application/json";
/* A MIME message already has a representation, so it is sent as one. Wrapping it in JSON or
 * base64 to reuse the helper below would mean this file had an opinion about its bytes. */
const MESSAGE_MEDIA = "message/rfc822";
const STORAGE_NAME = "careersignal.launch";
const MARKER = "#token=";
const ROOT = "/api/v1";

export function claim() {
  /* Take the credential out of the address bar before anything else runs.
   *
   * It arrives in the fragment because a fragment is never sent in an HTTP request: it
   * reaches this script and nothing else. Session storage, not local storage, because it
   * is valid for one server process -- outliving the tab would only leave a stale value
   * to fail with later. The address bar is rewritten immediately, so the credential does
   * not survive in history, in a bookmark, or in whatever gets pasted somewhere next.
   */
  if (window.location.hash.startsWith(MARKER)) {
    const supplied = decodeURIComponent(window.location.hash.slice(MARKER.length));
    window.sessionStorage.setItem(STORAGE_NAME, supplied);
    window.history.replaceState(null, "", window.location.pathname);
  }
  return window.sessionStorage.getItem(STORAGE_NAME);
}

function part(value) {
  /* An id is a digest and a limit is a number, but neither is trusted to be: every one is
   * encoded before it becomes part of a path. */
  return encodeURIComponent(value);
}

async function read(path, credential) {
  const response = await fetch(ROOT + path, {
    headers: { [HEADER]: credential },
    cache: "no-store",
    credentials: "omit",
  });
  const body = await response.json();
  if (!response.ok) {
    throw new Error(body.error || response.statusText);
  }
  return body;
}

async function deliver(path, credential, media, body, extra) {
  /* The one request on this surface that is not a read.
   *
   * The verb is written out here and nowhere else, so there is no helper a screen could hand
   * a different one to: every command in this file goes through this function, and adding a
   * command means adding a named function beside the others rather than passing a method.
   *
   * What varies is the media type and the body, because one of these commands carries a
   * message rather than a command. What does not vary is the credential, the verb, and that
   * nothing is sent with cookies or from cache.
   *
   * The refusal is returned rather than thrown. A 409 is not an error in the sense a
   * failed request is -- the command was well formed and the operator's authority was
   * real, and what the caller needs is what the server found, not a message.
   */
  const response = await fetch(ROOT + path, {
    method: "POST",
    headers: { [HEADER]: credential, "Content-Type": media, ...(extra || {}) },
    cache: "no-store",
    credentials: "omit",
    body,
  });
  return { ok: response.ok, status: response.status, body: await response.json() };
}

function send(path, credential, payload) {
  /* A command whose body is fields. Everything it can say is a JSON object the server
   * validates field by field, and it may say nothing else. */
  return deliver(path, credential, JSON_MEDIA, JSON.stringify(payload));
}

export function connect(credential) {
  /* One object carrying the credential, so no screen handles it directly. */
  const ask = (path) => read(path, credential);
  return {
    session: () => ask("/session"),
    /* Presentation is opt-in per request. The strings it returns are produced by
     * system.views on the server; nothing here recomputes a coverage label, a queue, an
     * approval sentence or a draft state, because CareerSignal's idea of what its own
     * state means lives in Python and has exactly one implementation. */
    opportunities: () => ask("/opportunities?presentation=true"),
    opportunity: (id) => ask(`/opportunities/${part(id)}?presentation=true`),
    sources: (id) => ask(`/opportunities/${part(id)}/sources`),
    communications: () => ask("/communications"),
    communication: (id) => ask(`/communications/${part(id)}`),
    authorization: (review) => ask(`/reviews/${part(review)}/authorization`),
    timeline: (limit) => ask(`/timeline?limit=${part(limit)}`),
    /* The vocabulary comes from the engine, so the control below is filled with what
     * recruiting.status actually governs rather than a copy kept here. */
    statuses: () => ask("/statuses"),
    recordStatus: (id, payload) =>
      send(`/opportunities/${part(id)}/status`, credential, payload),
    /* The caller hands over the expectation it was rendering, unchanged. Nothing here
     * assembles one, and there is no second read to assemble it from: an expectation built
     * anywhere but the packet on screen would name a moment nobody was shown. */
    decide: (review, payload) =>
      send(`/reviews/${part(review)}/decision`, credential, payload),
    /* The two outward commands. Each is the address and nothing else: what a draft would say
     * was settled by the approval, and reconciliation asks the destination what happened
     * rather than telling it anything. An empty object, not a payload with fields the server
     * would have to refuse. */
    draft: (review) => send(`/reviews/${part(review)}/draft`, credential, {}),
    reconcile: (review) => send(`/reviews/${part(review)}/reconcile`, credential, {}),
    /* Which sources this launch can take material in from. A read of CareerSignal's own
     * configuration: it contacts no mailbox, and the controls offered are decided by what it
     * says rather than inferred from anything else the session happens to report. */
    intakeSources: () => ask("/intake/sources"),
    /* The message itself, exactly as it was read from the operator's disk. Nothing here
     * parses it, inspects it, or decides whether it looks like a job -- and no path is sent,
     * because the server resolves no names. The namespace is what the operator declared and
     * travels as a header, since the body is already spoken for. */
    ingestEml: (namespace, message) =>
      deliver(`/intake/eml`, credential, MESSAGE_MEDIA, message, { [NAMESPACE]: namespace }),
    /* A bound on a read, and nothing more. The mailbox, the credential, the namespace and the
     * evaluation profile are launch facts the server refuses to take from a request. */
    ingestGmail: (bounds) => send(`/intake/gmail`, credential, bounds),
  };
}
