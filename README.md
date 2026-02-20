# DiVA_Find_DOI



This script downloads metadata from DiVA for a given year range, picks records that only have a Scopus or ISI identifier (no DOI), queries Crossref by title+year, and writes out only the records where it could confidently suggest a DOI.


***

## What this script does

1. **Downloads a CSV from DiVA**
    - Uses the DiVA export endpoint for a given portal (e.g. `kth.diva-portal.org`).
    - Filters by `dateIssued` between `FROM_YEAR` and `TO_YEAR`.
    - Requests fields: `PID, PublicationType, Year, DOI, ISI, ScopusId, Title`.
    - Saves the file locally as `diva_raw.csv`.
2. **Filters the DiVA data by publication year and identifiers**
    - Reads `diva_raw.csv` into a pandas DataFrame.
    - Enforces that the exported `Year` column is between `FROM_YEAR` and `TO_YEAR` (numeric).
    - Ensures there is a column `Possible DOI:s` placed immediately after `DOI`.
    - Builds masks to find records that:
        - Have **no DOI**, and
        - Have either:
            - **Scopus‑only**: `ScopusId` present, `ISI` empty, or
            - **ISI‑only**: `ISI` present, `ScopusId` empty, or
            - **Both types** (union), depending on the config flags.
    - Requires non‑empty `Title` and `Year`.
    - This filtered subset is stored in `df_work`.
3. **Queries Crossref to find possible DOIs**
    - For each row in `df_work` (up to `MAX_ACCEPTED` accepted matches):
        - Uses the title and year to call the Crossref REST API `/works` endpoint.
        - Applies a Crossref filter `from-pub-date=Year-01-01, until-pub-date=Year-12-31`.
        - For each candidate returned:
            - Extracts Crossref’s title and issued year.
            - Skips candidates where the Crossref year does **not** equal the DiVA year.
            - Computes a simple title similarity score (token Jaccard).
        - Chooses the candidate with the highest similarity.
        - If `similarity >= SIM_THRESHOLD` (default 0.9), writes that DOI into `Possible DOI:s`.
    - Stops early once `MAX_ACCEPTED` records have accepted DOIs.
4. **Writes only matched records to output**
    - Keeps only rows in `df_work` where `Possible DOI:s` is non‑empty.
    - Writes them to `doi_candidates.csv`.

***

## Configuration section (`HEAD` of script)

At the top of the script you can tune behavior without touching the logic:

```python
# Year window for DiVA export and local filtering
FROM_YEAR = 1990
TO_YEAR = 2000

# Which DiVA portal to use (host prefix)
DIVA_PORTAL = "kth"   # e.g. "kth", "uu", "umu", "lnu"
DIVA_BASE = f"https://{DIVA_PORTAL}.diva-portal.org/smash/export.jsf"

# Identifier selection
SCOPUS_ONLY = False   # only ScopusId present, no DOI/ISI
ISI_ONLY = False      # only ISI present, no DOI/ScopusId
BOTH_TYPES = True     # if True, union of the two masks above

# Crossref matching parameters
SIM_THRESHOLD = 0.9   # minimum title similarity to accept a DOI
MAX_ACCEPTED = 10     # stop after this many accepted matches
CROSSREF_ROWS_PER_QUERY = 5  # how many Crossref candidates to fetch per title
MAILTO = "email@domain.com"  # your email (polite Crossref usage)

# File names
DOWNLOADED_CSV = "diva_raw.csv"
OUTPUT_CSV = "doi_candidates.csv"
```

Where you can change:

- **Year window**:
    - Adjust `FROM_YEAR` and `TO_YEAR` to any range you want.
- **Portal**:
    - Change `DIVA_PORTAL` to another DiVA site (`"www"`, `"umu"`, `"lnu"`, etc.).
- **Identifier logic**:
    - Scopus‑only: `SCOPUS_ONLY = True`, `ISI_ONLY = False`, `BOTH_TYPES = False`.
    - ISI‑only: `SCOPUS_ONLY = False`, `ISI_ONLY = True`, `BOTH_TYPES = False`.
    - Either Scopus‑only or ISI‑only: `BOTH_TYPES = True`.
- **Crossref strictness**:
    - Raise `SIM_THRESHOLD` to be stricter (e.g. `0.95`), or lower it to get more candidates.
    - Increase `MAX_ACCEPTED` to collect more matches in a single run.
    - Adjust `CROSSREF_ROWS_PER_QUERY` if you want more/less candidates per query.
- **Output filenames**:
    - Change `OUTPUT_CSV` to keep multiple runs separate.

***

## How to run the script

1. **Install dependencies** (once):
```bash
pip install requests pandas tqdm
```

2. **Save the script** as e.g. `find_doi.py`.
3. **Edit the configuration block** at the top:

- Set `MAILTO` to your real email.
- Optionally adjust years, portal, and flags (`SCOPUS_ONLY`, `ISI_ONLY`, `BOTH_TYPES`, thresholds).

4. **Run the script** from the directory where you saved it:
```bash
python3 find_doi.py
```

You will see logs like:

- “Downloading DiVA CSV from …”
- “After Year filter 1990–2000: N rows”
- “Working rows: M”
- Per‑row messages for Crossref queries and candidate DOIs.
- Finally:
    - “Accepted X records.”
    - “Wrote Y rows with Possible DOI:s to doi_candidates.csv”.

The resulting `doi_candidates.csv` will contain only those records (from the specified period and identifier logic) for which a high‑confidence DOI could be inferred.
