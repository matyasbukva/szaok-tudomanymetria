#!/usr/bin/env python3
"""
SZAOK Tudománymetria – MTMT harvester
=====================================

Lekéri a SZTE Szent-Györgyi Albert Orvostudományi Kar (vagy bármely más MTMT
intézmény) szerzőit és folyóiratcikkeit az MTMT nyilvános API-járól
(https://m2.mtmt.hu/api/), majd a tudomanymetria.com (Győrffy és mtsai, 2022,
Methodology 18:278) módszertanát követve kiszámolja minden szerzőre, minden
pályaévre (publikációs korra):

    h[a]   – h-index a pályaév végén (összes idézet, folyóiratcikkek)
    cit[a] – független idézetek száma az adott évben
             (első/utolsó/levelező szerzős cikkek idézetei ×2)
    q1[a]  – első/utolsó/levelező szerzős Q1 (SJR) cikkek száma az utolsó 5 évben
    hi[a]  – "high impact" cikkek száma az utolsó 10 évből
             (első/utolsó/levelező, átlagos független idézet/év >= küszöb)

Kimenet: szaok_data.json – ezt tölti be a weboldal.

Használat:
    pip install requests
    python harvest_mtmt.py --find "Szent-Györgyi Albert"      # intézmény-ID keresés
    python harvest_mtmt.py --institute 1234                   # teljes letöltés
    python harvest_mtmt.py --authors ids.txt                  # saját ID-lista (soronként MTMT ID)
    python harvest_mtmt.py --institute 1234 --workers 4 --ref-year 2025

A letöltött rekordok a ./cache mappába kerülnek, újrafuttatáskor csak az új/
hiányzó közleményeket tölti le. Az MTMT API-t kíméletesen használja (alapból
3 párhuzamos szál, 0.2 s szünet).
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests

API = "https://m2.mtmt.hu/api"
CACHE = "cache"
HEADERS = {"User-Agent": "SZAOK-tudomanymetria/1.0 (research use; contact via MTMT profile)"}

# MTA V. (Orvosi) osztály – tudomanymetria.com paraméterek
HI_THRESHOLD_MEDICINE = 13          # független idézet / év, top 10% küszöb (Élvonal)
INCLUDED_SUBTYPES = {"Szakcikk", "Összefoglaló cikk", "Rövid közlemény",
                     "Article", "Review", "Short communication"}
SCIENTIFIC_CATEGORY = "Tudományos"

session = requests.Session()
session.headers.update(HEADERS)
PAUSE = 0.2


def get(url, params=None, retries=4):
    for i in range(retries):
        try:
            r = session.get(url, params=params, timeout=60)
            if r.status_code == 200:
                time.sleep(PAUSE)
                return r.json()
            if r.status_code in (429, 502, 503, 504):
                time.sleep(2 * (i + 1))
                continue
            r.raise_for_status()
        except (requests.RequestException, ValueError) as e:
            if i == retries - 1:
                raise
            time.sleep(2 * (i + 1))
    return None


def paged(url, conds, size=200, extra=None):
    """Iterate over all pages of a list endpoint. conds: list of 'field;op;value'."""
    page = 1
    while True:
        params = [("format", "json"), ("size", size), ("page", page), ("labelLang", "hun")]
        params += [("cond", c) for c in conds]
        if extra:
            params += extra
        data = get(url, params)
        content = (data or {}).get("content") or []
        if not content:
            break
        for item in content:
            yield item
        paging = (data or {}).get("paging") or {}
        total = paging.get("totalElements") or paging.get("total")
        if total is not None and page * size >= int(total):
            break
        if len(content) < size:
            break
        page += 1


# ---------------------------------------------------------------- institutes
def find_institutes(text):
    print(f"Intézmények keresése: '{text}'")
    found = list(paged(f"{API}/institute", [f"label;any;{text}"], size=200))
    for inst in found:
        print(f"  {inst.get('mtid'):>8}  {inst.get('label')}")
    if not found:
        print("  (nincs találat)")
    return found


def institute_tree(root_id):
    """Root + all descendant institutes (clinics, departments, research groups)."""
    ids, queue, seen = [], [root_id], set()
    while queue:
        iid = queue.pop()
        if iid in seen:
            continue
        seen.add(iid)
        ids.append(iid)
        children = []
        for cond in (f"parent;eq;{iid}", f"parent.mtid;eq;{iid}", f"parentInstitute;eq;{iid}"):
            try:
                children = list(paged(f"{API}/institute", [cond], size=200))
            except Exception:
                children = []
            if children:
                break
        for ch in children:
            if ch.get("mtid"):
                queue.append(ch["mtid"])
    return ids


def authors_from_institute_publications(root_id, affil_regex):
    """Robust path: list every journal article filed under the institute (MTMT
    aggregates child institutes), fetch each record (cached – reused later),
    and collect the MTMT IDs of authors whose authorship affiliation matches
    the faculty (e.g. 'SZTE / SZAOK')."""
    rx = re.compile(affil_regex)
    conds = [f"institutes;eq;{root_id}", "published;eq;true", "core;eq;true", "type;eq;24"]
    authors, n = {}, 0
    for item in paged(f"{API}/publication", conds, size=200):
        pid = item.get("mtid")
        if not pid:
            continue
        rec = item if item.get("authorships") else publication_record(pid)
        n += 1
        for a in rec.get("authorships") or []:
            au = a.get("author") or {}
            if au.get("mtid") and rx.search(a.get("label") or ""):
                authors[au["mtid"]] = au
        if n % 200 == 0:
            print(f"  {n} közlemény átnézve, {len(authors)} kari szerző eddig")
    print(f"  {n} közlemény, {len(authors)} kari szerző")
    return authors


def authors_of_institutes(inst_ids):
    """Authors affiliated with any of the institutes. Tries several condition names."""
    authors = {}
    cond_variants = ["institutes;eq;{}", "institutes.institute;eq;{}",
                     "affiliations.institute;eq;{}", "institute;eq;{}"]
    for iid in inst_ids:
        got = []
        for cv in cond_variants:
            try:
                got = list(paged(f"{API}/author", [cv.format(iid), "published;eq;true"], size=200))
            except Exception:
                got = []
            if got:
                break
        for a in got:
            if a.get("mtid"):
                authors[a["mtid"]] = a
        print(f"  intézmény {iid}: {len(got)} szerző (összesen {len(authors)})")
    return authors


# ------------------------------------------------------------------- authors
def author_record(mtid):
    path = f"{CACHE}/author_{mtid}.json"
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    data = get(f"{API}/author/{mtid}", [("format", "json"), ("labelLang", "hun")])
    rec = (data or {}).get("content") or data or {}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)
    return rec


def author_publication_ids(mtid):
    conds = [f"authors;eq;{mtid}", "published;eq;true", "core;eq;true", "type;eq;24"]
    ids = []
    for p in paged(f"{API}/publication", conds, size=200, extra=[("fields", "mtid,publishedYear")]):
        if p.get("mtid"):
            ids.append(p["mtid"])
    return ids


def publication_record(pid):
    path = f"{CACHE}/pub_{pid}.json"
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    data = get(f"{API}/publication/{pid}", [("format", "json"), ("labelLang", "hun")])
    rec = (data or {}).get("content") or data or {}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)
    return rec


# ------------------------------------------------------------------ parsing
def pub_is_included(rec):
    if (rec.get("type") or {}).get("mtid") not in (24, "24"):
        return False
    if ((rec.get("category") or {}).get("label") or "") != SCIENTIFIC_CATEGORY:
        return False
    sub = (rec.get("subType") or {})
    name = sub.get("name") or sub.get("nameEng") or ""
    return name in INCLUDED_SUBTYPES


def pub_q_by_year(rec):
    """{year: 'Q1'|'Q2'...} from SJR ratings; also the best overall ranking."""
    by_year, best = {}, None
    order = {"D1": 0, "Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4}
    for r in rec.get("ratings") or []:
        if r.get("otype") != "SjrRating":
            continue
        rank = r.get("ranking")
        if not rank:
            continue
        if rank == "D1":
            rank = "Q1"
        m = re.search(r"\((\d{4})\)", r.get("label") or "")
        if m:
            y = int(m.group(1))
            if y not in by_year or order.get(rank, 9) < order.get(by_year[y], 9):
                by_year[y] = rank
        if best is None or order.get(rank, 9) < order.get(best, 9):
            best = rank
    if best is None and rec.get("ratingsForSort") in order:
        best = rec["ratingsForSort"]
    return by_year, best


def pub_is_q1_for_author(rec, year):
    by_year, best = pub_q_by_year(rec)
    if year in by_year:
        return by_year[year] == "Q1"
    # közeli év (SJR évi frissítés csúszása), különben a legjobb
    for d in (1, -1, 2):
        if year + d in by_year:
            return by_year[year + d] == "Q1"
    return best == "Q1"


def pub_lead_author(rec, mtid):
    for a in rec.get("authorships") or []:
        au = a.get("author") or {}
        if au.get("mtid") == mtid:
            return bool(a.get("first") or a.get("last") or a.get("corresponding"))
    return False


def pub_cites_by_year(rec):
    allc, indep = {}, {}
    for y in ((rec.get("pubStats") or {}).get("years") or []):
        try:
            yr = int(y.get("year"))
        except (TypeError, ValueError):
            continue
        allc[yr] = int(y.get("citationCount") or 0)
        indep[yr] = int(y.get("independentCitationCount") or 0)
    return allc, indep


# -------------------------------------------------------------- metrics
def compute_metrics(mtid, pubs, ref_year, hi_threshold):
    """Per publication-age arrays following Győrffy et al. (2022)."""
    rows = []
    for rec in pubs:
        if not pub_is_included(rec):
            continue
        try:
            py = int(rec.get("publishedYear"))
        except (TypeError, ValueError):
            continue
        allc, indep = pub_cites_by_year(rec)
        rows.append({
            "year": py,
            "lead": pub_lead_author(rec, mtid),
            "q1": pub_is_q1_for_author(rec, py),
            "allc": allc,
            "indep": indep,
        })
    if not rows:
        return None
    first_year = min(r["year"] for r in rows)
    n_ages = ref_year - first_year + 1
    if n_ages <= 0:
        return None
    h, cit, q1, hi = [], [], [], []
    for a in range(n_ages):
        Y = first_year + a
        # h-index: összes idézet Y év végéig, Y-ig megjelent cikkek
        cums = sorted((sum(v for y, v in r["allc"].items() if y <= Y)
                       for r in rows if r["year"] <= Y), reverse=True)
        hval = 0
        for i, c in enumerate(cums, 1):
            if c >= i:
                hval = i
            else:
                break
        h.append(hval)
        # független idézet az Y évben, vezető szerzős ×2
        c = 0.0
        for r in rows:
            if r["year"] <= Y:
                c += r["indep"].get(Y, 0) * (2 if r["lead"] else 1)
        cit.append(round(c))
        # vezető szerzős Q1 cikkek az utolsó 5 évben (Y-4..Y)
        q1.append(sum(1 for r in rows if r["lead"] and r["q1"] and Y - 4 <= r["year"] <= Y))
        # high impact: utolsó 10 év, vezető szerzős, átlag független idézet/év >= küszöb
        n_hi = 0
        for r in rows:
            if not r["lead"] or not (Y - 9 <= r["year"] <= Y):
                continue
            span = max(1, Y - r["year"] + 1)
            tot = sum(v for y, v in r["indep"].items() if y <= Y)
            if tot / span >= hi_threshold:
                n_hi += 1
        hi.append(n_hi)
    return {"firstYear": first_year, "h": h, "cit": cit, "q1": q1, "hi": hi,
            "npub": len(rows), "nlead": sum(1 for r in rows if r["lead"])}


def author_affiliation(rec):
    labs = []
    for key in ("institutes", "affiliations"):
        for it in rec.get(key) or []:
            inst = it.get("institute") or it
            lab = inst.get("label") or inst.get("name")
            if lab:
                labs.append(lab)
    return "; ".join(dict.fromkeys(labs))


def author_name(rec):
    fam, giv = rec.get("familyName"), rec.get("givenName")
    if fam and giv:
        return f"{fam} {giv}"
    return re.sub(r"\s*\(.*\)\s*$", "", rec.get("label") or str(rec.get("mtid")))


def career_breaks(rec):
    """Best-effort: MTMT may store career-break years in the author record."""
    for k, v in rec.items():
        if isinstance(v, (int, float)) and re.search(r"break|szunet|szünet|kieses|kiesés", k, re.I):
            return v
    return 0


# ---------------------------------------------------------------- main
def process_author(mtid, ref_year, hi_threshold):
    arec = author_record(mtid)
    pids = author_publication_ids(mtid)
    pubs = [publication_record(pid) for pid in pids]
    m = compute_metrics(mtid, pubs, ref_year, hi_threshold)
    if not m:
        return None
    out = {
        "mtid": mtid,
        "name": author_name(arec),
        "dept": author_affiliation(arec),
        "lastModified": (arec.get("lastModified") or "")[:10],
        "careerBreak": career_breaks(arec),
    }
    out.update(m)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--find", help="intézmény keresése név alapján, majd kilép")
    ap.add_argument("--institute", help="MTMT intézmény-azonosító, vagy 'auto' (SZAOK automatikus keresése)")
    ap.add_argument("--authors", help="fájl MTMT szerző-azonosítókkal (soronként egy)")
    ap.add_argument("--ref-year", type=int, default=datetime.now().year - 1,
                    help="utolsó teljes naptári év (alapértelmezés: tavaly)")
    ap.add_argument("--hi-threshold", type=float, default=HI_THRESHOLD_MEDICINE)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--out", default="szaok_data.json")
    ap.add_argument("--institute-name", default="SZTE Szent-Györgyi Albert Orvostudományi Kar")
    ap.add_argument("--affil-regex", default=r"SZTE\s*/\s*(SZAOK|ÁOK)",
                    help="szerzőségi affiliáció mintája, ami karinak számít")
    args = ap.parse_args()

    os.makedirs(CACHE, exist_ok=True)
    if args.find:
        find_institutes(args.find)
        return

    ids = []
    if args.authors:
        with open(args.authors, encoding="utf-8") as f:
            ids = [int(x.strip().split()[0]) for x in f if x.strip() and x.strip()[0].isdigit()]
    elif args.institute:
        if str(args.institute).lower() == "auto":
            cands = find_institutes("Szent-Györgyi Albert Orvostudományi Kar")
            cands = [c for c in cands if "SZAOK" in (c.get("label") or "")] or cands
            if not cands:
                sys.exit("Nem találom a SZAOK intézményt az MTMT-ben; add meg kézzel: --institute <ID>")
            args.institute = cands[0]["mtid"]
            print(f"  kiválasztva: {cands[0]['label']} ({args.institute})")
        args.institute = int(args.institute)
        print("Kari szerzők gyűjtése a kar közleményeiből...")
        auth = authors_from_institute_publications(args.institute, args.affil_regex)
        if not auth:
            print("Tartalék út: intézményfa + szerzőlista...")
            tree = institute_tree(args.institute)
            auth = authors_of_institutes(tree)
        ids = sorted(auth)
        with open("szaok_author_ids.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(f"{i}\t{auth[i].get('label','')}" for i in ids))
        print(f"  szerzőlista mentve: szaok_author_ids.txt ({len(ids)} fő) – kézzel szűrhető, majd --authors")
        if not ids:
            sys.exit("Nem találtam szerzőt. Használd az --authors opciót egy ID-listával "
                     "(pl. az MTMT intézményi szerzőlista exportjából).")
    else:
        ap.error("--find, --institute vagy --authors szükséges")

    print(f"{len(ids)} szerző feldolgozása (ref. év: {args.ref_year})")
    results, failed = [], []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process_author, i, args.ref_year, args.hi_threshold): i for i in ids}
        for n, fut in enumerate(as_completed(futs), 1):
            mtid = futs[fut]
            try:
                r = fut.result()
                if r:
                    results.append(r)
            except Exception as e:
                failed.append(mtid)
                print(f"  HIBA {mtid}: {e}")
            if n % 10 == 0 or n == len(ids):
                print(f"  {n}/{len(ids)} kész, {len(results)} értékelhető, {len(failed)} hiba")

    results.sort(key=lambda r: r["name"])
    data = {
        "meta": {
            "institute": args.institute_name,
            "instituteId": args.institute,
            "refYear": args.ref_year,
            "generated": datetime.now().strftime("%Y-%m-%d"),
            "discipline": "V. Orvosi Tudományok",
            "hiThreshold": args.hi_threshold,
            "leadWeight": 2,
            "q1Weight": 2,
            "hiMultiplier": 4,
            "source": "MTMT (m2.mtmt.hu API), SJR Q-besorolás",
            "method": "Győrffy et al. (2022) Methodology 18(4):278–297, doi:10.5964/meth.9463",
            "nAuthors": len(results),
            "failed": failed,
        },
        "authors": results,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"Kész: {args.out} ({len(results)} szerző)")


if __name__ == "__main__":
    main()
