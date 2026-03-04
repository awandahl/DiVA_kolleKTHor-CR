
# DiVA-kolleKTHor


This script harvests publications from a DiVA portal for a given year range and tries to find missing DOIs via the Crossref REST API. It focuses on records **without any external identifiers** (DOI, ISI, ScopusId, PMID) and classifies Crossref matches as either **verified** or **possible** DOIs, with per–publication-type verification rules.

Outputs:

- A raw DiVA CSV snapshot for the given year range.  
- A CSV with DOI candidates (`Verified_DOI` / `Possible_DOI` + check flags).  
- An Excel file with the same data plus clickable links back to DiVA and to the DOI resolver.

---

## Overview of the workflow

1. Build a DiVA export URL for `FROM_YEAR`–`TO_YEAR` using the `export.jsf` endpoint and a CSV field list.
2. Download the CSV and read it into a pandas DataFrame.  
3. Filter to:
   - Records within the year range.
   - Publication types: article, review, book, chapter, conference paper.
   - Records with **no** DOI, ISI, ScopusId, or PMID.
   - Records with non-empty `Title` and `Year`.  
4. For each remaining record:
   - Derive a coarse publication category (`article`, `conference`, `chapter`, `book`) from DiVA’s `PublicationType`.
   - Query Crossref `/works` with `query.title` and a publication-year filter.
   - For each Crossref candidate:
     - Check title similarity and publication year.
     - Map Crossref `type` to a coarse category and enforce a type match.
     - Fetch full Crossref metadata for promising candidates.
     - Apply **type-specific verification checks** (ISSN, biblio, authors, host/book ISBN).
   - If a candidate passes *all* required checks → record as `Verified_DOI`.  
   - If no candidate fully verifies, but one passes similarity + type/year → record the best as `Possible_DOI`.  
   - If even that fails but there is a perfect title match, record that DOI as `Possible_DOI` with `"title_only"` flags.
5. Write out CSV and Excel with:
   - `Verified_DOI`, `Possible_DOI`.
   - `Check_*` columns summarizing which checks passed.
   - Links back to DiVA (`PID_link`) and to the DOI (`*_DOI_link`).

---

## Requirements and installation

- Python 3.9+ recommended.
- Packages:
  - `requests`
  - `pandas`
  - `tqdm`
  - `xlsxwriter` (via pandas ExcelWriter)

Install dependencies, for example:

```bash
pip install requests pandas tqdm xlsxwriter
```


---

## Configuration

At the top of the script:

```python
FROM_YEAR = 2001
TO_YEAR = 2002              # inclusive

DIVA_PORTAL = "kth"         # e.g. "kth", "uu", "umu", "lnu"
NO_ID_ONLY = True           # only records with no DOI/ISI/ScopusId/PMID

SIM_THRESHOLD = 0.9         # minimum title similarity for considering a candidate
MAX_ACCEPTED = 9999         # cap on how many records to process
CROSSREF_ROWS_PER_QUERY = 5 # max Crossref candidates per title/year
MAILTO = "email@domain.org" # your email for Crossref polite usage
```

Filenames are derived from portal + year range + a timestamp:

- `kth_2001-2002_diva_raw.csv`
- `kth_2001-2002_diva_doi_candidates_YYYYMMDD-HHMMSS.csv`
- `kth_2001-2002_diva_doi_candidates_YYYYMMDD-HHMMSS.xlsx`

You normally only change:

- `FROM_YEAR`, `TO_YEAR`
- `DIVA_PORTAL`
- `MAILTO`

---

## How DiVA records are selected

1. **Export from DiVA**

`build_diva_url` constructs a CSV export URL to `.../smash/export.jsf` including:
    - Date filter: `dateIssued` between `FROM_YEAR` and `TO_YEAR`.
    - Publication types: bookReview, review, article, book, chapter, conferencePaper.
    - Fields: `PID`, `DOI`, `ISI`, `ScopusId`, `PMID`, title, year, journal, volume/issue/pages, ISSNs/ISBNs, authors, notes etc.
2. **Initial filtering in the script**

After reading the CSV:
    - Year range is re-checked from the `Year` column.
    - Titles like `Foreword` / `Preface` are excluded.
    - Only records with:
        - Empty `DOI`, `ISI`, `ScopusId`, `PMID` (if `NO_ID_ONLY = True`).
        - Non-empty `Title` and `Year`.

The resulting subset (`df_work`) is the set of DiVA records the script tries to enrich with DOIs.

---

## Publication type categories

`diva_pubtype_category` maps a DiVA `PublicationType` string to a coarse category:

- **Article**
    - `article`
    - `Article in journal`
    - `Article, review/survey`
    - `Article, book review`
    - `review`, `bookreview`, `book review`
- **Conference**
    - `conferencepaper`
    - `Conference paper`
    - `Paper in conference proceeding(s)`
- **Chapter**
    - `chapter`
    - `Chapter in book`
    - `Chapter in anthology`
- **Book**
    - `book`
    - `monograph`

Anything else returns `None` and is treated as “unknown type”; in that case the script still requires authors and basic biblio for verification, but no ISSN/ISBN checks.

On the Crossref side, `crossref_type_category` maps `message["type"]` to the same categories:

- `journal-article`, `journal-review`, `peer-review` → article
- `proceedings-article`, `proceedings-paper`, `conference-paper` → conference
- `book-chapter`, `chapter` → chapter
- `book` → book

A Crossref candidate is skipped if both sides have a category and they do not match.

---

## Title similarity and candidate selection

For each DiVA record:

1. `search_crossref_title` calls `/works` with:
    - `query.title` = cleaned DiVA title.
    - `filter` = `from-pub-date:YYYY-01-01,until-pub-date:YYYY-12-31` based on DiVA `Year`.
    - `rows` = `CROSSREF_ROWS_PER_QUERY` and `select=DOI,title,issued,type` for efficiency.
2. For each Crossref candidate:
    - Discard if the publication year from `issued["date-parts"]` does not equal the DiVA year.
    - Discard if Crossref type category conflicts with DiVA category.
    - Compute Jaccard-like title similarity: tokenized lowercase title, intersection/union of word sets.
    - Keep only candidates with `sim ≥ SIM_THRESHOLD`.

The best similarity score among candidates above threshold is kept as a potential **possible match**, while stronger conditions must hold for a **verified match**.

---

## Per-type verification checks

For candidates that pass title similarity + year (+ type category), the script fetches full metadata (`/works/{doi}`) and applies **type-dependent checks**.

### Common building blocks

- **Bibliographic match** (`bibliographic_match`):
    - Compares DiVA vs Crossref for:
        - Volume
        - Issue
        - Start page (or article number)
        - End page
    - For any field present on both sides, it logs match/mismatch and returns True only if *all compared fields* match.
- **ISSN match** (`issn_match`):
    - DiVA: `JournalISSN`, `JournalEISSN`, `SeriesISSN`, `SeriesEISSN`.
    - Crossref: `ISSN` array + `journal-issue.ISSN`.
    - True if the normalized ISSN sets intersect.
- **Author match** (`authors_match`):
    - DiVA: parses the `Name` column, strips local IDs and affiliations, assumes `Family, Given` format, uses just family names.
    - Crossref: uses `author[i]["family"]`.
    - True if there is at least one shared family name.
- **Host ISBN match** (`extract_host_isbns` + `extract_crossref_isbns`):

Used for **conference papers** and **chapters** to connect a chapter/paper to its host proceedings/book.
    - DiVA host ISBNs:
        - Any value in `ISBN`, `ISBN_PRINT`, `ISBN_ELECTRONIC` (for older records where host ISBNs were put directly on the item).
        - Any ISBN pattern found in `Notes` (e.g. “Part of ISBN 978-1-2345-6789-0”, “Part of book ISBN …”, “Part of proceedings ISBN …”).
        - Normalized by stripping non-digits and `X/x`.
    - Crossref ISBNs: `message["ISBN"]`, similarly normalized.

Host ISBN check is True if `host_isbns ∩ crossref_isbns` is non-empty.
- **Book ISBN match** (`extract_diva_book_isbns` + `extract_crossref_isbns`):

Used for **books**:
    - DiVA: ISBNs from `ISBN`, `ISBN_PRINT`, `ISBN_ELECTRONIC`.
    - Crossref: `message["ISBN"]`.
    - True if the normalized sets intersect.


### Category-specific rules

For each candidate, the script sets booleans:

```python
need_issn
need_biblio
need_authors
need_host_isbn
need_book_isbn
```

Then evaluates:

```python
all_ok = (
    issn_ok
    and biblio_ok
    and (not need_authors or author_ok)
    and (not need_host_isbn or host_isbn_ok)
    and (not need_book_isbn or book_isbn_ok)
)
```

and uses `all_ok` to decide if the candidate is **verified**.

#### Article (journal article / review)

Conditions:

- Title similarity ≥ `SIM_THRESHOLD`.
- Year match.
- Crossref type maps to `article`.
- **Required:**
    - ISSN match (`need_issn = True`).
    - Bibliographic match on volume/issue/pages (`need_biblio = True`).
    - Author overlap (`need_authors = True`).

No ISBN checks are used for articles.

#### Conference paper

Conditions:

- Title similarity ≥ `SIM_THRESHOLD`.
- Year match.
- Crossref type maps to `conference`.
- **Required:**
    - Bibliographic match on pages (and volume/issue if present) (`need_biblio = True`).
    - Author overlap (`need_authors = True`).
    - Host ISBN match (`need_host_isbn = True`) using misused ISBN fields and “Part of … ISBN …” strings in `Notes`.

ISSN is **not** required for conference papers.

#### Chapter (book chapter)

Conditions:

- Title similarity ≥ `SIM_THRESHOLD`.
- Year match.
- Crossref type maps to `chapter`.
- **Required:**
    - Bibliographic match on pages (and volume/issue if present) (`need_biblio = True`).
    - Author overlap (`need_authors = True`).
    - Host ISBN match (`need_host_isbn = True`) as above.


#### Book

Conditions:

- Title similarity ≥ `SIM_THRESHOLD`.
- Year match.
- Crossref type maps to `book`.
- **Required:**
    - Author overlap (`need_authors = True`).
    - Book ISBN match (`need_book_isbn = True`) between DiVA ISBNs and Crossref ISBNs.

No pages or ISSNs are required for books.

#### Unknown / other types

If `diva_pubtype_category` returns `None`:

- The script still requires:
    - Bibliographic match (`need_biblio = True`).
    - Author overlap (`need_authors = True`).
- ISSN and ISBN checks are disabled.

This prevents “everything” from becoming verified when the type string is not recognized.

---

## Verified vs possible DOIs

For each DiVA record:

1. **Verified DOI**
    - If at least one candidate passes **all required checks** for its category (`all_ok=True`), the candidate with the highest similarity is stored as `Verified_DOI`.
    - The script also stores:
        - `Check_Category` (`article`, `conference`, `chapter`, `book`, or empty).
        - `Check_ISSN_OK`, `Check_Biblio_OK`, `Check_Authors_OK`, `Check_HostISBN_OK`, `Check_BookISBN_OK` (string booleans).
2. **Possible DOI**
    - If no candidate is fully verified but at least one has `sim ≥ SIM_THRESHOLD` and matches year/type:
        - The best such candidate is stored as `Possible_DOI`.
        - Its check results are stored in the same `Check_*` columns.
    - If there is no such candidate, but there exists a **perfect title match** (`sim == 1.0`) with matching year:
        - That DOI is stored as `Possible_DOI`.
        - `Check_*` columns are set to `"title_only"` to indicate it is a title-only fallback.
3. If neither verified nor possible conditions are met, the record is left without a DOI candidate.

---

## Running the script

Run directly:

```bash
python find_doi_from_diva_smart.py
```

To save a detailed log of the CLI output (for later inspection of decisions):

```bash
python find_doi_from_diva_smart.py 2>&1 | tee kth_2001-2002_doi.log
```

After completion, you will have:

- `kth_2001-2002_diva_raw.csv` – the DiVA export snapshot.
- `kth_2001-2002_diva_doi_candidates_YYYYMMDD-HHMMSS.csv` – candidates + checks.
- `kth_2001-2002_diva_doi_candidates_YYYYMMDD-HHMMSS.xlsx` – same, with:
    - `PID_link` – URL to the DiVA record.
    - `Verified_DOI_link` – `https://doi.org/<Verified_DOI>`.
    - `Possible_DOI_link` – `https://doi.org/<Possible_DOI>`.

You can sort or filter on:

- `Check_Category` (article / conference / chapter / book).
- `Check_*_OK` to find borderline matches.
- `Verified_DOI` vs `Possible_DOI` to prioritize manual review.

---
```
 

