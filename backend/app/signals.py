"""Simple, deterministic detection of names and error codes in ticket text.

These are surface rules, not understanding:

* A *name* is a word written with capital letters where ordinary words are not
  capitalised (inside a sentence), or with internal capitals or all capitals
  ("HubSpot", "SSO"). Words the historical tickets write that way are also
  recognised when a new ticket types them in lower case.
* An *error code* is a code-shaped token ("ERR_CONN_RESET", "0x80070005") or a
  three-digit number next to a word such as "error" or "status".

The rules will miss things and will sometimes pick up a word that is not a
product. Their output is always shown with how it was obtained.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .text import STOP_WORDS

_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)?")
_CODE_TOKEN_RE = re.compile(r"\b(?:0x[0-9A-Fa-f]{3,}|[A-Z][A-Z0-9]*(?:[_-][A-Z0-9]+)+|[A-Z]{1,5}\d{2,})\b")
_PLAIN_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*")
_CODE_TRIGGERS = frozenset({"error", "errors", "code", "codes", "status", "http", "response", "responses"})
_SENTENCE_BREAK = ".!?:\n"


def _is_sentence_initial(text: str, start: int) -> bool:
    index = start - 1
    while index >= 0 and text[index] in " \t\"'“‘(":
        index -= 1
    return index < 0 or text[index] in _SENTENCE_BREAK


def _name_strength(word: str) -> str | None:
    """'strong' for internal or all capitals, 'weak' for Capitalised, else None."""
    letters = [ch for ch in word if ch.isalpha()]
    if len(word) < 2 or not letters:
        return None
    if any(ch.isupper() for ch in word[1:]):
        return "strong"
    if word[0].isupper():
        return "weak"
    return None


def _candidate_names(text: str) -> list[tuple[str, str, bool]]:
    """(display form, strength, sentence_initial) for each capitalised word."""
    found: list[tuple[str, str, bool]] = []
    for match in _WORD_RE.finditer(text):
        word = re.split(r"['’]", match.group(0), maxsplit=1)[0]
        strength = _name_strength(word)
        if strength is None or word.lower() in STOP_WORDS:
            continue
        found.append((word, strength, _is_sentence_initial(text, match.start())))
    return found


def build_name_lexicon(texts: Iterable[str]) -> dict[str, str]:
    """Names the historical tickets use, keyed by lower case, valued by display form."""
    lexicon: dict[str, str] = {}
    for text in texts:
        for word, strength, initial in _candidate_names(text):
            if strength == "strong" or not initial:
                lexicon.setdefault(word.lower(), word)
    return lexicon


def detect_names(text: str, lexicon: dict[str, str]) -> list[str]:
    """Names in the text, in order of first appearance, without repeats."""
    names: list[str] = []
    seen: set[str] = set()

    def add(display: str) -> None:
        key = display.lower()
        if key not in seen:
            seen.add(key)
            names.append(display)

    for match in _WORD_RE.finditer(text):
        word = re.split(r"['’]", match.group(0), maxsplit=1)[0]
        lowered = word.lower()
        if lowered in STOP_WORDS or len(word) < 2:
            continue
        strength = _name_strength(word)
        initial = _is_sentence_initial(text, match.start())
        if lowered in lexicon:
            add(lexicon[lowered])
        elif strength == "strong" or (strength == "weak" and not initial):
            add(word)
    return names


def detect_codes(text: str, known_ticket_ids: frozenset[str] = frozenset()) -> list[str]:
    """Error-code-like tokens in the text, in order, without repeats."""
    codes: list[str] = []
    seen: set[str] = set()

    def add(code: str) -> None:
        if code not in seen and code not in known_ticket_ids:
            seen.add(code)
            codes.append(code)

    for match in _CODE_TOKEN_RE.finditer(text):
        add(match.group(0))

    tokens = [match.group(0) for match in _PLAIN_TOKEN_RE.finditer(text)]
    for index, token in enumerate(tokens):
        if not (token.isdigit() and len(token) == 3 and 100 <= int(token) <= 599):
            continue
        window = tokens[max(0, index - 3) : index] + tokens[index + 1 : index + 4]
        words = {part.lower() for item in window for part in item.split("-")}
        if words & _CODE_TRIGGERS:
            add(token)
    return codes


def find_referenced_ids(text: str, known_ids: frozenset[str], exclude: str) -> list[str]:
    """Known ticket ids mentioned in the text (other than the ticket itself)."""
    referenced: list[str] = []
    for match in re.finditer(r"[A-Za-z0-9][A-Za-z0-9_.-]*", text):
        token = match.group(0).rstrip(".-_")
        if token in known_ids and token != exclude and token not in referenced:
            referenced.append(token)
    return referenced
