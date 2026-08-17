# jobbot

Job application automation for software engineering roles, built around a
Turkish job market that international tooling largely ignores.

It finds companies, decides which of their openings are actually worth applying
to, works out how to reach each one, and drafts a specific application for each
using a model running on your own machine. **You send them.** Nothing is
transmitted and no account credential is stored for anything.

```
 Arm 0  companies    find employers nobody wrote down
 Arm 1  discovery    open roles from ATS APIs, scored against your profile
 Arm 2  contact      the best reachable application channel per company
 Arm 3  apply        a drafted application per role, written to outbox/
```

The arms share one database and never call each other directly. Each handoff is
a row transition, so any arm can be re-run in isolation and a crash mid-batch
loses nothing.

They also compound: when arm 2 finds an ATS token behind a company's careers
page it writes that token into the board registry, and arm 1 polls that company
forever after. One page fetch becomes a permanent source.

## Credentials

The system holds exactly one credential, and it is the narrowest one that can
do the job.

**No account credentials are stored for anything.** Not for mail, not for
LinkedIn, not for kariyer.net, not for company sites. The system prepares
applications; a person sends them.

| Service | What is stored | Why |
|---|---|---|
| Mail | **Nothing** | Applications are written to `outbox/` as files. You copy the address, subject and body into your own mail client and send. Your normal sending reputation applies, and there is nothing to leak or revoke. |
| LinkedIn | **Nothing** | Automated access violates their terms and gets accounts permanently banned. Your LinkedIn *is* your professional identity — worth more than any automation. |
| kariyer.net | **Nothing** | Same terms problem, and it is your live job-search account. |
| Company sites | **Nothing** | Career pages are public; no login is needed to read them. |

Most postings we find are ATS web forms rather than email addresses anyway, so
each card carries whichever route applies — an address to write to, or a link
to open.

### The outbox

```bash
jobbot outbox build --limit 10   # draft with the local model, write the files
jobbot outbox list               # what is prepared, and what you have sent
jobbot outbox sent 3             # record card 3 as sent
```

`outbox/README.md` is the index; each card is one file with the address or
link, the subject, the body, and the CV to attach — each in its own fenced
block so it can be copied without selecting around anything.

Recording a send is what starts the per-company cooldown, so the same employer
is not drafted again next month. Nothing else depends on it.

An automated sender exists under `jobbot mail` (SMTP or Microsoft Graph with a
send-only OAuth scope) but is **not the default path** and needs no setup
unless you choose to use it.

## Ground rules

These are structural, not preferences:

- **No scraping of LinkedIn or kariyer.net.** Both prohibit automated access.
  The system generates a targeted search URL and puts a card in the queue for you
  to open by hand. Everything else runs on official, public APIs.
- **Nothing is sent without your approval.** Arm 3 stops at the review queue.
- **Email addresses are never guessed.** Only addresses actually published by the
  company are stored, which keeps bounce rate near zero and the sending domain healthy.
- **One application per company per month, enforced by a database constraint** —
  not by application logic that can be bypassed.
- **robots.txt is always honoured** and cannot be turned off by configuration.

## Setup

```bash
uv venv --python 3.12
uv pip install -e ".[dev,api]"
ollama pull qwen3:8b                 # the local model arm 3 writes with

cp .env.example .env                             # then fill it in
cp cv/profile.example.json cv/profile.json       # your skills, jobs, projects
cp data/boards.example.json data/boards.json     # ATS boards to poll
cp data/companies.example.json data/tr_companies.json
cp data/directories.example.json data/directories.json
```

Then put your CV PDFs in `cv/` and point `cv_variants` at them.

Every file you copy is git-ignored: `.env`, your profile, your CVs and your
registries stay on your machine. Only the `.example` templates are in the repo.

`cv/profile.json` is the one that matters most. Scoring reads its skills, role
families and geography; drafting reads its experience and project highlights and
is **forbidden from naming any technology that does not appear in it**. A profile
with only a skill list gives the model nothing concrete to write about, and it
will invent projects to fill the gap — so write real bullet points.

## The local model

Application text is written by a model running on this machine through Ollama —
no API key, no per-request cost, no posting leaving the network. Scoring never
touches it: deciding which of 12,894 postings are worth pursuing is rule-based
and free, and the model only writes the ~15 that survive.

A small local model will invent things, so every rule given to it in the prompt
is re-checked in code before a draft can reach the queue:

| Check | Catches |
|---|---|
| `cited_detail` must appear in the posting | A "specific detail" the model made up |
| Every technology named must be in the profile | A fabricated career — the failure that motivated this list |
| Word count, language, subject length | Drafts that are unusable in shape |
| Filler-phrase blocklist | "I am writing to express my interest in your dynamic team" |
| Placeholder scan (`[Company]`, `{{role}}`, `TODO`) | Template scaffolding left unfilled |
| Years-of-experience claims vs the profile | An inflated résumé |

A failed draft is regenerated with its own violations appended to the prompt —
a small model corrects a stated fault far more reliably than it avoids one
described in advance. If no attempt passes, that is an error; a draft that
failed its own checks is never quietly queued.

```bash
jobbot llm health          # is the model pulled and running
jobbot llm draft           # write one draft for the top-scoring posting
jobbot llm draft --job-id 7 --lang tr
```

## Usage

```bash
jobbot config check       # which features your .env can actually run
jobbot db init            # create the schema
jobbot db status          # row counts per table
jobbot run --arm 1        # run a single arm
jobbot run                # run the whole pipeline, arm 0 through arm 3
jobbot jobs top           # highest-scoring stored postings
```

Every arm is checked before any arm starts. Arm 1 takes twenty minutes against a
real registry, and finding out afterwards that the model was never running — so
arm 3 was never going to work — wastes the whole run to learn something knowable
at the start.

After that the arms are independent, so a failing one is recorded and the rest
still run; the exit code is non-zero if any of them failed. `--stop-on-error`
opts into stopping at the first failure instead.

## Running it unattended

```bash
jobbot schedule install   # prints the scheduler entry; --apply registers it
jobbot schedule status    # when each arm last ran, and when it is next due
jobbot run --due-only     # run only the arms whose interval has elapsed
```

The scheduler fires one command on a short fixed interval and `--due-only`
decides what is actually due, so cadence lives in your `.env` and the scheduler
entry never changes when you adjust it:

| Setting | Default | Why |
|---|---|---|
| `JOBBOT_ARM0_INTERVAL_HOURS` | 168 | Company directories change over months; polling daily spends requests to learn nothing. |
| `JOBBOT_ARM1_INTERVAL_HOURS` | 24 | ATS boards are cheap to poll and postings appear daily. |
| `JOBBOT_ARM2_INTERVAL_HOURS` | 24 | Paced by `ARM2_BATCH_SIZE` (25) rather than by frequency. |
| `JOBBOT_ARM3_INTERVAL_HOURS` | 24 | Budgeted to `ARM3_BATCH_SIZE` (5): drafting is about a minute of GPU each. |

Due-ness is measured from each arm's last *completed* run, so an arm that fails
is retried on the next tick rather than locked out for a full interval by its own
failure.

**Nothing is sent by any of this.** An unattended run ends at a folder of drafts
in `outbox/`, exactly as a manual one does.

One run at a time is enforced with a lock file beside the database. Overlapping
runs were never a correctness problem — the cooldown constraint rejects a
duplicate application whatever the caller does — but two processes drafting the
same queue burn the GPU twice to produce one usable card. A lock older than
`JOBBOT_MAX_RUN_HOURS` (6) is treated as abandoned and reclaimed; that is
age-based rather than a check on whether the recorded process is alive, because
pid liveness is not portable to Windows and a wrong answer either way is worse
than no check. If a run is killed hard, `jobbot run` says which pid holds the
lock and where the file is.

### Managing the board registry

```bash
jobbot boards list                       # what arm 1 currently polls
jobbot boards verify --prune             # drop boards that stopped responding
jobbot boards add greenhouse acme \
    --name "Acme" --domain acme.com --country TR
```

`boards add` refuses a token that returns no postings, because an unverified
token polls nothing forever without ever failing visibly.

**Finding a company's token** — open its careers page and look at where the
apply button goes. The token is the path segment:

| URL | provider | token |
|---|---|---|
| `boards.greenhouse.io/acme` | greenhouse | `acme` |
| `jobs.lever.co/acme/...` | lever | `acme` |
| `jobs.ashbyhq.com/acme` | ashby | `acme` |
| `apply.workable.com/acme/` | workable | `acme` |
| `acme.recruitee.com/o/...` | recruitee | `acme` |
| `jobs.smartrecruiters.com/Acme/...` | smartrecruiters | `Acme` |

`jobbot.connectors.ats.detect_board()` does this automatically, and arm 2 uses
it so the registry grows on its own as companies are discovered.

## Development

```bash
uv run pytest --cov       # tests, 80% coverage floor
uv run ruff check .
uv run mypy
```

## Project status

| Phase | Scope | State |
|---|---|---|
| 0-1 | Scaffolding, config, schema, audit log, CLI | done |
| 2 | Arm 1 - ATS connectors, scoring, discovery pipeline | done |
| 3 | Arm 2 - contact resolution, TR seed, assisted links | done |
| 4a | Arm 3 - local model, drafting, validation | done |
| 4b | Arm 3 - outbox: prepared applications as files | done |
| 4c | Arm 3 - optional automated sender | done, not the default |
| 5 | Orchestration, cadence, single-instance locking | done |
| 6 | Observability and hardening | pending |

### Why there are two discovery arms

Arm 1 reads ATS APIs. In a live run those returned **12,900 postings, of which
14 were worth applying to and 3 companies remained after deduplication** — and
almost none were Turkish, because Turkish employers overwhelmingly do not use
Greenhouse or Lever. They publish on their own careers pages, on kariyer.net, or
on LinkedIn.

So arm 1 covers "abroad, remote" and arm 2 covers Türkiye by visiting company
sites directly. Arm 0 keeps arm 2 supplied with companies to visit.

Arm 2 ranks its findings by what each is worth:

1. **An ATS token** — a careers page that redirects to Greenhouse or Lever is
   the best outcome by a wide margin: one page fetch becomes a permanent feed
   that arm 1 polls forever. Detected boards are written straight back into
   `data/boards.json`, so the two arms compound.
2. **A careers page** — somewhere for a human to go.
3. **A published hiring address** — never a guessed one. `ik@company.com` is
   only recorded if the company printed it; a guess that bounces teaches the
   receiving mail system that this sender does not know who it is writing to.
4. **Assisted search links** — LinkedIn and kariyer.net URLs a human opens in
   their own browser. Free to produce, and for an employer that posts only on
   kariyer.net it is the entire answer.

```bash
jobbot companies discover   # find employers from directories and GitHub orgs
jobbot companies status     # how many are known, how many still unvisited
jobbot run --arm 2          # visit the ones not visited yet
jobbot contacts list --kind email
```

### Finding companies nobody wrote down

A hand-maintained employer list is biased toward whoever is famous, which is
exactly the wrong bias for a junior: the well-known names receive thousands of
applications and rarely hire juniors, while a twenty-person firm hires two a
year and gets forty applications.

So discovery is a source, not a file. `Company.investigated_at` tracks what arm
2 has seen, which means a source can add thousands of employers and arm 2 works
through only the new ones. Two sources ship:

- **Directories** (`data/directories.json`) — technopark and trade-association
  member lists, harvested by generic link extraction rather than a parser per
  site, so a redesign costs nothing and a dead URL costs one request.
- **GitHub organisations** filtered to Turkish locations. This signal scales
  *down*, which is the point: a two-person startup has an org for the same
  reason a large company does. Set `GITHUB_TOKEN` to lift the 10-requests-a-
  minute search limit.

Adding a source grows the search permanently. Editing a JSON file grows it once.

## License

MIT — see [LICENSE](LICENSE). Use it, change it, ship it.

## Layout

```
src/jobbot/
  config.py          settings + per-feature requirement checks
  models.py          schema; the safety constraints live here
  db.py              engine, sessions, SQLite pragmas
  events.py          append-only audit trail
  orchestrate.py     arm order, preflight, run history, cadence
  locks.py           one run at a time
  logging_setup.py   console logging (ASCII-only for Windows terminals)
  cli.py             command line entry point
tests/
```
