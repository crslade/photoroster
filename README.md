# photoroster

Turn a Workday **View Course Section Roster** PDF export into a one-glance photo
roster: every student's picture and name on a single sheet, with the course
number and title across the top.

The Workday export is a wide landscape table — one student per row, spread over
many pages, with the photo buried among email addresses, credit hours, academic
units and programs of study. Useful for records, useless for learning names.
This script keeps the two columns that matter and lays them out as a grid.

![Sample output](docs/sample.png)

*Sample generated from fabricated names and placeholder tiles — see
[Privacy](#privacy).*

## Requirements

- Python 3.9+
- [PyMuPDF](https://pymupdf.readthedocs.io/) — plus FastAPI and Uvicorn if you
  want the website (`requirements-web.txt`)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Usage

```bash
.venv/bin/python photo_roster.py data/CS_140-01_-_WEB_DESIGN.pdf
```

```
40 students (1 without a photo) -> data/CS_140-01_-_WEB_DESIGN_photo_roster.pdf [2 page(s)]
```

By default the new PDF is written next to the input as
`<input>_photo_roster.pdf`, letter size, five photos across and five down.

### Options

| Option | Description |
| --- | --- |
| `-o`, `--output` | Output path (default: `<input>_photo_roster.pdf`) |
| `--cols`, `--rows` | Grid size (default `5` × `5`, so 25 per page) |
| `--sort` | Sort alphabetically by name instead of keeping roster order |
| `--course` | Override the course number, e.g. `"CS 140-01"` |
| `--title` | Override the course title, e.g. `"Web Design"` |
| `--subtitle` | Replace the line under the heading (default: student count) |
| `--font` | Path to a `.ttf`/`.ttc` to embed, overriding the font search |

The course number and title are read out of the export's own heading
(`View Course Section Roster: CS 140-01 - WEB DESIGN`), so `--course` and
`--title` are only needed if that heading is missing or you want different
wording.

Bigger faces for a large lecture hall, or a compact sheet for a small section:

```bash
# 4 x 4 = 16 per page, alphabetical
.venv/bin/python photo_roster.py roster.pdf --cols 4 --rows 4 --sort

# 6 x 6 = 36 per page
.venv/bin/python photo_roster.py roster.pdf --cols 6 --rows 6 \
    --subtitle "Fall 2026 - Portable 2 - MWF 9:00"
```

## Run it as a website

`app.py` puts the same converter behind a one-page site: drop in a roster PDF,
get the photo roster back. The upload is converted in memory and returned in
the same response — it is never written to disk, queued, or logged — so there
is no copy of a student record left on the server to clean up afterwards.

The site is behind a shared passcode, set with `ROSTER_PASSCODE`. It refuses
to start without one unless you pass `ROSTER_ALLOW_OPEN=1` to say you meant it.

### Locally

```bash
.venv/bin/pip install -r requirements-web.txt
ROSTER_PASSCODE=letmein .venv/bin/uvicorn app:app --reload --port 8000
```

### Deploying with Kamal

One container, no database, no volumes, no accessories. [Kamal](https://kamal-deploy.org)
deploys any Docker image, so this can share a host with unrelated apps —
`kamal-proxy` routes by hostname and gets the certificate itself.

[`config/deploy.yml`](config/deploy.yml) is a working config rather than a
skeleton, so change `image`, `servers` and `proxy.hosts` to your own before
the first deploy.

1. Point a DNS `A` record at the server. Let's Encrypt is asked for a
   certificate on the first deploy and will fail if the name does not resolve
   yet.

2. Fill in the secrets. `.kamal/secrets` is gitignored; the template lists
   what belongs in it:

   ```bash
   cp .kamal/secrets.example .kamal/secrets
   ```

   As written it reads both values from your shell — a registry token that
   can push, and the passcode for the site:

   ```fish
   set -x KAMAL_REGISTRY_PASSWORD (cat ~/.registry-token)
   set -x ROSTER_PASSCODE (openssl rand -base64 12)
   ```

   Kamal evaluates that file as shell, so either value can come from a
   password manager instead; `kamal secrets --help` lists the ones it knows.

3. Commit. Kamal tags images with the git SHA and will not deploy a dirty
   tree.

4. Deploy:

   ```bash
   kamal setup     # first time: pushes the image, boots the app
   kamal deploy    # every time after
   ```

`kamal setup` also boots `kamal-proxy`. If the host already runs other Kamal
apps that is a no-op, as long as every project on the box is on the same Kamal
version — when one moves ahead, the proxy has to be rebooted for all of them
at once, so upgrade them together.

`kamal logs -f` follows the app, `kamal shell` opens a bash prompt inside it,
and `kamal rollback` puts the previous version back.

To rotate the passcode, change `ROSTER_PASSCODE` and run `kamal deploy`.
Everyone is signed out and enters the new one.

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `ROSTER_PASSCODE` | — | Shared passcode. Required. |
| `ROSTER_ALLOW_OPEN` | — | `1` to run with no passcode at all |
| `ROSTER_MAX_UPLOAD_MB` | `25` | Largest accepted upload |
| `ROSTER_FONT` | — | Font to embed, overriding the search in `photo_roster.py` |

Keep `proxy.buffering.max_request_body` in `deploy.yml` above
`ROSTER_MAX_UPLOAD_MB`, so an oversized upload gets the app's own readable
error instead of the proxy cutting the connection.

## Privacy

Class rosters are student records. This tool is built to keep them local:

- **No network access.** `photo_roster.py` imports `pymupdf` and five modules
  from the standard library. It makes no HTTP requests and has no telemetry,
  whether you run it from the command line or behind the website.
- **Nothing personal is committed.** `.gitignore` excludes `data/` and `*.pdf`,
  so neither the export nor the generated roster can be pushed by accident.
  Keep your PDFs in `data/`.
- **The sample image is fake.** `docs/sample.png` is generated by
  `examples/make_sample.py` from invented names and flat colour tiles. Nothing
  in this repository came from a real roster. Regenerate it with
  `.venv/bin/python examples/make_sample.py`.

Hosting it adds a server to that picture, so the web front end is built to
hold as little as possible:

- **Nothing hits disk.** The upload is read into memory, converted, and
  returned in the same response. There is no upload directory, no job queue
  and no database, so there is nothing to purge and nothing to leak.
- **No names in logs.** Access logs record the request line only, and
  unexpected errors return a fixed message rather than a traceback, which
  could otherwise quote a name out of the roster.
- **The passcode is a shared secret, not an account system.** It keeps the
  form off the open internet; it is not an audit trail. Rotate
  `ROSTER_PASSCODE` at the end of each term — changing it signs everyone out.

The output PDF contains the same photos and names as the input, so treat it
like the export itself: keep it off shared drives, and shred the printout when
the term ends. Check your institution's FERPA guidance before distributing it.

## How it works

1. **Find the columns.** The `Photo` and `Student` header labels are located on
   each page and used to derive the x-ranges of those two columns, so the script
   does not depend on hard-coded coordinates.
2. **Rebuild rows from spans.** The export draws an entire table row as a single
   text block spanning many columns, so reading names block-by-block splices
   them together with email addresses. Instead, individual text spans are
   filtered by x-position and regrouped into rows by vertical gap — wrapped
   lines of one name sit about 9pt apart, separate students about 38pt.
3. **Match photos to rows.** Each row occupies a band running from just above
   its first line of text to just above the next student's. A photo belongs to
   whichever band its centre falls in, so a student with no photo on file does
   not shift everyone else by one.
4. **Lay out the grid.** Photos are drawn 3:4 portrait at a size derived from
   the grid dimensions, with the name centred below, wrapped to two lines and
   shrunk from 8.5pt down to 6pt as needed. Students with no photo get a grey
   "no photo" tile.
5. **Fonts.** A Unicode font is embedded only if a name uses characters outside
   Latin-1; otherwise built-in Helvetica keeps the file roughly the size of the
   photos alone. Embedding an unsubsetted Arial Unicode turns a 1.3 MB output
   into a 16 MB one.

## Limitations

- Written against the Workday "View Course Section Roster" export. Other student
  information systems lay their PDFs out differently and will need the column
  detection in `find_columns()` adjusted.
- Photos are used at whatever resolution the export embeds (about 113 × 150 px),
  which is fine for a printed grid but will not enlarge well.
- The Unicode font search covers common macOS and Linux paths; elsewhere, pass
  `--font` (or set `ROSTER_FONT`), or accept Helvetica and its Latin-1-only
  coverage. Use a single-face `.ttf`/`.otf`: MuPDF mis-subsets Noto Sans CJK's
  `.ttc` collection, inflating the output to ~14 MB and dropping glyphs.

## License

[Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/)
(CC BY 4.0) — see [LICENSE](LICENSE). Use, modify and redistribute it, including
commercially, as long as you credit Christopher Slade and note any changes.

The license covers this code and documentation only. It says nothing about the
rosters you feed it: student names and photos belong to the students and your
institution — see [Privacy](#privacy).
