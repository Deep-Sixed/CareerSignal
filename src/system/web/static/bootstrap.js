/* Take the launch credential out of the address bar, then ask the surface one question.
 *
 * The credential arrives in the URL fragment because a fragment is never sent in an HTTP
 * request: it reaches this script and nothing else. The first thing done with it is to move
 * it into session storage and rewrite the address bar, so it does not survive in history,
 * in a bookmark, or in whatever the operator pastes into a chat window next.
 *
 * Everything shown below is written with textContent. No response value is ever turned into
 * markup, which is what keeps recruiter-controlled text from becoming a script on the one
 * origin that holds the operator's launch credential.
 */

const HEADER = "X-CareerSignal-Token";
const STORAGE_NAME = "careersignal.launch";
const MARKER = "#token=";

function claim() {
  /* Session storage, not local storage: the credential is valid for one server process,
   * so outliving the tab would only leave a stale value to fail with later. */
  if (window.location.hash.startsWith(MARKER)) {
    const supplied = decodeURIComponent(window.location.hash.slice(MARKER.length));
    window.sessionStorage.setItem(STORAGE_NAME, supplied);
    window.history.replaceState(null, "", window.location.pathname);
  }
  return window.sessionStorage.getItem(STORAGE_NAME);
}

function say(message, state) {
  const line = document.getElementById("state");
  line.textContent = message;
  line.dataset.state = state;
}

function list(facts) {
  const target = document.getElementById("facts");
  target.replaceChildren();
  for (const [name, value] of Object.entries(facts)) {
    const term = document.createElement("dt");
    term.textContent = name;
    const description = document.createElement("dd");
    description.textContent = String(value);
    target.append(term, description);
  }
}

async function read(path, credential) {
  const response = await fetch(path, {
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

async function start() {
  const credential = claim();
  if (!credential) {
    say("Open the address CareerSignal printed when it started; this tab has no launch token.", "refused");
    return;
  }
  try {
    list(await read("/api/v1/session", credential));
    say("Connected. This surface is read-only.", "ready");
  } catch (failure) {
    say(`The local surface refused this tab: ${failure.message}`, "refused");
  }
}

start();
