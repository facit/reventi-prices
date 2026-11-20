# reventi-prices

Daily card price files for the SWU Scan and DeckWare apps (iOS and Android).

A scheduled GitHub Action fetches the TCGplayer prices from [TCGCSV](https://tcgcsv.com) once a day and
publishes one file per game on GitHub Pages. The apps read these files. They do not call TCGCSV, because
TCGCSV's [usage guidelines](https://tcgcsv.com/docs#usage-guidelines) say: "Don't make requests to TCGCSV
directly from user-facing applications." The same job publishes the euro exchange rates of the European
Central Bank (ECB).

The files are deployed by the Action. They are not committed, so the history of this repository does not grow.

## Files

| URL | Content |
| --- | --- |
| `https://facit.github.io/reventi-prices/tcgplayer/79.json` | Star Wars: Unlimited (TCGplayer category 79), prices in USD |
| `https://facit.github.io/reventi-prices/tcgplayer/92.json` | Cyberpunk TCG (TCGplayer category 92), prices in USD |
| `https://facit.github.io/reventi-prices/fx/eur.json` | ECB euro foreign exchange reference rates |

Each game file contains all groups (sets) of its category, so a new set needs no app update.

## Schedule

The job runs each day at 21:17 UTC (`.github/workflows/publish.yml`), and on demand from the Actions tab.

- TCGCSV rebuilds once a day, "around 20:00:00 UTC" (<https://tcgcsv.com/>). The job runs about one hour later.
- The ECB publishes its rates at about 16:00 CET (15:00 UTC) on its working days. On other days the file
  keeps the rates of the last working day; read `date`.
- GitHub can start a scheduled run late. Do not rely on an exact time.

TCGCSV asks clients to sync only when its `last-updated.txt` is newer than their last pull. If the published
file already has that time in `source.lastUpdated`, the job publishes it again unchanged and does not fetch
the groups. The manual run has a `force` input to fetch anyway.

## Format

JSON, UTF-8, on one line without spaces. `version` is 1. A change that old apps cannot read gets a new
version and a new path.

### `tcgplayer/<category>.json`

```json
{
  "version": 1,
  "generatedAt": "2026-11-20T21:17:42Z",
  "source": {
    "name": "TCGCSV",
    "url": "https://tcgcsv.com/tcgplayer/79/groups",
    "lastUpdated": "2026-11-20T20:04:34Z"
  },
  "category": 79,
  "groups": [
    {"groupId": 24812, "name": "Secrets of Power", "abbreviation": "SEC", "isSupplemental": false,
     "publishedOn": "2025-11-07T00:00:00", "modifiedOn": "2026-10-15T18:39:52.643", "categoryId": 79}
  ],
  "prices": {
    "24812": [
      {"productId": 652001, "lowPrice": 0.05, "midPrice": 0.2, "highPrice": 4.99, "marketPrice": 0.14,
       "directLowPrice": null, "subTypeName": "Normal"},
      {"productId": 652001, "lowPrice": 0.35, "midPrice": 0.75, "highPrice": 9.99, "marketPrice": 0.62,
       "directLowPrice": 0.5, "subTypeName": "Foil"}
    ]
  }
}
```

- `generatedAt`: when this job made the file (UTC).
- `source.lastUpdated`: the time in TCGCSV's `last-updated.txt` (UTC), or `null` if it was not readable. This
  is the age of the prices.
- `groups`: TCGCSV's group list for the category, unchanged.
- `prices`: one key for each group in `groups` (the `groupId` as a string). The value is the `results` list
  of `https://tcgcsv.com/tcgplayer/<category>/<groupId>/prices`, unchanged. A price can be `null`. A group
  that has no prices has an empty list.

### `fx/eur.json`

```json
{
  "version": 1,
  "generatedAt": "2026-11-20T21:17:42Z",
  "source": {"name": "European Central Bank",
             "url": "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"},
  "date": "2026-11-20",
  "base": "EUR",
  "rates": {"USD": 1.0992, "SEK": 11.147}
}
```

1 EUR = `rates[X]` units of X. For USD to SEK, calculate `amount / rates.USD * rates.SEK`. `date` is the
ECB's date for the rates. The rates are the ECB's values, not changed.

## Rules for the apps

- Get the file with a conditional GET. Save the `ETag` (or `Last-Modified`) of the last answer and send it
  as `If-None-Match` (or `If-Modified-Since`). An answer of `304 Not Modified` means: keep the saved file.
- Save the file on the device and read prices from the saved copy. The file changes once a day, so one check
  per day is sufficient.
- Send `Accept-Encoding: gzip`. The Star Wars: Unlimited file is about 1.4 MB, and about 120 kB with gzip.
- If the request fails, use the saved file. Show the age of the prices from `source.lastUpdated`.
- Do not call tcgcsv.com from an app.

## Validation

The job fails, and the last good files stay online, if one of these is true:

- A file is missing or empty, or its JSON does not parse.
- A game file has no groups or no price rows, its header is wrong, or its `prices` keys are not its groups.
- A game has fewer groups than the published file. A drop of one group, or of 10 % for a game with 20 or
  more groups, is allowed, because TCGplayer sometimes removes or merges a group. Without this allowance,
  one removed group would stop all updates until a person steps in.
- A group that has prices in the published file has none now.
- A rate is not a positive number, or the USD rate is missing.

If a larger change is correct, start the workflow by hand with the `allow_drop` input.

## Sources

- Card prices: TCGplayer data through [TCGCSV](https://tcgcsv.com). TCGCSV has no attribution rule: "A link
  thanking TCGCSV is always appreciated." Thank you, TCGCSV. Each request has the User-Agent
  `reventi-prices/1.0.0 (+https://github.com/facit/reventi-prices)` and there is a pause of 250 ms between
  requests. One full sync is about 55 requests.
- Exchange rates: source European Central Bank,
  [euro foreign exchange reference rates](https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html).
  The ECB says that the reference rates are "for information purposes only" and are not for market
  transactions. An app that shows a converted price must name the ECB as the source of the rate.

## Run it locally

Python 3.10 or later. No packages are necessary.

```sh
python3 -m unittest                # tests, no network
python3 prices_job.py previous     # download the published files into previous/
python3 prices_job.py build        # fetch the sources and write public/
python3 prices_job.py validate     # check public/ against previous/
```

`public/` and `previous/` are not committed.

## Setup

- Settings, Pages, Build and deployment, Source: "GitHub Actions".
- GitHub runs a schedule only from the default branch.
- GitHub disables a schedule in a public repository after 60 days without repository activity. This job
  makes no commits. If the daily run stops, enable the workflow again in the Actions tab.
