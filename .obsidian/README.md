# Why this vault is configured the way it is

The repository is the vault. `app.json` is committed rather than ignored,
because two of its settings are **load-bearing for the code**, not preferences.

## `useMarkdownLinks: true` — do not change this

The chapters in `content/` link to each other with ordinary markdown:

```markdown
[Hydrostatics](02-hydrostatics.md)
```

`wellcontrol/content.py` finds those with a regex and reduces them to their
text, because a relative link that works on GitHub 404s inside the Streamlit
planner. `tests/test_content.py::test_body_has_no_relative_links_left` checks
none survive.

Obsidian's default is `[[wikilinks]]`. If it converts them, the regex stops
matching — and the failure is **silent**: the test still passes, because there
are no relative *markdown* links left to find, and the planner renders raw
`[[02-hydrostatics]]` at the reader instead. That is a worse failure than a
crash, so the setting is pinned here rather than left to whoever opens the
vault next.

## `newLinkFormat: relative`

Same reason. Absolute vault paths would not resolve on GitHub, which is where
the content is also read.

## Renaming chapters

`alwaysUpdateLinks` keeps markdown links in step when a file is renamed, but it
cannot fix everything:

- `content.py` globs chapters as `[0-9][0-9]-*.md` and orders them by that
  prefix, so the numbering is structural.
- Every kill method in `methods/` names its chapter in a `chapter` attribute,
  and `tests/test_content.py` asserts the file exists.

So renaming a chapter in Obsidian will fix the prose links and break the
Python. Rename deliberately, then run the suite.

## `userIgnoreFilters`

Keeps `.venv` (56 markdown files of third-party docs) and the build directories
out of search and the graph. They are noise, not content.

## What is not committed

`workspace.json` and friends — pane layout and window position, which change
every time the app is closed and would be pure diff noise. See `.gitignore`.
