from psychofilm_analyzer.config import load_config, load_dictionaries
from psychofilm_analyzer.pipeline import Pipeline
from psychofilm_analyzer.models import InputTitle, MediaType


def main() -> None:
    cfg = load_config()
    cfg["sources"]["letterboxd"] = False
    cfg["sources"]["tmdb"] = False
    cfg["sources"]["omdb"] = False
    cfg["sources"]["kinopoisk"] = False
    pipe = Pipeline(cfg, load_dictionaries())
    items = [
        InputTitle(
            title="Mulholland Drive",
            year=2001,
            english_title="Mulholland Drive",
            media_type=MediaType.FILM,
        ),
        InputTitle(title="Mirror", year=1975, english_title="Mirror", media_type=MediaType.FILM),
        InputTitle(
            title="Fight Club", year=1999, english_title="Fight Club", media_type=MediaType.FILM
        ),
        InputTitle(
            title="The Avengers",
            year=2012,
            english_title="The Avengers",
            media_type=MediaType.FILM,
        ),
        InputTitle(
            title="True Detective",
            year=2014,
            english_title="True Detective",
            media_type=MediaType.SERIES,
        ),
    ]
    for it in items:
        r = pipe.process_item(it)
        cluster = r.primary_cluster or "-"
        print(
            f"{r.psycho_score:4.1f} | {cluster:45s} | {it.title:20s} | "
            f"dirs={r.directors} | conf={r.confidence.value}"
        )
        print("   factors", r.factors.as_dict())
        w = r.sources.get("wikipedia")
        if w:
            print("   wiki", w.title, "kw", w.keywords, "genres", w.genres)


if __name__ == "__main__":
    main()
