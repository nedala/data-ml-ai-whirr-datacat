"""Taxonomy validator: exactly 104 unique leaves, 3 levels, complete leaf records."""
from collections import Counter

from bsp_ccai import taxonomy


def test_exactly_104_unique_leaves():
    tax = taxonomy.load()
    ids = tax.ids
    assert len(ids) == 104
    assert len(set(ids)) == 104


def test_validator_passes():
    assert taxonomy.validate(taxonomy.load()) == []


def test_three_levels_and_l1_split():
    tax = taxonomy.load()
    l1 = Counter(x.l1_name for x in tax.leaves)
    assert len(l1) == 12 and sum(l1.values()) == 104
    for x in tax.leaves:
        assert x.id.count(".") == 2 and x.id.startswith(x.l2 + ".") and x.l2.startswith(x.l1 + ".")
        assert 3 <= len(x.examples) <= 5 and x.keywords and x.definition and x.include and x.exclude
        assert {"ticket_category", "ticket_subcategory", "sap_reason"} <= set(x.mapping)
    assert any(x.churn_risk for x in tax.leaves) and not all(x.churn_risk for x in tax.leaves)


def test_validator_catches_problems():
    tax = taxonomy.load()
    broken = taxonomy.Taxonomy(tax.version, tax.name, tax.digest, tax.leaves[:-1] + [tax.leaves[0]], tax.domains)
    errs = taxonomy.validate(broken)
    assert any("duplicate" in e for e in errs)
