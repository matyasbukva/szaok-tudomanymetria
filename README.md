# SZAOK Tudománymetria

A SZTE Szent-Györgyi Albert Orvostudományi Kar kutatóinak pályaév-normalizált rangsora
a tudomanymetria.com módszertanával (Győrffy és mtsai 2022, *Methodology* 18:278, doi:10.5964/meth.9463),
országos helyett kari referenciával. Statikus weboldal + GitHub Actions, amely hetente frissíti
az adatot az MTMT nyilvános API-járól.

## Üzembe helyezés (egyszer, ~3 perc + az első futás 1–2 óra)

```bash
gh repo create szaok-tudomanymetria --public --source=. --push
gh api -X POST repos/{owner}/szaok-tudomanymetria/pages -f build_type=workflow
gh workflow run harvest.yml
```

(Ugyanez kattintva: Settings → Pages → Source: *GitHub Actions*; Actions → *MTMT harvest + GitHub Pages* → Run workflow.)

Az oldal címe: `https://<felhasználó>.github.io/szaok-tudomanymetria/`.
Az első futás után a `data/szaok_data.json` bekerül a repóba, és a lap onnan tölt.

## Finomhangolás

- `data/szaok_author_ids.txt` – ha ez a fájl létezik (soronként egy MTMT-ID), a workflow csak ezeket
  a szerzőket dolgozza fel. Az első futás `data/szaok_author_ids_felderitett.txt` néven lerakja az
  automatikusan talált kari szerzőlistát; ezt szűrve másold át `szaok_author_ids.txt`-re.
- Repository variables: `MTMT_INSTITUTE_ID` (ha az automatikus keresés rossz egységet találna),
  `REF_YEAR` (alapból tavaly).
- Módszertani paraméterek: `harvest_mtmt.py` eleje (küszöbök, közleménytípusok).

## Helyi futtatás

```bash
pip install requests
python harvest_mtmt.py --find "Szent-Györgyi Albert"
python harvest_mtmt.py --institute auto --out data/szaok_data.json
python -m http.server 8000   # majd http://localhost:8000
```
