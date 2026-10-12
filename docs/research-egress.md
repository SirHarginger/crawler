# Research egress

`screening_crawler.research` is the only place live-investigation search and page fetches leave
the platform. The screening engine never fetches the public internet
([decisions/crawler-engine-boundary.md](decisions/crawler-engine-boundary.md)); it calls this
service over an internal interface. Optional (`pip install '.[research]'`) and **off by
default**.

## What it does

| Route | Input | Output |
|---|---|---|
| `POST /internal/research/search` | A structured `ResearchQuery` and a limit (1–10) | Signed lead tokens with rank, host and provider. No title or snippet |
| `POST /internal/research/fetch` | One lead token | Extracted plain text with provenance. No markup, no raw bytes |

Both need `Authorization: Bearer <token>`, checked before a body is read. There is no other
route, no route that takes a URL, and no interactive documentation. A rejected request gets a
fixed code (`invalid_request`); what was sent is never echoed. Bodies over 16 KB are refused.

## Rules

- **Minimum query data.** `ResearchQuery` has `subject_kind` (`PUBLIC_FIGURE` or
  `PUBLIC_INTEREST_ORGANISATION`), `name`, `role`, `organisation`, `country` and `birth_year`.
  Nothing else can be sent. Digits are refused except inside one organisation name token
  ("G4S"); ordinals, month names, `letter.letter` domain shapes, `@`, quotes, colons, slashes,
  a hyphen that does not join two parts of a word, and the capitalised words `OR`, `AND` and
  `NOT` are refused; length is checked after Unicode normalisation (policy
  `research_query_v3`). This narrows what can be sent and cannot make free text safe: a
  second name in `name`, a date or an address spelled out in words ("at gmail dot com"), and
  a house number written as one token ("12A Ring Road") still pass. The rules also refuse
  some genuine forms, and fail closed: initials with full stops ("U.S. Department of the
  Treasury", "J.P. Morgan", "M.P."), an organisation with a number of its own ("37 Military
  Hospital", "Channel 4"), an ordinal ("2nd Deputy Speaker"), a month word ("May & Baker"),
  a typographic apostrophe or hyphen ("O’Brien"), and a capitalised "OR" ("OR Tambo").
  `subject_kind` is the caller's assertion and is not verified here. `query.py` is identical
  in the engine.
- **A result is a lead.** The provider adapter keeps a URL, its rank and the provider's date
  estimate. It discards the title, snippet, author and the raw response, and requests no page
  content from the provider.
- **Only issued leads can be fetched.** A lead token is the URL and an expiry (15 minutes)
  with an HMAC over them. It is signed, not secret: a holder can read the URL, and can replay
  the token until it expires, but nothing outside this process can make it fetch a URL of its
  own choosing.
- **One fetch stack.** Pages are fetched by `ResearchGateway` (`research/gateway.py`): GET
  only, http/https on default ports, no credentials in the URL, every resolved address must be
  public, connection pinned to the validated address, every redirect re-validated, no
  downgrade, byte cap, compressed responses refused, robots.txt consulted, one deadline.
  `gateway.py`, `page_text.py` and `query.py` are byte-identical to the engine's copies; each
  repository's tests pin their SHA-256.
- **HTML only**, 2 MB at most. One request per host every two seconds; a fetch that would wait
  more than ten seconds is refused (`host_busy`), at most four fetches run at once, and
  searches have a daily ceiling (200) that is held in memory and so resets on a restart.
- **Web content is data.** Markup is parsed in a child process that is killed after 15
  seconds and limited to 1 GB of memory (`research/extract.py`). Text is extracted there and
  returned bounded (20,000 characters; see "Extraction" below). Links, scripts, forms and instructions
  in a page cause no request. Inside the child, Python's socket calls are replaced by ones
  that refuse and count; a page whose parsing tries one is refused
  (`parser_network_attempt`). This covers code that uses Python's `socket` module, which is
  every pure-Python library. It does not cover a C extension opening a socket of its own, so
  it is a guard, not a sandbox. A page that gives no text is recorded as `js_required`,
  `consent_wall`, `not_a_content_page` or `no_article_text`. The date finder is given a parsed tree and never a string:
  handed a string that begins with a URL, `htmldate` would download it. The tests run with
  sockets disabled and fail if anything tries one; in a deployment nothing but a network
  policy stops the parsing child from opening a socket, and that policy is not set up here.
  The service deletes its `RESEARCH_*` variables from its own environment once it has read
  them, so a parsing child, which inherits the environment of the process that starts it,
  never has them; the child also removes any `RESEARCH_*` name it finds. Deleting a variable
  does not erase it from the block the service itself was started with, which a process of
  the same user can read from `/proc`; on Linux the service marks itself non-dumpable after
  start-up so that a child cannot (best effort, checked by a test, not a sandbox). Any other
  secret in the service's environment is inherited by the child: run the service with none.
- **Logs** carry a query hash, hosts, outcome codes and sizes. They never carry the query, the
  subject, page text or a credential.

## Extraction

Two extractors run inside the parsing child, in a fixed order. Neither uses a model, runs
JavaScript or fetches anything.

1. **Article text** (`page_text.py`, `trafilatura-2.0.0|precision|v1`): Trafilatura in
   precision mode, at least 400 characters and a headline. Its result is used unchanged, with
   the publication date the page states.
2. **Structured text** (`structured_text.py`, `lxml-structured-html|v1`), only when the first
   finds no article. A fixed walk over the tree parsed by lxml's HTML parser: one line per
   heading, paragraph and list item; a row of a data table is one line with ` | ` between
   cells; a definition is `term: value`. The walk starts at `<main>` when the page has one
   that is not hidden, else at `<body>`; an `<article>` is not treated as the page, because
   on these sites it is as often a news card about someone else. Scripts, styles, templates, `noscript`, navigation, page
   headers and footers, forms, controls and elements the markup itself marks hidden are
   dropped whole. At least 200 characters and a headline (the first `<h1>`, else the
   `<title>`). No publication date is returned: a date on a list or landing page says nothing
   reliable about the text taken from it.

Every document carries the `extractor_version` that produced it. **Structured text is lower
precision than article text and must not be read as the same thing.** What it cannot do:

- It reads the markup, not the rendering. Text hidden by a stylesheet, moved off screen or
  made transparent is kept, as is a closed `<details>` and `<del>` text. Hidden text is
  recognised only from the element itself (`hidden`, `aria-hidden="true"`, inline
  `display:none` / `visibility:hidden`, a few framework class names).
- Link lists, related-story cards and sidebars built from plain `<div>`s are kept with the
  page. A profile page can therefore carry lines about other people.
- `rowspan` and `colspan` are not expanded, so a value under a spanning cell can sit one
  column left of its heading. A table that holds another table, a heading in a body cell or
  a cell with more than three blocks, or that has one cell to a row, is read as page layout:
  each cell is its own line, and empty cells and row boundaries are not shown. A page laid
  out in columns with fewer blocks than that is read as a data table, one line to a row. A
  ` | ` or `: ` written by the page cannot be told from a separator.
- A framework class such as `hidden md:block` is read as hidden everywhere.
- Bidirectional-override and zero-width characters pass through, as they do in article text.
- It does not judge what a page is. The egress refuses structured text from a page it reads
  as a script shell or consent wall (`js_required`, `consent_wall`) and from a short page
  (under 1,500 characters) worded like an error, block, sign-in or paywall page
  (`not_a_content_page`). That second check is a list of phrases matched as whole words: it
  will miss other wordings ("We can't find that page"), and a short genuine page that uses
  one ("has been blocked", "not authorised") is refused. A genuine page is refused too when
  it carries an "enable JavaScript" notice, or has under 2,000 visible characters and names
  a consent tool ("onetrust", "cookiebot", "cookie-consent"). The evaluation harness leaves
  `js_required` and `consent_wall` pages out of the extraction rate, as the gate defines, so
  a genuine page lost this way does not lower it; they are listed by id in every result.
- A walk that stops at a limit (200,000 elements or characters), or a page longer than the
  2,000,000 characters the parser is given, sets `text_truncated`.

The article extractor has limits of its own, unchanged here: on a landing page with enough
paragraph text it can return a teaser as the article under the page's title, and it does not
drop `hidden` or `<template>` content the way the structured walk does (seen on this
repository's landing-page fixture with a news card added). `page_text.py` is shared with the
engine and was not changed in this work.

Why this and not something else (recorded 2026-10-12): Trafilatura's documentation offers
`favor_recall`, `baseline()` and `html2txt()` for pages where content is missing, and
describes `html2txt` as a last resort that returns "all text in the document, including
navigation and footers". On this repository's fixtures `favor_recall` returned hidden,
`<template>` and `<noscript>` text as content and repeated definition-list text, and
`html2txt` flattens a table into one line. lxml is already a dependency and its HTML parser
has no DTD, entity or XInclude processing, so the fallback is a small walk over an lxml tree
and no new library was added. A readability port, a headless browser and model extraction
were not considered further: the first is another article heuristic, the others are out of
scope by owner decision.

Parser safety: all three parses (Trafilatura's, the date finder's and the structured walk)
use `lxml.html.HTMLParser`, never an XML parser, with `no_network` on and `huge_tree` off.
A test feeds a page with an external DTD, external and parameter entities, XInclude, an
`xml-stylesheet`, `<base>` and linked resources, and checks that a file it names is never
read and that a local listener it names is never contacted. The pinned lxml wheel bundles a
libxml2 built without an HTTP or FTP client, so the listener check cannot fail on this
build; a test asserts that build property, and it does not hold for an lxml built against a
system libxml2. libxml2 is quadratic in the number of distinct attribute names on one
element; such a page is killed at the time limit and yields nothing.

## Running it

```
RESEARCH_EGRESS_ENABLED=true          # exactly "true"
RESEARCH_EGRESS_TOKEN=...             # shared with the engine, 32+ characters
RESEARCH_LEAD_SECRET=...              # signs lead tokens, 32+ characters
RESEARCH_EXA_API_KEY=...              # search provider credential
RESEARCH_EGRESS_HOST=127.0.0.1        # loopback or private only
RESEARCH_EGRESS_PORT=8090
screening-research-egress
```

It refuses to start if any of these is missing or the host is not loopback or private.

This gives the crawler side three credentials it did not hold before: the provider key, the
bearer token and the lead-signing secret. It still holds no database or search-index
credential.

## Provider

An Exa adapter (`https://api.exa.ai/search`) sits behind the `SearchProvider` protocol.
**Named-person search on Exa's standard terms is not approved** (owner decision, 2026-10-11):
Exa's privacy policy says query data must not contain personal information. The adapter
refuses a `PUBLIC_FIGURE` query before sending anything unless
`RESEARCH_EXA_NAMED_PERSON_TERMS_APPROVED=true` declares that terms covering that use (a DPA
and zero data retention) are in place. No key has been requested or used and no search
provider has been contacted. No other provider has been chosen.

## DEV evaluation

`tools/research_eval/` holds two things.

**Search (not run).** `subjects.json` and `run.py`: 20 public subjects (Ghana first) and a
harness for lead relevance. Owner-set pilot gates: API technical success at least 95%,
relevant-lead Hit@5 at least 90%; official-source Hit@5 is reported on the pre-labelled
subset and is not a gate. It refuses to run until a provider's terms permit named-person
queries.

**Fetch and extraction (run once, 2026-10-11).** `fetch_urls.json` (frozen, SHA-256
`69fc1309fb9c8aeac0e8ddf1d60717b0551429d5b7f61497802989d027722ab4`) and `fetch_eval.py`: 32
public URLs from Ghana government and Parliament, sanctions authorities and registered
Ghanaian publishers, fetched through the egress with a provider that only returns the frozen
list. Result in `results/fetch-dev-v1.json`: fetch 29/30, usable extraction 21/28 (**below the
90% gate**), mention recovery 19/19, no network policy violation and no ungoverned request in
55 connections. Every failure was an official profile, list or landing page that the
article extractor rejects.

**Second run (2026-10-12), after the structured-text fallback.** Same frozen list, labels
and gates; result in `results/fetch-dev-v1.run2-structured-v1.json`, first result untouched.
Fetch 29/30, usable extraction 27/28 (96.4%, gate met), mention recovery 23/23, no network
policy violation and no ungoverned request in 55 connections. Six of the seven failed pages
now give structured text (`gov-01` to `gov-05`, `gov-07`); `san-03` still gives nothing; the
21 article pages still come from the article extractor. The rate counts a page that yielded
text; it does not measure the quality of that text, the harness keeps none, and the
structured output of those six pages has not been read by a person.

## Not covered

PDFs, JavaScript-rendered pages, cookie or consent walls; a fetch cache; rate limiting across
processes; network-level egress restriction for this process (a deployment matter).
