# Using semantic-search-demo

## The web app

`uv run resumes web --open` starts two processes on this machine: the Python engine (127.0.0.1:8770) and the app server
(http://localhost:8765, the page). Stopping one stops the other.

- **The chat** (left) turns a question into filters, shown as chips above the results. Ask for more ("only remote",
  "under 60 an hour", "at least 5 years"), or take one back ("remove the SQL filter"). With 50 people or fewer it can
  rank them against what you asked for ("the talented ones at a decent price"); above 50 it says how to narrow first.
- **The results** (right) work without the chat: `+ Filter`, a chip's ×, the column heads to sort, a row to open its
  card, **Open resume** for the whole text, Undo and History.
- **Commands** typed in the chat act without the model: `/next`, `/prev`, `/page 3`, `/top 25`, `/filters`,
  `/drop f2` (or `/drop sql`, `/drop rank`), `/clear-filters`, `/back`, `/sets`, `/show r000910`.
- **+ Add file** attaches a `.md` or `.txt` file, usually a job description: "who fits this best?" then ranks against it.
- **Settings** (the gear) picks the model among the providers that have a key, and the light or dark look.

Conversations are kept in `.resumes/sessions/`; the menu at the top reopens them.

## The command line

Every verb prints plain text for a person or an agent, and `--json` for scripts. A conversation is a *session*;
`RESUMES_SESSION=name` picks one, otherwise the last one used continues.

```sh
uv run resumes search --topic data-pipelines           # a new question: the count and the facets
uv run resumes search --skill java --min-years 5       # skills, topics, constraints combine
uv run resumes filter --skill sql                      # add a filter to the current view
uv run resumes filter --remote --rate-max 60           # constraints are filters too
uv run resumes filters                                 # what is active, and what each does to the count
uv run resumes drop sql                                # remove one (by its id f2, or a word of it)
uv run resumes clear                                   # all of them
uv run resumes next | prev | page 3 | top 25           # page through the current set, at any size
uv run resumes sort --by rate                          # also years, judgment; a new set, same people
uv run resumes show r000910 [--full]                   # one resume
uv run resumes vocab kubernetes                        # how a word resolves to a skill or topic
uv run resumes search --text "moved data between systems at night"   # free text (needs OPENAI_API_KEY)
uv run resumes search --like eval/jds/java-backend.md  # rank against a job description (needs OPENAI_API_KEY)
```

Ranking from the command line is done by an agent: `resumes cards` prints the cards of a set of at most 50 people,
the agent writes a score and a note for each, `resumes score` records them, `resumes sort --by judgment` orders by
them. `resumes judge SET --criterion "…"` does the same with OpenAI directly.

## Building and rebuilding

```sh
scripts/get-data.sh                 # the two public datasets into data/
uv run resumes index seed load      # the model's saved outputs for them (no key needed afterwards)
uv run resumes corpus build         # data/ → corpus/md/ (one markdown file per resume, contact details redacted)
uv run resumes index build          # corpus/ → index/ (about a minute from the saved outputs)
uv run resumes index stats          # what the index holds
```

Everything a model produced is cached by content under `index/cache/`: a rebuild with nothing new costs nothing.
Without the saved outputs, a full build of the public data with `gpt-5-mini` costs about $14 and takes about an hour;
`uv run resumes index build --extractor none` skips the extraction (embeddings only, well under a dollar), with fewer
fields known per resume.

## Your own resumes

Put markdown files in `data/inbox/`: a title line, then sections such as `## Summary`, `## Skills`, `## Experience`.
Front matter may give `rate`, `currency`, `location`, `remote` and `availability`. Then:

```sh
export OPENAI_API_KEY=…
uv run resumes watch --once         # converts and indexes the new files: about 20 s for one resume
```

Each new resume costs about $0.004 with `gpt-5-mini` for the extraction, plus a fraction of a cent for its
embeddings; `uv run resumes index build --extractor none` skips the extraction. To replace the demo data instead, edit
`[[sources]]` in `resumes.toml`, then run `uv run resumes corpus build` and `uv run resumes index build`.

## How it works

The architecture, the build and the design choices are in the [README](../README.md#how-it-works).
