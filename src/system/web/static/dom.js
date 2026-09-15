/* Node construction, and the one place text from outside this machine becomes visible.
 *
 * Everything here builds elements and sets `textContent`. There is no innerHTML, no
 * insertAdjacentHTML, no document.write and no template that concatenates a value into a
 * string of markup. A recruiter controls a subject line, a job title, an excerpt and a
 * provider receipt; on this origin -- which holds the launch credential -- none of them may
 * ever be parsed as HTML. Node construction is what guarantees that, not escaping.
 */

/* C0, DEL and C1, exactly the range the terminal surface escapes. */
const NONPRINTING = /[\u0000-\u001f\u007f-\u009f]/g;

export function visible(value) {
  /* Show a non-printing byte rather than dropping it.
   *
   * A title carrying an escape sequence renders in HTML as nothing at all, which hides
   * from the operator that the stored evidence contains it. This is a display formatter
   * for bytes the browser would otherwise swallow -- it decides nothing about CareerSignal
   * state, and it never deletes: what arrived stays legible, one escape per byte.
   */
  return String(value).replace(
    NONPRINTING,
    (found) => "\\x" + found.charCodeAt(0).toString(16).padStart(2, "0"),
  );
}

export function el(tag, className, value) {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (value !== undefined && value !== null) {
    node.textContent = visible(value);
  }
  return node;
}

export function text(node, value) {
  /* The only assignment to textContent outside el(). Keeping both in this file means the
   * question "can stored text become markup here?" has one file to read, not four.
   */
  node.textContent = visible(value);
  return node;
}

export function put(parent, ...children) {
  for (const child of children) {
    if (child) {
      parent.append(child);
    }
  }
  return parent;
}

export function clear(node) {
  node.replaceChildren();
  return node;
}

export function chip(text, className = "") {
  return el("span", ("chip " + className).trim(), text);
}

export function lines(value) {
  /* Multi-line operator text: the newline stays structure, every other byte is escaped.
   *
   * Split on LF after folding CRLF, the same rule the terminal surface applies, so a bare
   * CR cannot disappear into a line break instead of being shown.
   */
  return String(value ?? "")
    .replace(/\r\n/g, "\n")
    .split("\n");
}

export function paragraphs(value, className = "") {
  const block = el("div", className);
  for (const line of lines(value)) {
    put(block, el("div", "line", line));
  }
  return block;
}

export function definitions(pairs, className = "facts") {
  const list = el("dl", className);
  for (const [term, value, valueClass] of pairs) {
    put(list, el("dt", null, term), el("dd", valueClass || null, value ?? "—"));
  }
  return list;
}

export function link(url) {
  /* An href is an attribute, not text, so it gets the one check textContent cannot give:
   * only http and https survive, which keeps a stored `javascript:` URL inert.
   */
  const anchor = el("a", "url", url);
  if (/^https?:\/\//i.test(String(url))) {
    anchor.href = url;
    anchor.rel = "noreferrer noopener";
  } else {
    anchor.className = "url refused";
    anchor.title = "Not an http(s) address; shown as text only";
  }
  return anchor;
}

export function button(className, onClick) {
  const node = el("button", className);
  node.type = "button";
  node.addEventListener("click", onClick);
  return node;
}

export function shorten(value, keep = 12) {
  const text = String(value ?? "");
  return text.length > keep ? text.slice(0, keep) + "…" : text;
}
