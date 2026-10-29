from bs4 import BeautifulSoup
from pwp import parse_project_page
from pwp.affiliations import parse_author_block


def block(html):
    return parse_author_block(BeautifulSoup(html, "html.parser"))


def affiliations(result):
    return [(item.marker, item.name, item.source) for item in result.affiliations]


def test_declared_containers_map_markers_and_footnotes():
    result = block(
        """<h1 class="title publication-title">A Project</h1>
        <div class="publication-venue">Conference 2026</div>
        <div class="publication-authors">
          <span class="author-block"><a href="#">Alice Example</a><sup>1,*</sup>,</span>
          <span class="author-block"><a href="#">Bob Sample</a><sup>2,†</sup></span>
        </div>
        <div class="publication-authors">
          <span class="author-block"><sup>1</sup>Example University,</span>
          <span class="author-block"><sup>2</sup>Sample Research Lab</span>
        </div>
        <div class="publication-authors">
          <span class="author-block"><sup>*</sup>Equal contribution</span>
          <span class="author-block"><sup>†</sup>Work done during an internship at Sample AI</span>
        </div>"""
    )
    assert [(author.name, author.markers) for author in result.authors] == [
        ("Alice Example", ["1", "*"]),
        ("Bob Sample", ["2", "†"]),
    ]
    assert affiliations(result) == [
        ("1", "Example University", "author_block"),
        ("2", "Sample Research Lab", "author_block"),
        ("†", "Sample AI", "author_block"),
    ]
    assert result.affiliations[-1].note == "Work done during an internship at Sample AI"


def test_title_region_reads_plain_markers_and_skips_subtitles():
    result = block(
        """<header><h1>A Project</h1>
        <h2>Learning Robust Policies Across Terrains</h2>
        <p>Alice Example¹ Bob Sample²</p>
        <p>1 Example University, 2Sample Institute of Technology</p>
        <p>Our approach is described in the University-style paper.</p></header>
        <section><h2>Related work</h2><p>¹Other University</p></section>
        <footer>Example University</footer>"""
    )
    assert [author.name for author in result.authors] == ["Alice Example", "Bob Sample"]
    assert affiliations(result) == [
        ("1", "Example University", "author_block"),
        ("2", "Sample Institute of Technology", "author_block"),
    ]


def test_unmarked_lines_keep_commas_inside_names_and_skip_venues():
    result = block(
        """<h1>A Project</h1>
        <p class="venue-line">Transactions on Machine Learning Research 2026</p>
        <p><a href="#">Alice Example</a><a href="#">Bob Sample</a></p>
        <p>University of Example, North Campus</p>"""
    )
    assert [author.name for author in result.authors] == ["Alice Example", "Bob Sample"]
    assert affiliations(result) == [
        (None, "University of Example, North Campus", "author_block"),
    ]


def test_marker_spans_letters_and_logos():
    result = block(
        """<h1>A Project</h1>
        <p class="authors">
          <span class="author">Alice Example<span class="star">1,2&dagger;</span></span>
          <span class="author">Bob Sample<sup>a</sup></span></p>
        <div class="affiliations">
          <div><span class="star">1</span>Example University</div>
          <div><span class="star">2</span>Sample Labs</div>
          <div><sup>a</sup>Other Institute</div>
          <div><span class="star">&dagger;</span>Project co-lead</div>
          <img src="logos/example.svg" alt="Example University">
          <img src="sample.png" alt="">
        </div>"""
    )
    assert [(author.name, author.markers) for author in result.authors] == [
        ("Alice Example", ["1", "2", "†"]),
        ("Bob Sample", ["a"]),
    ]
    assert affiliations(result) == [
        ("1", "Example University", "author_block"),
        ("2", "Sample Labs", "author_block"),
        ("a", "Other Institute", "author_block"),
        (None, "Example University", "logo"),
    ]


def test_json_ld_people_without_cited_works():
    result = block(
        """<script type="application/ld+json">{
          "@type": "ScholarlyArticle",
          "author": [{"@type": "Person", "name": "Alice Example",
                      "affiliation": {"@type": "Organization",
                                      "name": "Example University; Sample Labs"}}],
          "citation": [{"@type": "ScholarlyArticle",
                        "author": {"@type": "Person", "name": "Cited Person",
                                   "affiliation": "Cited University"}}]
        }</script>
        <script type="application/ld+json">not json</script>"""
    )
    assert [author.name for author in result.authors] == ["Alice Example"]
    assert affiliations(result) == [
        (None, "Example University", "json_ld"),
        (None, "Sample Labs", "json_ld"),
    ]


def test_pages_without_declarations_and_readmes():
    assert block("<h1>A Project</h1><p>We study robust learning.</p>") is None
    html = """<h1>A Project</h1><p>Alice Example¹</p><p>¹Example University</p>"""
    page = parse_project_page(html, "https://project.example/")
    assert page.author_block.affiliations[0].name == "Example University"
    readme = parse_project_page(html, "https://github.example/README.md", visuals=False)
    assert readme.author_block is None


def test_cjk_affiliations_after_title():
    result = block("<h1>项目</h1><p>张三¹ 李四²</p><p>¹示例大学 ²样例研究院</p>")
    assert [author.name for author in result.authors] == ["张三", "李四"]
    assert affiliations(result) == [
        ("1", "示例大学", "author_block"),
        ("2", "样例研究院", "author_block"),
    ]


def test_marked_headings_long_container_lines_and_button_labels():
    result = block(
        """<h2>A Project</h2>
        <h4><a href="#">Alice Example</a><sup>1</sup>, </h4>
        <h4><a href="#">Bob Sample</a><sup>2</sup></h4>
        <h4><a href="#">Sample Labs</a><sup>1</sup></h4>
        <h4><a href="#">Example Institute for Informatics, SIC</a><sup>2</sup></h4>
        <h3>Conference 2026</h3>"""
    )
    assert [author.name for author in result.authors] == ["Alice Example", "Bob Sample"]
    assert affiliations(result) == [
        ("1", "Sample Labs", "author_block"),
        ("2", "Example Institute for Informatics, SIC", "author_block"),
    ]
    long_line = (
        "Example Institute, First University of Lyon, Second University of Lyon, CNRS, "
        "Laboratory of Images, Villeurbanne, France"
    )
    result = block(
        f"""<p class="authors">张三 · 李四 · 论文 · 即将开放</p>
        <p class="affil">{long_line}</p>
        <div class="affiliations">
          <img alt="Example University logo and wordmark" src="a.png">
        </div>"""
    )
    assert [author.name for author in result.authors] == ["张三", "李四"]
    assert affiliations(result) == [
        (None, long_line, "author_block"),
        (None, "Example University", "logo"),
    ]
