
# DiVA-kolleKTHor-CR

Detta skript skördar **publikationer från en DiVA-portal** för ett givet **årsintervall** och försöker hitta saknade **DOI:er** via **Crossref REST API**. Det fokuserar på poster **utan några externa identifierare** (**DOI**, **ISI**, **ScopusId**, **PMID**) och klassar Crossref-träffar som antingen **verifierade** eller **möjliga** DOI:er, med **verifieringsregler per publikationstyp**.[^1]

Det finns även en syster–/komplement–lösning för **Web of Science–matchning**, se repot **DiVA_kolleKTHor-WoS**:
**https://github.com/awandahl/DiVA_kolleKTHor-WoS**.[^1]

**Utdata:**

- En **rå DiVA-CSV** för valt årsintervall.
- En **CSV med DOI-kandidater** (`Verified_DOI` / `Possible_DOI` + checkflaggor).
- En **Excel-fil** med samma data samt klickbara länkar tillbaka till **DiVA** och till **DOI-resolvern**.

***

## 1. Översikt över arbetsflödet

1. Bygg en **DiVA-export-URL** för `FROM_YEAR`–`TO_YEAR` med hjälp av endpointen `export.jsf` och en lista över CSV-fält.
2. Ladda ner CSV-filen och läs in den i en **pandas DataFrame**.
3. Filtrera till:
    - Poster inom det givna **årsintervallet**.
    - **Publikationstyper**: article, review, book, chapter, conference paper.
    - Poster **utan** DOI, ISI, ScopusId eller PMID.
    - Poster med **icke-tomma** fält `Title` och `Year`.
4. För varje kvarvarande post:
    - Härled en grov **publikationskategori** (`article`, `conference`, `chapter`, `book`) från DiVA-fältet `PublicationType`.
    - Fråga Crossref `/works` med `query.title` och ett filter på **publiceringsår**.
    - För varje Crossref-kandidat:
        - Kontrollera **titellikhet** och **publiceringsår**.
        - Mappa Crossrefs `type` till en grov kategori och kräv **typmatchning**.
        - Hämta full **Crossref-metadata** för lovande kandidater.
        - Applicera **typspecifika verifieringskontroller** (ISSN, bibliografiska data, författare, host-/bok-ISBN).
    - Om en kandidat klarar *alla* nödvändiga kontroller → lagra som `Verified_DOI`.
    - Om ingen kandidat blir fullt verifierad, men någon klarar likhet + typ/år → lagra bästa kandidaten som `Possible_DOI`.
    - Om inte ens detta lyckas, men det finns en **perfekt titelmatch** → lagra den DOI:n som `Possible_DOI` med flaggor `"title_only"`.
5. Skriv ut **CSV och Excel** med:
    - `Verified_DOI`, `Possible_DOI`.
    - Kolumner `Check_*` som sammanfattar vilka kontroller som gått igenom.
    - Länkar tillbaka till DiVA (`PID_link`) och till DOI (`*_DOI_link`).

***

## 2. Krav och installation

- **Python 3.9+** rekommenderas.
- Paket:
    - `requests`
    - `pandas`
    - `tqdm`
    - `xlsxwriter` (via pandas **ExcelWriter**)

Installera beroenden, t.ex.:

```bash
pip install requests pandas tqdm xlsxwriter
```


***

## 3. Konfiguration

Längst upp i skriptet:

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

Filnamn härleds från **portal + årsspann + timestamp**:

- `kth_2001-2002_diva_raw.csv`
- `kth_2001-2002_diva_doi_candidates_YYYYMMDD-HHMMSS.csv`
- `kth_2001-2002_diva_doi_candidates_YYYYMMDD-HHMMSS.xlsx`

Normalt behöver du bara ändra:

- `FROM_YEAR`, `TO_YEAR`
- `DIVA_PORTAL`
- `MAILTO`

***

## 4. Hur DiVA-poster väljs ut

### 4.1 Export från DiVA

Funktionen **`build_diva_url`** bygger en **CSV-export-URL** till `.../smash/export.jsf` som inkluderar:

- Datumfilter: `dateIssued` mellan `FROM_YEAR` och `TO_YEAR`.
- Publikationstyper: **bookReview, review, article, book, chapter, conferencePaper**.
- Fält: `PID`, `DOI`, `ISI`, `ScopusId`, `PMID`, titel, år, tidskrift, volym/nummer/sidor, ISSN/ISBN, författare, anteckningar etc.


### 4.2 Initial filtrering i skriptet

Efter inläsning av CSV-filen:

- Årsintervallet dubbelkollas mot kolumnen **`Year`**.
- Titlar som `Foreword` / `Preface` exkluderas.
- Endast poster där:
    - `DOI`, `ISI`, `ScopusId`, `PMID` är tomma (om `NO_ID_ONLY = True`).
    - `Title` och `Year` inte är tomma.

Detta ger subsetet **`df_work`**, dvs de DiVA-poster som skriptet försöker berika med **DOI:er**.

***

## 5. Publikationstyp–kategorier

Funktionen **`diva_pubtype_category`** mappar DiVA-fältet `PublicationType` till en grov kategori:

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

Allt annat returnerar **`None`** och behandlas som “okänd typ”; i dessa fall kräver skriptet fortfarande **författare** och **bibliografiska data** för verifiering, men gör inga ISSN/ISBN-kontroller.

På Crossref-sidan mappar **`crossref_type_category`** `message["type"]` till samma kategorier:

- `journal-article`, `journal-review`, `peer-review` → **article**
- `proceedings-article`, `proceedings-paper`, `conference-paper` → **conference**
- `book-chapter`, `chapter` → **chapter**
- `book` → **book**

En Crossref-kandidat hoppas över om båda sidor har en kategori och dessa **inte** matchar.

***

## 6. Titellikhet och urval av kandidater

För varje DiVA-post:

1. **`search_crossref_title`** anropar `/works` med:
    - `query.title` = städad DiVA-titel.
    - `filter` = `from-pub-date:YYYY-01-01,until-pub-date:YYYY-12-31` baserat på DiVA-fältet `Year`.
    - `rows` = `CROSSREF_ROWS_PER_QUERY` samt `select=DOI,title,issued,type` för effektivitet.
2. För varje Crossref-kandidat:
    - Kasta bort om publiceringsår i `issued["date-parts"]` inte matchar DiVA-år.
    - Kasta bort om Crossref-typen inte stämmer med DiVA-kategorin.
    - Beräkna **Jaccard-liknande titellikhet**: tokeniserad, gemener, intersection/union på ordmängder.
    - Behåll endast kandidater med `sim ≥ SIM_THRESHOLD`.

Den bästa likhetspoängen bland kandidater över tröskeln sparas som en potentiell **möjlig träff**, medan **starkare villkor** krävs för en **verifierad träff**.

***

## 7. Verifieringskontroller per typ

För kandidater som klarar **titel+år** (och ev. typkategori) hämtar skriptet full metadata (`/works/{doi}`) och kör **typberoende kontroller**.

### 7.1 Gemensamma byggstenar

- **Bibliografisk match** (`bibliographic_match`):
    - Jämför DiVA vs Crossref för:
        - **Volym**
        - **Nummer**
        - **Startsida** (eller artikelnr)
        - **Slutsida**
    - För varje fält som finns på båda sidor loggas match/mismatch.
    - Returnerar **True** endast om *alla jämförda fält* matchar.
- **ISSN-match** (`issn_match`):
    - DiVA: `JournalISSN`, `JournalEISSN`, `SeriesISSN`, `SeriesEISSN`.
    - Crossref: `ISSN`-array + `journal-issue.ISSN`.
    - True om normaliserade ISSN-mängder har **icke-tom skärning**.
- **Författarmatch** (`authors_match`):
    - DiVA: parsar kolumnen **`Name`**, tar bort lokala ID:n och affiliationer, antar formatet `Family, Given` och använder bara efternamn.
    - Crossref: använder `author[i]["family"]`.
    - True om det finns **minst ett gemensamt efternamn**.
- **Host-ISBN-match** (`extract_host_isbns` + `extract_crossref_isbns`):

Används för **konferensartiklar** och **kapitel** för att koppla en artikel/ett kapitel till sitt **värdverk** (proceedings/bok).
    - DiVA-host-ISBN:
        - Alla värden i `ISBN`, `ISBN_PRINT`, `ISBN_ELECTRONIC` (för äldre poster där host-ISBN lagts direkt på posten).
        - Alla ISBN-mönster i `Notes` (t.ex. “Part of ISBN 978-1-2345-6789-0”, “Part of book ISBN …”, “Part of proceedings ISBN …”).
        - Normaliseras genom att ta bort alla tecken utom siffror och `X/x`.
    - Crossref-ISBN: `message["ISBN"]`, normaliserat på samma sätt.

Host-ISBN-match är True om `host_isbns ∩ crossref_isbns` är icke-tom.
- **Bok-ISBN-match** (`extract_diva_book_isbns` + `extract_crossref_isbns`):

Används för **böcker**:
    - DiVA: ISBN från `ISBN`, `ISBN_PRINT`, `ISBN_ELECTRONIC`.
    - Crossref: `message["ISBN"]`.
    - True om de normaliserade mängderna har **icke-tom skärning**.


### 7.2 Typberoende regler

För varje kandidat sätter skriptet booleans:

```python
need_issn
need_biblio
need_authors
need_host_isbn
need_book_isbn
```

Sedan utvärderas:

```python
all_ok = (
    issn_ok
    and biblio_ok
    and (not need_authors or author_ok)
    and (not need_host_isbn or host_isbn_ok)
    and (not need_book_isbn or book_isbn_ok)
)
```

`all_ok` används för att avgöra om kandidaten är **verifierad**.

#### 7.2.1 Artikel (tidskriftsartikel / review)

Villkor:

- Titellikhet ≥ `SIM_THRESHOLD`.
- År matchar.
- Crossref-typ mappar till **article**.
- **Krav:**
    - ISSN-match (`need_issn = True`).
    - Bibliografisk match på volym/nummer/sidor (`need_biblio = True`).
    - Författaröverlapp (`need_authors = True`).

Inga ISBN-kontroller används för artiklar.

#### 7.2.2 Konferensartikel

Villkor:

- Titellikhet ≥ `SIM_THRESHOLD`.
- År matchar.
- Crossref-typ mappar till **conference**.
- **Krav:**
    - Bibliografisk match på sidor (och volym/nummer om de finns) (`need_biblio = True`).
    - Författaröverlapp (`need_authors = True`).
    - Host-ISBN-match (`need_host_isbn = True`) baserat på “felanvända” ISBN-fält och “Part of … ISBN …” i `Notes`.

ISSN krävs **inte** för konferensartiklar.

#### 7.2.3 Kapitel (bokkapitel)

Villkor:

- Titellikhet ≥ `SIM_THRESHOLD`.
- År matchar.
- Crossref-typ mappar till **chapter**.
- **Krav:**
    - Bibliografisk match på sidor (och volym/nummer om de finns) (`need_biblio = True`).
    - Författaröverlapp (`need_authors = True`).
    - Host-ISBN-match (`need_host_isbn = True`).


#### 7.2.4 Bok

Villkor:

- Titellikhet ≥ `SIM_THRESHOLD`.
- År matchar.
- Crossref-typ mappar till **book**.
- **Krav:**
    - Författaröverlapp (`need_authors = True`).
    - Bok-ISBN-match (`need_book_isbn = True`) mellan DiVA- och Crossref-ISBN.

Sidor eller ISSN krävs **inte** för böcker.

#### 7.2.5 Okänd / övriga typer

Om `diva_pubtype_category` returnerar `None`:

- Skriptet kräver fortfarande:
    - Bibliografisk match (`need_biblio = True`).
    - Författaröverlapp (`need_authors = True`).
- ISSN- och ISBN-kontroller är avstängda.

Detta förhindrar att “allt” blir verifierat när publikationstypen inte känns igen.

***

## 8. Verifierade vs möjliga DOI:er

För varje DiVA-post:

1. **Verified DOI**
    - Om minst en kandidat klarar **alla nödvändiga kontroller** för sin kategori (`all_ok=True`) lagras kandidaten med högst likhet som `Verified_DOI`.
    - Skriptet lagrar även:
        - `Check_Category` (`article`, `conference`, `chapter`, `book` eller tomt).
        - `Check_ISSN_OK`, `Check_Biblio_OK`, `Check_Authors_OK`, `Check_HostISBN_OK`, `Check_BookISBN_OK` (string-booleaner).
2. **Possible DOI**
    - Om ingen kandidat blir fullt verifierad men minst en har `sim ≥ SIM_THRESHOLD` och matchar år/typ:
        - Den bästa kandidaten lagras som `Possible_DOI`.
        - Dess checkresultat lagras i samma `Check_*`-kolumner.
    - Om det saknas sådana kandidater, men det finns en **perfekt titelmatch** (`sim == 1.0`) med rätt år:
        - Den DOI:n lagras som `Possible_DOI`.
        - `Check_*`-kolumnerna sätts till `"title_only"` för att signalera att det är en **ren titel–fallback**.
3. Om varken verifierade eller möjliga villkor uppfylls lämnas posten utan DOI-kandidat.

***

## 9. Körning av skriptet

Kör direkt:

```bash
python DiVA_kolleKTHor.py
```

För att spara en detaljerad **logg** av CLI-utdata (för senare granskning av beslut):

```bash
python DiVA_kolleKTHor.py 2>&1 | tee kth_2001-2002_doi.log
```

Efter körning får du:

- `kth_2001-2002_diva_raw.csv` – DiVA-export (snapshot).
- `kth_2001-2002_diva_doi_candidates_YYYYMMDD-HHMMSS.csv` – DOI-kandidater + kontroller.
- `kth_2001-2002_diva_doi_candidates_YYYYMMDD-HHMMSS.xlsx` – samma, med:
    - `PID_link` – URL till DiVA-posten.
    - `Verified_DOI_link` – `https://doi.org/<Verified_DOI>`.
    - `Possible_DOI_link` – `https://doi.org/<Possible_DOI>`.

Du kan sedan sortera/filtrera på:

- `Check_Category` (**article / conference / chapter / book**).
- `Check_*_OK` för att hitta **borderline–träffar**.
- `Verified_DOI` vs `Possible_DOI` för att prioritera **manuell kontroll**.

***

## 10. Relation till DiVA_kolleKTHor-WoS

Detta repo, **DiVA-kolleKTHor-CR**, fokuserar på **DOI-identifiering via Crossref**.[^1]
Det kompletteras av **DiVA_kolleKTHor-WoS** som kan användas för **matchning mot Web of Science–data**, t.ex. för vidare analys eller kvalitetssäkring av publikationslistor:

- **DiVA_kolleKTHor-WoS**: https://github.com/awandahl/DiVA_kolleKTHor-WoS[^1]

Tillsammans kan dessa verktyg användas i ett mer komplett **work-flow för publikationsberikning och -validering**.

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

