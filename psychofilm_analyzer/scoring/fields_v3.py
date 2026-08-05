"""Structured field extractors B–J (rules/templates; null if weak)."""

from __future__ import annotations

from typing import Any, Optional

from psychofilm_analyzer.scoring.phrase_engine import (
    ScoreResult,
    list_matched_labels,
    match_phrases,
    score_dictionary,
)


def _bags(profile: dict) -> list[dict]:
    return list(profile.get("evidence_bags") or [])


def _join(xs: list[str], n: int = 12) -> Optional[str]:
    xs = [x for x in xs if x]
    return "; ".join(xs[:n]) if xs else None


def extract_all_fields(
    profile: dict,
    dicts: dict,
    scores: dict[str, ScoreResult],
    cluster_ranking: list[tuple[str, float]],
    primary: Optional[str],
    secondary: Optional[str],
) -> dict[str, Any]:
    bags = _bags(profile)
    maps = dicts.get("field_maps") or {}
    content_type = profile.get("content_type") or "feature"
    title = (profile.get("titles") or {}).get("en") or profile.get("imported", {}).get("title") or "This film"

    # --- B Psychological ---
    archetypes = list_matched_labels(bags, maps.get("archetypes") or {})
    trauma_types = list_matched_labels(bags, maps.get("trauma_types") or {})
    defenses = list_matched_labels(bags, maps.get("defenses") or {})
    attachment = list_matched_labels(bags, maps.get("attachment") or {})
    transform = scores.get("Identity_Transformation")
    char_transform = transform.score if transform else 0.0

    # --- C Conflict ---
    surface_map = maps.get("surface_conflicts") or {}
    surface_hits: list[tuple[str, float]] = []
    for label, phrases in surface_map.items():
        raw, _ = match_phrases(bags, phrases, allow_t1_alone=True)
        if raw >= 1.2:
            surface_hits.append((label, raw))
    surface_hits.sort(key=lambda x: -x[1])
    primary_surface = surface_hits[0][0] if surface_hits else None
    secondary_surface = [x[0] for x in surface_hits[1:4]] or None

    # hidden conflict template from primary theme
    hidden_templates = {
        "Adolescence & Identity Formation": "Who am I when social masks and roles collapse?",
        "Childhood / Transgenerational Trauma": "Can inherited or early wounds be integrated without repeating them?",
        "Madness, Psychosis & Borderline States": "What is real when perception and self-structure fragment?",
        "Jungian Shadow, Persona & Individuation": "What shadow material must be faced for a truer self to emerge?",
        "Family Systems, Attachment & Parental Complexes": "How do family bonds both form and imprison the self?",
        "Existential Crisis, Meaning, Death & Midlife": "How to live meaningfully under finitude and uncertainty?",
        "Collective Unconscious, Power & Historical Psychotypes": "How does historical/collective power shape individual psyche?",
    }
    core_hidden = None
    if primary and primary in hidden_templates and scores.get("Psychological_Depth") and scores["Psychological_Depth"].score >= 3:
        core_hidden = hidden_templates[primary]
    elif primary and primary.startswith("Underspecified"):
        core_hidden = None

    # internal vs external
    raw_int, _ = match_phrases(
        bags,
        {"t2": ["inner conflict", "guilt", "shame", "anxiety", "depression", "memory", "identity"]},
        allow_t1_alone=True,
    )
    raw_ext, _ = match_phrases(
        bags,
        {"t2": ["war", "chase", "battle", "crime", "enemy", "escape", "mission"]},
        allow_t1_alone=True,
    )
    if raw_int + raw_ext < 1:
        balance = None
    elif raw_int > raw_ext * 1.3:
        balance = "internal-dominant"
    elif raw_ext > raw_int * 1.3:
        balance = "external-dominant"
    else:
        balance = "balanced"

    res_map = maps.get("resolution_types") or {}
    resolution = None
    best_r = 0.0
    for label, phrases in res_map.items():
        raw, _ = match_phrases(bags, phrases, allow_t1_alone=True)
        if raw > best_r:
            best_r = raw
            resolution = label
    if best_r < 1.2:
        resolution = None

    # --- D Narrative ---
    structures = list_matched_labels(bags, maps.get("narrative_structure") or {}, min_raw=1.0)
    craft = scores.get("Narrative_Craft")
    # Ambiguity / symbolism: prefer dedicated score; fall back to discussability
    amb = scores.get("Symbolism_Ambiguity") or scores.get("Discussability_Podcast_Potential")
    raw_unrel, ev_unrel = match_phrases(
        bags,
        {
            "t3": ["unreliable narrator", "ненадёжный рассказчик", "ненадежный рассказчик"],
            "t2": ["unreliable", "cannot trust", "ненадёжн", "ненадежн"],
        },
        allow_t1_alone=False,
    )
    raw_sym, _ = match_phrases(
        bags,
        {
            "t3": ["symbolic imagery", "dream logic", "символическ", "аллегори"],
            "t2": [
                "symbol",
                "metaphor",
                "allegory",
                "dream imagery",
                "surreal",
                "символ",
                "метафор",
                "аллегор",
                "сюрреал",
                "подтекст",
            ],
        },
        allow_t1_alone=True,
    )

    # --- E Spiritual ---
    traditions = list_matched_labels(bags, maps.get("religious_traditions") or {})
    spirit = scores.get("Spiritual_Religious_Mystical_Depth")
    destiny = scores.get("Purpose_Destiny_Meaning_of_Life")
    raw_ritual, _ = match_phrases(
        bags, {"t2": ["ritual", "prayer", "ceremony", "liturgy", "rite"]}, allow_t1_alone=True
    )
    raw_silence, _ = match_phrases(
        bags, {"t2": ["silence", "contemplation", "meditation", "stillness"]}, allow_t1_alone=True
    )
    raw_myst, _ = match_phrases(
        bags, {"t2": ["vision", "miracle", "mystical", "epiphany", "revelation"]}, allow_t1_alone=True
    )
    raw_sync, _ = match_phrases(
        bags, {"t2": ["synchronicity", "coincidence", "signs", "omen"]}, allow_t1_alone=True
    )

    # --- F Human nature ---
    hns = scores.get("Human_Nature_Spectrum")
    spectrum = hns.score if hns else 5.0
    if spectrum >= 7:
        stance = "leans spiritual/transcendent"
        fw = "free will / inner agency emphasized" if raw_myst + (destiny.raw_hits if destiny else 0) > 1 else "mixed"
        view = "Human beings as more than biological machinery; soul/spirit language present in sources."
    elif spectrum <= 3:
        stance = "leans materialist/biological"
        fw = "determinism / constraint emphasized"
        view = "Human behavior framed in bodily, social, or mechanistic terms more than spiritual."
    else:
        stance = "ambiguous / dual"
        fw = "insufficient_evidence"
        view = "Sources do not clearly privilege machine vs spiritual anthropology."

    # --- G Audience ---
    stages = list_matched_labels(bags, maps.get("life_stages") or {}, min_raw=1.0)
    life_stage = _join(stages) or (
        "Midlife" if (scores.get("Existential_Weight") and scores["Existential_Weight"].score >= 5) else "General adult"
    )
    psych = scores.get("Psychological_Depth")
    trauma = scores.get("Trauma_Clinical_Relevance")
    reflective = min(10.0, ((psych.score if psych else 0) * 0.5 + (trauma.score if trauma else 0) * 0.3 + (spirit.score if spirit else 0) * 0.2))

    target = []
    if psych and psych.score >= 6:
        target.append("psychologically curious viewers")
    if spirit and spirit.score >= 5:
        target.append("spiritually reflective viewers")
    if scores.get("Fairy_Tale_Folklore_Mystical_Density") and scores["Fairy_Tale_Folklore_Mystical_Density"].score >= 5:
        target.append("myth/folklore lovers; possibly families if Easy_to_Watch high")
    if scores.get("Intellectual_Scientific_Complexity") and scores["Intellectual_Scientific_Complexity"].score >= 5:
        target.append("intellectually curious / science-philosophy audience")
    if not target:
        target.append("general cinephile audience")

    pre_q = []
    if primary and not str(primary).startswith("Underspecified"):
        pre_q.append(f"How does this story illuminate {primary}?")
    if trauma and trauma.score >= 4:
        pre_q.append("What personal or historical wounds does this work open?")
    if not pre_q:
        pre_q = None

    # --- H Fairy tale ---
    fairy = scores.get("Fairy_Tale_Folklore_Mystical_Density")
    fairy_score = fairy.score if fairy else 0
    motifs = list_matched_labels(bags, maps.get("folklore_motifs") or {}, min_raw=1.0)
    initiation = "Initiation" in motifs or any(
        x in (profile.get("keywords") or []) for x in ["initiation", "coming of age", "rite"]
    )

    # Historical / scientific truth — conservative
    is_doc = content_type == "documentary" or profile.get("type_flags", {}).get("is_documentary")
    hist = scores.get("Collective_Historical_Psychotype")
    sci = scores.get("Intellectual_Scientific_Complexity")
    hist_acc = None
    if is_doc and hist and hist.score >= 3:
        hist_acc = "presented as documentary/historical testimony (accuracy not independently verified)"
    elif hist and hist.score >= 4:
        hist_acc = "historical setting present; factual accuracy not validated by this pipeline"
    sci_acc = None
    if sci and sci.score >= 4:
        sci_acc = "scientific/philosophical claims appear in synopsis; accuracy not lab-validated here"

    raw_alt, _ = match_phrases(
        bags, {"t2": ["debate", "controversial", "alternative", "disputed", "theory"]}, allow_t1_alone=True
    )
    raw_prop, _ = match_phrases(
        bags, {"t2": ["propaganda", "indoctrination", "ideological"]}, allow_t1_alone=True
    )

    # --- I subject scores ---
    subjects = {}
    for name, phrases in (maps.get("subject_scores") or {}).items():
        subjects[name] = score_dictionary(name, bags, phrases, scale=2.5).score
    if content_type == "animation":
        anim = score_dictionary(
            "Animation_Specific_Strengths",
            bags,
            {
                "t2": ["animation", "animated", "visual metaphor", "fantasy world", "studio ghibli"],
                "t3": ["hand-drawn", "stop-motion"],
            },
            scale=2.0,
            allow_t1_alone=True,
        )
        subjects["Animation_Specific_Strengths"] = anim.score
    else:
        subjects["Animation_Specific_Strengths"] = None

    # --- J Podcast ---
    hook = None
    if primary and not str(primary).startswith("Underspecified"):
        hook = f"{title}: a story that presses on {primary.lower()} without reducing it to a slogan."
    elif psych and psych.score >= 4:
        hook = f"{title}: denser psychologically than its surface genre suggests."
    else:
        hook = f"{title}: better approached as entertainment craft than deep psyche work — useful as a contrast case."

    angles = []
    for sname in [
        "Psychological_Depth",
        "Trauma_Clinical_Relevance",
        "Identity_Transformation",
        "Spiritual_Religious_Mystical_Depth",
        "Narrative_Craft",
        "Fairy_Tale_Folklore_Mystical_Density",
        "Intellectual_Scientific_Complexity",
    ]:
        s = scores.get(sname)
        if s and s.score >= 4.5:
            angles.append(f"{sname.replace('_', ' ')} ({s.score:.1f}/10)")
    if core_hidden:
        angles.append(f"Hidden conflict: {core_hidden}")
    angles = angles[:5] or ["Use as contrast title: why psych density stays low despite cultural visibility"]

    triggers = list_matched_labels(bags, maps.get("triggers") or {}, min_raw=1.5)

    easy = scores.get("Easy_to_Watch")
    context = "solo reflective viewing" if (easy and easy.score < 5) else "flexible; can be group or solo"
    if fairy_score >= 6 and easy and easy.score >= 6:
        context = "possible shared/family viewing depending on triggers"

    # Awards_Prestige is a real multi-score (engine); field mirrors it for BJ export
    awards_sr = scores.get("Awards_Prestige")
    prestige = awards_sr.score if awards_sr else 0.0

    # Overall_Priority_for_Podcast is computed in engine (distinct from Podcast_Priority)
    overall_sr = scores.get("Overall_Priority_for_Podcast")
    pp = scores.get("Podcast_Priority")
    overall = overall_sr.score if overall_sr else (pp.score if pp else 0.0)

    return {
        # B
        "Primary_Psychological_Themes": primary,
        "Secondary_Themes": secondary,
        "Archetypes_Present": _join(archetypes),
        "Trauma_Types": _join(trauma_types),
        "Defense_Mechanisms_Shown": _join(defenses),
        "Character_Transformation_Level": round(char_transform, 2),
        "Attachment_Patterns": _join(attachment),
        # C
        "Primary_Surface_Conflict": primary_surface,
        "Secondary_Surface_Conflicts": _join(secondary_surface or []),
        "Core_Hidden_Conflict": core_hidden,
        "Conflict_Layers": _join([primary_surface] + (secondary_surface or []) if primary_surface else []),
        "Internal_vs_External_Conflict_Balance": balance,
        "Resolution_Type": resolution,
        # D
        "Narrative_Structure": _join(structures),
        "Subtext_Level": round(
            min(
                10.0,
                max(raw_sym * 1.2, (amb.score if amb else 0) * 0.55)
                + (craft.score * 0.3 if craft else 0),
            ),
            2,
        ),
        # Prefer dedicated Symbolism_Ambiguity multi-score; fall back to raw phrase density
        "Symbolic_Density": round(
            min(10.0, max(raw_sym * 1.5, (amb.score if amb else 0) * 0.9)),
            2,
        ),
        "Ambiguity_Level": round(amb.score if amb else 0.0, 2),
        "Unreliable_Narrator": bool(raw_unrel >= 2),
        "Visual_Metaphor_Strength": round(
            min(
                10.0,
                max(raw_sym * 1.3, (amb.score if amb else 0) * 0.7)
                + (1.5 if content_type == "animation" else 0),
            ),
            2,
        ),
        # E
        "Religious_or_Spiritual_Traditions_Referenced": _join(traditions),
        "Rites_Rituals_Practices_Shown": "present" if raw_ritual >= 1.5 else None,
        "Themes_of_Destiny_Calling_Purpose": round(destiny.score if destiny else 0.0, 2),
        "Hidden_Destiny_Parallels_or_Synchronicities": "suggested" if raw_sync >= 1.5 else None,
        "Secret_or_Esoteric_Meanings_Level": round(min(10.0, (spirit.score if spirit else 0) * 0.6 + raw_myst), 2)
        if (spirit and spirit.score >= 3)
        else 0.0,
        "Mystical_Experience_Portrayal": "present" if raw_myst >= 1.5 else None,
        "Silence_Contemplation_Level": round(min(10.0, raw_silence * 2.0), 2),
        # F
        "View_of_Human_Nature": view,
        "Human_Nature_Spectrum_Position": round(spectrum, 2),
        "Soul_Spirit_Consciousness_Stance": stance,
        "Free_Will_vs_Determinism_Emphasis": fw,
        # G
        "Target_Viewer_Types": _join(target),
        "Typical_Pre_Watching_Questions_or_Problems": _join(pre_q) if pre_q else None,
        "How_the_Film_May_Address_Them": (
            f"Offers narrative space to explore {primary}" if primary and not str(primary).startswith("Underspecified") else None
        ),
        "Recommended_Life_Stage": life_stage,
        "Reflective_or_Therapeutic_Potential": round(reflective, 2),
        # H fairy
        "Is_Fairytale_Myth_or_Folklore": fairy_score >= 4.0,
        "Cultural_Source_Tradition": _join(traditions) if fairy_score >= 3 else None,
        "Folklore_Motifs": _join(motifs),
        "Initiation_Structure_Present": bool(initiation),
        # H historical
        "Historical_Factual_Accuracy_Level": hist_acc,
        "Historiographical_Stance": "documentary mode" if is_doc else ("historical fiction" if hist and hist.score >= 3 else None),
        "Alternative_Historical_Interpretations_Acknowledged": "possible" if raw_alt >= 2 and hist and hist.score >= 3 else None,
        "Ideological_Bias_Notes": "propaganda/ideology language present" if raw_prop >= 1.5 else None,
        "Psychological_Truth_vs_Factual_Truth_Emphasis": (
            "psychological truth foregrounded" if (psych and psych.score >= 5 and (not is_doc)) else ("factual/testimonial frame" if is_doc else None)
        ),
        "Modern_Psychological_Analogy": core_hidden,
        "Modern_Analogy_Validity": "heuristic only — not validated",
        "Truth_Validation_Comment_Historical": hist_acc or "insufficient_evidence for factual audit",
        # H scientific
        "Scientific_Accuracy_Level": sci_acc,
        "Dramatic_License_Degree": "likely high" if sci and sci.score >= 4 else None,
        "Consensus_Alignment": "not assessed by this pipeline",
        "Main_Scientific_or_Philosophical_Claims": "see plot_en/plot_ru" if sci and sci.score >= 4 else None,
        "Alternative_Outlooks_Presented": "suggested by language" if raw_alt >= 2 else None,
        "Competing_Views_or_Controversies": "possible" if raw_alt >= 2.5 else None,
        "Truth_Validation_Comment_Scientific": sci_acc or "insufficient_evidence for scientific audit",
        # H cross-cutting truth
        "Outlook_Type": "plural/exploratory" if raw_alt >= 2 else ("advocacy-leaning" if raw_prop >= 2 else "singular narrative frame"),
        "Perspective_Diversity": round(min(10.0, raw_alt * 2), 2),
        "Invitation_to_Critical_Thinking": round(min(10.0, (amb.score if amb else 0) * 0.6 + raw_alt), 2),
        "Propaganda_or_Advocacy_Risk": round(min(10.0, raw_prop * 2.5), 2),
        # I
        **{f"subj_{k}": (round(v, 2) if v is not None else None) for k, v in subjects.items()},
        # J
        "Spoiler_Free_Psychological_Hook": hook,
        "Podcast_Angles": _join(angles, 5),
        "Best_Audience": _join(target),
        "Recommended_Watching_Context": context,
        "Trigger_Warnings": _join(triggers),
        "Overall_Priority_for_Podcast": round(overall, 2),
        "Awards_Prestige": round(prestige, 2),
        # Mirror of multi-score: modern watchability / non-boring deliverability
        "Modern_Viewer_Deliverability": round(
            float(scores["Modern_Viewer_Deliverability"].score)
            if scores.get("Modern_Viewer_Deliverability")
            else 0.0,
            2,
        ),
        "fields_method": "rules_templates_v3",
    }
