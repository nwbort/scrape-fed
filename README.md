# scrape-fed

Tracks Federal Court files on the
[Commonwealth Courts Portal](https://www.comcourts.gov.au), one matter at a
time. For each file it saves the portal's tables, downloads the orders and
judgments, and sends a push notification via [ntfy.sh](https://ntfy.sh) when
anything changes. The changes it watches for:

- new or changed court listings (hearings, outcomes, orders made, judgments),
- new documents filed,
- parties and lawyers coming on or off the record, docket changes,
- the file or an application being finalised.

It runs on GitHub Actions every 30 minutes (at :13 and :43 past the hour).

## Tracked files

Listed in [`matters.txt`](matters.txt), one per line, as a file number
(`NSD1310/2026`) or its portal URL
(`https://www.comcourts.gov.au/file/Federal/P/NSD1310/2026/actions`). Add a
line to start tracking another file. The first scrape of a new file records a
baseline without sending notifications.

## Notifications

Subscribe to the topic named in `NTFY_TOPIC` in
[`.github/workflows/scrape.yml`](.github/workflows/scrape.yml) in the ntfy
app. Anyone who knows the topic name can read it. For a private one, set a
repository variable called `NTFY_TOPIC` (Settings → Secrets and variables →
Actions → Variables) and it will be used instead.

A notification looks like:

```
NSD1310/2026: 1 file detail, 1 listing change, 1 document
~ finalised date: 2026-08-27 (was -)
+ 2026-08-27 8:30 Administrative Listing (Justice O'Bryan): Finalised - Discontinued/Withdrawn
+ 2026-08-25 Notice of Discontinuance (Coles Supermarkets Australia Pty Limited)
```

## Output

For each file, in `matters/<number>-<year>/` (e.g. `matters/nsd1310-2026/`):

- `file.json`: everything the portal shows the public about the file.
- `orders/`: the "View orders" PDFs, named as the portal names them
  (`P_NSD1310_2026_2835642.pdf`).
- `judgments/`: the "View judgment" files from the Federal Court's judgments
  site, as the original Word document (`2026FCA1000.docx`).

`url_gh` on an order or judgment is where its download lives in this repo.
One that couldn't be downloaded (often a judgment that isn't published yet)
has no `url_gh` and is retried on the next run.

```json
{
  "file": {
    "court": "Federal Court of Australia, New South Wales Registry",
    "number": "NSD1310/2026",
    "title": "COLES SUPERMARKETS AUSTRALIA PTY LIMITED v AUSTRALIAN COMPETITION AND CONSUMER COMMISSION",
    "filing_date": "2026-07-17",
    "finalised_date": "2026-08-27",
    "url": "https://www.comcourts.gov.au/file/Federal/P/NSD1310/2026/actions"
  },
  "applications": [
    {
      "title": "MISCELLANEOUS",
      "type": "Judicial Review",
      "filed": "2026-07-17",
      "status": "Closed",
      "finalised": "2026-08-27",
      "level": 1,
      "id": "4066335",
      "events": [
        {
          "date": "2026-07-28",
          "time": "13:00",
          "reason": "Judgment",
          "presiding": "Justice O'Bryan",
          "location": "Chambers",
          "outcome": "Adjourned - For review of progress",
          "event_id": "33021628",
          "orders": [
            {
              "title": "View orders",
              "url": "https://www.comcourts.gov.au/file/Federal/P/NSD1310/2026/4066335/event/33021628/document/2835642",
              "url_gh": "/matters/nsd1310-2026/orders/P_NSD1310_2026_2835642.pdf"
            }
          ],
          "judgments": [
            {
              "citation": "[2026] FCA 1000",
              "url": "https://www.judgments.fedcourt.gov.au/judgments/Judgments/fca/single/2026/2026fca1000",
              "url_gh": "/matters/nsd1310-2026/judgments/2026FCA1000.docx"
            }
          ],
          "outcomes": [
            {
              "order_type": "Directions Orders",
              "decision_type": "Directions - free text order specific to case"
            }
          ]
        }
      ],
      "documents": [
        {
          "date": "2026-08-25",
          "time": "11:27",
          "title": "Notice of Discontinuance",
          "filed_by": "Coles Supermarkets Australia Pty Limited"
        }
      ],
      "parties": [
        {
          "role": "Legal Representative Applicant",
          "name": "Herbert Smith Freehills Kramer",
          "end_date": "",
          "contact": {
            "address": "Level 22, 80 Collins Street, MELBOURNE 3000",
            "business_fax": "03 9288 1567",
            "business_phone": "03 9288 1234"
          }
        }
      ]
    }
  ]
}
```

`applications` holds the originating application and any interlocutory
ones. `events` holds the listings, newest first, as the portal shows them. A
court event can span several listings (the day judgment is reserved and the
day it's delivered, say); they share an `event_id`, and the event's
`outcomes` sit on its first listing. The public can't download filed
documents, so `documents` is just the register.

## How it works

The portal itself is plain server-rendered HTML with no Cloudflare in front
of it, so [`portal.py`](portal.py) uses urllib. The actions page lists the
applications, and each application's events, documents and parties are HTML
fragments that the portal normally loads when you expand a row. An event's
outcomes and a party's contact take one more request each. To keep a
long-running file cheap, those are reused from the last run while the event
or party hasn't changed.

The judgments site (judgments.fedcourt.gov.au) *is* behind Cloudflare's
managed challenge. For those, [`judgments.py`](judgments.py) drives a real
Chrome via nodriver (headful under Xvfb) to clear the challenge and follow
the "Original Word Document" link to the file store on www.fedcourt.gov.au.
This is the same approach as
[tribunal-tracker](https://github.com/nwbort/tribunal-tracker). Chrome only
starts when there's a judgment that hasn't been saved yet, so most runs never
launch it.

If any of a file's pages fails to load, its saved state is left untouched,
which avoids false alarms, and the run is marked failed in Actions.

## Running locally

```bash
pip install -r requirements.txt
./scrape.sh                          # all files in matters.txt
./scrape.sh NSD1310/2026             # just one
NTFY_TOPIC=my-topic ./scrape.sh      # also send notifications
python portal.py NSD1310/2026        # print a file as JSON, saving nothing
xvfb-run -a python judgments.py URL  # fetch one judgment
```

Without `NTFY_TOPIC` the notifications are only printed. Without Chrome
everything but the judgments still works. Set `CHROME_PATH` if Chrome isn't
on your `PATH`.
