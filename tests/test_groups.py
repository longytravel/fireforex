from ff.data.groups import group_pairs, PAIR_GROUPS


def test_majors_metals_other():
    out = group_pairs(["EUR_USD", "XAU_USD", "FOO_BAR"])
    assert out == {
        "Majors": ["EUR_USD"],
        "Metals": ["XAU_USD"],
        "Other": ["FOO_BAR"],
    }


def test_drops_empty_groups():
    out = group_pairs(["EUR_USD"])
    assert "Majors" in out
    assert "Crosses" not in out
    assert "Metals" not in out
    assert "Other" not in out


def test_preserves_group_order():
    pairs = ["XAU_USD", "EUR_GBP", "EUR_USD"]
    out = group_pairs(pairs)
    assert list(out.keys()) == ["Majors", "Crosses", "Metals"]


def test_other_sorted():
    out = group_pairs(["ZZZ_AAA", "AAA_ZZZ", "EUR_USD"])
    assert out["Other"] == ["AAA_ZZZ", "ZZZ_AAA"]


def test_handles_empty_input():
    assert group_pairs([]) == {}


def test_known_pair_in_correct_group():
    all_known = []
    for pairs in PAIR_GROUPS.values():
        all_known.extend(pairs)
    out = group_pairs(all_known)
    assert "Other" not in out
    for group, pairs in PAIR_GROUPS.items():
        assert out[group] == pairs
