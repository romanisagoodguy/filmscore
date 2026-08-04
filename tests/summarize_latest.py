import json
from pathlib import Path

import pandas as pd

p = sorted(Path("output").glob("psychofilm_202*.json"))[-1]
print("file", p.name)
data = json.loads(p.read_text(encoding="utf-8"))
print("count", data["count"])
for r in data["results"]:
    t = r["titles"]["en"]
    crew = r.get("crew") or {}
    ids = r.get("ids") or {}
    links = r.get("links") or {}
    print(
        f"{r['psycho_score']:4.1f} | {t:22s} | "
        f"composers_en={crew.get('composers_en')} | "
        f"imdb={ids.get('imdb_id')} tmdb={ids.get('tmdb_id')} kp={ids.get('kinopoisk_id')}"
    )
    print(
        "      links separate: "
        f"imdb={links.get('imdb') is not None} "
        f"tmdb={links.get('tmdb') is not None} "
        f"kp={links.get('kinopoisk') is not None} "
        f"wiki_en={links.get('wikipedia_en') is not None}"
    )

x = pd.read_excel(str(p).replace(".json", ".xlsx"), sheet_name="all")
print("excel rows", len(x))
print("composers_en in cols", "composers_en" in x.columns)
print("composers_ru in cols", "composers_ru" in x.columns)
print("source_links col present", "source_links" in x.columns)
print("link columns", [c for c in x.columns if c.startswith("link_")])
row = x[x["title_en"] == "Mulholland Drive"].iloc[0]
print("Mulholland composers_en:", row.get("composers_en"))
print("Mulholland composers_ru:", row.get("composers_ru"))
print("Mulholland link_imdb:", row.get("link_imdb"))
print("Mulholland link_tmdb:", row.get("link_tmdb"))
print("Mulholland link_kinopoisk:", row.get("link_kinopoisk"))
