import pytest
from pwp.institutions import Institution, InstitutionIndex, default_index, match_institutions
from pwp.models import (
    Author,
    AuthorBlock,
    PageAffiliation,
    PageAuthor,
    Paper,
    PaperResult,
    ProjectPage,
    Repository,
)

INDEX = default_index()


def ids(text):
    return [(item_id, rule) for item_id, rule, _ in INDEX.match_text(text)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Massachusetts Institute of Technology", [("mit", "exact")]),
        ("The University of Texas at Austin, USA", [("utaustin", "exact")]),
        ("Stanford", [("stanford", "exact")]),
        (
            "Tandon School of Engineering, New York University, New York, NY, USA",
            [("nyu", "segment")],
        ),
        ("Johns Hopkins University Applied Physics Laboratory", [("jhu", "contains")]),
        ("清华大学计算机系", [("tsinghua", "contains")]),
        ("Meta Reality Labs", [("meta", "leading_name")]),
        ("CSAIL, MIT", [("mit", "acronym")]),
    ],
)
def test_rules_from_exact_to_acronym(text, expected):
    assert ids(text) == expected


def test_one_string_can_name_several_institutions():
    assert ids("Technical University of Munich, Massachusetts Institute of Technology") == [
        ("tum", "segment"),
        ("mit", "segment"),
    ]
    assert ids("Zhejiang University and Tencent") == [
        ("zju", "contains"),
        ("tencent", "leading_name"),
    ]


def test_brackets_and_parent_units():
    assert ids("HKUST (GZ), Guangzhou, China") == [("hkust-gz", "segment")]
    assert ids("The Hong Kong University of Science and Technology (HKUST)") == [
        ("hkust", "segment")
    ]
    # A matched lab hides its parent; root_id still reports the parent.
    assert ids("DAMO Academy, Alibaba Group") == [("damo", "segment")]
    assert INDEX.root("damo") == "alibaba"


def test_names_shared_by_two_institutions_need_a_qualifier():
    assert ids("Northeastern University") == []
    assert ids("Northeastern University, Boston, MA, USA") == [("neu", "exact")]


def test_longest_name_wins_inside_a_segment():
    assert ids("Technical University of Munich Department of Informatics") == [("tum", "contains")]


def test_state_university_campus_qualifiers():
    assert ids("University of Maryland, Baltimore County") == []
    assert ids("University of Illinois Chicago") == []
    assert ids("University of Maryland, MD, USA") == [("umd", "exact")]
    assert ids("Department of CS, University of Maryland, College Park") == [("umd", "segment")]
    assert ids("University of Washington, Seattle, WA") == [("uw", "segment")]
    assert ids("University of Michigan Medical School") == [("umich", "contains")]


def test_single_words_and_unknown_names_do_not_match_loosely():
    assert ids("Research Lab, Example Meta Institute") == []
    assert ids("Seed Lab of Example") == []
    assert ids("Example Institute of Science") == []


def test_hosts_and_repository_owners():
    assert INDEX.match_host("https://ai.meta.com/research/") == [("fair", "domain", "ai.meta.com")]
    assert INDEX.match_host("cs.stanford.edu") == [("stanford", "domain", "stanford.edu")]
    assert INDEX.match_host("https://example.github.io/project/") == []
    assert INDEX.match_github_owner("THUDM") == [("tsinghua", "github_org", "THUDM")]
    assert INDEX.root("google-deepmind") == "google"


def test_custom_registry_github_pages_owner():
    index = InstitutionIndex(
        [
            Institution(id="example", names=["Example University"], country="US"),
            Institution(
                id="example-lab",
                names=["Example Vision Lab"],
                github_orgs=["example-lab"],
                parent="example",
                country="US",
            ),
        ]
    )
    assert index.match_host("https://example-lab.github.io/paper/") == [
        ("example-lab", "github_org", "example-lab")
    ]
    assert index.root("example-lab") == "example"


def test_paper_matches_keep_sources_and_author_overlap():
    result = PaperResult(
        requested="2410.12345",
        paper=Paper(
            arxiv_id="2410.12345",
            title="A paper",
            authors=[
                Author(name="Alice Example", affiliations=["Tsinghua University"]),
                Author(name="Bob Sample"),
            ],
        ),
        pages=[
            ProjectPage(
                url="https://project.example/",
                final_url="https://vision.stanford.edu/project/",
                institutions=["Stanford University"],
                author_block=AuthorBlock(
                    authors=[PageAuthor(name="Alice Example"), PageAuthor(name="Carol Other")],
                    affiliations=[
                        PageAffiliation(name="Google DeepMind", marker="1"),
                        PageAffiliation(name="Unknown Institute", marker="2"),
                    ],
                ),
            )
        ],
        repositories=[
            Repository(url="https://github.com/THUDM/project", full_name="THUDM/project")
        ],
    )
    found = {
        (item.institution_id, item.source, item.rule): item for item in match_institutions(result)
    }
    assert set(found) == {
        ("tsinghua", "arxiv", "exact"),
        ("stanford", "citation_meta", "exact"),
        ("google-deepmind", "author_block", "exact"),
        ("stanford", "page_host", "domain"),
        ("tsinghua", "repository_owner", "github_org"),
    }
    deepmind = found["google-deepmind", "author_block", "exact"]
    assert deepmind.root_id == "google"
    assert deepmind.author_overlap == 1
    assert deepmind.source_url == "https://vision.stanford.edu/project/"
    assert found["tsinghua", "arxiv", "exact"].author_overlap is None


def test_results_without_paper_or_declarations():
    assert match_institutions(PaperResult(requested="missing")) == []
