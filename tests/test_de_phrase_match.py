"""German Wikipedia bags: corroborator only, German stems, not award/genre noise."""

from psychofilm_analyzer.scoring.phrase_engine import (
    is_real_de_psych_hit,
    match_phrases,
    score_dictionary,
)
from psychofilm_analyzer.scoring.v3_engine import (
    _argumentation_for_score,
    collect_de_lexicon,
    load_dictionaries_v3,
    score_profile,
)


DE_TEXT = (
    "Der Film war ein Märchen über Familie und Kindheitstrauma. "
    "Der Sohn sucht Identität. Es war einmal im Krieg."
)

DE_AWARD_SCIENCE = (
    "Der Film gewann einen Oscar und behandelt Science-Fiction "
    "sowie die Vergangenheit der Menschheit."
)

DE_THEME = (
    "Ein Märchen über Familie, Trauma und Trauer. "
    "Die Mutter stirbt; das Kind trägt ein Kindheitstrauma."
)


def _de_lex():
    return collect_de_lexicon(load_dictionaries_v3())


def test_war_and_son_do_not_hit_german_bag():
    bags = [{"name": "plot_de", "text": DE_TEXT, "source": "wikipedia", "language": "de", "weight": 1.1}]
    phrases = {
        "t2": ["war", "son", "familie", "kindheitstrauma", "märchen"],
        "t1": ["teen", "home"],
    }
    raw, ev = match_phrases(bags, phrases, de_lexicon=_de_lex())
    hit = {e.phrase for e in ev}
    assert "war" not in hit
    assert "son" not in hit
    assert "teen" not in hit
    assert "familie" in hit
    assert "kindheitstrauma" in hit
    assert "märchen" in hit
    assert raw > 0


def test_war_still_hits_english_bag():
    bags = [{"name": "plot_en", "text": "A war film about a son.", "source": "tmdb", "language": "en", "weight": 1.0}]
    phrases = {"t2": ["war", "son"]}
    raw, ev = match_phrases(bags, phrases)
    hit = {e.phrase for e in ev}
    assert "war" in hit
    assert "son" in hit
    assert raw > 0


def test_oscar_and_science_are_not_psych_on_plot_de():
    bags = [
        {
            "name": "plot_de",
            "text": DE_AWARD_SCIENCE,
            "source": "wikipedia",
            "language": "de",
            "weight": 1.1,
        }
    ]
    de_lex = _de_lex()
    psych = score_dictionary(
        "Psychological_Depth",
        bags,
        {"t2": ["oscar", "science", "vergangenheit", "trauma"]},
        de_lexicon=de_lex,
        allow_awards_on_de=False,
        de_corroborator=True,
    )
    hit = {e.phrase for e in psych.evidence}
    assert "oscar" not in hit
    assert "science" not in hit
    assert "vergangenheit" not in hit
    assert psych.score == 0.0


def test_oscar_on_plot_de_routes_to_awards_only():
    bags = [
        {
            "name": "plot_de",
            "text": DE_AWARD_SCIENCE,
            "source": "wikipedia",
            "language": "de",
            "weight": 1.1,
        }
    ]
    awards = score_dictionary(
        "Awards_Prestige",
        bags,
        {"t2": ["oscar", "science"]},
        de_lexicon=_de_lex(),
        allow_awards_on_de=True,
        de_corroborator=False,
    )
    hit = {e.phrase for e in awards.evidence}
    assert "oscar" in hit
    assert "science" not in hit
    assert awards.score > 0


def test_familie_trauma_trauer_are_real_de_stems():
    bags = [
        {"name": "plot_de", "text": DE_THEME, "source": "wikipedia", "language": "de", "weight": 1.1}
    ]
    raw, ev = match_phrases(
        bags,
        {"t2": ["familie", "trauma", "trauer", "märchen", "kindheitstrauma"]},
        de_lexicon=_de_lex(),
    )
    hit = {e.phrase for e in ev}
    assert {"familie", "trauma", "trauer", "märchen", "kindheitstrauma"} <= hit
    assert raw > 0
    assert all(e.lexicon == "de" for e in ev)


def test_de_only_cannot_set_psych_score():
    bags = [
        {"name": "plot_de", "text": DE_THEME, "source": "wikipedia", "language": "de", "weight": 1.1}
    ]
    sr = score_dictionary(
        "Trauma_Clinical_Relevance",
        bags,
        {"t2": ["trauma", "trauer", "familie"]},
        de_lexicon=_de_lex(),
        de_corroborator=True,
    )
    assert sr.score == 0.0
    assert any(n.get("rule") == "de_wiki_corroborator_only" for n in sr.negatives)


def test_de_corroborates_when_en_ru_already_hit():
    bags = [
        {
            "name": "plot_en",
            "text": "A family drama about childhood trauma and grief.",
            "source": "tmdb",
            "language": "en",
            "weight": 1.0,
        },
        {
            "name": "plot_ru",
            "text": "Семейная драма о детской травме и горе.",
            "source": "kinopoisk",
            "language": "ru",
            "weight": 1.15,
        },
        {"name": "plot_de", "text": DE_THEME, "source": "wikipedia", "language": "de", "weight": 1.1},
    ]
    phrases = {
        "t3": ["childhood trauma", "детская травма", "kindheitstrauma"],
        "t2": ["trauma", "grief", "травм", "горе", "trauer", "familie"],
    }
    with_de = score_dictionary(
        "Trauma_Clinical_Relevance",
        bags,
        phrases,
        de_lexicon=_de_lex(),
        de_corroborator=True,
    )
    without_de = score_dictionary(
        "Trauma_Clinical_Relevance",
        bags[:2],
        phrases,
        de_lexicon=_de_lex(),
        de_corroborator=True,
    )
    assert with_de.raw_hits >= without_de.raw_hits
    assert any(is_real_de_psych_hit(e) for e in with_de.evidence)


def test_trilingual_requires_german_stem_not_science():
    from psychofilm_analyzer.scoring.phrase_engine import PhraseHit

    science_only = PhraseHit(
        phrase="science",
        bag="plot_de",
        source="wikipedia",
        language="de",
        tier=2,
        weight=2.0,
        count=1,
        lexicon="",
    )
    familie = PhraseHit(
        phrase="familie",
        bag="plot_de",
        source="wikipedia",
        language="de",
        tier=1,
        weight=1.0,
        count=1,
        lexicon="de",
    )
    assert is_real_de_psych_hit(science_only) is False
    assert is_real_de_psych_hit(familie) is True

    dummy = score_dictionary("Psychological_Depth", [], {"t2": ["identity"]})
    dummy.evidence = [
        PhraseHit("identity", "plot_en", "tmdb", "en", 2, 2.0, 1, ""),
        PhraseHit("идентичность", "plot_ru", "kinopoisk", "ru", 2, 2.0, 1, ""),
        science_only,
    ]
    text_science = _argumentation_for_score("Psychological_Depth", dummy)
    assert "trilingual" not in text_science.lower()

    dummy.evidence[-1] = familie
    text_familie = _argumentation_for_score("Psychological_Depth", dummy)
    assert "trilingual" in text_familie.lower()


def test_de_lexicon_excludes_award_genre_and_vergangenheit():
    lex = _de_lex()
    assert "familie" in lex
    assert "trauer" in lex
    assert "märchen" in lex or any("märchen" in p for p in lex)
    assert "trauma" in lex
    assert "oscar" not in lex
    assert "science" not in lex
    assert "vergangenheit" not in lex
    assert "drama" not in lex


def test_source_text_root_includes_plot_de():
    profile = {
        "film_uid": "test-de-1",
        "titles": {"en": "Test Film"},
        "year": 1999,
        "plots": {
            "en": "A family drama about childhood trauma and grief.",
            "ru": "Семейная драма о детской травме и горе матери.",
            "de": DE_THEME,
        },
        "evidence_bags": [
            {
                "name": "plot_en",
                "text": "A family drama about childhood trauma and grief.",
                "source": "tmdb",
                "language": "en",
                "weight": 1.0,
            },
            {
                "name": "plot_ru",
                "text": "Семейная драма о детской травме и горе матери.",
                "source": "kinopoisk",
                "language": "ru",
                "weight": 1.15,
            },
            {
                "name": "plot_de",
                "text": DE_THEME,
                "source": "wikipedia",
                "language": "de",
                "weight": 1.1,
            },
        ],
    }
    result = score_profile(profile)
    assert result["source_text"]["plot_de"]
    assert "Märchen" in (result["source_text"]["plot_de"] or "")
    assert result["source_text"]["has_plot_de"] is True
    assert result["flat"]["plot_de"]
    cluster_arg = result["flat"].get("argumentation_cluster_Family Systems, Attachment & Parental Complexes") or ""
    # method line lives on cluster argumentation
    any_cluster = next(
        (v for k, v in result["flat"].items() if k.startswith("argumentation_cluster_")),
        "",
    )
    assert "EN/RU/DE" in any_cluster or "plots EN/RU/DE" in any_cluster


def test_de_award_page_does_not_push_podcast_alone():
    profile = {
        "film_uid": "test-de-oscar",
        "titles": {"en": "Award Flick"},
        "year": 2010,
        "plots": {"de": DE_AWARD_SCIENCE},
        "evidence_bags": [
            {
                "name": "plot_de",
                "text": DE_AWARD_SCIENCE,
                "source": "wikipedia",
                "language": "de",
                "weight": 1.1,
            }
        ],
    }
    result = score_profile(profile)
    psych = result["scores"]["Psychological_Depth"]["score"]
    podcast = result["scores"]["Podcast_Priority"]["score"]
    awards = result["scores"]["Awards_Prestige"]["score"]
    assert psych < 1.5
    assert podcast < 2.5
    assert awards > 0
    summary = result["flat"].get("argumentation_Psychological_Depth") or ""
    assert "trilingual" not in summary.lower()
