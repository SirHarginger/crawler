"""The only question the egress will ask a search provider on the engine's behalf.

A query is built here, from structured fields, and nowhere else. There is no field for a date of
birth, an identity number or an address, and every field is checked: digits are allowed only
inside an organisation's name token ("G4S", "3news"), never as a number on its own; month
names and domain shapes are refused in a role or organisation; search operators are refused
everywhere.

This narrows what can be sent; it cannot make free text safe. A caller who is determined to can
still put a second person's name in `name`, spell a date or an address out in words, or write
a domain without its dots; and `name` is not checked for month names or domain shapes at all,
because real names contain both ("Theresa May", "J.A. Kufuor"). The contract makes a mistake
hard, not a bypass impossible. One numbered token is still allowed in an organisation, so a
house number written as one token ("12A Ring Road") passes, and so does any one short token
with two digits in it, which cannot be told from "G4S". Deriving role and organisation from
canonical records, so that no caller text travels at all, is the stronger control and is not
built yet.

`subject_kind` is the caller's statement that the subject is a public figure or a
public-interest organisation. Nothing here can check it.

This file is identical in the crawler (`screening_crawler/research/query.py`) and the engine
(`screening_engine/external_research/query.py`); a test in each repository fails if they drift.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

QUERY_POLICY_VERSION = "research_query_v3"
# No colon, quote, slash, @ or operator characters: a field cannot carry a search operator, a
# URL or an e-mail address.
_ALLOWED_PUNCTUATION = frozenset(" .,'-()&")
_MONTH = re.compile(
    r"(?<![^\W\d_])(jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|aug(ust)?|"
    r"sep(t(ember)?)?|oct(ober)?|nov(ember)?|dec(ember)?)(?![^\W\d_])", re.IGNORECASE)
_DOMAIN_SHAPE = re.compile(r"[^\W\d_]\.[^\W\d_]")  # letter.letter: "gmail.com", "www.x"
# A hyphen joins two parts of a name ("Akufo-Addo"). Anywhere else -- at the start or end of a
# word, or after punctuation -- it is a search engine's exclusion operator.
_JOINING_HYPHEN = re.compile(r"(?<=[^\W_])-(?=[^\W_])")
_ORDINAL = re.compile(r"\d(st|nd|rd|th)\b", re.IGNORECASE)
_OPERATOR_WORDS = frozenset({"OR", "AND", "NOT"})  # operators only in capitals
_MAX_CLEAN_CHARS = {"name": 120, "role": 80, "organisation": 120}


def _clean(value: str, *, kind: Literal["name", "role", "organisation"]) -> str:
    text = " ".join(unicodedata.normalize("NFKC", value).split())
    if not text:
        raise ValueError("must not be empty")
    for ch in text:
        category = unicodedata.category(ch)
        if ch in _ALLOWED_PUNCTUATION or category[0] in ("L", "M"):
            continue
        if kind == "organisation" and category == "Nd":
            continue
        raise ValueError("contains a character that is not allowed in a research query")
    if len(text) > _MAX_CLEAN_CHARS[kind]:
        raise ValueError("too long")  # checked again here: normalising can lengthen a value
    if "-" in _JOINING_HYPHEN.sub("", text):
        raise ValueError("a hyphen may only join two parts of a word")
    tokens = [token for token in re.split(r"[\s.,'()&-]+", text) if token]
    if not _OPERATOR_WORDS.isdisjoint(tokens):
        raise ValueError("contains a search operator")
    if kind == "organisation":
        digits = sum(ch.isdigit() for ch in text)
        numbered = [token for token in tokens if any(ch.isdigit() for ch in token)]
        # A digit belongs inside one name token ("G4S", "3news"). A number standing alone, a
        # second numbered token or an ordinal is a house number, a day, a year or part of an
        # identifier.
        if (digits > 2 or len(numbered) > 1 or any(token.isdigit() for token in numbered)
                or _ORDINAL.search(text)):
            raise ValueError("an organisation name may not carry a number")
    # A role or organisation never needs a month name or a domain.
    if kind != "name" and (_MONTH.search(text) or _DOMAIN_SHAPE.search(text)):
        raise ValueError("looks like a date or a domain")
    return text


class ResearchQuery(BaseModel):
    """Minimum data about a public figure or public-interest organisation."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    # The caller states which it is. Research on private individuals is not offered.
    subject_kind: Literal["PUBLIC_FIGURE", "PUBLIC_INTEREST_ORGANISATION"]
    name: str = Field(min_length=2, max_length=120)
    role: str | None = Field(default=None, max_length=80)
    organisation: str | None = Field(default=None, max_length=120)
    country: str | None = Field(default=None, pattern=r"^[a-z]{2}$")
    # A year only, and only when it is needed to tell people apart. Never a full date.
    birth_year: int | None = Field(default=None, ge=1900)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _clean(value, kind="name")

    @field_validator("role")
    @classmethod
    def _role(cls, value: str | None) -> str | None:
        return None if value is None else _clean(value, kind="role")

    @field_validator("organisation")
    @classmethod
    def _organisation(cls, value: str | None) -> str | None:
        return None if value is None else _clean(value, kind="organisation")

    @field_validator("birth_year")
    @classmethod
    def _year(cls, value: int | None) -> int | None:
        if value is not None and value > datetime.now(UTC).year:
            raise ValueError("birth_year is in the future")
        return value

    def text(self) -> str:
        """The exact string sent to the provider. Deterministic."""
        parts = [self.name, self.role, self.organisation]
        if self.birth_year is not None:
            parts.append(f"born {self.birth_year}")
        return " ".join(p for p in parts if p)

    def fields_used(self) -> list[str]:
        """Which fields carry a value: recorded with a query in place of the values."""
        return sorted(name for name, value in self.model_dump().items() if value is not None)

    def sha256(self) -> str:
        """Identifies the query in logs and records without repeating it. A pseudonym, not
        anonymisation: the text is short and guessable."""
        canonical = f"{QUERY_POLICY_VERSION}|{self.country or ''}|{self.text()}"
        return hashlib.sha256(canonical.encode()).hexdigest()
