"""Deterministic persona specs for the fixture corpus.

`build_personas(seed, count)` returns one spec per fixture resume. Every
fact that the golden scenario and the evaluation rely on (who worked on data
pipelines, who knows Elixir, rates, near-duplicates, the Java JD test
candidates) is decided HERE, with a seeded RNG, and written to
data/fixture/ground_truth.json. The LLM only writes prose that satisfies the
spec; validate.py checks that it did. See data/fixture/SPEC.md.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

# ----------------------------------------------------------------- pools

ROLE_GROUPS: dict[str, dict] = {
    "data": {
        "category": "Data Engineering",
        "roles": ["Data Engineer", "Analytics Engineer", "Data Platform Engineer", "Backend Engineer, Data"],
        "skills": ["Python", "SQL", "Postgres", "AWS", "GCP", "Terraform", "Docker", "Kubernetes", "Scala", "Go", "Redis", "Grafana"],
        "topics": ["analytics", "ml-ops", "observability"],
    },
    "backend": {
        "category": "Backend Engineering",
        "roles": ["Backend Engineer", "Software Engineer", "Platform Engineer", "API Engineer"],
        "skills": ["Go", "Python", "TypeScript", "Node.js", "Postgres", "Redis", "gRPC", "GraphQL", "AWS", "Docker", "Kubernetes", "Rust", "Ruby on Rails", "Django", "FastAPI"],
        "topics": ["payments", "search", "identity", "api-platform"],
    },
    "java": {
        "category": "Backend Engineering",
        "roles": ["Java Developer", "Backend Java Engineer", "Software Engineer"],
        "skills": ["Java", "Spring Boot", "Hibernate", "REST APIs", "Kafka", "Docker", "PostgreSQL", "Maven", "JUnit", "Oracle"],
        "topics": ["payments", "api-platform", "identity"],
    },
    "frontend": {
        "category": "Frontend Engineering",
        "roles": ["Frontend Engineer", "Web Developer", "UI Engineer", "Full-stack Engineer"],
        "skills": ["TypeScript", "React", "Vue", "Next.js", "CSS", "Tailwind", "Node.js", "GraphQL", "Jest", "Playwright", "Webpack", "Storybook"],
        "topics": ["design-systems", "accessibility", "e-commerce"],
    },
    "mobile": {
        "category": "Mobile Engineering",
        "roles": ["iOS Engineer", "Android Engineer", "Mobile Engineer"],
        "skills": ["Swift", "SwiftUI", "Kotlin", "Jetpack Compose", "React Native", "Flutter", "Firebase", "REST APIs", "GraphQL", "Fastlane"],
        "topics": ["e-commerce", "payments", "accessibility"],
    },
    "devops": {
        "category": "DevOps / SRE",
        "roles": ["DevOps Engineer", "Site Reliability Engineer", "Platform Engineer", "Cloud Engineer"],
        "skills": ["Kubernetes", "Terraform", "AWS", "GCP", "Azure", "Docker", "Helm", "Prometheus", "Grafana", "Ansible", "Linux", "Bash", "Python", "GitHub Actions"],
        "topics": ["kubernetes-platform", "observability", "ci-cd"],
    },
    "ml": {
        "category": "Machine Learning",
        "roles": ["Machine Learning Engineer", "Data Scientist", "Applied Scientist", "MLOps Engineer"],
        "skills": ["Python", "PyTorch", "TensorFlow", "scikit-learn", "SQL", "Docker", "AWS", "MLflow", "Pandas", "FastAPI", "Kubernetes"],
        "topics": ["ml-ops", "recommendation", "search"],
    },
    "qa": {
        "category": "Quality Engineering",
        "roles": ["QA Engineer", "Test Automation Engineer", "SDET"],
        "skills": ["Selenium", "Playwright", "Cypress", "Python", "Java", "TypeScript", "Postman", "JMeter", "GitHub Actions", "Docker"],
        "topics": ["ci-cd", "e-commerce", "api-platform"],
    },
    "security": {
        "category": "Security Engineering",
        "roles": ["Security Engineer", "Application Security Engineer", "Penetration Tester"],
        "skills": ["Python", "Burp Suite", "OWASP", "AWS", "Kubernetes", "Terraform", "Go", "SIEM", "Linux", "OAuth 2.0"],
        "topics": ["appsec", "identity", "observability"],
    },
    "embedded": {
        "category": "Embedded Engineering",
        "roles": ["Embedded Software Engineer", "Firmware Engineer", "Robotics Software Engineer"],
        "skills": ["C", "C++", "Rust", "FreeRTOS", "ARM Cortex-M", "Linux", "Python", "CAN bus", "Zephyr", "MQTT"],
        "topics": ["embedded-firmware", "robotics", "iot"],
    },
    "product": {
        "category": "Product / Design",
        "roles": ["Product Manager", "Technical Product Manager", "UX Designer", "Engineering Manager"],
        "skills": ["SQL", "Figma", "Jira", "A/B testing", "Amplitude", "Python", "Roadmapping", "OKRs", "Agile"],
        "topics": ["e-commerce", "payments", "design-systems"],
    },
}

# canonical phrase per topic: must appear verbatim (case-insensitive) somewhere in the resume
TOPIC_PHRASE: dict[str, str] = {
    "data-pipelines": "data pipelines", "analytics": "analytics", "ml-ops": "MLOps", "observability": "observability",
    "payments": "payments", "search": "search", "identity": "authentication", "api-platform": "API platform",
    "design-systems": "design system", "accessibility": "accessibility", "e-commerce": "e-commerce",
    "kubernetes-platform": "Kubernetes", "ci-cd": "CI/CD", "recommendation": "recommendation",
    "appsec": "application security", "embedded-firmware": "firmware", "robotics": "robotics", "iot": "IoT",
}

# --- data pipelines: two evidence styles ------------------------------------
PIPELINE_TOOLS = ["Airflow", "dbt", "Spark", "Kafka Connect", "Flink", "Dagster", "Fivetran"]
PIPELINE_CANONICAL_PHRASES = ["data pipelines", "ETL"]
PIPELINE_PARAPHRASES = [
    "ingestion jobs", "nightly batch jobs that moved data from", "change data capture",
    "streaming jobs", "loading data from source systems into", "scheduled data loads",
    "moved data between systems", "replicated tables from", "backfilled historical data",
]
# forbidden for anyone who did NOT work on data pipelines (regexes, case-insensitive)
PIPELINE_FORBIDDEN = [
    r"\bpipelines?\b", r"\bETL\b", r"\bELT\b", r"\bAirflow\b", r"\bdbt\b", r"\bSpark\b", r"Kafka Connect", r"\bFlink\b",
    r"\bDagster\b", r"\bFivetran\b", r"\bingestion\b", r"change data capture", r"\bCDC\b", r"data warehouse",
    r"\bSnowflake\b", r"\bBigQuery\b", r"\bRedshift\b", r"\bdata loads?\b", r"\bbackfill",
]  # plain "Kafka" is allowed: a message broker is not pipeline work by itself
# forbidden for paraphrase-only pipeline people (they exercise the semantic channel)
PIPELINE_PARAPHRASE_FORBIDDEN = [
    r"\bpipelines?\b", r"\bETL\b", r"\bELT\b", r"\bAirflow\b", r"\bdbt\b", r"\bSpark\b", r"\bKafka\b", r"\bFlink\b",
    r"\bDagster\b", r"\bFivetran\b",
]

ELIXIR_FORBIDDEN = [r"\bElixir\b", r"\bPhoenix\b", r"\bLiveView\b", r"\bErlang\b", r"\bEcto\b", r"\bBEAM\b"]
PHOENIX_ONLY_FORBIDDEN = [r"\bElixir\b", r"\bErlang\b", r"\bBEAM\b"]
CONTROL_FORBIDDEN = [r"\bElixir\b", r"\bLiveView\b", r"\bErlang\b", r"\bEcto\b", r"Phoenix Framework", r"\bBEAM\b"]

FIRST_NAMES = [
    "Aisha", "Amara", "Anika", "Arjun", "Beatriz", "Bence", "Chidi", "Clara", "Dana", "Diego", "Elif", "Emeka", "Farah",
    "Felix", "Gabriel", "Hana", "Ines", "Ivan", "Jonas", "Kaito", "Katarzyna", "Lars", "Leila", "Luca", "Mai", "Malik",
    "Marta", "Mateo", "Mia", "Nadia", "Niko", "Nora", "Omar", "Priya", "Rafael", "Rania", "Rohan", "Sara", "Sofia",
    "Tariq", "Teodora", "Tomas", "Vera", "Wei", "Yara", "Yusuf", "Zara", "Zoe", "Anton", "Ingrid",
]
LAST_NAMES = [
    "Adeyemi", "Almeida", "Andersson", "Bauer", "Bianchi", "Chen", "Costa", "Dimitrov", "Dubois", "Fischer", "Garcia",
    "Haddad", "Horvath", "Ivanova", "Jansen", "Kim", "Kovacs", "Kumar", "Larsen", "Lindqvist", "Lopez", "Mensah",
    "Moreau", "Nakamura", "Novak", "Okafor", "Oliveira", "Park", "Petrov", "Popescu", "Rahman", "Rossi", "Schmidt",
    "Silva", "Singh", "Tanaka", "Varga", "Weber", "Yilmaz", "Zhang",
]
COMPANIES = [
    "Northwind Logistics", "Brightpath Health", "Ledgerly", "Quanta Retail", "Helios Energy", "Veltra Mobility",
    "Orbis Media", "Marlowe Insurance", "Kestrel Analytics", "Bluefern Travel", "Cobalt Payments", "Tessera Games",
    "Fjordline Shipping", "Ardent Biotech", "Nimbus Cloud Services", "Sable & Co", "Pinecrest Education", "Voltaic Labs",
    "Harbourview Bank", "Lumen Telecom", "Greenfield Agritech", "Meridian Software", "Atlas Freight", "Cinder Studios",
    "Solstice Fintech", "Ravel Marketplace", "Terrapin Robotics", "Ironwood Security", "Mosaic Health", "Zephyr Airlines",
]
LOCATIONS = [
    "Berlin, Germany", "Munich, Germany", "Hamburg, Germany", "Amsterdam, Netherlands", "Rotterdam, Netherlands",
    "London, UK", "Manchester, UK", "Paris, France", "Lyon, France", "Madrid, Spain", "Barcelona, Spain",
    "Lisbon, Portugal", "Milan, Italy", "Vienna, Austria", "Zurich, Switzerland", "Stockholm, Sweden",
    "Copenhagen, Denmark", "Warsaw, Poland", "Krakow, Poland", "Prague, Czech Republic", "Budapest, Hungary",
    "Bucharest, Romania", "Dublin, Ireland", "Tallinn, Estonia", "Helsinki, Finland",
]
AVAILABILITY = ["immediately", "2 weeks", "1 month", "3 months"]
SENIORITY_YEARS = {"junior": (1, 3), "mid": (3, 7), "senior": (7, 12), "lead": (9, 15), "principal": (12, 20)}
SENIORITY_RATE = {"junior": (40, 55), "mid": (55, 80), "senior": (75, 110), "lead": (90, 130), "principal": (100, 140)}
SENIORITY_TITLE = {"junior": "Junior ", "mid": "", "senior": "Senior ", "lead": "Lead ", "principal": "Principal "}

# --- the mix ---------------------------------------------------------------
N_PIPELINE = 110              # topic data-pipelines (ground truth exact)
N_PIPELINE_PARAPHRASE = 30    #   … of which describe it without any tool/keyword (semantic channel)
N_PIPELINE_ELIXIR = 25        #   … of which know Elixir  (17 say "Elixir", 8 only "Phoenix"+"LiveView")
N_PIPELINE_ELIXIR_PHOENIX_ONLY = 8
N_ELIXIR_DISTRACTORS = 30     # Elixir, no pipeline work (26 direct, 4 phoenix-only)
N_ELIXIR_DISTRACTOR_PHOENIX_ONLY = 4
N_NEGATIVE_CONTROLS = 6       # "University of Phoenix" ×3, "Phoenix, AZ" ×3, no Elixir
N_JAVA_FULL = 4               # Java + Spring Boot + Hibernate + REST APIs, all used
N_JAVA_PARTIAL = 3            # two of the four
N_NEAR_DUPS = 10              # second, updated version of an existing persona
N_CHEAP_STARS = 3             # pipeline people: junior/mid, strong, rate 40–50
N_EXPENSIVE_BLAND = 3         # pipeline people: senior, weak, rate 130–140


def _pick(rng: random.Random, seq: list, k: int) -> list:
    return rng.sample(seq, min(k, len(seq)))


def _base(rng: random.Random, group: str, seniority: str | None = None) -> dict:
    g = ROLE_GROUPS[group]
    sen = seniority or rng.choices(["junior", "mid", "senior", "lead", "principal"], weights=[15, 35, 30, 13, 7])[0]
    role = rng.choice(g["roles"])
    title = f"{SENIORITY_TITLE[sen]}{role}"
    if sen == "principal" and role.startswith(("Product", "Engineering")):
        title = role
    years = rng.randint(*SENIORITY_YEARS[sen])
    lo, hi = SENIORITY_RATE[sen]
    skills = _pick(rng, g["skills"], rng.randint(5, 8))
    used, listed = skills[:-2], skills[-2:]
    return {
        "group": group,
        "category": g["category"],
        "name": f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}",
        "title": title,
        "seniority": sen,
        "years": years,
        "rate": rng.randint(lo, hi),
        "currency": "EUR",
        "location": rng.choice(LOCATIONS),
        "remote": rng.random() < 0.7,
        "availability": rng.choice(AVAILABILITY),
        "quality": rng.choices(["strong", "average", "weak"], weights=[30, 50, 20])[0],
        "skills_used": used,
        "skills_listed": listed,
        "topics": _pick(rng, g["topics"], rng.randint(1, 2)),
        "topics_exact": [],
        "required": [],       # verbatim phrases (case-insensitive) that must appear
        "forbidden": [],      # regexes (case-insensitive) that must not appear
        "elixir": "none",     # none | direct | phoenix_only
        "pipeline_evidence": None,  # None | canonical | paraphrase
        "negative_control": None,
        "special": None,
        "near_dup_of": None,
        "extra_instructions": [],
        "companies": _pick(rng, COMPANIES, 3),
    }


def _make_pipeline(rng: random.Random, i: int) -> dict:
    p = _base(rng, rng.choice(["data", "data", "data", "backend", "ml"]))
    p["topics"] = ["data-pipelines"] + [t for t in p["topics"] if t != "data-pipelines"][:1]
    p["topics_exact"] = ["data-pipelines"]
    if i < N_PIPELINE_PARAPHRASE:
        p["pipeline_evidence"] = "paraphrase"
        phrases = _pick(rng, PIPELINE_PARAPHRASES, 2)
        p["required"] += phrases
        p["forbidden"] += PIPELINE_PARAPHRASE_FORBIDDEN
        p["extra_instructions"].append(
            "Describe the data-movement work ONLY with these phrases (use each verbatim at least once): "
            + "; ".join(f'"{x}"' for x in phrases)
            + ". Never use the words pipeline, ETL, ELT, Airflow, dbt, Spark, Kafka, Flink, Dagster or Fivetran."
        )
    else:
        p["pipeline_evidence"] = "canonical"
        tools = _pick(rng, PIPELINE_TOOLS, rng.randint(1, 3))
        phrase = rng.choice(PIPELINE_CANONICAL_PHRASES)
        p["required"] += [phrase] + tools
        p["skills_used"] = list(dict.fromkeys(tools + p["skills_used"]))[:8]
        p["extra_instructions"].append(
            f'Use the phrase "{phrase}" verbatim at least once, and describe hands-on work with {", ".join(tools)} in the Experience bullets.'
        )
    return p


def _apply_elixir(rng: random.Random, p: dict, phoenix_only: bool) -> None:
    p["topics_exact"] = list(dict.fromkeys(p["topics_exact"] + ["elixir"]))
    if phoenix_only:
        p["elixir"] = "phoenix_only"
        p["required"] += ["Phoenix", "LiveView"]
        p["forbidden"] += PHOENIX_ONLY_FORBIDDEN
        p["skills_used"] = list(dict.fromkeys(["Phoenix", "LiveView"] + p["skills_used"]))[:8]
        p["extra_instructions"].append(
            "The web stack in at least one role is the Phoenix Framework with LiveView; describe it by those names only. "
            "Never write the words Elixir, Erlang or BEAM anywhere."
        )
    else:
        p["elixir"] = "direct"
        p["required"] += ["Elixir"]
        p["skills_used"] = list(dict.fromkeys(["Elixir"] + p["skills_used"]))[:8]
        p["extra_instructions"].append("Elixir is a language used hands-on in at least one role (mention it in the bullets).")


def build_personas(seed: int = 42, count: int = 400) -> list[dict]:
    rng = random.Random(seed)
    personas: list[dict] = []

    # 1. data-pipeline people
    pipeline: list[dict] = [_make_pipeline(rng, i) for i in range(N_PIPELINE)]
    elixir_idx = rng.sample(range(N_PIPELINE), N_PIPELINE_ELIXIR)
    for j, idx in enumerate(elixir_idx):
        _apply_elixir(rng, pipeline[idx], phoenix_only=j < N_PIPELINE_ELIXIR_PHOENIX_ONLY)
    non_elixir_pipeline = [i for i in range(N_PIPELINE) if i not in set(elixir_idx)]
    outliers = rng.sample(non_elixir_pipeline, N_CHEAP_STARS + N_EXPENSIVE_BLAND)
    for k, idx in enumerate(outliers):
        p = pipeline[idx]
        if k < N_CHEAP_STARS:
            p.update(seniority=rng.choice(["junior", "mid"]), years=rng.randint(2, 4), rate=rng.randint(40, 50),
                     quality="strong", special="cheap_star")
            p["title"] = p["title"].replace("Senior ", "").replace("Lead ", "").replace("Principal ", "")
        else:
            p.update(seniority="senior", years=rng.randint(9, 13), rate=rng.randint(130, 140), quality="weak",
                     special="expensive_bland")
            if not p["title"].startswith("Senior "):
                p["title"] = "Senior " + p["title"].split(" ", 1)[-1] if p["title"].split(" ")[0] in ("Lead", "Principal", "Junior") else "Senior " + p["title"]
    personas += pipeline

    # 2. Elixir distractors (no pipeline work)
    for j in range(N_ELIXIR_DISTRACTORS):
        p = _base(rng, rng.choice(["backend", "backend", "frontend"]))
        _apply_elixir(rng, p, phoenix_only=j < N_ELIXIR_DISTRACTOR_PHOENIX_ONLY)
        p["forbidden"] += PIPELINE_FORBIDDEN
        personas.append(p)

    # 3. negative controls
    for j in range(N_NEGATIVE_CONTROLS):
        p = _base(rng, rng.choice(["backend", "frontend", "devops", "qa", "product"]))
        p["forbidden"] += PIPELINE_FORBIDDEN + CONTROL_FORBIDDEN
        if j % 2 == 0:
            p["negative_control"] = "university_of_phoenix"
            p["required"].append("University of Phoenix")
            p["extra_instructions"].append("The degree in the Education section is from the University of Phoenix (write that name verbatim).")
        else:
            p["negative_control"] = "phoenix_az"
            p["location"] = "Phoenix, AZ, USA"
            p["required"].append("Phoenix, AZ")
            p["extra_instructions"].append('Every job is located in "Phoenix, AZ" (write it exactly like that on each job line).')
        personas.append(p)

    # 4. the Java JD test candidates
    for j in range(N_JAVA_FULL):
        p = _base(rng, "java", seniority=rng.choice(["senior", "lead", "mid"]))
        p["skills_used"] = ["Java", "Spring Boot", "Hibernate", "REST APIs"] + [s for s in p["skills_used"] if s not in ("Java", "Spring Boot", "Hibernate", "REST APIs")][:3]
        p["skills_listed"] = [s for s in p["skills_listed"] if s not in p["skills_used"]]
        p["required"] += ["Java", "Spring Boot", "Hibernate", "REST"]
        p["special"] = "java_full"
        p["forbidden"] += PIPELINE_FORBIDDEN + ELIXIR_FORBIDDEN
        personas.append(p)
    for j in range(N_JAVA_PARTIAL):
        p = _base(rng, "java", seniority=rng.choice(["mid", "senior"]))
        # Java plus one of the other three; the remaining two are what this candidate lacks
        two = ["Java", rng.choice(["Spring Boot", "Hibernate", "REST APIs"])]
        others = [s for s in ("Spring Boot", "Hibernate", "REST APIs") if s not in two]
        p["skills_used"] = two + [s for s in p["skills_used"] if s not in ("Java", "Spring Boot", "Hibernate", "REST APIs")][:4]
        p["skills_listed"] = [s for s in p["skills_listed"] if s not in ("Java", "Spring Boot", "Hibernate", "REST APIs")]
        p["required"] += [t.replace(" APIs", "") for t in two]
        p["forbidden"] += [rf"\b{o.replace(' APIs', '')}\b" for o in others] + PIPELINE_FORBIDDEN + ELIXIR_FORBIDDEN
        p["special"] = "java_partial"
        personas.append(p)
    # B: only "web services", never REST
    b = _base(rng, "java", seniority="senior")
    b.update(title="Senior Java Developer", quality="strong", special="B")
    b["skills_used"] = ["Java", "Spring", "Hibernate", "web services", "Kafka", "PostgreSQL"]
    b["skills_listed"] = ["Maven", "JUnit"]
    b["required"] += ["Java", "Spring", "Hibernate", "web services"]
    b["forbidden"] += [r"\bREST"] + PIPELINE_FORBIDDEN + ELIXIR_FORBIDDEN
    b["extra_instructions"].append('Describe all API work as "web services" (SOAP and HTTP web services). Never use the word REST or RESTful anywhere.')
    personas.append(b)
    # E: Java/Spring/Hibernate only listed; experience is SAP consulting
    e = _base(rng, "java", seniority="senior")
    e.update(title="Senior SAP Consultant", category="Consulting", quality="average", special="E")
    e["skills_used"] = ["SAP", "SAP FI/CO", "ABAP", "SQL"]
    e["skills_listed"] = ["Java", "Spring", "Hibernate"]
    e["required"] += ["SAP", "Java", "Spring", "Hibernate"]
    e["forbidden"] += PIPELINE_FORBIDDEN + ELIXIR_FORBIDDEN
    e["extra_instructions"].append(
        "All roles are SAP functional/technical consulting (SAP FI/CO, ABAP). Java, Spring and Hibernate appear ONLY in the Skills section, never in Experience bullets."
    )
    personas.append(e)
    # D: unreadable job dates
    d = _base(rng, "java", seniority="senior")
    d.update(title="Senior Backend Engineer", quality="average", special="D")
    d["skills_used"] = ["Java", "Spring Boot", "REST APIs", "Docker", "PostgreSQL"]
    d["skills_listed"] = ["Kafka"]
    d["required"] += ["Java", "Spring Boot", "REST", "dates available on request"]
    d["forbidden"] += PIPELINE_FORBIDDEN + ELIXIR_FORBIDDEN
    d["extra_instructions"].append(
        'Write every job line as "*<Company> — <City, Country> · dates available on request*". The Experience section must contain no year and no date at all, and the resume must nowhere state a number of years of experience (the Education year is fine).'
    )
    personas.append(d)

    # 5. everyone else
    n_dups = N_NEAR_DUPS
    while len(personas) < count - n_dups:
        group = rng.choice([g for g in ROLE_GROUPS if g not in ("data",)] + ["backend", "frontend", "devops"])
        p = _base(rng, group)
        p["forbidden"] += PIPELINE_FORBIDDEN + ELIXIR_FORBIDDEN
        personas.append(p)

    # topic phrases (non-exact topics: the phrase must appear; nothing is forbidden for others)
    for p in personas:
        for t in p["topics"]:
            if t != "data-pipelines":
                p["required"].append(TOPIC_PHRASE[t])

    # 6. interleave and number
    rng.shuffle(personas)
    for i, p in enumerate(personas, 1):
        p["id"] = f"f{i:04d}"

    # 7. near-duplicates: an updated version of an existing persona, numbered after all others
    dup_parents = rng.sample([p for p in personas if p["special"] is None], n_dups)
    for j, parent in enumerate(dup_parents):
        child = json.loads(json.dumps(parent))
        child["id"] = f"f{len(personas) + 1:04d}"
        child["near_dup_of"] = parent["id"]
        child["years"] = parent["years"] + 1
        child["rate"] = round(parent["rate"] * 1.1)
        child["availability"] = rng.choice(AVAILABILITY)
        child["extra_instructions"] = [
            "This is the same person's resume one year later. Keep the text of the earlier version essentially unchanged "
            "(at least 90% of sentences identical) and add ONE new, most recent role on top with 3–4 bullets."
        ]
        personas.append(child)

    return personas


def lint(personas: list[dict]) -> list[str]:
    """Self-consistency: nothing a persona must contain may match something it must not contain."""
    import re

    problems: list[str] = []
    ids = set()
    for p in personas:
        if p["id"] in ids:
            problems.append(f"{p['id']}: duplicate id")
        ids.add(p["id"])
        must = list(p["required"]) + list(p["skills_used"]) + list(p["skills_listed"]) + [p["title"], p["location"]]
        for pat in p["forbidden"]:
            for m in must:
                if re.search(pat, m, re.I):
                    problems.append(f"{p['id']}: must contain {m!r} but forbids {pat!r}")
        for s in p["skills_listed"]:
            if s in p["skills_used"]:
                problems.append(f"{p['id']}: {s!r} is both used and listed-only")
        if p["near_dup_of"] and p["near_dup_of"] not in ids:
            problems.append(f"{p['id']}: near_dup_of {p['near_dup_of']} must come earlier")
    return problems


def ground_truth(personas: list[dict]) -> dict:
    docs = {}
    for p in personas:
        docs[p["id"]] = {
            "topics": p["topics"],
            "topics_exact": p["topics_exact"],
            "skills_used": p["skills_used"],
            "skills_listed": p["skills_listed"],
            "elixir": p["elixir"],
            "pipeline": "data-pipelines" in p["topics"],
            "pipeline_evidence": p["pipeline_evidence"],
            "negative_control": p["negative_control"],
            "special": p["special"],
            "near_dup_of": p["near_dup_of"],
            "quality": p["quality"],
            "seniority": p["seniority"],
            "years": p["years"],
            "rate": p["rate"],
        }
    summary = {
        "total": len(personas),
        "data_pipelines": sum(1 for d in docs.values() if d["pipeline"]),
        "data_pipelines_paraphrase_only": sum(1 for d in docs.values() if d["pipeline_evidence"] == "paraphrase"),
        "elixir": sum(1 for d in docs.values() if d["elixir"] != "none"),
        "elixir_phoenix_only": sum(1 for d in docs.values() if d["elixir"] == "phoenix_only"),
        "elixir_and_pipelines": sum(1 for d in docs.values() if d["elixir"] != "none" and d["pipeline"]),
        "negative_controls": sum(1 for d in docs.values() if d["negative_control"]),
        "near_duplicates": sum(1 for d in docs.values() if d["near_dup_of"]),
        "specials": sorted({d["special"] for d in docs.values() if d["special"]}),
    }
    return {"summary": summary, "docs": docs}


def write(personas: list[dict], fixture_dir: Path) -> dict:
    problems = lint(personas)
    if problems:
        raise ValueError("persona spec is inconsistent:\n" + "\n".join(problems))
    fixture_dir.mkdir(parents=True, exist_ok=True)
    with (fixture_dir / "personas.jsonl").open("w", encoding="utf-8") as f:
        for p in personas:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    gt = ground_truth(personas)
    (fixture_dir / "ground_truth.json").write_text(json.dumps(gt, indent=1, ensure_ascii=False), encoding="utf-8")
    return gt["summary"]


def read(fixture_dir: Path) -> list[dict]:
    path = fixture_dir / "personas.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
