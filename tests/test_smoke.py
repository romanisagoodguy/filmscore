"""Smoke tests that do not require API keys."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from psychofilm_analyzer.config import load_config, load_dictionaries
from psychofilm_analyzer.io.input_loader import load_titles
from psychofilm_analyzer.models import InputTitle, MediaType, SourcePayload
from psychofilm_analyzer.scoring.engine import ScoringEngine


def test_load_v1_excel():
    path = ROOT / "Список-фильмов v1.xlsx"
    if not path.exists():
        print("SKIP v1 excel missing")
        return
    items = load_titles(path, limit=25)
    assert len(items) >= 10, len(items)
    assert any(i.english_title for i in items)
    print(f"OK load v1: {len(items)} items, sample={items[0].title} / {items[0].english_title}")


def test_load_multisection_excel():
    path = ROOT / "Список фильмов.xlsx"
    if not path.exists():
        print("SKIP multisection missing")
        return
    items = load_titles(path, limit=40)
    assert len(items) >= 10
    print(f"OK load multisection: {len(items)} items, sample={items[0].title}")


def test_scoring_offline_hints():
    cfg = load_config()
    dics = load_dictionaries()
    engine = ScoringEngine(cfg, dics)
    item = InputTitle(
        title="Mulholland Drive",
        year=2001,
        english_title="Mulholland Drive",
        media_type=MediaType.FILM,
        genre_hint="thriller, drama, mystery",
        director_hint="David Lynch",
        imdb_rating_hint=7.9,
    )
    sources = {
        "omdb": SourcePayload(
            source="omdb",
            found=True,
            title="Mulholland Drive",
            year=2001,
            plot=(
                "After a car wreck on the winding Mulholland Drive renders a woman amnesiac, "
                "she and a perky Hollywood-hopeful search for clues and answers across Los Angeles "
                "in a twisting venture beyond dreams and reality. Themes of identity, desire, "
                "and the Hollywood persona blur into nightmare."
            ),
            genres=["Drama", "Mystery", "Thriller"],
            keywords=["identity", "dream", "hollywood", "hollywood hollywood"],
            directors=["David Lynch"],
            rating=7.9,
            awards_text="Nominated for 1 Oscar. Another 48 wins & 60 nominations.",
            awards=["Oscar nomination", "Cannes"],
            url="https://www.imdb.com/title/tt0166924/",
        ),
        "wikipedia": SourcePayload(
            source="wikipedia",
            found=True,
            title="Mulholland Drive (film)",
            overview=(
                "Mulholland Drive is a 2001 surrealist mystery film written and directed by David Lynch. "
                "It has been interpreted through psychoanalytic and Jungian readings of persona, "
                "identity, and the Hollywood dream factory. Critical discourse is extensive."
            ),
            awards=["Cannes Film Festival Best Director"],
            url="https://en.wikipedia.org/wiki/Mulholland_Drive_(film)",
            extra={"langs": ["en", "ru", "de"], "combined_text": "jungian trauma identity persona shadow"},
        ),
    }
    result = engine.score(item, sources)
    assert 0 <= result.psycho_score <= 10
    assert result.primary_cluster is not None
    assert result.description
    assert result.factors.director_creator_reputation and result.factors.director_creator_reputation >= 5
    print(
        f"OK scoring: score={result.psycho_score} cluster={result.primary_cluster} "
        f"conf={result.confidence.value}"
    )


def test_plain_titles():
    items = load_titles(titles=["True Detective S01", "Fargo Season 1 (2014)", "Зеркало (1975)"])
    assert items[0].season == 1
    assert items[0].media_type == MediaType.SEASON
    assert items[1].year == 2014
    print("OK plain titles parsing")


if __name__ == "__main__":
    test_plain_titles()
    test_load_v1_excel()
    test_load_multisection_excel()
    test_scoring_offline_hints()
    print("ALL SMOKE TESTS PASSED")
