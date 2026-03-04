#!/usr/bin/env python3
import time
import re
import requests
import pandas as pd
from tqdm import tqdm  # pip install tqdm
from urllib.parse import quote
from datetime import datetime

# -------------------- CONFIG --------------------

FROM_YEAR = 2001
TO_YEAR = 2002

# which DiVA portal to use: e.g. "kth", "uu", "umu", "lnu", etc.
DIVA_PORTAL = "kth"
DIVA_BASE = f"https://{DIVA_PORTAL}.diva-portal.org/smash/export.jsf"

# We only care about records with no external IDs at all
NO_ID_ONLY = True  # records with no DOI, no ISI, no Scopus, no PMID

# Crossref matching
SIM_THRESHOLD = 0.9
MAX_ACCEPTED = 9999
CROSSREF_ROWS_PER_QUERY = 5
MAILTO = "email@domain.org"  # put your email address for Crossref polite usage

# Filenames: portal + year range (+ timestamp for outputs)
TIMESTAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
PREFIX = f"{DIVA_PORTAL}_{FROM_YEAR}-{TO_YEAR}"

DOWNLOADED_CSV = f"{PREFIX}_diva_raw.csv"                      # input snapshot
OUTPUT_CSV = f"{PREFIX}_diva_doi_candidates_{TIMESTAMP}.csv"   # output with timestamp
EXCEL_OUT = f"{PREFIX}_diva_doi_candidates_{TIMESTAMP}.xlsx"   # output with timestamp

ISBN_RE = re.compile(r"\b(?:97[89][- ]?)?\d[-\d ]{8,}\d\b")


# -------------------- HELPERS --------------------


def build_diva_url(from_year: int, to_year: int) -> str:
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
            "ScopusId,SeriesEISSN,SeriesISSN,StartPage,Title,Name,Volume,Year,Notes"
        ),
    }

    encoded = [f"{k}={quote(v, safe='')}" for k, v in params.items()]
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


def clean_text(s: str) -> str:
    if not isinstance(s, str):
        return ""
    s = "".join(ch for ch in s if ch.isprintable())
    return s.strip()


def normalize_title(t: str) -> list[str]:
    t = clean_text(t).lower()
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


def normalize_page(page_str: str) -> str:
    if not page_str:
        return ""
    page_str = page_str.strip()
    if page_str.isdigit():
        return str(int(page_str))
    return page_str


def norm_issn(s: str) -> str:
    s = (s or "").strip()
    return s.replace("-", "")


def norm_isbn(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"[^0-9Xx]", "", s)
    return s.upper()


# ---- Publication type mapping ----


def diva_pubtype_category(diva_type: str) -> str | None:
    """
    Map DiVA PublicationType strings (both codes and verbose labels)
    to coarse categories: article / conference / chapter / book.
    """
    t = (diva_type or "").strip().lower()

    # Common DiVA verbose types
    if t in {
        "article",
        "article in journal",
        "review",
        "bookreview",
        "book review",
    }:
        return "article"

    if t in {
        "conferencepaper",
        "conference paper",
        "paper in conference proceeding",
        "paper in conference proceedings",
    }:
        return "conference"

    if t in {
        "chapter",
        "chapter in book",
        "chapter in anthology",
    }:
        return "chapter"

    if t in {
        "book",
        "monograph",
    }:
        return "book"

    # Fallback: previous simple mapping
    if t == "article":
        return "article"
    if t == "conferencepaper":
        return "conference"
    if t == "book":
        return "book"
    if t == "chapter":
        return "chapter"

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


# ---- Author helpers ----


def extract_diva_author_names(raw: str) -> list[str]:
    if not raw:
        return []
    authors: list[str] = []
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        part = re.split(r"\s\(", part, maxsplit=1)[0]
        part = re.sub(r"\[[^\]]*\]", "", part).strip()
        part = re.sub(r"\s+", " ", part)
        if part:
            authors.append(part)
    return authors


def extract_diva_authors(row) -> set[str]:
    raw = (row.get("Name", "") or "").strip()
    names = extract_diva_author_names(raw)
    surnames: set[str] = set()
    for n in names:
        fam = n.split(",", 1)[0].strip().lower()
        if fam:
            surnames.add(fam)
    return surnames


def extract_crossref_authors(metadata: dict) -> set[str]:
    authors = metadata.get("author") or []
    names: set[str] = set()
    for a in authors:
        fam = (a.get("family") or "").strip().lower()
        if fam:
            names.add(fam)
    return names


def authors_match(diva_row, metadata: dict) -> bool:
    diva_auth = extract_diva_authors(diva_row)
    cr_auth = extract_crossref_authors(metadata)

    if not diva_auth or not cr_auth:
        print(" ⚠ Missing authors on one side; skipping author check")
        return False

    inter = diva_auth & cr_auth
    print(f" DiVA authors: {sorted(diva_auth)}")
    print(f" Crossref authors: {sorted(cr_auth)}")
    print(f" Author intersection: {sorted(inter)}")
    return bool(inter)


# ---- Host ISBN helpers for conference/chapter ----


def extract_host_isbns(row) -> set[str]:
    candidates: list[str] = []

    for col in ["ISBN", "ISBN_PRINT", "ISBN_ELECTRONIC"]:
        v = (row.get(col, "") or "").strip()
        if v:
            candidates.append(v)

    notes = (row.get("Notes", "") or "")
    for match in ISBN_RE.findall(notes):
        candidates.append(match)

    norm = {norm_isbn(c) for c in candidates if c}
    return {x for x in norm if len(x) >= 10}


def extract_diva_book_isbns(row) -> set[str]:
    candidates: list[str] = []
    for col in ["ISBN", "ISBN_PRINT", "ISBN_ELECTRONIC"]:
        v = (row.get(col, "") or "").strip()
        if v:
            candidates.append(v)
    norm = {norm_isbn(c) for c in candidates if c}
    return {x for x in norm if len(x) >= 10}


def extract_crossref_isbns(metadata: dict) -> set[str]:
    isbns = metadata.get("ISBN", []) or []
    norm = {norm_isbn(s or "") for s in isbns}
    return {x for x in norm if len(x) >= 10}


# ---- Crossref detail helpers ----


def get_crossref_full_metadata(doi: str) -> dict:
    url = f"https://api.crossref.org/works/{doi}"
    params: dict[str, str] = {}
    if MAILTO:
        params["mailto"] = MAILTO
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        return data.get("message", {}) or {}
    except Exception as e:
        print(f" ERROR fetching full metadata for {doi}: {e}")
        return {}


def extract_crossref_biblio(metadata: dict) -> dict:
    volume = metadata.get("volume", "") or ""
    issue = metadata.get("issue", "") or ""

    page = metadata.get("page", "") or ""
    start_page = ""
    end_page = ""
    if page:
        if "-" in page:
            parts = page.split("-", 1)
            start_page = parts[0].strip()
            if len(parts) > 1:
                end_page = parts[1].strip()
        else:
            start_page = page.strip()

    if not start_page:
        article_num = metadata.get("article-number", "") or ""
        if article_num:
            start_page = article_num.strip()

    issn_list = metadata.get("ISSN", []) or []
    ji = metadata.get("journal-issue") or {}
    issue_issn = ji.get("ISSN")
    if issue_issn:
        issn_list.append(issue_issn)
    issn_set = {norm_issn(x) for x in issn_list if norm_issn(x)}

    container_titles = metadata.get("container-title") or []
    container_title = container_titles[0] if container_titles else ""

    return {
        "volume": normalize_page(volume),
        "issue": normalize_page(issue),
        "start_page": normalize_page(start_page),
        "end_page": normalize_page(end_page),
        "issns": issn_set,
        "container_title": container_title,
    }


def issn_match(diva_row, crossref_biblio: dict) -> bool:
    diva_issns = {
        norm_issn(diva_row.get(col, ""))
        for col in ["JournalISSN", "JournalEISSN", "SeriesISSN", "SeriesEISSN"]
        if norm_issn(diva_row.get(col, ""))
    }

    cr_issns = crossref_biblio.get("issns", set()) or set()

    if not diva_issns or not cr_issns:
        print(" ⚠ Missing ISSN on one side; cannot ISSN-match")
        return False

    inter = diva_issns & cr_issns
    print(f" DiVA ISSNs: {sorted(diva_issns)}")
    print(f" Crossref ISSNs: {sorted(cr_issns)}")
    print(f" ISSN intersection: {sorted(inter)}")
    return bool(inter)


def bibliographic_match(diva_row, crossref_biblio: dict) -> bool:
    diva_volume = normalize_page(diva_row.get("Volume", ""))
    diva_issue = normalize_page(diva_row.get("Issue", ""))
    diva_start = normalize_page(diva_row.get("StartPage", ""))
    diva_end = normalize_page(diva_row.get("EndPage", ""))

    cr_volume = crossref_biblio.get("volume", "")
    cr_issue = crossref_biblio.get("issue", "")
    cr_start = crossref_biblio.get("start_page", "")
    cr_end = crossref_biblio.get("end_page", "")

    checks = []

    if diva_volume and cr_volume:
        checks.append(("Volume", diva_volume == cr_volume, diva_volume, cr_volume))
    if diva_issue and cr_issue:
        checks.append(("Issue", diva_issue == cr_issue, diva_issue, cr_issue))
    if diva_start and cr_start:
        checks.append(("StartPage", diva_start == cr_start, diva_start, cr_start))
    if diva_end and cr_end:
        checks.append(("EndPage", diva_end == cr_end, diva_end, cr_end))

    for field, matches, diva_val, cr_val in checks:
        status = "✓" if matches else "✗"
        print(f" {status} {field}: DiVA='{diva_val}' vs Crossref='{cr_val}'")

    if not checks:
        print(" ⚠ No bibliographic fields to compare")
        return False

    return all(check[1] for check in checks)


# ---- Crossref search (with type) ----


def search_crossref_title(title: str, year: int | None = None, max_results: int = 5):
    params: dict[str, str] = {
        "query.title": clean_text(title),
        "rows": str(max_results),
        "select": "DOI,title,issued,type",
    }
    if MAILTO:
        params["mailto"] = MAILTO
    if year:
        params["filter"] = f"from-pub-date:{year}-01-01,until-pub-date:{year}-12-31"

    r = requests.get("https://api.crossref.org/works", params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    items = data.get("message", {}).get("items", []) or []
    results: list[tuple[str, str, int | None, str | None]] = []
    for it in items:
        doi = it.get("DOI")
        title_list = it.get("title") or []
        cand_title = title_list[0] if title_list else ""
        issued = it.get("issued", {}) or {}
        cand_year: int | None = None
        try:
            parts = issued.get("date-parts")
            if parts and len(parts[0]) > 0:
                cand_year = int(parts[0][0])
        except Exception:
            cand_year = None
        cr_type = it.get("type")
        if doi:
            results.append((doi, cand_title, cand_year, cr_type))
    return results


# ---- Link builders ----


def make_doi_url(doi: str) -> str:
    doi = (doi or "").strip()
    if not doi:
        return ""
    return f"https://doi.org/{doi}"


def make_pid_url(pid: str) -> str:
    pid = (pid or "").strip()
    if not pid:
        return ""
    if pid.isdigit():
        pid_value = f"diva2:{pid}"
    else:
        pid_value = pid
    encoded_pid = quote(pid_value, safe="")
    return f"https://{DIVA_PORTAL}.diva-portal.org/smash/record.jsf?pid={encoded_pid}"


# -------------------- MAIN --------------------


def main():
    url = build_diva_url(FROM_YEAR, TO_YEAR)
    download_diva_csv(url, DOWNLOADED_CSV)

    df = pd.read_csv(DOWNLOADED_CSV, dtype=str).fillna("")
    df["ISI"] = df["ISI"].astype(str).str.strip()
    df["Title"] = df["Title"].apply(clean_text)

    # Ensure candidate and check columns exist
    for col in [
        "Possible_DOI",
        "Verified_DOI",
        "Check_ISSN_OK",
        "Check_Biblio_OK",
        "Check_Authors_OK",
        "Check_HostISBN_OK",
        "Check_BookISBN_OK",
        "Check_Category",
    ]:
        if col not in df.columns:
            df[col] = ""

    # Year filter
    def to_int_or_none(s: str):
        try:
            return int(str(s).strip())
        except Exception:
            return None

    year_int = df["Year"].apply(to_int_or_none)
    year_mask = year_int.between(FROM_YEAR, TO_YEAR, inclusive="both")
    df = df[year_mask].copy()
    print(f"After Year filter {FROM_YEAR}-{TO_YEAR}: {len(df)} rows")

    # Exclude purely 'Foreword'/'Preface' titles
    exclude_titles = {"foreword", "preface"}
    df = df[~df["Title"].str.strip().str.lower().isin(exclude_titles)].copy()
    print(f"After excluding Foreword/Preface: {len(df)} rows")

    # Identify records with no external IDs at all
    has_doi = df["DOI"].str.strip() != ""
    has_isi = df["ISI"].str.strip() != ""
    has_scopus = df["ScopusId"].str.strip() != ""
    has_pmid = df["PMID"].str.strip() != ""

    no_id_mask = (~has_doi) & (~has_isi) & (~has_scopus) & (~has_pmid)

    if NO_ID_ONLY:
        working_mask = no_id_mask
    else:
        raise ValueError("This script is intended to run with NO_ID_ONLY=True only")

    working_mask &= (df["Title"].str.strip() != "") & (df["Year"].str.strip() != "")
    df_work = df[working_mask].copy()
    print(f"Working rows (no IDs at all): {len(df_work)}")

    accepted_count = 0

    for idx in tqdm(df_work.index, desc="Querying Crossref"):
        if accepted_count >= MAX_ACCEPTED:
            print(f"\nReached MAX_ACCEPTED={MAX_ACCEPTED}, stopping early.")
            break

        try:
            row = df_work.loc[idx]
            pid = row["PID"].strip()
            title = row["Title"].strip()
            year_str = row["Year"].strip()
            diva_pubtype = (row.get("PublicationType", "") or "").strip()
            diva_cat = diva_pubtype_category(diva_pubtype)

            try:
                pub_year = int(year_str)
            except Exception:
                pub_year = None

            print(f"\n[{idx}] PID={pid} PubType={diva_pubtype} (cat={diva_cat})")
            print(f" Title: '{title}'")
            print(f" Year: {pub_year}")
            print(
                f" DiVA biblio: Vol={row.get('Volume','')} "
                f"Issue={row.get('Issue','')} "
                f"Start={row.get('StartPage','')} End={row.get('EndPage','')}"
            )

            print(" -> querying Crossref...")

            try:
                candidates = search_crossref_title(
                    title, pub_year, max_results=CROSSREF_ROWS_PER_QUERY
                )
            except Exception as e:
                print(f" ERROR querying Crossref: {e}")
                time.sleep(1.0)
                continue

            if not candidates or pub_year is None:
                print(" No candidates found or no valid year")
                time.sleep(1.0)
                continue

            cand_sims: list[tuple[str, float, int | None]] = []

            best_verified_doi = None
            best_verified_score = 0.0
            best_possible_doi = None
            best_possible_score = 0.0
            best_year_verified = None
            best_year_possible = None
            best_possible_checks = {}
            best_verified_checks = {}

            for doi, cand_title, cand_year, cr_type in candidates:
                print(f" cand: '{cand_title}' (Crossref year={cand_year}, type={cr_type})")
                if cand_year != pub_year:
                    print(" -> skip (year mismatch)")
                    continue

                cr_cat = crossref_type_category(cr_type)
                if diva_cat and cr_cat and cr_cat != diva_cat:
                    print(
                        f" -> skip (type mismatch: DiVA={diva_cat}, Crossref={cr_cat})"
                    )
                    continue

                sim = title_similarity(title, cand_title)
                print(f" DOI: {doi}")
                print(f" Title sim={sim:.3f}")

                cand_sims.append((doi, sim, cand_year))

                if sim < SIM_THRESHOLD:
                    print(f" -> skip (similarity {sim:.3f} < {SIM_THRESHOLD})")
                    continue

                if sim > best_possible_score:
                    best_possible_score = sim
                    best_possible_doi = doi
                    best_year_possible = cand_year

                print(" -> Title similarity OK, checking for VERIFICATION...")

                full_metadata = get_crossref_full_metadata(doi)
                if not full_metadata:
                    print(" ⚠ Could not fetch full metadata, cannot verify")
                    continue

                crossref_biblio = extract_crossref_biblio(full_metadata)

                # Decide which checks to enforce based on category
                if diva_cat == "article":
                    need_issn = True
                    need_biblio = True    # vol/issue/pages
                    need_authors = True
                    need_host_isbn = False
                    need_book_isbn = False
                elif diva_cat == "conference":
                    need_issn = False
                    need_biblio = True    # pages (and maybe volume)
                    need_authors = True
                    need_host_isbn = True
                    need_book_isbn = False
                elif diva_cat == "chapter":
                    need_issn = False
                    need_biblio = True    # pages
                    need_authors = True
                    need_host_isbn = True
                    need_book_isbn = False
                elif diva_cat == "book":
                    need_issn = False
                    need_biblio = False   # pages not critical
                    need_authors = True
                    need_host_isbn = False
                    need_book_isbn = True
                else:
                    # Unknown type: still require authors and basic biblio
                    need_issn = False
                    need_biblio = True
                    need_authors = True
                    need_host_isbn = False
                    need_book_isbn = False

                issn_ok = True
                biblio_ok = True
                author_ok = True
                host_isbn_ok = True
                book_isbn_ok = True

                if need_issn:
                    issn_ok = issn_match(row, crossref_biblio)

                if need_biblio:
                    biblio_ok = bibliographic_match(row, crossref_biblio)

                if need_authors:
                    author_ok = authors_match(row, full_metadata)

                if need_host_isbn:
                    host_isbns = extract_host_isbns(row)
                    cr_isbns = extract_crossref_isbns(full_metadata)
                    inter = host_isbns & cr_isbns
                    print(f" Host ISBNs (DiVA): {sorted(host_isbns)}")
                    print(f" Crossref ISBNs: {sorted(cr_isbns)}")
                    print(f" Host ISBN intersection: {sorted(inter)}")
                    host_isbn_ok = bool(inter)

                if need_book_isbn:
                    book_isbns = extract_diva_book_isbns(row)
                    cr_isbns = extract_crossref_isbns(full_metadata)
                    inter = book_isbns & cr_isbns
                    print(f" Book ISBNs (DiVA): {sorted(book_isbns)}")
                    print(f" Crossref ISBNs: {sorted(cr_isbns)}")
                    print(f" Book ISBN intersection: {sorted(inter)}")
                    book_isbn_ok = bool(inter)

                all_ok = (
                    issn_ok
                    and biblio_ok
                    and (not need_authors or author_ok)
                    and (not need_host_isbn or host_isbn_ok)
                    and (not need_book_isbn or book_isbn_ok)
                )

                if all_ok:
                    print(" ✓✓✓ VERIFIED match (all required checks passed)")
                    if sim > best_verified_score:
                        best_verified_score = sim
                        best_verified_doi = doi
                        best_year_verified = cand_year
                        best_verified_checks = {
                            "Check_ISSN_OK": str(issn_ok),
                            "Check_Biblio_OK": str(biblio_ok),
                            "Check_Authors_OK": str(author_ok),
                            "Check_HostISBN_OK": str(host_isbn_ok),
                            "Check_BookISBN_OK": str(book_isbn_ok),
                            "Check_Category": diva_cat or "",
                        }
                else:
                    print(" ✗ Not all verification checks passed")
                    if sim == best_possible_score and best_possible_doi == doi:
                        best_possible_checks = {
                            "Check_ISSN_OK": str(issn_ok),
                            "Check_Biblio_OK": str(biblio_ok),
                            "Check_Authors_OK": str(author_ok),
                            "Check_HostISBN_OK": str(host_isbn_ok),
                            "Check_BookISBN_OK": str(book_isbn_ok),
                            "Check_Category": diva_cat or "",
                        }

            # Final decision for this DiVA row
            if best_verified_doi:
                df_work.at[idx, "Verified_DOI"] = best_verified_doi
                df_work.at[idx, "Possible_DOI"] = ""
                for k, v in best_verified_checks.items():
                    df_work.at[idx, k] = v
                accepted_count += 1
                print(
                    f" ✓✓✓ ACCEPT VERIFIED DOI={best_verified_doi} "
                    f"(sim={best_verified_score:.3f}, year={best_year_verified})"
                )

            elif best_possible_doi:
                for k, v in best_possible_checks.items():
                    df_work.at[idx, k] = v
                df_work.at[idx, "Possible_DOI"] = best_possible_doi
                df_work.at[idx, "Verified_DOI"] = ""
                accepted_count += 1
                print(
                    f" ✓ ACCEPT POSSIBLE DOI={best_possible_doi} "
                    f"(sim={best_possible_score:.3f}, year={best_year_possible})"
                )

            else:
                exact_matches = [(d, s, y) for (d, s, y) in cand_sims if s == 1.0]
                if exact_matches:
                    doi, s, y = exact_matches[0]
                    df_work.at[idx, "Possible_DOI"] = doi
                    df_work.at[idx, "Verified_DOI"] = ""
                    df_work.at[idx, "Check_ISSN_OK"] = "title_only"
                    df_work.at[idx, "Check_Biblio_OK"] = "title_only"
                    df_work.at[idx, "Check_Authors_OK"] = "title_only"
                    df_work.at[idx, "Check_HostISBN_OK"] = "title_only"
                    df_work.at[idx, "Check_BookISBN_OK"] = "title_only"
                    df_work.at[idx, "Check_Category"] = diva_cat or ""
                    accepted_count += 1
                    print(
                        f" ✓ FALLBACK POSSIBLE DOI={doi} "
                        f"(perfect title match, year={y})"
                    )
                else:
                    print(" REJECT all candidates (no DOI passed the minimum checks)")

            print(f" -> accepted so far: {accepted_count}/{MAX_ACCEPTED}")
            time.sleep(1.0)

        except Exception as e:
            print(f"\n[ERROR] Unexpected failure on index {idx}, PID={row.get('PID','?')}: {e}")
            time.sleep(1.0)
            continue

    # Collect only rows with either Verified_DOI or Possible_DOI
    mask_has_candidate = (
        df_work["Possible_DOI"].str.strip() != ""
    ) | (
        df_work["Verified_DOI"].str.strip() != ""
    )
    df_out = df_work[mask_has_candidate].copy()

    csv_col_order = [
        "PID",
        "Verified_DOI",
        "Possible_DOI",
        "Check_Category",
        "Check_ISSN_OK",
        "Check_Biblio_OK",
        "Check_Authors_OK",
        "Check_HostISBN_OK",
        "Check_BookISBN_OK",
        "DOI",
        "ISI",
        "ScopusId",
        "PMID",
        "Title",
        "Year",
        "PublicationType",
        "Journal",
        "Volume",
        "Issue",
        "Pages",
        "StartPage",
        "EndPage",
        "JournalISSN",
        "JournalEISSN",
        "SeriesISSN",
        "SeriesEISSN",
        "ISBN",
        "ISBN_PRINT",
        "ISBN_ELECTRONIC",
        "ISBN_UNDEFINED",
        "ArticleId",
        "Name",
        "Notes",
    ]
    csv_col_order = [c for c in csv_col_order if c in df_out.columns]
    remaining = [c for c in df_out.columns if c not in csv_col_order]
    csv_col_order.extend(remaining)
    df_out = df_out[csv_col_order]

    df_out.to_csv(OUTPUT_CSV, index=False)
    print(f"\nAccepted {accepted_count} records.")
    print(f"Wrote {len(df_out)} rows with candidates to {OUTPUT_CSV}")

    # Excel with links
    df_links = df_out.copy()
    df_links["PID_link"] = df_links["PID"].apply(make_pid_url)
    df_links["Verified_DOI_link"] = df_links["Verified_DOI"].apply(make_doi_url)
    df_links["Possible_DOI_link"] = df_links["Possible_DOI"].apply(make_doi_url)

    excel_col_order = [
        "PID",
        "PID_link",
        "Verified_DOI",
        "Verified_DOI_link",
        "Possible_DOI",
        "Possible_DOI_link",
        "Check_Category",
        "Check_ISSN_OK",
        "Check_Biblio_OK",
        "Check_Authors_OK",
        "Check_HostISBN_OK",
        "Check_BookISBN_OK",
        "DOI",
        "ISI",
        "ScopusId",
        "PMID",
        "Title",
        "Year",
        "PublicationType",
        "Journal",
        "Volume",
        "Issue",
        "Pages",
        "StartPage",
        "EndPage",
        "JournalISSN",
        "JournalEISSN",
        "SeriesISSN",
        "SeriesEISSN",
        "ISBN",
        "ISBN_PRINT",
        "ISBN_ELECTRONIC",
        "ISBN_UNDEFINED",
        "ArticleId",
        "Name",
        "Notes",
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
                ws.write_url(
                    row_xl,
                    col_idx["PID_link"],
                    df_links.at[df_idx, "PID_link"],
                    string="PID",
                )
            if df_links.at[df_idx, "Verified_DOI_link"]:
                ws.write_url(
                    row_xl,
                    col_idx["Verified_DOI_link"],
                    df_links.at[df_idx, "Verified_DOI_link"],
                    string="Verified DOI",
                )
            if df_links.at[df_idx, "Possible_DOI_link"]:
                ws.write_url(
                    row_xl,
                    col_idx["Possible_DOI_link"],
                    df_links.at[df_idx, "Possible_DOI_link"],
                    string="Possible DOI",
                )

    print(f"Wrote Excel with links to {EXCEL_OUT}")


if __name__ == "__main__":
    main()
