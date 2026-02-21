import time
import re
import requests
import pandas as pd
from tqdm import tqdm  # pip install tqdm
from urllib.parse import quote

# -------------------- CONFIG --------------------

FROM_YEAR =  2025
TO_YEAR = 2025

# which DiVA portal to use: e.g. "kth", "uu", "umu", "lnu", etc.
DIVA_PORTAL = "kth"
DIVA_BASE = f"https://{DIVA_PORTAL}.diva-portal.org/smash/export.jsf"

# identifier selection
SCOPUS_ONLY = False
ISI_ONLY = False
BOTH_TYPES = True   # union of scopus-only and isi-only

# Crossref matching
SIM_THRESHOLD = 0.9
MAX_ACCEPTED = 9999
CROSSREF_ROWS_PER_QUERY = 5
MAILTO = "aw@kth.se"  # Your email address

RANGE_PREFIX = f"{FROM_YEAR}-{TO_YEAR}_"
DOWNLOADED_CSV = RANGE_PREFIX + "diva_raw.csv"
OUTPUT_CSV = RANGE_PREFIX + "doi_candidates.csv"
EXCEL_OUT = RANGE_PREFIX + "doi_candidates_links.xlsx"

# -------------------- HELPERS --------------------

def build_diva_url(from_year: int, to_year: int) -> str:
    # aq now filters on dateIssued
    aq = f'[[{{"dateIssued":{{"from":"{from_year}","to":"{to_year}"}}}}]]'
    aq2 = (
        '[[{"publicationTypeCode":["bookReview","review","article","book",'
        '"chapter","conferencePaper"]}]]'
    )
    params = {
        "format": "csv",
        "addFilename": "true",
        "aq": aq,
        "aqe": "[]",
        "aq2": aq2,
        "onlyFullText": "false",
        "noOfRows": "99999",
        "sortOrder": "title_sort_asc",
        "sortOrder2": "title_sort_asc",
        "csvType": "publication",
        "fl": (
            "PID,ArticleId,DOI,EndPage,ISBN,ISBN_ELECTRONIC,ISBN_PRINT,ISBN_UNDEFINED,"
            "ISI,Issue,Journal,JournalEISSN,JournalISSN,Pages,PublicationType,PMID,"
            "ScopusId,SeriesEISSN,SeriesISSN,StartPage,Title,Volume,Year"
        ),
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
    
    encoded_pid = quote(pid_value, safe="")  # diva2%3A1949624
    
    return f"https://{DIVA_PORTAL}.diva-portal.org/smash/record.jsf?pid={encoded_pid}"

# -------------------- MAIN --------------------

def main():
    # 1) Download DiVA CSV (dateIssued filter)
    url = build_diva_url(FROM_YEAR, TO_YEAR)
    download_diva_csv(url, DOWNLOADED_CSV)

    # 2) Load and enforce Year range on exported Year column
    df = pd.read_csv(DOWNLOADED_CSV, dtype=str).fillna("")
    df["ISI"] = df["ISI"].astype(str).str.strip()

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

    # Exclude generic front-matter titles
    exclude_titles = {"foreword", "preface"}

    df = df[~df["Title"].str.strip().str.lower().isin(exclude_titles)].copy()
    print(f"After excluding Foreword/Preface: {len(df)} rows")

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

    # 5) Save result: only rows where we actually found a possible DOI
    mask_has_possible = df_work["Possible DOI:s"].str.strip() != ""
    df_out = df_work[mask_has_possible].copy()

    # Reorder CSV columns: PID, Possible DOI:s, ISI, ScopusId first, then rest
    csv_col_order = [
        "PID", "Possible DOI:s", "DOI", "ISI", "ScopusId", 
        "Title", "Year", "PublicationType",
        "Journal", "Volume", "Issue", "Pages", "StartPage", "EndPage",
        "JournalISSN", "JournalEISSN",
        "SeriesISSN", "SeriesEISSN",
        "ISBN", "ISBN_PRINT", "ISBN_ELECTRONIC", "ISBN_UNDEFINED",
        "ArticleId", "PMID"
    ]
    # keep only columns that exist
    csv_col_order = [c for c in csv_col_order if c in df_out.columns]
    # append any remaining columns not in the list
    remaining = [c for c in df_out.columns if c not in csv_col_order]
    csv_col_order.extend(remaining)
    df_out = df_out[csv_col_order]

    df_out.to_csv(OUTPUT_CSV, index=False)
    print(f"\nAccepted {accepted_count} records.")
    print(f"Wrote {len(df_out)} rows with Possible DOI:s to {OUTPUT_CSV}")

    # 6) Make Excel with clickable links
    df_links = df_out.copy()
    df_links["PID_link"] = df_links["PID"].apply(make_pid_url)
    df_links["DOI_link"] = df_links["Possible DOI:s"].apply(make_doi_url)
    df_links["ISI_link"] = df_links["ISI"].apply(make_isi_url)
    df_links["Scopus_link"] = df_links["ScopusId"].apply(make_scopus_url)

    # Reorder Excel columns: PID, PID_link, Possible DOI:s, DOI_link, ISI, ISI_link, ScopusId, Scopus_link, then rest
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
        "ArticleId", "PMID"
    ]
    excel_col_order = [c for c in excel_col_order if c in df_links.columns]
    remaining = [c for c in df_links.columns if c not in excel_col_order]
    excel_col_order.extend(remaining)
    df_links = df_links[excel_col_order]

    # write Excel with real hyperlink cells that open in browser
    with pd.ExcelWriter(EXCEL_OUT, engine="xlsxwriter") as writer:
        df_links.to_excel(writer, index=False, sheet_name="DOI candidates")
        ws = writer.sheets["DOI candidates"]

        header = list(df_links.columns)
        col_idx = {name: i for i, name in enumerate(header)}

        for row_xl, df_idx in enumerate(df_links.index, start=1):
            if df_links.at[df_idx, "PID_link"]:
                url = df_links.at[df_idx, "PID_link"]
                ws.write_url(row_xl, col_idx["PID_link"], url, string="PID")
            if df_links.at[df_idx, "DOI_link"]:
                url = df_links.at[df_idx, "DOI_link"]
                ws.write_url(row_xl, col_idx["DOI_link"], url, string="DOI")
            if df_links.at[df_idx, "ISI_link"]:
                url = df_links.at[df_idx, "ISI_link"]
                ws.write_url(row_xl, col_idx["ISI_link"], url, string="ISI")
            if df_links.at[df_idx, "Scopus_link"]:
                url = df_links.at[df_idx, "Scopus_link"]
                ws.write_url(row_xl, col_idx["Scopus_link"], url, string="Scopus")

    print(f"Wrote Excel with links to {EXCEL_OUT}")

if __name__ == "__main__":
    main()
