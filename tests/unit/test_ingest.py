from pathlib import Path

import pytest

from agentic_search.ingest import csv_kaggle, csv_livecareer
from agentic_search.ingest.dedupe import content_hash, near_duplicate_groups
from agentic_search.ingest.markdown_dir import split_front_matter
from agentic_search.ingest.normalize import collapse, normalize
from agentic_search.ingest.rates import seniority_hint, synthetic_rate
from agentic_search.ingest.redact import redact
from agentic_search.ingest.registry import Registry, doc_id

ROOT = Path(__file__).resolve().parents[2]
LIVECAREER = ROOT / "data" / "resume_dataset_livecareer.csv"
KAGGLE = ROOT / "data" / "UpdatedResumeDataSet.csv"


# ---------------------------------------------------------------- normalize

def test_normalize_repairs_and_tidies():
    out = normalize("NaÃ¯ve​ Bayes   x  \r\n\r\n\r\n\r\n  - nested\t bullet\n\n － ,\n\nEnd")
    assert out == "Naïve Bayes x\n\n  - nested bullet\n\nEnd\n"


def test_collapse():
    assert collapse("  a \n b  c ") == "a b c"


# ---------------------------------------------------------------- redact

@pytest.mark.parametrize(
    "text, expected, phones",
    [
        ("Call (212) 555-1980 or +91 98765 43210 now", "Call [phone] or [phone] now", 2),
        ("Mobile: 2125551980.", "Mobile: [phone].", 1),
        ("Worked 1999 - 2002 and 12.05.2013 - 03.2017", "Worked 1999 - 2002 and 12.05.2013 - 03.2017", 0),
        ("budget $2,000,000; revenue 2 000 000 EUR; ref 2013 2017 2018 2020", "budget $2,000,000; revenue 2 000 000 EUR; ref 2013 2017 2018 2020", 0),
        ("Materials Letters, 59 (2005) 570-574.", "Materials Letters, 59 (2005) 570-574.", 0),
        ("ISBN: 959-7160-31-5 and US 20050276743 A1", "ISBN: 959-7160-31-5 and US 20050276743 A1", 0),
        ("Employee ID 1234567890123456", "Employee ID 1234567890123456", 0),
    ],
)
def test_phone_rule(text, expected, phones):
    out, counts = redact(text)
    assert out == expected
    assert counts.phones == phones


def test_email_and_url():
    out, counts = redact("mail john.doe@example.com, see https://linkedin.com/in/john-doe and github.com/jdoe/repo or www.x.org/p.")
    assert out == "mail [email], see [url] and [url] or [url]"
    assert (counts.emails, counts.urls) == (1, 3)


# ---------------------------------------------------------------- converters (inline fixtures)

LC_HTML = """
<div id="document">
 <div class="section"><div class="name"><span class="field fName"></span><span class="field">SENIOR JAVA DEVELOPER</span></div></div>
 <div class="section"><div class="heading"><div class="sectiontitle">Summary</div></div>
   <div class="paragraph"><div class="field singlecolumn"><p>Backend engineer.</p><p>Builds web services.</p></div></div></div>
 <div class="section"><div class="heading"><div class="sectiontitle">Highlights</div></div>
   <div class="paragraph"><table class="twocol"><tr><td class="field"><p>Java</p><p>Spring</p></td><td class="field"><p>Hibernate</p></td></tr></table></div></div>
 <div class="section"><div class="heading"><div class="sectiontitle">Experience</div></div>
   <div class="paragraph"><div class="singlecolumn">
     <span class="paddedline"><span class="jobtitle">Software Engineer</span><span class="datesWrapper"><span class="jobdates">Jan 2015</span><span> to </span><span class="jobdates">Current</span></span></span>
     <span class="paddedline"><span class="companyname">Company Name</span><span> － </span><span class="joblocation jobcity">City</span><span> , </span><span class="joblocation jobstate">State</span></span>
     <span class="jobline"><ul><li>Designed <b>RESTful</b> web services.</li><li>Tuned Hibernate.</li></ul></span></div></div></div>
 <div class="section"><div class="heading"><div class="sectiontitle">Education</div></div>
   <div class="paragraph"><div class="singlecolumn">
     <span class="paddedline"><span class="degree">Bachelor of Science</span><span> , </span><span class="programline">Computer Science</span><span class="datesWrapper"><span class="jobdates">2014</span></span></span>
     <span class="paddedline"><span class="companyname companyname_educ">State University</span><span> － </span><span class="joblocation jobcity">City</span></span></div></div></div>
</div>
"""


def test_livecareer_html_to_markdown():
    md = csv_livecareer.html_to_markdown(LC_HTML)
    assert md == (
        "# SENIOR JAVA DEVELOPER\n\n"
        "## Summary\n\nBackend engineer.\n\nBuilds web services.\n\n"
        "## Highlights\n\n- Java\n- Spring\n- Hibernate\n\n"
        "## Experience\n\n### Software Engineer\n*Company Name — City, State · Jan 2015 to Current*\n\n"
        "- Designed RESTful web services.\n- Tuned Hibernate.\n\n"
        "## Education\n\n### Bachelor of Science, Computer Science\n*State University — City · 2014*\n"
    )


def test_livecareer_str_fallback():
    assert csv_livecareer.str_to_markdown("  CHEF\n\n  Summary  Cooks.  ") == "# CHEF\n\nSummary Cooks.\n"


KAGGLE_TEXT = (
    "Skills * Python (pandas), Sql * Machine learning: SVM, NaÃ¯ve Bayes.Education Details \n\n"
    "May 2013 to May 2017 B.E UIT-RGPV\nData Scientist \n\nData Scientist - Matelabs\nSkill Details \n"
    "Python- Exprience - Less than 1 year months\nKeras- Exprience - 12 monthsCompany Details \n"
    "company - Matelabs\ndescription - ML Platform. * Deployed models\nWorked on ARIMA.\n\ncompany - Other Co\ndescription - \n"
)


def test_kaggle_text_to_markdown():
    md = csv_kaggle.text_to_markdown(KAGGLE_TEXT, "Data Science")
    assert md == (
        "# Data Scientist\n\n*Data Scientist - Matelabs*\n\n"
        "## Skills\n\n- Python (pandas), Sql\n- Machine learning: SVM, Naïve Bayes.\n"
        "- Python — Less than 1 year months\n- Keras — 12 months\n\n"
        "## Experience\n\n### Matelabs\n\nML Platform.\n\n- Deployed models\n\nWorked on ARIMA.\n\n### Other Co\n\n"
        "## Education\n\n- May 2013 to May 2017 B.E UIT-RGPV\n"
    )


def test_kaggle_headline_falls_back_to_category():
    md = csv_kaggle.text_to_markdown("Education Details \nBA Mumbai\nSkill Details \nHr- Exprience - 6 months\nCompany Details \ncompany - X\ndescription - Hr\n", "HR")
    assert md.startswith("# HR\n")


# ---------------------------------------------------------------- real data (skipped if the CSVs are absent)

@pytest.mark.skipif(not LIVECAREER.is_file(), reason="data not present")
def test_livecareer_first_rows_have_structure():
    import itertools

    docs = list(itertools.islice(csv_livecareer.load(LIVECAREER), 25))
    assert len(docs) == 25
    for d in docs:
        assert d.markdown.startswith("# "), d.source_id
        assert "\n## " in d.markdown
        assert "<div" not in d.markdown and "<span" not in d.markdown


@pytest.mark.skipif(not KAGGLE.is_file(), reason="data not present")
def test_kaggle_unique_texts_count():
    docs = list(csv_kaggle.load(KAGGLE))
    assert len(docs) == 166
    assert all("Ã" not in d.markdown for d in docs)


# ---------------------------------------------------------------- dedupe / registry / rates / front matter

def test_content_hash_stable():
    assert content_hash("a\n") == content_hash("a\n") and len(content_hash("x")) == 16


def test_near_duplicate_groups():
    base = " ".join(f"word{i}" for i in range(300))
    texts = {1: base, 2: base + " extra words at the end", 3: " ".join(f"other{i}" for i in range(300)), 4: base.replace("word1 ", "changed ")}
    groups = near_duplicate_groups(texts)
    assert groups.get(1) == groups.get(2) == groups.get(4) and groups[1] is not None
    assert 3 not in groups


def test_registry_assigns_stable_doc_nos(tmp_path):
    reg = Registry.load(tmp_path / "registry.json")
    a = reg.assign("s", "1", "HR", "h1")
    b = reg.assign("s", "2", "HR", "h2")
    reg.save()
    reg2 = Registry.load(tmp_path / "registry.json")
    assert reg2.assign("s", "1", "HR", "h1-changed").doc_no == a.doc_no == 1
    assert reg2.assign("s", "3", None, "h3").doc_no == 3 and b.doc_no == 2
    assert doc_id(412) == "r000412"


def test_synthetic_rate_is_deterministic_and_bounded():
    r1 = synthetic_rate("INFORMATION-TECHNOLOGY", "r000001", headline="SENIOR JAVA DEVELOPER")
    assert r1 == synthetic_rate("INFORMATION-TECHNOLOGY", "r000001", headline="SENIOR JAVA DEVELOPER")
    assert 15 <= r1 <= 250
    assert synthetic_rate("CHEF", "r000002") < synthetic_rate("CONSULTANT", "r000002", seniority="director")
    assert seniority_hint("Sr. Data Engineer") == "senior" and seniority_hint("HR Intern") == "intern" and seniority_hint("Chef") == "mid"


def test_split_front_matter():
    meta, body = split_front_matter("---\nrate: 85\nfixture: true\n---\n# Title\n")
    assert meta == {"rate": 85, "fixture": True} and body == "# Title\n"
    assert split_front_matter("# no front matter\n") == ({}, "# no front matter\n")
