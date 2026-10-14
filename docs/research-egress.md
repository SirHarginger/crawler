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

- **Minimum query data.** `ResearchQuery` is a small typed record: `subject_kind`
  (`PUBLIC_FIGURE` or `PUBLIC_INTEREST_ORGANISATION`), `name`, `role`, `organisation`,
  `country` and `birth_year`. Nothing else can be sent, and the string a provider receives
  is assembled in trusted code. Each field is validated for what it is, after one
  normalisation step (NFKC, typographic apostrophes and hyphens folded): letters of any
  script and a few punctuation marks everywhere; no quote, colon, slash or `@`; a hyphen only
  between two parts of a word; `OR`, `AND` and `NOT` in capitals only as a first word. A name
  carries no digits. A role or an organisation may ("37 Military Hospital", "Channel 4",
  "2nd Deputy Speaker", "U.S. Department of the Treasury"), but not in the shape of an
  identifier, a telephone number, a date or a domain (policy `research_query_v4`). This
  narrows what can be sent and cannot make free text safe: a second name in `name`, a date
  spelled out in words and a short street address ("12 Ring Road") still pass. `subject_kind`
  is a statement by the code that built the query and is not verified here; in the engine
  the controller sets it from its own records. `query.py` is identical in the engine.
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

The page is parsed once inside the parsing child. `<template>` and the elements the markup
marks as hidden (the `hidden` attribute, `aria-hidden="true"`, an inline `display:none`,
`visibility:hidden` or `content-visibility:hidden` declaration, a few framework class names)
are removed before anything reads the page. Then two extractors, in a fixed order. Neither
uses a model, runs JavaScript or fetches anything.

1. **Article text** (`trafilatura-2.0.0|precision|v1+declared-visible|v1`): the shared
   article extractor (`page_text.py`: Trafilatura in precision mode, at least 400 characters
   and a headline), given the page without those elements, and used only when all hold:
   - the page declares an article in standard markup: Open Graph `og:type` of `article`, an
     `article:` property, or its `<h1>` inside `<article>` or inside an element with a
     schema.org article `itemtype`. Many sites declare this on every page, so it permits
     the article path and decides nothing by itself;
   - the structured walk found the page readable (below);
   - the page's running text (visible text outside links, from the content root) is no more
     than twice the article text;
   - the first full line of the article text (40 characters or more: a date, a place or a
     byline is skipped) is found in what the walk read from the content root, at a place
     where the nearest heading (`<h1>` to `<h6>`) at or above it is the page's own. Text
     that sits under another heading is an item on the page with a title of its own, whether
     or not the article extractor kept that title; text above the page's headline is
     something placed before the page;
   - the page, once re-serialised, still fits in the 2,000,000 characters the article
     extractor reads. Re-serialising escapes and quotes, and can lengthen a page by up to
     about three times.
   The publication date the page states is returned with it.
2. **Structured text** (`structured_text.py`, `lxml-structured-html|v2`) for every other
   page. A fixed walk over the tree parsed by lxml's HTML parser: one line per
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

Why the article path has two conditions (E1, 2026-10-12). Measured before anything was
changed, the bare article extractor:

- kept `hidden`, `<template>` and `hidden="until-found"` text. Trafilatura 2.0.0 strips the
  `<template>` tag and keeps what is inside it, and has no rule for the `hidden` attribute;
- returned a two-paragraph news card as the whole article of a profile, table or index page,
  under that page's headline, with the page's own content gone;
- on live pages, returned a cookie banner as the article of one official page and the first
  617 of about 9,000 characters of another.

Removing these elements before extraction is what Mozilla's Readability does
(`_isProbablyVisible`: inline `display:none`, `visibility:hidden`, the `hidden` attribute,
`aria-hidden="true"`), and the HTML standard says an element in the hidden state should not
be rendered. The factor of two is the one Trafilatura itself uses to prefer another
extraction of the same page (`compare_extraction`). The declaration is the page's own
statement, in Open Graph or schema.org terms; all five Ghanaian publishers sampled make it
and none of the six official pages sampled do, which is a small sample. No threshold was
fitted to the sample: gaps and length ratios between a heading and the text were measured
and separated the cases by too little to justify a number. The opening-line condition was
added after an independent review showed that a declaration alone brought the card failure
straight back on any page that declares an article and is not one, and made positional after
a second review showed that the article extractor drops a card's heading for most card
markups, so a check on the article's own first line saw nothing wrong. A third showed that
looking only at the first place the line occurs let a card through when its first line was
a date or a place the page also stated. Requiring every occurrence to qualify fixed that
and demoted a live publisher's articles, which print the first sentence twice (a standfirst
under the headline, then the body below a "listen" widget with its own heading); short lines
are skipped instead, and one qualifying place is enough. The 40 characters are a project
choice, not a measured constant.

What this costs, knowingly:

- A page that reads like an article and declares nothing (a ministry press release, for
  example) is returned as structured text: complete, but with the site's menus where they
  are built from plain `<div>`s, and without a publication date.
- **Known gap.** A news card with no `<h1>` to `<h6>` heading of its own, below the page's
  headline inside the content root of a page that declares an article, and at least half of
  that page's running text, is still returned as the article under the page's headline.
  Running text leaves out link text, so a page made of links (a linked table, an index of
  members) is "short" for this purpose however long it looks. A card titled with `<b>`,
  `<strong>`, a styled `<p>` or `<div>`, or a bare link looks headed to a reader and is in
  the same position, as is a card whose heading the walk does not read as a heading: one
  inside a `<header>`, an `<aside>` or a data-table cell, or marked `aria-hidden`. The same
  holds on a page whose own `<h1>` is in a dropped `<header>`, or that has no headings at
  all. Nothing the walk reads separates such a card from the page. A test holds the plain
  case so that it is not forgotten.
- **Known gap, second.** A card with a heading of its own is still taken as the article
  when its first line of 40 characters or more also stands under the page's own headline:
  a page that lists a headline as a link directly under its `<h1>` and carries the same
  story as a card, a tagline of that length printed in both places, shared boilerplate.
  This cannot be closed by position, because it is the same structure as a real
  publisher's article that prints its first sentence twice. It needs the same 40 or more
  characters in both places, the page's copy under no sub-heading, and the card at least
  half of the running text. A test holds the linked-headline case.
- A genuine article whose first paragraph sits under a second heading (a standfirst or a
  subhead set as `<h2>` directly under the headline), or whose `<h1>` comes after its text,
  is returned as structured text. So is one with no line of 40 characters or more, and one
  whose `<h1>` is two long lines split by `<br>`.
- On a page with a `<main>`, the content root is `<main>`. A declared article that sits
  outside it, or is split across it, is returned as what `<main>` holds, as structured text
  and without a truncation flag; before, the article path returned the body.
- When the page's own content is in `<main>` and a card or banner sits outside it, the
  article extractor may still append that card or banner after the page's content; the
  opening is the page's, so the conditions hold. Lower quality, not another page's text
  under this one's headline.
- `aria-hidden="true"` and the screen-reader class names (`sr-only`, `visually-hidden`,
  `screen-reader-text`) mark text that is on screen or spoken, not text that is absent. It
  is dropped all the same, as Readability drops it. Where a page shows a date to the eye
  and repeats it for a screen reader, both copies are lost; where a wrapper around the whole
  article carries one of these marks, the page yields nothing.
- A page that is demoted to structured text is then subject to the structured-text
  refusals below: an article that declares nothing and carries an "enable JavaScript"
  notice, names a consent tool, or is short and uses one of the error-page wordings is
  refused, where before it was returned as article text.
- Trafilatura has a looser rule of its own for the article path: it drops any `div`, `p`,
  `section` or `span` whose `style` merely contains "hidden" or "display:none".
- An article whose page also carries a great deal of running text outside links (long
  server-rendered comments, for example) is returned as structured text.
- An inline element that a browser does not draw (`hidden`, an inline hiding style) is
  removed without leaving a space, so the word around it stays whole as it does on screen.
  A hidden block, and an inline element that is drawn but dropped (`aria-hidden`, the
  screen-reader classes: often a separator between two names), leaves one. Structured text changed in the same
  way, which is why it is now `v2`.
- The article text is not always byte-identical to what the bare extractor returns, because
  it reads a re-serialised tree; on one of five live articles a "latest stories" tail came
  out with its timestamps. Hence the separate version. `page_text.py` itself is unchanged
  and still shared with the engine, whose own use of it (the visual-investigation path)
  does not have these conditions.
- Neither path removes a block of related headlines that a publisher places inside the
  article's own container, or a site menu built from `<div>`s on a structured page. On the
  judiciary's pages about sixty menu lines precede the list of judges.

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

## Staging mode

`RESEARCH_EGRESS_MODE=staging-fixtures` with `RESEARCH_FIXTURE_DIR` (and no provider key: the
two are refused together) runs the whole egress with no internet (`research/staging.py`). The
search provider answers from the directory's `manifest.json`: subjects by name, each with the
local pages that are its leads. The gateway is the real one with an in-process transport that
serves those pages and a resolver that knows one host, which must end in `.invalid`, a
top-level domain reserved never to exist. Every other name is a DNS failure, so a fixture
page that links to a real site cannot make this process reach it.

Everything between provider and transport is the production code: lead tokens are signed and
redeemed, the gateway validates scheme, port, address, robots and size, pages are parsed in
the killable child, and extraction and refusals are unchanged. The pages in
`tests/fixtures/research_staging/` are synthetic: an official-style profile, a news article,
a page of hostile instructions, a page that is gone and one that is not HTML. This mode
exists so the engine's staging investigation can be demonstrated end to end
(`docs/external-research.md` in the engine, "Staging"). It proves the integration, not live
retrieval.

### Provider restriction

Every search request names the only provider its caller permits (`"provider":
"staging-fixtures"`); since A2.1 the field is required, and a request without it, or with
`null`, is refused as `invalid_request` before the provider is asked (closes N1-a). In
process, `ResearchEgress.search` requires it too, and refuses an empty or different one before
the provider is asked (closes R2). The egress compares it with the provider it is configured
with and, if they differ, answers `403 provider_not_permitted` **before** the daily budget is
touched and before the provider is asked. A refused search sends nothing to any provider. A
successful answer names the provider that answered (`"provider"`), which the engine checks as
a second line. The engine's staging investigation always sends the restriction, from a
constant, so an engine pointed at a live-mode egress gets a refusal, never a live search. An
egress older than this field refuses any request carrying it as `invalid_request` (the
request model forbids unknown fields), again before searching.

This is deny-by-default authorisation enforced at the server, the egress, not in the caller
(OWASP Authorization Cheat Sheet, "Deny by Default", "Verify that Authorization Checks are
Performed in the Right Location"; retrieved 2026-10-13). Rejected: checking the provider only
in the engine after the answer, because the search has already happened by then; a separate
staging egress token, because the token authenticates the engine, not what it may ask for, and
would add a credential without stopping a misconfiguration; an attestation endpoint the engine
asks first, because the mode could change between the two requests.

### Provider approval

Naming a provider is not permission to use it. `search.APPROVED_LIVE_PROVIDERS` lists the
providers that may be used against the network, **and it is empty**: as committed no live
search or live fetch is possible from any path -- the HTTP service in any mode, an in-process
caller, or an evaluation tool. It is enforced in three places:

- `ResearchEgress.search` refuses any provider not on the list, centrally, before the daily
  budget and before the provider is asked (`search_provider_not_approved`), whatever the
  provider is;
- `ResearchEgress.fetch` refuses a lead whose provider is no longer on the list, before any
  request;
- each network-capable provider (Exa, curated) checks the list itself before sending anything;
  Exa's name and endpoint are fixed for the class, so no instance can borrow an approved name.

The one exemption is an egress whose gateway is **offline**: the fixture gateway or a test
gateway over an in-memory `httpx.MockTransport`, or a stand-in that replays stored bytes. That
is a test seam, decided by the gateway the egress was given, not a defence against code in
this process, which could do anything; no gateway in `src/` or `tools/` is offline except the
staging fixtures. Approving a provider is a code change citing the owner decision in the
engine's `docs/decisions/research-access.md`, which also needs the engine's own list changed.

### Source policy (live and curated modes)

A live or curated egress needs `RESEARCH_SOURCE_DOMAINS`, the approved source domains, and
refuses to start without it. Each entry must be a registrable domain under the Public Suffix
List, or a host beneath one, checked with the pinned `publicsuffixlist` package (its list is
bundled; nothing is downloaded at run time). A public suffix -- `gov.gh`, `com.gh`, `co.uk`,
`github.io` -- would approve every site registered under it and is refused, as is a domain
under a top-level domain the list does not know, an IP address, a wildcard, a URL, and
anything with a port or credentials (closes R3). Entries are ASCII: an internationalised name
is given in its `xn--` form, the form the gateway resolves, because two IDNA standards map
some Unicode names to different hosts. A domain approves itself and its subdomains, never a
sibling: `bog.gov.gh` approves `www.bog.gov.gh` and not `mofep.gov.gh` or `evilbog.gov.gh`;
`www.bog.gov.gh` approves only that host and below. Which domains are approved is an owner
decision (engine `docs/decisions/research-access.md`); no list is shipped.

The list is enforced in the gateway's resolver (`research/source_policy.py`), which the
gateway calls for every hop -- the first request, each redirect and robots.txt -- before DNS
and before any connection: a lead or a redirect to any other host is refused as
`source_not_permitted` (HTTP 403), and nothing is sent to it. The gateway itself
(`gateway.py`, shared with the engine and hash-pinned) is unchanged. `policy_gateway` is the
only constructor of a gateway that can reach a network; the resolver re-validates its domains
however it is built; the egress refuses to be built on any other network gateway and checks
again before each fetch. A test keeps `ResearchGateway(` out of `src/` and `tools/` except in
`policy_gateway` and the fixture gateway; it matches text, so an aliased import would evade
it -- the egress checks, not the test, are the boundary for every egress path.

The evaluation tools (`tools/research_eval/`) fetch live only with `--live`, an approved
provider (`frozen-list`, `frozen-page-list`, `exa`: none is approved) and `--source-domains`;
`realpage_eval`'s `select` and `snapshot`, which use the gateway without an egress, check the
approval and build it through `policy_gateway` first. Without them each refuses and fetches
nothing.

### Curated mode

`RESEARCH_EGRESS_MODE=curated` answers a search from a reviewed list of official pages per
subject (`RESEARCH_CURATED_SOURCES`, JSON: `{"subjects": [{"name": ..., "pages": [...]}]}`)
instead of a search provider, so no third party sees a subject's name (`research/curated.py`).
Every page must be a plain https URL on an approved source domain, at most five per subject,
checked at start-up; the mode refuses to start with a provider key. A subject the list does
not name gets no leads. Everything after the lead is the live path: signed leads, the real
gateway behind the source policy, the parsing child, the extractors. Its provider,
`curated-official`, is network-capable and therefore subject to the approval list: as
committed it answers every search `provider_not_approved`.

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

**Evidence quality (E1, 2026-10-12).** `quality_eval.py` runs synthetic pages, each built for
one way extraction goes wrong, through the real parsing child, offline. Each case states the
text a reader sees, the text that must not appear, the headline and the expected extractor.
A result keeps counts and labels and no page text, and is never overwritten. The gates are
project gates, set before the extractor was changed and not altered since: every expected
segment recovered, no forbidden segment, right headline and path on every case, identical
output on a repeat, no network attempt, under 15 seconds and 20,000 characters a page.

Two frozen case files:

- `quality_cases.json` (v1, 20 cases, SHA-256
  `a321b13fe9186b761f17b78cae80ed2dc0923a5ffd7023fe1f66e7ef23f7a736`), frozen before any
  change. A first draft was run once before freezing; that run exposed a broken fixture and
  led to five more cases, and its result was discarded (the file's `history` says so).
- `quality_cases_v2.json` (v2, the same 20 plus 13, SHA-256
  `6a70efa29390fee3f41856dece2059e5b4e83a256dcea9e08fbaa5281076f243`), added after an
  independent review showed v1 could not tell whether the rules worked: every article
  fixture in v1 declares an article and no other fixture does. v2 was frozen and run against
  the committed extractor and against the first change before that change was revised.

| Run (`results/…`) | Cases passing | Expected segments | Forbidden segments | Pages led by another item | Expected extractor |
|---|---|---|---|---|---|
| v1 `baseline` (committed extractor) | 15/20 | 64/73 | 3 | 3 | 16/20 |
| v1 `after` (first change) | 20/20 | 73/73 | 0 | 0 | 20/20 |
| v1 `final` (revised after review) | 20/20 | 73/73 | 0 | 0 | 20/20 |
| v2 `baseline` (committed extractor) | 17/33 | 78/117 | 6 | 10 | 18/33 |
| v2 `first-change` | 24/33 | 92/117 | 2 | 7 | 24/33 |
| v2 `final` (revised after review) | 32/33 | 114/117 | 0 | 1 | 32/33 |
| v1 `revision-2` (revised after re-review) | 20/20 | 73/73 | 0 | 0 | 20/20 |
| v2 `revision-2` | 32/33 | 114/117 | 0 | 1 | 32/33 |
| v1 `revision-3` (every occurrence must qualify; withdrawn) | 20/20 | 73/73 | 0 | 0 | 20/20 |
| v2 `revision-3` | 32/33 | 114/117 | 0 | 1 | 32/33 |
| v1 `revision-4` (the code as it stands) | 20/20 | 73/73 | 0 | 0 | 20/20 |
| v2 `revision-4` | 32/33 | 114/117 | 0 | 1 | 32/33 |

**v2 does not meet its gate.** One case fails, `r-09`: the known gap above (a card with no
heading element of its own that is most of a short page declaring an article). It was put in
the set knowing it might not be passable, and it stays there.

v2 has a blind spot of its own, found on re-review: every headed card in it is an
`<article>` or a `class="story"` block, which is the markup where the article extractor
keeps the card's heading, and so where a check on the article's first line was enough. The
later revisions changed no v2 row, and the third, which the case sets passed, demoted a
live publisher's article: only the hand-read live pages showed that. The card markups that defeated the first revision
(`class="card"`, no class, a grid column, a list item, a card above the headline, a page
with no `<h1>`), and the short repeated first line that defeated the second, are held by
unit tests, not by a case file.

This is development evidence: the extractor was changed in view of these cases, so the
results show the listed properties on these pages and nothing about pages in general.

Review. One independent read-only reviewer made five passes over this change. The first
three returned BLOCKER, each for a way a news card was still returned as a page's article;
the fourth, on the code as it stands, returned PASS with non-blocking findings, which were
documentation and two tests and are in. Its closing remark is kept here because it is the
honest state of the rule: it "has now been revised four times against constructed shapes",
a further pass would find it narrower and not complete, and a labelled sample of real pages
would say more than another round of constructed ones.

Fourteen pages of the frozen fetch list were also fetched once through the gateway, kept
outside the repository, and read by hand before and after. The six Ghana government and
Parliament pages and four of five news articles came out identical. The page that had
yielded only a cookie banner now yields its content as article text, and the page that had
yielded a 617-character fragment now yields its whole text as structured text. One EU page
that declares `og:type` `website` moved from article text to structured text and lost its
date. The frozen fetch list was not run a third time, so its recorded rates describe the
extractor as it was on the second run.

**Real pages (E1.1, 2026-10-13; provisional, not yet labelled by a person).**
`realpage_eval.py` scores the extractor as committed (`9f22a06`) on 48 real pages against
labels of short segments that must be present and segments that must be absent, the method of
Trafilatura's benchmark, with boilerplate defined as in the CleanEval annotation guidelines.
The protocol (`realpage_protocol.json`, SHA-256
`2ee1003c9132547b40295773b771860875f3745bd068a04cc572e3967b0db749`) was written before any
page was chosen: sampling rules and seed, annotation instructions, scoring, gates and the
extractor's file hashes. Nothing is committed yet, so that order rests on file times and on
the hashes each later file records.

*The sample.* 32 pages are the frozen fetch list, which the implementer chose by hand on
2026-10-11 from the platform's records and official sites; its 18 news pages are political
stories of early September, several of them the same story at different publishers. 16
more were drawn by a seeded rule from the registered publishers' listings (one each) and from
the official pages' own links; those 16 nobody picked. 14 of the 48 had been read while the
extractor was being changed in E1, and one drawn page (`i-eu-1`) turned out to hold the same
content as one of those. Counted honestly the sample is 47 distinct pages on about eleven
site templates: six publishers; Parliament (four member profiles of one template and the
member list); the judiciary (4); the Ministry of Finance (3); the Bank of Ghana (3); and
eight international pages of which two distinct ones, a GOV.UK publication page and an EU
overview, returned text. There is no ministry but Finance, no agency but the central bank, one table
page, three landing pages, and no usable UN or OFAC page. Results are close to all-or-nothing
per site, so they describe these templates, not page types or publishers in general.

*The labels.* **The protocol requires a person to label every page from the page as
displayed. No person has.** The labels (`realpage_labels.json`) were drafted by the
implementing AI from a condensed digest of a plain dump of each page's markup (headings, the
first and last lines, a few long lines), each segment then checked against the full dump. It
did not see a rendered page, and it did not see most of the middle of a page, so boilerplate
there is under-labelled: on 9 of the 23 pages it later judged contaminated, no forbidden
segment was found. The labels were frozen before the extractor was run. The judgements
(`realpage_judgements.json`) were drafted afterwards from the first and last lines of each
returned text, by a rule written while judging. Every number below is provisional until the
steps in `realpage_annotation_guide.md` have been done, and the noise figures are upper
bounds.

| Gate (project criteria, set before selection) | All 48 pages | Pages not read during E1 (33) | |
|---|---|---|---|
| Catastrophic failure = 0 | 1 of 42 supported pages | 1 of 28 | **not met** |
| Hidden or template text = 0 | 0 of 5 labelled checks | 0 of 3 | met, on almost no evidence |
| Critical segments recovered ≥ 95% | 123/127 (96.9%) | 78/82 (95.1%) | met |
| Page success ≥ 90% | 40/42 (95.2%) | 26/28 (92.9%) | met |
| Forbidden segments absent ≥ 90% | at most 152/184 (82.6%) | at most 101/121 (83.5%) | **not met** |
| Material contamination ≤ 10% of pages | 12/41 (29.3%) | 8/27 (29.6%) | **not met** |
| Articles demoted ≤ 10% | 2/24 (8.3%) | 2/19 (10.5%) | **not established** |

The catastrophic failure is `i-govuk-1`: a GOV.UK landing page with its content in the markup
was refused as needing JavaScript, because the words "requires JavaScript" appear in a
`<noscript>` block of the site's survey link. The same block is on the other GOV.UK page in
the sample, which escaped only because it took the article path. No returned text was judged
to be the wrong item or to carry hidden text; that judgement is the AI's, from the first and
last lines. With 42 pages, one failure is consistent with a
true rate anywhere from under 1% to about 12%; none of the rates here is tight.

Outside every rate: 6 of 48 pages are unsupported (two need JavaScript, one PDF, one empty
response, one HTTP error, one page with no content of its own). **One of those returned text
all the same**: a judiciary "Public Notice" page whose notice appears to be an image came
back as 95
lines of site menu and footer under that headline. Evidence that is entirely chrome is the
worst output in the sample, and the protocol's categories had no name for it.

What the pages showed, by cause:

- **Site menus kept by structured text.** All six supported judiciary and Ministry of Finance
  pages (and the chrome-only page above) open with thirty to sixty lines of menu. In the
  markup these are nested link lists (`ul.nav`, a `superfish` menu, a navigation block
  `div`), not marked as `<nav>`.
- **A list of other stories inside the article container.** All four MyJoyOnline articles
  carry thirty or more lines of other headlines and timestamps after the body; the publisher
  puts its "latest" list inside the element that holds the article. Already recorded in E1.
- **Short articles demoted by the running-text condition.** Both demotions are Ghanaian
  Times pages on which the article extractor had returned the right text (about 500 and 700
  characters) before the condition "article at least half of the running text" rejected it;
  the structured text that came back instead is mostly other headlines. A third short
  article extracted normally.
- **The JavaScript check refusing a page that does not need it** (above).
- Otherwise: 17 of 22 article texts have no menu or related items (13 are the body alone;
  four Ghana News Agency texts also carry byline and editor lines). Parliament, Bank of
  Ghana and EU pages are complete with a few footer or breadcrumb lines. All 14 supported
  Ghanaian official pages returned every critical segment.
- Parliament member profiles come back under the section heading "Members of Parliament",
  not the member's name (4 of 4 member profiles), and carry what Parliament publishes about
  the member, including date of birth, religion, hometown, e-mail address and telephone
  extension. Religion is special-category personal data; nothing here filters it.
- **The two known gaps were not observed, and this says almost nothing about them.** Both
  need a page that declares an article and whose own text is short beside a card. One
  non-news page in the sample declares an article, and the short news pages were demoted
  before the question arose.
- One headline segment was missed only because the page's heading uses a typographic
  apostrophe and the returned headline a plain one. The frozen matching was left as it is;
  with quote folding, critical recovery would be 124/127 and page success 41/42.

Changed after the first scoring, and after an independent review of the method: the tool now
judges catastrophic failure for every supported page, lists an unsupported page that returned
text, and reports unseen pages apart; one field of label metadata was corrected to say what
the labels were really made from (the frozen label content is unchanged, and a test rebuilds
the frozen file to prove it); one judgement was added, one was marked as a duplicate, and the
wording of the Parliament notes and of the file's description of how it was made was
corrected, with no judged value changed. The first results are kept
(`results/e11-realpage-dev-v1.automated.json`, `…provisional.json`, `…provisional-2.json`);
the figures above are from `…provisional-3.json`, scored with the tool as it now stands. No gate, no label content and nothing in the extractor was
changed.

*Status (closed 2026-10-13 as provisional evidence).* The evaluation was performed and its
inputs, tool and results are kept. Human review is limited: no person has labelled or judged
a page, so every metric above is provisional and the noise figures are upper bounds. Three
gates are not met (catastrophic failure, forbidden segments absent, material contamination)
and one is not established (article demotion); E1.1 does not show that the extractor meets
its quality gates. The known limits stand: menu and related-story contamination, demoted
short articles, the false JavaScript refusal, a chrome-only page returned as evidence, and
six unsupported pages. Turning these numbers into findings needs the human steps in
`realpage_annotation_guide.md`; no extraction change follows from E1.1 until then.

## Not covered

PDFs, JavaScript-rendered pages, cookie or consent walls; a fetch cache; rate limiting across
processes; network-level egress restriction for this process (a deployment matter).
