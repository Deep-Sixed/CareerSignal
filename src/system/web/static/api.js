/* The launch credential, and every request this application is able to make.
 *
 * All of them are GET. There is no helper here that takes a method, so a screen cannot
 * accidentally acquire one: the surface answers nothing else, and neither does this.
 */

const HEADER = "X-CareerSignal-Token";
const JSON_MEDIA = "application/json";
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

async function send(path, credential, payload) {
  /* The one request on this surface that is not a read.
   *
   * It is written out here rather than reached through a method parameter, so there is no
   * helper a screen could hand a different verb to: adding a second command means adding a
   * second function, in this file, where the credential lives.
   *
   * The refusal is returned rather than thrown. A 409 is not an error in the sense a
   * failed request is -- the command was well formed and the operator's authority was
   * real, and what the caller needs is what the server found, not a message.
   */
  const response = await fetch(ROOT + path, {
    method: "POST",
    headers: { [HEADER]: credential, "Content-Type": JSON_MEDIA },
    cache: "no-store",
    credentials: "omit",
    body: JSON.stringify(payload),
  });
  return { ok: response.ok, status: response.status, body: await response.json() };
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
  };
}
