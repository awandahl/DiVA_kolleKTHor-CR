
# DiVA Find DOI

`DiVA_Find_DOI` is a small Python script that helps you find missing DOIs for DiVA records that already have Scopus IDs and/or Web of Science (ISI) IDs, but no DOI registered in DiVA.

It:

- Exports publication metadata from a chosen DiVA portal (e.g. `kth`, `uu`, `umu`) in CSV format.
- Filters to a specific publication year range and a set of publication types.
- Identifies records that have Scopus or ISI identifiers but no DOI.
- Queries the Crossref API by title (with optional year filter), computes a simple title–similarity score, and proposes a **single “Possible DOI”** for high‑confidence matches.
- Writes both a CSV file and an Excel file with clickable links back to DiVA, Crossref DOI, Scopus, and Web of Science.

The script is intended as a **batch helper for manual curation**: it does not write anything back to DiVA. You review the suggested DOIs and then update DiVA records separately.

***

## Requirements

- Python 3.9+ (tested with recent CPython versions)
- Python packages:
    - `pandas`
    - `requests`
    - `tqdm`
    - `xlsxwriter` (via `pandas`’ Excel writer; `openpyxl` also works if you prefer, but this script explicitly uses `xlsxwriter`)

Install dependencies, for example:

```bash
pip install pandas requests tqdm xlsxwriter
```


***

## How it works

At a high level, the script:

1. **Builds a DiVA export URL** using `DIVA_BASE` and a JSON‑style query (`aq`/`aq2`) that:
    - Filters by `dateIssued` between `FROM_YEAR` and `TO_YEAR`.
    - Restricts to publication types: `bookReview`, `review`, `article`, `book`, `chapter`, `conferencePaper`.
    - Requests a fixed set of fields (`PID`, `ArticleId`, `DOI`, `ISI`, `ScopusId`, `Title`, `Year`, journal/series/ISBN fields, etc.).
2. **Downloads the CSV** from the chosen DiVA portal and stores it locally.
3. **Loads the CSV into pandas**, normalizes empty values, and:
    - Ensures there is a `Possible DOI:s` column, positioned directly after `DOI`.
    - Applies an additional filter on `Year` (based on the exported `Year` column).
    - Excludes generic front‑matter titles such as `Foreword` and `Preface` (case‑insensitive).
4. **Selects candidate rows** based on identifier logic:
    - Keeps rows where:
        - `DOI` is empty, and
        - Either:
            - `ScopusId` is present but `ISI` is empty (`scopus_only`), or
            - `ISI` is present but `ScopusId` is empty (`isi_only`), or
            - Both types, depending on the configuration flags.
    - Also requires that both `Title` and `Year` are non‑empty.
5. **Queries Crossref** for each candidate title:
    - Sends a `query.title` request to `https://api.crossref.org/works` with:
        - A `filter` restricting to the candidate’s publication year (from‑ and until‑pub dates) if available.
        - `rows = CROSSREF_ROWS_PER_QUERY`.
        - `mailto = MAILTO` (as recommended by Crossref).
    - Parses returned items and extracts `(DOI, title, issued year)`.
6. **Computes a simple Jaccard title‑similarity** between the DiVA title and each candidate:
    - Normalizes titles to lowercase alphanumeric tokens.
    - Computes intersection/union of token sets.
    - Only considers candidates where Crossref’s year equals the DiVA `Year`.
    - Picks the highest‑similarity candidate, and accepts it as “Possible DOI” if `similarity >= SIM_THRESHOLD`.
7. **Respects a global limit** `MAX_ACCEPTED` on the number of accepted DOIs and sleeps between queries (`time.sleep(1.0)`) to be gentle on Crossref.
8. **Writes results**:
    - A CSV containing only rows where `Possible DOI:s` is non‑empty, with a column order that surfaces identifiers and bibliographic details first.
    - An Excel file with additional hyperlink columns, where:
        - `PID_link` points to the DiVA record.
        - `DOI_link` points to `https://doi.org/<DOI>`.
        - `ISI_link` points to Web of Science.
        - `Scopus_link` points to the Scopus record.

***

## Configuration

All settings are currently defined as module‑level constants near the top of `find_doi.py`:

```python
FROM_YEAR = 2025
TO_YEAR = 2025

DIVA_PORTAL = "kth"
DIVA_BASE = f"https://{DIVA_PORTAL}.diva-portal.org/smash/export.jsf"

SCOPUS_ONLY = False
ISI_ONLY = False
BOTH_TYPES = True

SIM_THRESHOLD = 0.9
MAX_ACCEPTED = 9999
CROSSREF_ROWS_PER_QUERY = 5
MAILTO = "aw@kth.se"

RANGE_PREFIX = f"{FROM_YEAR}-{TO_YEAR}_"
DOWNLOADED_CSV = RANGE_PREFIX + "diva_raw.csv"
OUTPUT_CSV = RANGE_PREFIX + "doi_candidates.csv"
EXCEL_OUT = RANGE_PREFIX + "doi_candidates_links.xlsx"
```


### DiVA portal and year range

- `DIVA_PORTAL`
    - The DiVA sub‑portal to query, e.g. `"kth"`, `"uu"`, `"umu"`, `"lnu"`.
    - Used to build both the export URL and DiVA record links.
- `FROM_YEAR`, `TO_YEAR`
    - Inclusive year bounds for both the **DiVA export** (`dateIssued` filter) and the additional filter on the exported `Year` column.
    - Example: `FROM_YEAR = 2020`, `TO_YEAR = 2021` restricts to publications from 2020–2021.


### Identifier selection logic

You can tweak which records are considered for DOI lookup:

- `SCOPUS_ONLY`, `ISI_ONLY`, `BOTH_TYPES`:
    - `BOTH_TYPES = True`
        - Use the **union** of:
            - “Scopus‑only” records (`ScopusId` present, `ISI` empty, no DOI), and
            - “ISI‑only” records (`ISI` present, `ScopusId` empty, no DOI).
    - `SCOPUS_ONLY = True`, `ISI_ONLY = False`, `BOTH_TYPES = False`
        - Only process “Scopus‑only” records.
    - `ISI_ONLY = True`, `SCOPUS_ONLY = False`, `BOTH_TYPES = False`
        - Only process “ISI‑only” records.

If you set an inconsistent combination (e.g. both `SCOPUS_ONLY` and `ISI_ONLY` true while `BOTH_TYPES` is false), the script raises a `ValueError`.

### Crossref search behaviour

- `SIM_THRESHOLD`
    - Minimum Jaccard title‑similarity between the DiVA title and the best Crossref candidate to accept a DOI.
    - Range: `0.0–1.0`; `0.9` is quite strict.
- `CROSSREF_ROWS_PER_QUERY`
    - Number of Crossref candidates to retrieve per title search (`rows=` parameter).
    - Default `5` is usually enough; higher values cost more API calls and time.
- `MAX_ACCEPTED`
    - Global cap on how many DOIs the script will accept before stopping early.
    - Useful if you want to test on a subset.
- `MAILTO`
    - Your email address, passed to Crossref via the `mailto` parameter as recommended for responsible API use.

***

## Input and output files

By default, filenames are prefixed with the year range:

- Raw DiVA export (CSV):
    - `DOWNLOADED_CSV = "<FROM_YEAR>-<TO_YEAR>_diva_raw.csv"`
- DOI candidates (CSV):
    - `OUTPUT_CSV = "<FROM_YEAR>-<TO_YEAR>_doi_candidates.csv"`
- DOI candidates with links (Excel):
    - `EXCEL_OUT = "<FROM_YEAR>-<TO_YEAR>_doi_candidates_links.xlsx"`


### CSV columns

The script preserves all columns from the DiVA export, but re‑orders them in the output CSV to bring identifiers to the front. The core order is:

```text
PID, Possible DOI:s, DOI, ISI, ScopusId,
Title, Year, PublicationType,
Journal, Volume, Issue, Pages, StartPage, EndPage,
JournalISSN, JournalEISSN,
SeriesISSN, SeriesEISSN,
ISBN, ISBN_PRINT, ISBN_ELECTRONIC, ISBN_UNDEFINED,
ArticleId, PMID,
... any remaining original columns ...
```

Notes:

- If `Possible DOI:s` was not present in the original export, it is created and inserted immediately after `DOI`.
- The final CSV only contains rows where `Possible DOI:s` is non‑empty.


### Excel columns and hyperlinks

The Excel output (`EXCEL_OUT`) uses the same base metadata as the CSV, plus link columns:

```
- `PID_link` – hyperlink to the DiVA record (`https://<portal>.diva-portal.org/smash/record.jsf?pid=<pid>`).  
```

- `DOI_link` – hyperlink to `https://doi.org/<DOI>`.
- `ISI_link` – hyperlink to the Web of Science record via `KeyUT=<ISI>`.
- `Scopus_link` – hyperlink to the Scopus record.

The main Excel column order is:

```text
PID, PID_link,
Possible DOI:s, DOI_link,
DOI,
ISI, ISI_link,
ScopusId, Scopus_link,
Title, Year, PublicationType,
Journal, Volume, Issue, Pages, StartPage, EndPage,
JournalISSN, JournalEISSN,
SeriesISSN, SeriesEISSN,
ISBN, ISBN_PRINT, ISBN_ELECTRONIC, ISBN_UNDEFINED,
ArticleId, PMID,
... any remaining original columns ...
```

Hyperlink cells are written via `xlsxwriter` so that clicking on them opens the respective URLs directly from Excel.

***

## URL generation helpers

The script contains small helper functions to prepare stable URLs:

- `make_pid_url(pid: str) -> str`
    - Handles both plain numeric PIDs and `diva2:*` IDs.
    - If `pid` is purely digits (e.g. `"1949624"`), it is transformed into `"diva2:1949624"` before building the DiVA record URL.
    - Returns:
        - `https://<DIVA_PORTAL>.diva-portal.org/smash/record.jsf?pid=diva2%3A1949624` (URL‑encoded PID).
- `make_doi_url(doi: str) -> str`
    - Returns `https://doi.org/<doi>`.
- `make_scopus_url(eid: str) -> str`
    - Returns `https://www.scopus.com/record/display.url?origin=inward&partnerID=40&eid=<eid>`.
- `make_isi_url(isi: str) -> str`
    - Returns a Web of Science “Full Record” link using `KeyUT=<ISI>`.

These are used to populate the hyperlink columns in the Excel export.

***

## Usage

At the moment the script is configured via constants rather than a CLI interface.

1. Clone (or download) the repository and install dependencies.
2. Open `find_doi.py` and adjust configuration near the top:
    - `FROM_YEAR`, `TO_YEAR`
    - `DIVA_PORTAL`
    - Identifier selection flags (`SCOPUS_ONLY`, `ISI_ONLY`, `BOTH_TYPES`)
    - Crossref parameters (`SIM_THRESHOLD`, `MAX_ACCEPTED`, `CROSSREF_ROWS_PER_QUERY`, `MAILTO`).
3. Run the script:
```bash
python find_doi.py
```

4. After it finishes, review:
    - `YYYY-YYYY_diva_raw.csv` – raw DiVA export.
    - `YYYY-YYYY_doi_candidates.csv` – subset of records with suggested DOIs.
    - `YYYY-YYYY_doi_candidates_links.xlsx` – the same subset with clickable links.

You can then manually check the suggested DOIs (e.g. by following the DOI links, comparing titles/years/journals) and update DiVA records as appropriate.

***

## Current “new” features compared to early versions

In its current form, the script adds several capabilities that may not have been present (or documented) earlier:

- **DiVA `dateIssued`‑based export**
    - Uses `aq` with `dateIssued.from`/`dateIssued.to` instead of, or in addition to, other filters to better target a specific publication period.
- **Configurable portal**
    - `DIVA_PORTAL` parameter for switching between DiVA consortium members without code changes elsewhere.
- **Title/Year filtering and exclusion of front matter**
    - Additional `Year` filtering on the exported CSV.
    - Drops generic `Foreword`/`Preface` titles to avoid noise.
- **Explicit identifier routing**
    - Clear handling of:
        - Scopus‑only (no DOI, no ISI, `ScopusId` present).
        - ISI‑only (no DOI, `ISI` present, no `ScopusId`).
    - Config flags to restrict to only one of these sets or both.
- **Jaccard title‑similarity threshold**
    - A simple, transparent similarity measure, with a configurable acceptance threshold.
- **Global acceptance cap**
    - `MAX_ACCEPTED` to stop after a certain number of accepted DOIs (useful for incremental runs).
- **Improved PID handling for DiVA URLs**
    - Automatically converts pure numeric `PID`s into `diva2:PID` before building record links.
- **Hyperlink‑rich Excel output**
    - Dedicated link columns for DiVA, DOI, Scopus, and Web of Science, written as true hyperlinks via `xlsxwriter`.

***

## Limitations and notes

- The script only **suggests** possible DOIs; final validation remains a manual task.
- Some records may not be in Crossref or may have ambiguous titles; these will either be skipped or rejected if similarity is below threshold.
- The Crossref API is a shared, rate‑limited service; adjust sleep, `rows`, and `MAX_ACCEPTED` responsibly if you run large batches.

***

## License

This project is licensed under the MIT License.

Copyright (c) 2025 Anders Wändahl

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the “Software”), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.

