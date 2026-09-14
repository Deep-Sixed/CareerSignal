# The local read surface

CareerSignal's first surface that listens on a socket. It is **read-only**: it records no status, decides no review, creates no draft, and contacts no mailbox. Every operator write is still made on the command line, where it is already bounded.

```sh
uv run careersignal serve --db /path/to/private.db
```

```
CareerSignal is reading /path/to/private.db
Open http://127.0.0.1:8765/#token=<a fresh token, printed once>
This address is valid for this process only. Stop with Ctrl-C.
```

Open that address. The URL is printed rather than opened for you: launching a browser is convenience, and this is a network boundary.

`--port` chooses a port. There is no `--host`, no `0.0.0.0` fallback, and no configuration file that could introduce one. The address is not a setting when the whole security model is "nothing leaves this machine".

## What it refuses

The surface is confined by construction rather than by configuration, so each of these is a property of the code and not of how it was started.

**It binds loopback only.** `127.0.0.1`, with no parameter that accepts another interface.

**It answers `GET` and `HEAD`, and nothing else.** Every other method — `POST`, `PUT`, `PATCH`, `DELETE`, and verbs this server has never heard of — is refused with `405` and `Allow: GET, HEAD` *before* anything is routed, authenticated, or read. Refusal is the default; reaching a route is what has to be spelled out.

**It reaches read projections and nothing else.** No intake, no `decide()`, no `record_status()`, no `claim()`, no `finish()`, no provider, no credential. A test reads the package's own syntax tree and fails the build if a write, a provider, or a mailbox client is ever named inside it — because a route that quietly started deciding something would answer `200` exactly as it does now.

**It derives no fact.** Every route hands back what a repository projection already returned. The rules were settled where the storage is; a second opinion computed at the edge is how a list and a detail pane start disagreeing about the same opportunity.

**It interpolates nothing into HTML.** `json.dumps` is the escaping boundary, and the page fills itself in with `textContent`. Recruiter-controlled text is never parsed as markup on the one origin that holds the launch credential.

## The launch token

Authority is a fresh high-entropy token, minted by `secrets` once per launch and held in process memory for the life of that process.

It is never written to the database, to configuration, to an environment variable, to a file, or to a log — the request logger writes nothing at all, precisely so the token cannot outlive the process in a file nobody thinks about. There is no parameter that supplies one: a token a caller could set is a token a script could pin and reuse.

It travels to the browser in the **URL fragment**, which browsers do not send in an HTTP request. It therefore reaches the page and nothing else. The bootstrap moves it into session storage and rewrites the address bar immediately, so it does not survive in history, in a bookmark, or in whatever gets pasted into a chat window next. In a query string it would reach the server, the request line, and every log that copies one.

It comes back as a request header and is compared in constant time:

```
X-CareerSignal-Token: <launch token>
```

A missing or wrong token on an `/api/v1/` request is `401`. The static page itself needs no token — it has to load before it can present one — and carries no stored evidence.

The API reports whether a Gmail credential is configured. It never reports its value.

## Who may ask

A request naming an unexpected `Host` is refused with `403`. The socket is not the identity; the name the client asked for is, and a hostname an attacker owns pointed at loopback is exactly the shape DNS rebinding takes.

A request carrying an `Origin` other than the server's own is refused with `403`, and **no `Access-Control-Allow-*` header is ever emitted** — on any response, including the refusals. Another origin is not refused and then quietly handed the answer by a permissive header.

Every response carries:

```
Content-Security-Policy: default-src 'self'; base-uri 'none'; object-src 'none';
  frame-ancestors 'none'; form-action 'self'; connect-src 'self'; img-src 'self' data:;
  style-src 'self'; script-src 'self'
Referrer-Policy: no-referrer
X-Content-Type-Options: nosniff
Cache-Control: no-store
```

There is no `'unsafe-inline'` and no `'unsafe-eval'`, and the page ships no inline script, so the frontend that arrives next is served under this policy unchanged rather than loosening it on the way in.

## The routes

All under `/api/v1`, all `GET`, all exactly the repository projection they name.

| Route | Answers |
| --- | --- |
| `/session` | Runtime facts: SQLite version, database path, credential **presence** booleans |
| `/opportunities` | The list, with the same filters the CLI accepts |
| `/opportunities/{id}` | One opportunity with its packet, action state and history |
| `/opportunities/{id}/sources` | Every message it arrived in, oldest first |
| `/communications` | Every message, in arrival order, with its evidence counts |
| `/communications/{message}` | One message, its evidence, and what it currently addresses |
| `/reviews/{review}/authorization` | What an approval for this review would bind |
| `/timeline` | Status and audit events as one stream, newest first |

`/opportunities` accepts `status`, `active`, `eligible`, `min_coverage` and `max_coverage`; `/timeline` accepts `limit` and `since`. The values are handed to the projection that owns them, so a coverage bound outside 0–100 or a status outside the vocabulary is refused in the repository's own words.

A parameter a route does not know is a `400`, not a silent pass: a misspelled filter that returned everything would tell the operator they are looking at a narrowed list when they are looking at all of it.

A blank value is a value. `?status=` and `?misspelled=` are both refused rather than read as "no filter" — the query is parsed with `keep_blank_values` for exactly that reason, because a parameter dropped at the parsing boundary is never validated and comes back as the whole list wearing the shape of a narrowed one.

An id that names nothing is `404`. A record that exists but has no evidence is an honest empty list — absence of a record and absence of evidence must not arrive looking the same.

## Static assets

`index.html`, `bootstrap.js` and `careersignal.css` are packaged resources under `system.web`, discovered with `importlib.resources` and served by **exact name**. Nothing joins or resolves a path, so a request for `../../etc/passwd` is not a traversal to defeat — it is a key that does not exist. There is no directory listing. An asset the surface cannot name a media type for stops the launch rather than being served as guessed bytes.

Locating assets beside `__file__` would pass every test in a checkout, so the wheel gate starts the surface from the installed wheel with no checkout anywhere and asks it for them.

## Threads and connections

The server is threaded so a slow read cannot block the pane beside it, and no `sqlite3` object crosses those threads. `Repository` holds a path; each of its methods opens and closes its own connection on the calling thread. That is why one handle can be shared where a connection could not be, and why this package creates no pool and never weakens `check_same_thread`.

## What is not here yet

Write authority. Approving a review, recording a status, creating a draft and reconciling one remain command-line actions. They arrive on this surface only with the operator write path that is designed for them — including the approval binding an operator's screen has to carry — and not as a side effect of being able to see things in a browser.
