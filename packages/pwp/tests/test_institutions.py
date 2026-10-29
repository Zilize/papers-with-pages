import re
from collections import Counter

import pytest
from pwp.institutions import Institution, load_registry
from pydantic import ValidationError

REGISTRY = load_registry()
BY_ID = {item.id: item for item in REGISTRY}
PUBLIC_SUFFIXES = ("github.io", "gitlab.io", "github.com", "huggingface.co/", "sites.google.com")


def duplicates(values):
    return sorted(value for value, count in Counter(values).items() if count > 1)


def ancestors(item):
    seen = []
    while item.parent:
        item = BY_ID[item.parent]
        seen.append(item.id)
    return seen


def test_registry_loads_with_unique_ids():
    assert 150 <= len(REGISTRY) <= 250
    assert duplicates(item.id for item in REGISTRY) == []


def test_ror_ids_are_unique():
    assert duplicates(item.ror for item in REGISTRY if item.ror) == []


def test_parents_exist_without_cycles():
    for item in REGISTRY:
        if item.parent:
            assert item.parent in BY_ID, item.id
            assert item.id not in ancestors(item), item.id


@pytest.mark.parametrize("field", ["names", "acronyms", "github_orgs"])
def test_matching_keys_do_not_collide_across_entries(field):
    owners = [value.casefold() for item in REGISTRY for value in set(getattr(item, field))]
    assert duplicates(owners) == []


def test_names_and_acronyms_do_not_overlap():
    names = {name.casefold() for item in REGISTRY for name in item.names}
    acronyms = {acronym.casefold() for item in REGISTRY for acronym in item.acronyms}
    assert names & acronyms == set()


def test_domains_are_bare_hosts_owned_by_one_entry():
    owner = {}
    for item in REGISTRY:
        for domain in item.domains:
            assert re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", domain), domain
            assert not domain.startswith("www."), domain
            assert not domain.endswith(PUBLIC_SUFFIXES), domain
            assert domain not in owner, domain
            owner[domain] = item.id
    # A subdomain of another entry's domain is allowed only under that entry's hierarchy.
    for domain, item_id in owner.items():
        for other, other_id in owner.items():
            if domain.endswith("." + other):
                assert other_id in ancestors(BY_ID[item_id]), (domain, other)


def test_rejects_malformed_entries():
    with pytest.raises(ValidationError):
        Institution(id="Bad Id", names=["X"], country="US")
    with pytest.raises(ValidationError):
        Institution(id="x", ror="ror.org/03cve4549", names=["X"], country="US")
    with pytest.raises(ValidationError):
        Institution(id="x", names=[], country="US")


def test_load_registry_accepts_custom_file(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text('{"institutions": [{"id": "x", "names": ["X"], "country": "US"}]}')
    assert load_registry(path) == [Institution(id="x", names=["X"], country="US")]
