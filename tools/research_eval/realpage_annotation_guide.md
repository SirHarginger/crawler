# E1.1: checking the labels by hand

The labels in `realpage_labels.json` and the judgements in `realpage_judgements.json` were
drafted by the implementing AI. The protocol requires a person. Until a person has checked
them, every result says `human_annotation_complete: false` and is provisional.

Do the two parts in this order, and do not open a result file or the extractor's output
before the first part is finished.

## Part 1: labels, from the page

For each case in `realpage_cases.json`:

1. Open the page in a browser. Use the saved snapshot (`<snapshots>/<id>.bin`, renamed to
   `.html`) so that you see what was scored; the live page may have changed since
   `fetch.retrieved_at`.
2. Check `page_type`, `supported` and `acceptable_routes` against what you see.
3. Check each `expected` segment: it is on the page as shown, it is part of the page's own
   content, and `critical` is right (without it the evidence would be useless).
4. Check each `forbidden` segment: it is navigation, a cookie banner, footer or legal text,
   a teaser or headline for another item, a control, or text the page does not show.
5. Add what the draft missed. The draft was made from a condensed digest of a plain dump of
   the markup (headings, the first and last lines, a few long ones), not from a rendered
   page. It saw little of the middle of a page and cannot tell what a stylesheet hides or
   how a page is laid out. Related-story lists are where it missed most.
6. Set `annotation_status` to `human_verified` and add your initials to `annotated_from`.

Keep segments short (160 characters at most), quote them exactly, and do not use a date of
birth, an address, a telephone number or an e-mail address as a segment. Prefer a neutral
sentence to an allegation about a named person, and a public role to personal history; the
draft has segments of both kinds that you may want to replace. Change a label
because the page says so, never because of how the extractor scored.

## Part 2: judgements, from the output

Only after Part 1. For each page that returned text, read the returned text and set in
`realpage_judgements.json`:

- `contamination`: `NONE`, `LOW` (a few lines of chrome a reader would skip) or `MATERIAL`
  (chrome or other items that could be mistaken for the page's content, or that bury it);
- `catastrophic`: `null`, `wrong_item`, `hidden_as_evidence` or `empty_despite_content`;
- `known_gap`: `null`, `1` (a card with no heading taken as the article) or `2` (a card whose
  opening also stands under the page's headline);
- `judged_by`: your initials.

Judge from the whole returned text. The draft judgements were made from its first and last
lines only. Decide what counts as `MATERIAL` before you start reading, and write it down; the
rule recorded in the draft was written while judging and need not be yours. Judge every
supported page, including one that was refused although it has content
(`empty_despite_content`).

## Keeping and deleting the snapshots

The snapshots are third-party pages, and some (Parliament member profiles) carry personal
data that Parliament publishes: date of birth, religion, hometown, e-mail address and
telephone extension. Keep the snapshot directory readable by you alone, keep it only until
the labels and judgements have been checked and scored, and then delete it. The manifest
keeps each page's URL, retrieval time and hash.

Then score again into a new result file. A result is never overwritten:

    python tools/research_eval/realpage_eval.py score --snapshots <dir> \
        --write tools/research_eval/results/e11-realpage-dev-v1.human.json
