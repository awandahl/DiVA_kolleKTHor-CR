
# DiVA DOI-Monitor

Below is an integrated script that:

- Uses **OAI-PMH** as input (with optional delta mode).
- Filters to your six **publication types of interest**.
- Reuses your **title similarity** and **Crossref matching** logic.
- Adds **publication-type comparison** between DiVA and Crossref.
- Produces the same outputs as before: CSV + Excel with link columns, with **year-range prefixes** in filenames.

After the code, I explain how to use it for an initial backfill and for ongoing monitoring.

***

## 1. Integrated script

Save as e.g. `doi_monitor.py`:

```python
#!/usr/bin/env python3
import time
import re
import requests
import pandas as pd
from tqdm import tqdm
from urllib.parse import urlencode, quote
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

# -------------------- CONFIG --------------------

# Year range for publication year filter
FROM_YEAR = 2025
TO_YEAR = 2025

# Mode
DELTA_MODE = False  # False = full OAI harvest, True = only new/updated records

# DiVA portal settings
DIVA_PORTAL = "kth"

# OAI-PMH settings for KTH DiVA
OAI_BASE = f"https://{DIVA_PORTAL}.diva-portal.org/dice/oai"
OAI_METADATA_PREFIX = "swepub_mods"
OAI_SET = "all-kth"          # all KTH publications; we filter pubType locally

# Delta state (for monitoring)
DELTA_STATE_FILE = "oai_last_datestamp.txt"

# Crossref matching
SIM_THRESHOLD = 0.9
MAX_ACCEPTED = 9999
CROSSREF_ROWS_PER_QUERY = 5
MAILTO = "aw@kth.se"  # your email

# Output filenames (with year range prefix)
RANGE_PREFIX = f"{FROM_YEAR}-{TO_YEAR}_"
DOWNLOADED_CSV = RANGE_PREFIX + "diva_raw_oai.csv"
OUTPUT_CSV = RANGE_PREFIX + "doi_candidates.csv"
EXCEL_OUT = RANGE_PREFIX + "doi_candidates_links.xlsx"

# Only keep these DiVA publication types
RELEVANT_PUBTYPES = {
    "article",
    "conferencePaper",
    "chapter",
    "book",
    "review",
    "bookReview",
}

# HTTP headers (avoid 403)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0 Safari/537.36"
    )
}

# XML namespaces
NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "mods": "http://www.loc.gov/mods/v3",
}

# -------------------- OAI-PMH DELTA HELPERS --------------------

def load_last_datestamp() -> str | None:
    try:
        with open(DELTA_STATE_FILE, "r", encoding="utf-8") as f:
            v = f.read().strip()
            return v or None
    except FileNotFoundError:
        return None

def save_last_datestamp(ds: str) -> None:
    with open(DELTA_STATE_FILE, "w", encoding="utf-8") as f:
        f.write(ds)

def current_oai_until() -> str:
    # OAI-PMH datestamp format: YYYY-MM-DDThh:mm:ssZ
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def oai_listrecords_url(resumption_token: str | None = None,
                        from_ds: str | None = None,
                        until_ds: str | None = None) -> str:
    if resumption_token:
        params = {"verb": "ListRecords", "resumptionToken": resumption_token}
    else:
        params = {
            "verb": "ListRecords",
            "metadataPrefix": OAI_METADATA_PREFIX,
            "set": OAI_SET,
        }
        if from_ds:
            params["from"] = from_ds
        if until_ds:
            params["until"] = until_ds
    return f"{OAI_BASE}?{urlencode(params)}"

def extract_year_from_date(date_str: str) -> str:
    date_str = (date_str or "").strip()
    if len(date_str) >= 4 and date_str[:4].isdigit():
        return date_str[:4]
    return ""

# -------------------- MODS PARSING --------------------

def parse_mods_record(rec_el: ET.Element) -> tuple[dict | None, str | None]:
    """
    Parse one <record> into a flat dict approximating the CSV fields.
    Returns (row_dict_or_None, datestamp_or_None).
    """
    header = rec_el.find("oai:header", NS)
    if header is None or header.get("status") == "deleted":
        return None, None

    datestamp = (header.findtext("oai:datestamp", default="", namespaces=NS) or "").strip()
    oai_identifier = header.findtext("oai:identifier", default="", namespaces=NS)

    metadata = rec_el.find("oai:metadata", NS)
    if metadata is None:
        return None, datestamp

    mods = metadata.find("mods:mods", NS)
    if mods is None:
        return None, datestamp

    row: dict[str, str] = {}

    # ---------- Identifiers: PID, DOI, URN, ISI, ScopusId, ISBNs ----------
    for ident in mods.findall("mods:identifier", NS):
        id_type = (ident.get("type") or "").strip().lower()
        value = (ident.text or "").strip()
        if not value:
            continue

        # DiVA PID / local ID
        if id_type in {"local", "diva"} or value.startswith("diva2:"):
            row["PID"] = value

        # DOI
        if id_type == "doi" or value.lower().startswith("10."):
            if id_type == "doi" or "DOI" not in row:
                row["DOI"] = value

        # URN
        if id_type == "urn":
            row.setdefault("URN", value)

        # ISI / Web of Science
        if id_type in {"isi", "wos"}:
            row["ISI"] = value

        # Scopus
        if id_type in {"scopus", "eid"}:
            row["ScopusId"] = value

        # ISBN variants
        if id_type in {"isbn", "isbn_print", "isbn-print"}:
            row.setdefault("ISBN_PRINT", value)
        if id_type in {"isbn_electronic", "isbn-electronic", "eisbn"}:
            row.setdefault("ISBN_ELECTRONIC", value)

    # Fallback PID from OAI identifier if needed
    if "PID" not in row and oai_identifier:
        row["PID"] = oai_identifier.split(":", 2)[-1]

    # ---------- Publication type ----------
    pub_type = ""
    for genre in mods.findall("mods:genre", NS):
        v = (genre.text or "").strip()
        if v:
            pub_type = v
            break
    row["PublicationType"] = pub_type

    # Only keep relevant pub types
    if pub_type and pub_type not in RELEVANT_PUBTYPES:
        return None, datestamp

    # ---------- Title ----------
    title_info = mods.find("mods:titleInfo", NS)
    if title_info is not None:
        main_title = title_info.findtext("mods:title", default="", namespaces=NS)
        row["Title"] = (main_title or "").strip()

    # ---------- Year and dateIssued ----------
    year = ""
    date_issued_full = ""
    for di in mods.findall("mods:originInfo/mods:dateIssued", NS):
        v = (di.text or "").strip()
        if v:
            date_issued_full = v
            year = extract_year_from_date(v)
            break
    row["Year"] = year
    row["dateIssued"] = date_issued_full

    # ---------- Journal / host info ----------
    related_item = mods.find("mods:relatedItem[@type='host']", NS)
    if related_item is not None:
        journal_title = related_item.findtext("mods:titleInfo/mods:title", default="", namespaces=NS)
        row["Journal"] = (journal_title or "").strip()

        volume = related_item.findtext(
            "mods:part/mods:detail[@type='volume']/mods:number",
            default="",
            namespaces=NS,
        )
        issue = related_item.findtext(
            "mods:part/mods:detail[@type='issue']/mods:number",
            default="",
            namespaces=NS,
        )
        start_page = related_item.findtext(
            "mods:part/mods:extent[@unit='pages']/mods:start",
            default="",
            namespaces=NS,
        )
        end_page = related_item.findtext(
            "mods:part/mods:extent[@unit='pages']/mods:end",
            default="",
            namespaces=NS,
        )

        row["Volume"] = (volume or "").strip()
        row["Issue"] = (issue or "").strip()
        row["StartPage"] = (start_page or "").strip()
        row["EndPage"] = (end_page or "").strip()

    # ---------- ISSNs ----------
    for ident in mods.findall("mods:identifier", NS):
        id_type = (ident.get("type") or "").strip().lower()
        value = (ident.text or "").strip()
        if not value:
            continue
        if id_type == "issn":
            row.setdefault("JournalISSN", value)
        if id_type in {"eissn", "issn-electronic"}:
            row.setdefault("JournalEISSN", value)

    # ---------- Simple ArticleId placeholder ----------
    if "ArticleId" not in row and "PID" in row:
        row["ArticleId"] = row["PID"]

    return row, datestamp

def harvest_diva_oai_to_df(delta_mode: bool) -> pd.DataFrame:
    """Harvest OAI-PMH records and return a DataFrame of relevant pub types."""
    records: list[dict] = []
    token: str | None = None

    if delta_mode:
        last_ds = load_last_datestamp()
        from_ds = last_ds
        until_ds = current_oai_until()
        print(f"Delta mode: from={from_ds}, until={until_ds}")
    else:
        from_ds = None
        until_ds = None
        print("Full harvest mode (no datestamp filter)")

    max_seen_ds = load_last_datestamp() or ""

    while True:
        url = oai_listrecords_url(token, from_ds, until_ds)
        print(f"Fetching OAI-PMH: {url}")
        r = requests.get(url, headers=HEADERS, timeout=60)
        r.raise_for_status()

        root = ET.fromstring(r.content)

        for rec_el in root.findall(".//oai:record", NS):
            row, ds = parse_mods_record(rec_el)
            if ds and ds > max_seen_ds:
                max_seen_ds = ds
            if row:
                records.append(row)

        rt_el = root.find(".//oai:resumptionToken", NS)
        if rt_el is None or not (rt_el.text or "").strip():
            break

        token = (rt_el.text or "").strip()
        time.sleep(1.0)  # be polite

    df = pd.DataFrame(records).fillna("")

    # Save last datestamp after delta run
    if delta_mode and max_seen_ds:
        save_last_datestamp(max_seen_ds)
        print(f"Saved last datestamp: {max_seen_ds}")

    # Ensure expected columns exist
    expected_cols = [
        "PID",
        "ArticleId",
        "DOI",
        "EndPage",
        "ISBN",
        "ISBN_ELECTRONIC",
        "ISBN_PRINT",
        "ISBN_UNDEFINED",
        "ISI",
        "Issue",
        "Journal",
        "JournalEISSN",
        "JournalISSN",
        "Pages",
        "PublicationType",
        "PMID",
        "ScopusId",
        "SeriesEISSN",
        "SeriesISSN",
        "StartPage",
        "Title",
        "Volume",
        "Year",
    ]
    for col in expected_cols:
        if col not in df.columns:
            df[col] = ""

    # Derive Pages if missing
    mask_pages_empty = df["Pages"].eq("")
    df.loc[mask_pages_empty, "Pages"] = (
        df["StartPage"].where(df["EndPage"].eq(""), df["StartPage"] + "-" + df["EndPage"])
    )

    # Filter by publication year range
    def to_int_or_none(s: str):
        try:
            return int(s.strip())
        except Exception:
            return None

    year_int = df["Year"].apply(to_int_or_none)
    mask_year = year_int.between(FROM_YEAR, TO_YEAR, inclusive="both")
    df = df[mask_year].copy()

    return df

# -------------------- TITLE SIMILARITY --------------------

def normalize_title(t: str) -> list[str]:
    t = t.lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return [tok for tok in t.split() if tok]

def title_similarity(a: str, b: str) -> float:
    ta = set(normalize_title(a))
    tb = set(normalize_title(b))
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union  # Jaccard similarity[web:78][web:153]

# -------------------- PUBLICATION TYPE MAPPING --------------------

def diva_pubtype_category(diva_type: str) -> str | None:
    t = (diva_type or "").strip().lower()
    if t == "article":
        return "article"
    if t == "conferencepaper":
        return "conference"
    if t == "book":
        return "book"
    if t == "chapter":
        return "chapter"
    if t == "review":
        return "article"   # treat review as journal article
    if t == "bookreview":
        return "article"   # book reviews are also journal pieces
    return None

def crossref_type_category(cr_type: str | None) -> str | None:
    if not cr_type:
        return None
    t = cr_type.strip().lower()
    if t == "journal-article":
        return "article"
    if t in {"proceedings-article", "proceedings-paper", "conference-paper"}:
        return "conference"
    if t == "book":
        return "book"
    if t in {"book-chapter", "chapter"}:
        return "chapter"
    if t in {"journal-review", "peer-review"}:
        return "article"
    return None

# -------------------- CROSSREF QUERY --------------------

def search_crossref_title(title: str, year: int | None = None, max_results: int = 5):
    params = {
        "query.title": title,
        "rows": max_results,
        "select": "DOI,title,issued,type",
        "mailto": MAILTO,
    }
    if year:
        params["filter"] = f"from-pub-date:{year}-01-01,until-pub-date:{year}-12-31"

    r = requests.get("https://api.crossref.org/works", params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    items = data.get("message", {}).get("items", [])
    results = []
    for it in items:
        doi = it.get("DOI")
        title_list = it.get("title") or []
        cand_title = title_list[^0] if title_list else ""
        issued = it.get("issued", {})
        cand_year = None
        try:
            parts = issued.get("date-parts")
            if parts and len(parts[^0]) > 0:
                cand_year = int(parts[^0][^0])
        except Exception:
            cand_year = None
        cr_type = it.get("type")
        if doi:
            results.append((doi, cand_title, cand_year, cr_type))
    return results  # Crossref REST fields & type doc[web:145][web:150]

# -------------------- LINK HELPERS --------------------

def make_scopus_url(eid: str) -> str:
    eid = eid.strip()
    if not eid:
        return ""
    return f"https://www.scopus.com/record/display.url?origin=inward&partnerID=40&eid={eid}"

def make_doi_url(doi: str) -> str:
    doi = doi.strip()
    if not doi:
        return ""
    return f"https://doi.org/{doi}"

def make_isi_url(isi: str) -> str:
    isi = isi.strip()
    if not isi:
        return ""
    return (
        "https://gateway.webofknowledge.com/api/gateway"
        "?GWVersion=2&SrcAuth=Name&SrcApp=sfx&DestApp=WOS"
        "&DestLinkType=FullRecord&KeyUT=" + requests.utils.quote(isi, safe="")
    )

def make_pid_url(pid: str) -> str:
    pid = pid.strip()
    if not pid:
        return ""
    # If PID is a plain number like "1949624", turn it into "diva2:1949624"
    if pid.isdigit():
        pid_value = f"diva2:{pid}"
    else:
        pid_value = pid
    encoded_pid = quote(pid_value, safe="")
    return f"https://{DIVA_PORTAL}.diva-portal.org/smash/record.jsf?pid={encoded_pid}"

# -------------------- MAIN PIPELINE --------------------

def run_crossref_matching(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # Ensure Possible DOI:s column exists and sits after DOI
    if "Possible DOI:s" not in df.columns:
        df["Possible DOI:s"] = ""

    cols = df.columns.tolist()
    if "Possible DOI:s" in cols and "DOI" in cols:
        cols.insert(cols.index("DOI") + 1, cols.pop(cols.index("Possible DOI:s")))
        df = df[cols]

    # Identifier logic (same as your script)
    has_doi = df["DOI"].str.strip() != ""
    has_isi = df["ISI"].str.strip() != ""
    has_scopus = df["ScopusId"].str.strip() != ""

    scopus_only_mask = (~has_doi) & (~has_isi) & has_scopus
    isi_only_mask = (~has_doi) & has_isi & (~has_scopus)

    working_mask = scopus_only_mask | isi_only_mask

    # also require title and year present
    working_mask &= (df["Title"].str.strip() != "") & (df["Year"].str.strip() != "")

    df_work = df[working_mask].copy()
    print(f"Working rows: {len(df_work)}")

    accepted_count = 0

    for idx in tqdm(df_work.index, desc="Querying Crossref"):
        if accepted_count >= MAX_ACCEPTED:
            print(f"\nReached MAX_ACCEPTED={MAX_ACCEPTED}, stopping early.")
            break

        row = df_work.loc[idx]
        pid = row["PID"].strip()
        scopus = row["ScopusId"].strip()
        isi = row["ISI"].strip()
        title = row["Title"].strip()
        year_str = row["Year"].strip()
        pub_type = row["PublicationType"].strip()

        try:
            pub_year = int(year_str)
        except Exception:
            pub_year = None

        diva_cat = diva_pubtype_category(pub_type)

        print(f"\n[{idx}] PID={pid} ScopusId={scopus} ISI={isi} PubType={pub_type}")
        print(f"  Title: '{title}'")
        print(f"  Year: {pub_year}")
        print("  -> querying Crossref...")

        try:
            candidates = search_crossref_title(title, pub_year, max_results=CROSSREF_ROWS_PER_QUERY)
        except Exception as e:
            print(f"  ERROR querying Crossref: {e}")
            continue

        if not candidates or pub_year is None:
            print("  No candidates found or no valid year")
            time.sleep(1.0)
            continue

        best_doi = None
        best_score = 0.0
        best_year = None

        for doi, cand_title, cand_year, cr_type in candidates:
            print(f"    cand: '{cand_title}' (Crossref year={cand_year}, type={cr_type})")
            if cand_year != pub_year:
                print("      -> skip (year mismatch)")
                continue

            cr_cat = crossref_type_category(cr_type)
            if diva_cat and cr_cat and cr_cat != diva_cat:
                print(f"      -> skip (type mismatch: DiVA={diva_cat}, Crossref={cr_cat})")
                continue

            sim = title_similarity(title, cand_title)
            print(f"      DOI: {doi}")
            print(f"      sim={sim:.3f}")
            if sim > best_score:
                best_score = sim
                best_doi = doi
                best_year = cand_year

        if best_doi and best_score >= SIM_THRESHOLD:
            df_work.at[idx, "Possible DOI:s"] = best_doi
            accepted_count += 1
            print(f"  ACCEPT best DOI={best_doi} (sim={best_score:.3f}, year={best_year})")
            print(f"  -> accepted so far: {accepted_count}/{MAX_ACCEPTED}")
        else:
            print(f"  REJECT all candidates (best sim={best_score:.3f}, year={best_year})")

        time.sleep(1.0)

    # Only rows where we actually found a possible DOI
    mask_has_possible = df_work["Possible DOI:s"].str.strip() != ""
    df_out = df_work[mask_has_possible].copy()

    # Reorder CSV columns: PID, Possible DOI:s, DOI, ISI, ScopusId first, then rest
    csv_col_order = [
        "PID", "Possible DOI:s", "DOI", "ISI", "ScopusId",
        "Title", "Year", "PublicationType",
        "Journal", "Volume", "Issue", "Pages", "StartPage", "EndPage",
        "JournalISSN", "JournalEISSN",
        "SeriesISSN", "SeriesEISSN",
        "ISBN", "ISBN_PRINT", "ISBN_ELECTRONIC", "ISBN_UNDEFINED",
        "ArticleId", "PMID",
    ]
    csv_col_order = [c for c in csv_col_order if c in df_out.columns]
    remaining = [c for c in df_out.columns if c not in csv_col_order]
    csv_col_order.extend(remaining)
    df_out = df_out[csv_col_order]

    return df_out

def write_outputs(df_out: pd.DataFrame):
    # Save CSV
    df_out.to_csv(OUTPUT_CSV, index=False)
    print(f"\nWrote {len(df_out)} rows with Possible DOI:s to {OUTPUT_CSV}")

    # Build links for Excel
    df_links = df_out.copy()
    df_links["PID_link"] = df_links["PID"].apply(make_pid_url)
    df_links["DOI_link"] = df_links["Possible DOI:s"].apply(make_doi_url)
    df_links["ISI_link"] = df_links["ISI"].apply(make_isi_url)
    df_links["Scopus_link"] = df_links["ScopusId"].apply(make_scopus_url)

    excel_col_order = [
        "PID", "PID_link",
        "Possible DOI:s", "DOI_link",
        "DOI",
        "ISI", "ISI_link",
        "ScopusId", "Scopus_link",
        "Title", "Year", "PublicationType",
        "Journal", "Volume", "Issue", "Pages", "StartPage", "EndPage",
        "JournalISSN", "JournalEISSN",
        "SeriesISSN", "SeriesEISSN",
        "ISBN", "ISBN_PRINT", "ISBN_ELECTRONIC", "ISBN_UNDEFINED",
        "ArticleId", "PMID",
    ]
    excel_col_order = [c for c in excel_col_order if c in df_links.columns]
    remaining = [c for c in df_links.columns if c not in excel_col_order]
    excel_col_order.extend(remaining)
    df_links = df_links[excel_col_order]

    with pd.ExcelWriter(EXCEL_OUT, engine="xlsxwriter") as writer:
        df_links.to_excel(writer, index=False, sheet_name="DOI candidates")
        ws = writer.sheets["DOI candidates"]

        header = list(df_links.columns)
        col_idx = {name: i for i, name in enumerate(header)}

        for row_xl, df_idx in enumerate(df_links.index, start=1):
            if df_links.at[df_idx, "PID_link"]:
                ws.write_url(row_xl, col_idx["PID_link"], df_links.at[df_idx, "PID_link"], string="PID")
            if df_links.at[df_idx, "DOI_link"]:
                ws.write_url(row_xl, col_idx["DOI_link"], df_links.at[df_idx, "DOI_link"], string="DOI")
            if df_links.at[df_idx, "ISI_link"]:
                ws.write_url(row_xl, col_idx["ISI_link"], df_links.at[df_idx, "ISI_link"], string="ISI")
            if df_links.at[df_idx, "Scopus_link"]:
                ws.write_url(row_xl, col_idx["Scopus_link"], df_links.at[df_idx, "Scopus_link"], string="Scopus")

    print(f"Wrote Excel with links to {EXCEL_OUT}")

# -------------------- ENTRY POINT --------------------

def main():
    df_harvest = harvest_diva_oai_to_df(delta_mode=DELTA_MODE)
    print(f"Harvested {len(df_harvest)} records after pubtype+year filters")
    df_harvest.to_csv(DOWNLOADED_CSV, index=False)
    print(f"Wrote raw OAI data to {DOWNLOADED_CSV}")

    df_out = run_crossref_matching(df_harvest)
    print(f"\nAccepted {len(df_out)} records with Possible DOI:s")

    if not df_out.empty:
        write_outputs(df_out)
    else:
        print("No DOI candidates found; nothing to write.")

if __name__ == "__main__":
    main()
```


***

## 2. Overarching logic in plain language

### What the script does

1. **Harvest from DiVA via OAI-PMH**
    - Talks to `https://kth.diva-portal.org/dice/oai` and requests `swepub_mods` records.
    - Keeps only records whose DiVA **publication type** is one of: article, conference paper, chapter in book, book, review, book review.
    - Extracts metadata: PID, title, year, publication type, existing DOI, ISI, ScopusId, pages, journal info, etc.
    - Filters to your **publication year range** (`FROM_YEAR` to `TO_YEAR`).
    - Optionally (in delta mode) fetches only records created/updated since your last run and updates a small state file with the latest OAI datestamp.[^1][^2][^3]
2. **Find records that might need DOIs**
    - From that harvested set, selects records that:
        - Have **no DOI**.
        - Have **ISI or ScopusId**, but not both (your original logic: Scopus‑only or ISI‑only).
        - Have a non-empty title and year.
3. **Query Crossref and score candidates**

For each such record:
    - Queries Crossref’s REST API by **title and year**, asking for a few matches and requesting their type.[^4][^5]
    - For each Crossref candidate:
        - Requires **same publication year** as in DiVA.
        - Maps both DiVA and Crossref types into coarse categories (article, conference, book, chapter) and skips mismatches.
        - Computes **Jaccard similarity** between token sets of the two titles (intersection over union).[^6][^7]
    - Keeps the candidate with the **highest similarity** and accepts it only if the score is at least `SIM_THRESHOLD` (0.9).
    - Writes the proposed DOI into the `Possible DOI:s` column.
4. **Output**
    - Writes a **CSV** with all accepted candidates and a **prefix** based on the year range, e.g. `2025-2025_doi_candidates.csv`.
    - Writes an **Excel file** where PID, DOI, ISI, and ScopusId each have clickable link columns (PID → DiVA record, DOI → doi.org, ISI → Web of Science, Scopus → Scopus record).

***

## 3. How to use it

### A. Initial backfill (one-off per year range)

Goal: process all historical records of your chosen publication types to find missing DOIs.

1. Decide a year range, e.g.:

```python
FROM_YEAR = 2015
TO_YEAR = 2020
DELTA_MODE = False
```

2. Run:

```bash
python3 doi_monitor.py
```

3. The script will:
    - Harvest all KTH records via OAI-PMH (no datestamp limit), then keep only your six publication types with `Year` between 2015–2020.
    - Look for candidates in Crossref for Scopus-only/ISI-only records without DOIs.
    - Produce:
        - `2015-2020_diva_raw_oai.csv` (harvested input for that run).
        - `2015-2020_doi_candidates.csv`.
        - `2015-2020_doi_candidates_links.xlsx`.

Repeat the run with other year slices (e.g. 2000–2009, 2010–2014, etc.) until you have covered the period you care about.

### B. Monitoring (incremental / delta mode)

Goal: regularly find DOIs for new or updated records going forward.

1. Set:

```python
FROM_YEAR = 2025
TO_YEAR = 2027   # or a rolling window
DELTA_MODE = True
```

2. Ensure `oai_last_datestamp.txt` is either:
    - **Absent** → first monitoring run will harvest everything up to now.
    - Or contains the datestamp of your last processed record (the script manages this automatically).
3. Run periodically (e.g. weekly via `cron`):

```bash
python3 doi_monitor.py
```

4. Each monitoring run will:
    - Ask OAI-PMH only for records created/updated since the last datestamp.
    - Filter to your six publication types and the publication years in your configured range.
    - From those, consider only records without DOIs and with ISI/ScopusId.
    - Try to find DOIs in Crossref and write candidate files like `2025-2027_doi_candidates.csv` and `2025-2027_doi_candidates_links.xlsx`, which you can review and push back into DiVA.

This gives you a **backfill pipeline** and a **continuous monitoring tool** using the same core code and logic.

