"""Keep student identifiers and other PII out of what AL shows (and out of what the model sees).

AL answers with aggregates only. Four layers use the rules in this module:
  1. PreToolUse hooks (agent.py) refuse queries that select, group by or filter on an identifier.
  2. A PostToolUse hook (agent.py) scrubs tool results before the model reads them.
  3. EventGuard (server.py) scrubs every event sent to the browser, live or replayed.
  4. The system prompts tell the model the rule, so it explains instead of retrying.

Pure functions, no SDK or HTTP imports, so they are easy to test.
"""
import json
import re
from typing import Any

REDACTED = "[redacted]"

# One name segment (the part after the last `__` or `.`) that identifies a person. Whole-segment
# matches only, so `stdnt_carr_desig_cd`, `stdnt_cat_pop_id` and `class_section_id` stay allowed.
_PERSON_FIELD = re.compile(r"""^(
    \w*emplid\w* | empl_id | (stdnt|student|prsn|person|asu|affiliate|sis|user)_id
  | asurite\w* | ssn | social_security\w* | itin | (stdnt|student|prsn|person)_(nbr|num|number)
  | e?mail | \w*_e?mail(_addr\w*)? | e?mail_addr\w* | \w*phone\w*
  | (first|last|middle|full|preferred|legal|display|given|family|sur)_?(name|nm)
  | (stdnt|student|prsn|person)_(name|nm)
  | \w*birth\w* | dob | \w*address\w* | \w*_addr | addr_\w* | street\w* | \w*zip\w* | postal\w*
)$""", re.I | re.X)
# Semantic-layer entities whose key is a person id: grouping by one returns a row per student.
_PERSON_ENTITIES = {"student"}


def is_identifier(name: str, entities: bool = True) -> bool:
    """True for a column or dimension name that identifies a person.

    `entities` also counts the bare semantic-layer entity names (`student`); raw SQL turns it off,
    because there `student` is more likely a schema or table name than a column.
    """
    seg = re.split(r"__|\.", str(name).strip().strip('"`').lower())[-1]
    return bool(_PERSON_FIELD.match(seg)) or (entities and seg in _PERSON_ENTITIES)


# ---- Values that are PII wherever they appear ----
_VALUE_PATTERNS = [
    re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),                    # email address
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),                             # SSN
    re.compile(r"(?:\(\d{3}\)\s?|\b\d{3}[-.\s])\d{3}[-.\s]\d{4}\b"),  # US phone number
    re.compile(r"\b\d{10}\b"),                                        # ASU EMPLID / affiliate id
]


def scrub_text(s: str) -> str:
    """Replace emails, SSNs, phone numbers and 10-digit ids. Counts like 73,871, dates and term codes pass."""
    for p in _VALUE_PATTERNS:
        s = p.sub(REDACTED, s)
    return s


def _scrub_value(v: Any) -> Any:
    if isinstance(v, str):
        return scrub_text(v)
    if isinstance(v, int) and not isinstance(v, bool) and 10**9 <= abs(v) < 10**10:
        return REDACTED  # a 10-digit id stored as a number
    return v


# ---- Query checks (PreToolUse) ----
AGGREGATE_ONLY = ("AL answers with aggregates only. Individual student identifiers (EMPLID, "
                  "student id, ASURITE), names and contact details can't be selected, grouped by "
                  "or filtered on. Rewrite the query as an aggregate, or tell the user this isn't available.")

_SQL_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_SQL_STRING = re.compile(r"'(?:[^']|'')*'")
_SQL_COUNT = re.compile(r"\bcount\s*\(\s*(?:distinct\s+)?[\w.\"\s,*]*\)", re.I)
_SQL_STAR = re.compile(r"(?:\bselect\s+(?:distinct\s+|top\s+\d+\s+)?|,\s*)(?:[\"\w]+\.)*\*", re.I)
_WORD = re.compile(r"[A-Za-z_][\w$]*")
_JINJA_REF = re.compile(r"\b(?:Dimension|TimeDimension|Entity)\(\s*['\"]([^'\"]+)['\"]")
_QUOTED = re.compile(r"'(?:[^']|'')*'|\"[^\"]*\"")


def check_sql(sql: str) -> str | None:
    """Reason to refuse a raw SQL query that could return identifiers, or None to allow it.

    Identifier columns may appear only inside count(...)/count(distinct ...); `select *` is refused
    because it would return whatever identifier columns the table has.
    """
    s = _SQL_COMMENT.sub(" ", sql)
    if scrub_text(s) != s:
        return AGGREGATE_ONLY  # filtering on an id value, email, ...
    s = _SQL_STRING.sub("''", s)
    s = _SQL_COUNT.sub("count()", s)
    if _SQL_STAR.search(s):
        return "Select the columns you need instead of `*`. " + AGGREGATE_ONLY
    if any(is_identifier(w, entities=False) for w in _WORD.findall(s.replace('"', ""))):
        return AGGREGATE_ONLY
    return None


def _strings(x: Any):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, (list, tuple)):
        for v in x:
            yield from _strings(v)


def check_semantic_args(args: dict[str, Any]) -> str | None:
    """Reason to refuse a query_metrics / compiled-SQL / dimension-values call, or None to allow it.

    Refuses grouping by, filtering on or listing values of the `student` entity or any identifier
    dimension. `where` is Jinja (`{{ Dimension('x') }} = 'y'`): the referenced names and any bare
    words are checked, not the quoted values being compared against.
    """
    for k, v in (args or {}).items():
        if k == "metrics":
            continue
        for s in _strings(v):
            if k == "where":
                # Names: the Jinja refs plus bare words; quoted literals ('Non-Degree Student') are values.
                names = _JINJA_REF.findall(s) + _WORD.findall(_QUOTED.sub(" ", _JINJA_REF.sub(" ", s)))
                if scrub_text(s) != s or any(is_identifier(w) for w in names):
                    return AGGREGATE_ONLY
            elif is_identifier(s):
                return AGGREGATE_ONLY
    return None


# ---- Tool results (PostToolUse) ----
def scrub_payload(x: Any) -> Any:
    """Copy of a tool result with identifier keys dropped and PII values redacted, at any depth.

    Strings holding JSON (the dbt tools wrap rows as text) are scrubbed inside and re-serialized.
    """
    if isinstance(x, dict):
        return {k: scrub_payload(v) for k, v in x.items() if not is_identifier(k)}
    if isinstance(x, list):
        return [scrub_payload(v) for v in x]
    if isinstance(x, str):
        t = x.strip()
        if t[:1] in ("[", "{"):
            try:
                data = json.loads(t)
            except ValueError:
                pass
            else:
                clean = scrub_payload(data)
                return x if clean == data else json.dumps(clean)
        return scrub_text(x)
    return _scrub_value(x)


# ---- Browser events ----
def _scrub_table(ev: dict[str, Any]) -> dict[str, Any] | None:
    cols = [c for c in ev.get("columns") or [] if not is_identifier(c)]
    if not cols:
        return None  # nothing but identifiers: drop the card
    keep = set(cols)
    out = dict(ev, columns=cols,
               rows=[{k: _scrub_value(v) for k, v in r.items() if k in keep}
                     for r in ev.get("rows") or [] if isinstance(r, dict)],
               metrics=[m for m in ev.get("metrics") or [] if m in keep],
               group_by=[g for g in ev.get("group_by") or []
                         if not is_identifier(g.get("name", "") if isinstance(g, dict) else g)])
    for k in ("labels", "short_labels"):
        if isinstance(ev.get(k), dict):
            out[k] = {m: scrub_text(str(v)) for m, v in ev[k].items() if m in keep}
    return out


def sanitize_event(ev: dict[str, Any]) -> dict[str, Any] | None:
    """The event as it may be shown to the user, or None to drop it."""
    t = ev.get("type")
    if "key" in ev:  # result_table/sql pairing key; scrubbed the same way on both so they still match
        ev = dict(ev, key=scrub_text(str(ev["key"])))
    if t == "result_table":
        return _scrub_table(ev)
    if t == "tool_use":
        return {k: v for k, v in ev.items() if k != "input"}  # the UI never shows tool input
    if t in ("text", "text_delta"):
        return dict(ev, text=scrub_text(ev.get("text", "")))
    if t == "sql":
        return dict(ev, sql=scrub_text(str(ev.get("sql", ""))))
    if t == "error":
        return dict(ev, message=scrub_text(str(ev.get("message", ""))))
    if t == "clarify":
        return dict(ev, question=scrub_text(str(ev.get("question", ""))),
                    options=[scrub_text(str(o)) for o in ev.get("options") or []])
    if t == "cite":
        return dict(ev, citations=[{k: scrub_text(v) if isinstance(v, str) else v for k, v in c.items()}
                                   for c in ev.get("citations") or []])
    return ev


# Trailing text that might be the start of an id, email or phone number: up to three "words".
_PARTIAL_TAIL = re.compile(r"(?:[\w@.+\-()]*\s+){0,2}[\w@.+\-()]*$")
_MAX_HOLD = 128


class StreamRedactor:
    """Redacts streamed text whose PII may be split across deltas, by holding back a short tail."""

    def __init__(self):
        self._buf = ""

    def feed(self, text: str) -> str:
        self._buf += text
        tail = _PARTIAL_TAIL.search(self._buf)
        cut = tail.start() if tail else len(self._buf)
        cut = max(cut, len(self._buf) - _MAX_HOLD)
        out, self._buf = self._buf[:cut], self._buf[cut:]
        return scrub_text(out)

    def flush(self) -> str:
        out, self._buf = self._buf, ""
        return scrub_text(out)


class EventGuard:
    """Sanitizes one response's event stream: call it per event, then close() at the end."""

    def __init__(self):
        self._stream = StreamRedactor()

    def _pending(self) -> list[dict[str, Any]]:
        rest = self._stream.flush()
        return [{"type": "text_delta", "text": rest}] if rest else []

    def __call__(self, ev: dict[str, Any]) -> list[dict[str, Any]]:
        if ev.get("type") == "text_delta":
            text = self._stream.feed(ev.get("text", ""))
            return [dict(ev, text=text)] if text else []
        out = self._pending()
        clean = sanitize_event(ev)
        return out + ([clean] if clean is not None else [])

    def close(self) -> list[dict[str, Any]]:
        return self._pending()
