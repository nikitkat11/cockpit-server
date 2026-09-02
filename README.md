# cockpit

A single-file local control center for a folder of markdown notes.

It reads an Obsidian-style vault — frontmatter, headings, checklists, wikilinks —
builds a dashboard out of it, and serves that over HTTP on localhost. No
framework, no build step, no `node_modules`, no `pip install`.

```bash
python3 cockpit.py                    # serve the folder it lives in
python3 cockpit.py --vault ~/notes    # or point it somewhere else
open http://127.0.0.1:8090
```

## What it actually is

**473 lines of Python and a 1,341-line embedded single-page UI**, in one file.
I'd rather say that up front than have you open it and find a large HTML string
where you expected a server.

The Python half is the part worth reading:

| | |
|---|---|
| `read_note`, `frontmatter`, `section`, `checklist` | a small markdown/frontmatter parser — no PyYAML, just `re` |
| `parse_projects`, `parse_agents`, `parse_handoff`, `parse_memory`, `parse_decisions` | turn convention-following notes into structured data |
| `parse_graph` | builds a wikilink graph across the vault |
| `parse_queue`, `answer_queue` | a write path — answers typed in the browser go back into the notes |
| `Handler`, `ThreadingHTTPServer` | routing and the threaded server |
| `chat`, `proxy_status` | optional passthrough to a local LLM proxy |

The UI half is one big HTML/CSS/JS string. It's a real single-page app — tabs,
a force-directed graph, a voice queue — but it is a template literal, not
engineering I'd ask anyone to be impressed by.

## Why stdlib only

The vault this runs against lives in iCloud and is backed up weekly. A
dependency tree is a thing that breaks silently between machines and a thing
that has to be audited before it reads my notes. `http.server` +
`ThreadingHTTPServer` covers the whole requirement, so the dependency count is
zero and the file is copy-pasteable.

The trade is real: no templating, so the UI is a string; no framework, so
routing is a chain of `if path ==`. For a localhost tool used by one person
that's the right side of the trade. For anything multi-user it isn't.

## Security posture

Binds `127.0.0.1` explicitly and is never exposed to the network. There's no
auth, because there's no listener anyone else can reach. **Don't put this behind
a reverse proxy** — it has a write path into your notes and it trusts every
request that reaches it.

## Conventions it expects

It reads a vault that follows a particular structure — numbered folders,
frontmatter with a `type` field, specific headings. Point it at an arbitrary
notes folder and most panels come back empty rather than crashing. The parsers
are the place to start if you want to adapt it; each one is short and
independent.

## What I'd change

- The UI string should be a separate file served from disk. It's one file for
  portability, and portability stopped being worth it around line 800.
- Routing is an `if/elif` chain in `Handler.do_GET`. A dict of path → handler
  would be shorter and testable.
- **No tests.** The parsers are pure functions over strings and are the obvious
  thing to cover; I haven't. That's the honest gap in this repo, and it's the
  first thing I'd fix.
- `parse_graph` re-reads every note on each request. Fine at a few hundred
  notes, wrong at a few thousand.

MIT licensed.
