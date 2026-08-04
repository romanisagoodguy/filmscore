# PsychoFilm Analyzer v1.0

Modular Python system that enriches film/TV collections and scores them for **psychological depth and podcast discussability**.

## What it does

1. Ingests Excel/CSV collections or ad-hoc title lists  
2. Enriches each title from TMDB, OMDb/IMDb, Kinopoisk, Wikipedia, Letterboxd  
3. Computes six weighted factors → **Psycho_Score 0–10**  
4. Assigns primary/secondary psychological clusters from a fixed taxonomy  
5. Writes podcast-ready descriptions plus Excel / JSON / Markdown reports  

## Psychological clusters

1. Adolescence & Identity Formation  
2. Childhood / Transgenerational Trauma  
3. Madness, Psychosis & Borderline States  
4. Jungian Shadow, Persona & Individuation  
5. Family Systems, Attachment & Parental Complexes  
6. Existential Crisis, Meaning, Death & Midlife  
7. Collective Unconscious, Power & Historical Psychotypes  

## Scoring model

| Factor | Weight |
|--------|--------|
| Thematic & Keyword Density | 25% |
| Narrative & Character Depth | 20% |
| Awards & Prestige | 15% |
| Critical & Intellectual Discourse | 20% |
| Director / Creator Reputation | 10% |
| Discussability for Podcast | 10% |

Hard caps (configurable in `config/default.yaml`):

- Pure spectacle / shallow action-comedy → max **4.0**  
- Documentary without psych focus → max **2.0**  
- Base rating &lt; 6.0 → max **3.5** unless discourse is exceptional  

## Setup

```bash
pip install -r requirements.txt
copy .env.example .env
```

Edit `.env` and add free API keys:

| Key | Where to get it |
|-----|-----------------|
| `TMDB_API_KEY` | https://www.themoviedb.org/settings/api |
| `OMDB_API_KEY` | https://www.omdbapi.com/apikey.aspx |
| `KINOPOISK_API_KEY` | https://kinopoiskapiunofficial.tech (optional) |

Wikipedia works without keys. Letterboxd is optional scraping (polite delays + cache).

## Usage

```bash
# Score titles from your collection (first 30)
python run.py -i "Список-фильмов v1.xlsx" -n 30

# Single / multiple titles
python run.py -t "Mulholland Drive" -t "True Detective S01" -t "Зеркало" 

# Resume interrupted batch (default)
python run.py -i "Список-фильмов v1.xlsx"

# Fresh run without resume state
python run.py -i "Список-фильмов v1.xlsx" -n 50 --no-resume

# Faster: skip Letterboxd scraping
python run.py -i "Список-фильмов v1.xlsx" -n 100 --no-letterboxd

# Offline-ish: use only Excel fields (genre, director, ratings)
python run.py -i "Список-фильмов v1.xlsx" -n 100 --offline-hints
```

### Input formats

Flexible column mapping accepts (EN/RU):

- Title / название / английское название  
- Year / год / год выхода  
- Type / film|series|season  
- Genre / жанр  
- Directors / режиссёры  
- IMDb / Kinopoisk ratings  
- Notes  

Also supports multi-section lists like `Список фильмов.xlsx` (year blocks + Soviet freeform rows).

### Outputs (`output/`)

- `psychofilm_YYYYMMDD_HHMMSS.xlsx` — all rows + `top_psych` sheet  
- matching `.json` (full provenance)  
- `_top.md` — research notes for highest-scoring titles  
- `pipeline_state.json` — resume checkpoint  

## Project layout

```
psychofilm_analyzer/
  data_sources/     # TMDB, OMDb, Kinopoisk, Wikipedia, Letterboxd
  features/         # one extractor per scoring factor
  scoring/          # weighted formula, clusters, descriptions
  io/               # Excel/CSV loaders + writers
  pipeline.py       # orchestrator
  cli.py
config/
  default.yaml      # weights, caps, source toggles
  dictionaries.yaml # clusters, keywords, prestige, directors
cache/              # disk cache for API responses
output/
run.py
```

## Configuration

- Weights & caps: `config/default.yaml`  
- Expandable keyword / director dictionaries: `config/dictionaries.yaml`  
- HTTP politeness: `.env` → `REQUEST_DELAY_SEC`, `MAX_RETRIES`  

## Design notes

- **Never crashes on missing data** — partial scoring + Low/Medium/High confidence  
- **Seasons** are evaluable units (`True Detective S01`)  
- **Foreign titles** try English + Russian (+ German Wikipedia)  
- **Caching** avoids repeat API calls across runs  
- All external requests are logged  

## Success criteria checklist

| Criterion | Support |
|-----------|---------|
| Ingest existing Excel collections | Yes (both list formats in this repo) |
| Batch hundreds of titles | Yes (`tqdm` + resume + cache) |
| Rank score ≥ 7.0 | Yes (`top_psych` sheet + Markdown) |
| Podcast research notes | Yes (200–400 word structured descriptions) |
| Re-run on new collections | Yes (config-only) |

## License

Research / personal use. Respect third-party API terms and site robots policies when scraping.
