# Contributing (team-internal notes)

This repo is a Smart India Hackathon 2026 submission (PS 26011), built by a
six-person team. These are working notes for us, not a formal open-source
policy — but a few habits are worth keeping from here on:

## Commit as yourself, on your own branch

Push from your own GitHub account, not through someone else's terminal or a
shared machine logged in as one person. Judges (and anyone browsing the repo
later) read commit history as evidence of who built what — a repo with one
commit from one author reads as "dumped," even when six people did real work
on it.

```bash
git checkout -b yourname/short-description
# make your change
git add <files you actually changed>
git commit -m "component: what changed and why"
git push -u origin yourname/short-description
```

Then open a pull request into `main` so the rest of the team can see the diff
before it lands. Small, frequent commits with real messages beat one giant
commit at the deadline.

## Before you open a PR

- Run the module(s) you touched directly — every `.py` file in this repo is
  executable and self-validates (`python geodesy.py`, `python pipeline.py`,
  etc.). If your change breaks a module's own self-test, fix it before
  pushing, not after.
- CI (`.github/workflows/ci.yml`) runs the same checks automatically on your
  PR. A red check means something regressed — don't merge past it without
  understanding why.
- Don't commit generated files: trained model pickles (`*.pkl`), the SQLite
  dev database, point-cloud/raster data. `.gitignore` already excludes these;
  if `git status` shows one anyway, you've probably `git add -f`'d something
  you didn't mean to.

## Where things live

See the "What this repository contains" table in [README.md](README.md) —
it's kept current, so start there before adding a new top-level file.

## Honesty over polish

This repo documents its own known limitations and at least one real bug we
caught and fixed (see README's "Honest limitations" section). Keep that
habit: if something doesn't work yet, say so in the code or docs rather than
leaving it to be discovered later. It's held up well with judges so far —
don't walk it back under deadline pressure.
