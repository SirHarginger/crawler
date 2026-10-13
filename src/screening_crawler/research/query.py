"""The only question the egress will ask a search provider on the engine's behalf.

A query is a small typed record, built in trusted code and nowhere else: the subject's name,
what kind of subject it is, and optionally a role, an organisation, a country and a birth
year. There is no field for a date of birth, an identity number, an address, a URL or free
text, and the string a provider receives is assembled here (`text`), never by a caller.

Each field is validated for what it is, after one normalisation step (Unicode NFKC, with
typographic apostrophes and hyphens folded to the plain ones):

- every field: letters and marks of any script, spaces and ``. , ' - ( ) &``; no quote, colon,
  slash or ``@``, so no search operator, URL or e-mail address; a hyphen only between two parts
  of a word; the capitalised words OR, AND and NOT only as the first word of a value ("OR Tambo
  International Airport"), never between two others, where a search engine reads an operator;
- a person's `name`: no digits;
- `role`, `organisation` and the `name` of an organisation: digits are allowed, because real
  organisations and titles carry them ("37 Military Hospital", "Channel 4", "2nd Deputy
  Speaker"), but not in the shapes of an identifier or a date: more than six digits in all,
  more than one free-standing number, numbers joined by hyphens or by several separators, a
  number beside a month name, or a domain name.

This narrows what can be sent; it cannot make free text safe. A caller who is determined to can
still put a second person's name in `name`, spell a date out in words, or write a short street
address as an organisation ("12 Ring Road"). The contract makes a mistake hard, not a bypass
impossible. Which fields may be sent for an investigation, and whether the subject may be
researched at all, is decided by the engine's controller from its own records, not here.

`subject_kind` says what kind of subject it is. It is a statement by the code that built the
query; nothing in this file can check it.

This file is identical in the crawler (`screening_crawler/research/query.py`) and the engine
(`screening_engine/external_research/query.py`); a test in each repository fails if they drift.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

QUERY_POLICY_VERSION = "research_query_v4"
_ALLOWED_PUNCTUATION = frozenset(" .,'-()&")
# One spelling for the apostrophes and hyphens people really type and publishers really print.
_FOLDED = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'", "\u2010": "-",
                         "\u2011": "-", "\u2012": "-", "\u2013": "-"})
_MONTH = (r"(jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|aug(ust)?|"
          r"sep(t(ember)?)?|oct(ober)?|nov(ember)?|dec(ember)?)")
# A month name next to a number, in either order: a date written out.
_DATE_IN_WORDS = re.compile(
    rf"(?<![^\W_])(\d+(st|nd|rd|th)?[\s,.-]+(of\s+)?{_MONTH}|{_MONTH}[\s,.-]+\d+)(?![^\W\d_])",
    re.IGNORECASE)
# Two labels of two or more characters joined by a dot: "gmail.com", "www.example". Initials
# ("U.S.", "J.P.", "Ph.D.") and decimals ("97.3") are not this shape.
_DOMAIN_SHAPE = re.compile(r"[^\W_]{2,}\.[^\W\d_]{2,}")
# A hyphen joins two parts of a name ("Akufo-Addo"). Anywhere else -- at the start or end of a
# word, or after punctuation -- it is a search engine's exclusion operator.
_JOINING_HYPHEN = re.compile(r"(?<=[^\W_])-(?=[^\W_])")
_NUMBERS_JOINED = re.compile(r"\d-\d|\d[.,]\d+[.,]\d")  # 1970-02-03, 12-3456, 3,2,1970
_FREE_NUMBER = re.compile(r"\d+([.,]\d+)?(st|nd|rd|th)?", re.IGNORECASE)
_OPERATOR_WORDS = frozenset({"OR", "AND", "NOT"})  # operators only in capitals
_MAX_CLEAN_CHARS = {"name": 120, "role": 80, "organisation": 120}
_MAX_DIGITS = 6


def _clean(value: str, *, kind: Literal["name", "role", "organisation"]) -> str:
    text = " ".join(unicodedata.normalize("NFKC", value).translate(_FOLDED).split())
    if not text:
        raise ValueError("must not be empty")
    if len(text) > _MAX_CLEAN_CHARS[kind]:
        raise ValueError("too long")  # checked again here: normalising can lengthen a value
    for ch in text:
        category = unicodedata.category(ch)
        if ch in _ALLOWED_PUNCTUATION or category[0] in ("L", "M"):
            continue
        if kind != "name" and category == "Nd":
            continue
        raise ValueError("contains a character that is not allowed in a research query")
    if "-" in _JOINING_HYPHEN.sub("", text):
        raise ValueError("a hyphen may only join two parts of a word")
    words = text.split(" ")
    if not _OPERATOR_WORDS.isdisjoint(word.strip(".,'()&") for word in words[1:]):
        raise ValueError("contains a search operator")
    if kind != "name":
        digits = sum(ch.isdigit() for ch in text)
        free_numbers = sum(bool(_FREE_NUMBER.fullmatch(word.strip(".,'()&"))) for word in words)
        if digits > _MAX_DIGITS or free_numbers > 1 or _NUMBERS_JOINED.search(text):
            raise ValueError("looks like an identifier, a telephone number or a date")
        if _DATE_IN_WORDS.search(text):
            raise ValueError("looks like a date")
        if _DOMAIN_SHAPE.search(text):
            raise ValueError("looks like a domain name")
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
    def _name(cls, value: str, info: ValidationInfo) -> str:
        # An organisation's name is an organisation name: "37 Military Hospital" is a subject.
        organisation = info.data.get("subject_kind") == "PUBLIC_INTEREST_ORGANISATION"
        return _clean(value, kind="organisation" if organisation else "name")

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
