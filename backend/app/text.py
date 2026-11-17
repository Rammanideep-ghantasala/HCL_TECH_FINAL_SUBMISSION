"""Text handling shared by keyword retrieval and the comparison card.

One tokenizer is used everywhere so that what the retriever matches and what
the card highlights are the same thing. Design choices, all deliberate:

* Negations are kept ("not", "no", "never", "can't" -> "not"). The stock
  scikit-learn English stop list removes them, which would make
  "cannot log in" and "can log in" look identical.
* Stop words are a short explicit list of function words only. Words such as
  "empty", "system" or "bill" carry meaning in support tickets and stay.
* Error codes and identifiers survive: digits are kept, and underscores stay
  inside a token ("err_conn_reset"). Bare numbers shorter than
  MIN_NUMBER_DIGITS are dropped from matching because "15" or "2" link
  unrelated tickets; they are never removed from the displayed text.
* Tokens carry character offsets into the original string, so highlights are
  built from positions and the original text is never rewritten.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

PREPROCESSING_VERSION = "text-v1"

MIN_NUMBER_DIGITS = 3

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*(?:'[a-z]+)?")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")

NEGATION_TERM = "not"
_NEGATION_WORDS = frozenset({"not", "no", "never", "none", "nothing", "without", "cannot", "neither", "nor"})

STOP_WORDS = frozenset(
    """
    a an the this that these those there here
    i me my we us our you your he him his she her it its they them their
    am is are was were be been being do does did doing have has had having
    will would shall should can could may might must
    and or but if then than so because as while when where which who whom whose what how why
    of in on at to from by for with about into onto over under after before between through during
    up out off again further also just very too only own same such both each any some all more most other
    s t d ll ve re m
    """.split()
)

# Words that describe that something is wrong without saying what. They are
# searched like any other word, but they do not count towards having enough
# detail to search on.
GENERIC_WORDS = frozenset(
    """
    help issue issues problem problems working work works broken urgent please asap need needs
    something anything wrong fix fixed support question questions hello hi thanks thank
    customer customers user users ticket tickets error errors
    """.split()
)


@dataclass(frozen=True)
class Token:
    """One word of the original text and the term it is matched by."""

    term: str
    start: int
    end: int
    surface: str


def exact_text(value: str) -> str:
    """The only normalization used for exact-match comparison.

    CRLF and CR line endings become LF, and leading and trailing whitespace is
    removed. Nothing else changes: case, punctuation, numbers, negation,
    internal spacing, Unicode form and word order must all be identical.
    Exact matching never uses tokens, TF-IDF or embeddings, so a similarity
    score of 1.0 is never taken as proof that two texts are the same.
    """
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def clean_text(value: str) -> str:
    """Normalize Unicode and remove control characters. Keeps newlines and tabs."""
    normalized = unicodedata.normalize("NFKC", value)
    return _CONTROL_RE.sub(" ", normalized)


def _light_stem(term: str) -> str:
    """Very conservative suffix stripping for plural and verb endings."""
    if len(term) <= 4 or any(ch.isdigit() for ch in term) or "_" in term:
        return term
    if term.endswith("ies") and len(term) > 5:
        return term[:-3] + "y"
    if term.endswith("ing") and len(term) > 6:
        return term[:-3]
    if term.endswith("ed") and len(term) > 5:
        return term[:-2]
    if term.endswith("es") and len(term) > 5 and term[-3] in "sxz":
        return term[:-2]
    if term.endswith("s") and not term.endswith("ss") and not term.endswith("us"):
        return term[:-1]
    return term


def _term_for(raw: str, stem: bool) -> str | None:
    """Map a lower-cased raw token to its matching term, or None to skip it."""
    word = raw
    if "'" in word:
        if word.endswith("n't"):
            return NEGATION_TERM
        word = word.split("'", 1)[0]
    if word in _NEGATION_WORDS:
        return NEGATION_TERM
    if word in STOP_WORDS:
        return None
    if word.isdigit():
        return word if len(word) >= MIN_NUMBER_DIGITS else None
    if len(word) < 2:
        return None
    return _light_stem(word) if stem else word


def tokenize(text: str, stem: bool = False) -> list[Token]:
    """Split text into matchable tokens with offsets into the given string."""
    tokens: list[Token] = []
    lowered = text.lower()
    # Lower-casing can change string length for a few characters; when it does,
    # fall back to positions in the lowered string by tokenizing char by char.
    same_length = len(lowered) == len(text)
    for match in _TOKEN_RE.finditer(lowered):
        term = _term_for(match.group(0), stem)
        if term is None:
            continue
        start, end = match.span()
        surface = text[start:end] if same_length else match.group(0)
        tokens.append(Token(term=term, start=start, end=end, surface=surface))
    return tokens if same_length else [Token(t.term, 0, 0, t.surface) for t in tokens]


def unigram_terms(text: str, stem: bool = False) -> list[str]:
    return [token.term for token in tokenize(text, stem)]


def analyze(text: str, stem: bool = False, bigrams: bool = False) -> list[str]:
    """Features for TF-IDF: word terms, plus optional two-word phrases.

    A two-word phrase is only formed from words that are next to each other in
    the original text (separated by spaces alone), so no phrase is invented by
    removing the words in between.
    """
    tokens = tokenize(text, stem)
    features = [token.term for token in tokens]
    if bigrams:
        for left, right in zip(tokens, tokens[1:]):
            if left.end == 0 and right.start == 0:
                continue
            gap = text[left.end : right.start]
            if gap and gap.strip() == "":
                features.append(f"{left.term} {right.term}")
    return features


def meaningful_terms(text: str) -> list[str]:
    """Distinct terms that say something specific (used to judge vague input)."""
    seen: list[str] = []
    for term in unigram_terms(text):
        if term == NEGATION_TERM or term in GENERIC_WORDS:
            continue
        if term not in seen:
            seen.append(term)
    return seen


def split_sentences(text: str) -> list[str]:
    """Split on sentence-ending punctuation followed by whitespace."""
    return [part for part in _SENTENCE_END_RE.split(text.strip()) if part]


def highlight_segments(text: str, terms: set[str], stem: bool = False) -> list[tuple[str, bool]]:
    """Cut text into runs marked True where a token's term is in `terms`.

    Joining the returned pieces always reproduces `text` exactly.
    """
    if not text:
        return []
    pieces: list[tuple[str, bool]] = []
    cursor = 0
    for token in tokenize(text, stem):
        if token.term not in terms or token.end <= token.start:
            continue
        if token.start > cursor:
            pieces.append((text[cursor : token.start], False))
        pieces.append((text[token.start : token.end], True))
        cursor = token.end
    if cursor < len(text):
        pieces.append((text[cursor:], False))
    return pieces
