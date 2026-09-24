from __future__ import annotations

import pytest

from lj_signage.naming import (
    ACTIVE_NAME_RE,
    active_name,
    is_playable_legacy,
    legacy_issues,
    slugify,
)


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        ("Coleção Filigrana — Natal 2026", "colecao-filigrana-natal-2026"),
        ("Alianças First Day", "aliancas-first-day"),
        ("  ÁÉÍÓÚ ç ñ  ", "aeiou-c-n"),
        ("Promo 50% OFF!!!", "promo-50-off"),
        ("***", "video"),
        ("", "video"),
    ],
)
def test_slugify(title, slug):
    assert slugify(title) == slug


def test_slug_is_at_most_40_characters_and_cut_at_a_word():
    slug = slugify("Campanha de Natal das Jóias em Ouro e Prata com Filigrana")
    assert len(slug) <= 40
    assert slug == "campanha-de-natal-das-joias-em-ouro-e"
    assert not slug.endswith("-")


def test_active_name_rejects_out_of_range_prefixes():
    sha = "a" * 64
    assert ACTIVE_NAME_RE.match(active_name(1, "x", sha))
    with pytest.raises(ValueError):
        active_name(0, "x", sha)
    with pytest.raises(ValueError):
        active_name(1000, "x", sha)
    with pytest.raises(ValueError):
        active_name(10, "Com Espaço", sha)


@pytest.mark.parametrize(
    ("name", "is_dir", "size", "issues", "playable"),
    [
        ("010_promo.mp4", False, 10, [], True),
        ("promo.mp4", False, 10, ["non_standard_name"], True),
        ("Promo Natal.mp4", False, 10, ["unsafe_name", "non_standard_name"], False),
        ("promo[1].mp4", False, 10, ["unsafe_name", "non_standard_name"], False),
        ("leia-me.txt", False, 10, ["not_video", "non_standard_name"], False),
        ("vazio.mp4", False, 0, ["empty", "non_standard_name"], False),
        ("fotos", True, None, ["dir"], False),
    ],
)
def test_legacy_issues(name, is_dir, size, issues, playable):
    found = legacy_issues(name, is_dir=is_dir, size=size)
    assert found == issues
    assert is_playable_legacy(found) is playable
