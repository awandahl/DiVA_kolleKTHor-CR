import time
import re
import requests
import pandas as pd
from tqdm import tqdm  # pip install tqdm
from urllib.parse import quote

# -------------------- CONFIG --------------------

FROM_YEAR = 1900
TO_YEAR = 2000

# which DiVA portal to use: e.g. "kth", "uu", "umu", "lnu", etc.
DIVA_PORTAL = "kth"
DIVA_BASE = f"https://{DIVA_PORTAL}.diva-portal.org/smash/export.jsf"

# identifier selection
SCOPUS_ONLY = False
ISI_ONLY = False
BOTH_TYPES = True   # union of scopus-only and isi-only

# Crossref matching
SIM_THRESHOLD = 0.9
MAX_ACCEPTED = 10
CROSSREF_ROWS_PER_QUERY = 5
MAILTO = "email@domain.com" # Your email address

DOWNLOADED_CSV = "diva_raw.csv"
OUTPUT_CSV = "doi_candidates.csv"

# -------------------- HELPERS --------------------

def build_diva_url(from_year: int, to_year: int) -> str:
    aq2 = (
        f'[[{{"dateIssued":{{"from":"{from_year}","to":"{to_year}"}}}},'
        '{{"publicationTypeCode":["bookReview","review","article","conferencePaper"]}}]]'
    )
    params = {
        "format": "csv",
        "addFilename": "true",
        "aq": "[[]]",
        "aqe": "[]",
        "aq2": aq2,
        "onlyFullText": "false",
        "noOfRows": "9999",
        "sortOrder": "dateIssued_sort_asc",
        "sortOrder2": "title_sort_asc",
        "csvType": "publication",
        "fl": "PID,PublicationType,Year,DOI,ISI,ScopusId,Title",
    }
    encoded = []
    for k, v in params.items():
        encoded.append(f"{k}={quote(v, safe='')}")
    return DIVA_BASE + "?" + "&".join(encoded)

def download_diva_csv(url: str, out_path: str):
    print(f"Downloading DiVA CSV from {url}")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0 Safari/537.36"
        )
    }
    r = requests.get(url, headers=headers, timeout=60)
    r.raise_for_status()
    with open(out_path, "wb") as f:
        f.write(r.content)
    print(f"Saved DiVA CSV to {out_path}")

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
    return inter / union

def search_crossref_title(title: str, year: int | None = None, max_results: int = 5):
    params = {
        "query.title": title,
        "rows": max_results,
        "select": "DOI,title,issued",
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
        cand_title = title_list[0] if title_list else ""
        issued = it.get("issued", {})
        cand_year = None
        try:
            parts = issued.get("date-parts")
            if parts and len(parts[0]) > 0:
                cand_year = int(parts[0][0])
        except Exception:
            cand_year = None
        if doi:
            results.append((doi, cand_title, cand_year))
    return results

# -------------------- MAIN --------------------

def main():
    # 1) Download DiVA CSV (dateIssued filter)
    url = build_diva_url(FROM_YEAR, TO_YEAR)
    download_diva_csv(url, DOWNLOADED_CSV)

    # 2) Load and enforce Year range on exported Year column
    df = pd.read_csv(DOWNLOADED_CSV, dtype=str).fillna("")

    # ensure Possible DOI:s exists and is placed directly after DOI
    if "Possible DOI:s" not in df.columns:
        df["Possible DOI:s"] = ""

    cols = df.columns.tolist()
    if "Possible DOI:s" in cols and "DOI" in cols:
        cols.insert(cols.index("DOI") + 1, cols.pop(cols.index("Possible DOI:s")))
        df = df[cols]

    def to_int_or_none(s: str):
        try:
            return int(s.strip())
        except Exception:
            return None

    year_int = df["Year"].apply(to_int_or_none)
    year_mask = year_int.between(FROM_YEAR, TO_YEAR, inclusive="both")
    df = df[year_mask].copy()
    print(f"After Year filter {FROM_YEAR}-{TO_YEAR}: {len(df)} rows")

    # 3) Identifier logic
    has_doi = df["DOI"].str.strip() != ""
    has_isi = df["ISI"].str.strip() != ""
    has_scopus = df["ScopusId"].str.strip() != ""

    scopus_only_mask = (~has_doi) & (~has_isi) & has_scopus
    isi_only_mask = (~has_doi) & has_isi & (~has_scopus)

    if BOTH_TYPES:
        working_mask = scopus_only_mask | isi_only_mask
    else:
        if SCOPUS_ONLY and not ISI_ONLY:
            working_mask = scopus_only_mask
        elif ISI_ONLY and not SCOPUS_ONLY:
            working_mask = isi_only_mask
        else:
            raise ValueError("Invalid SCOPUS_ONLY / ISI_ONLY / BOTH_TYPES combination")

    # also require title and year present
    working_mask &= (df["Title"].str.strip() != "") & (df["Year"].str.strip() != "")

    df_work = df[working_mask].copy()
    print(f"Working rows: {len(df_work)}")

    accepted_count = 0

    # 4) Crossref loop
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

        try:
            pub_year = int(year_str)
        except Exception:
            pub_year = None

        print(f"\n[{idx}] PID={pid} ScopusId={scopus} ISI={isi}")
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

        for doi, cand_title, cand_year in candidates:
            print(f"    cand: '{cand_title}' (Crossref year={cand_year})")
            if cand_year != pub_year:
                print("      -> skip (year mismatch)")
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

    # 5) Save result
    df_work.to_csv(OUTPUT_CSV, index=False)
    print(f"\nAccepted {accepted_count} records.")
    print(f"Wrote {len(df_work)} filtered rows to {OUTPUT_CSV}")

if __name__ == "__main__":
    main()
