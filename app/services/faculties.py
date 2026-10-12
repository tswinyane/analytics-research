from __future__ import annotations

from collections import Counter
import re

from app.models import Work


FACULTY_RULES: list[tuple[str, tuple[str, ...]]] = [
    (
        "Economic and Management Sciences",
        (
            "economics",
            "economic",
            "management",
            "business",
            "accounting",
            "finance",
            "marketing",
            "human resource",
            "entrepreneur",
            "commerce",
            "mba",
        ),
    ),
    (
        "Education",
        (
            "education",
            "teaching",
            "learning",
            "curriculum",
            "school",
            "pedagog",
            "classroom",
            "teacher",
            "student achievement",
        ),
    ),
    (
        "Engineering, Built Environment and Information Technology",
        (
            "engineering",
            "built environment",
            "architecture",
            "construction",
            "civil",
            "mechanical",
            "electrical",
            "mining",
            "information technology",
            "computer science",
            "informatics",
            "ebit",
            "geospatial",
        ),
    ),
    (
        "Health Sciences",
        (
            "medicine",
            "medical",
            "health",
            "clinical",
            "hospital",
            "nursing",
            "public health",
            "pharmacy",
            "physiology",
            "dentistry",
            "epidemiology",
            "patient",
        ),
    ),
    (
        "Humanities",
        (
            "humanities",
            "history",
            "philosophy",
            "language",
            "literature",
            "sociology",
            "politics",
            "psychology",
            "anthropology",
            "music",
            "drama",
            "heritage",
        ),
    ),
    (
        "Law",
        (
            "law",
            "legal",
            "jurisprudence",
            "constitution",
            "constitutional",
            "human rights",
            "criminal justice",
            "regulation",
            "statutory",
        ),
    ),
    (
        "Natural and Agricultural Sciences",
        (
            "agric",
            "biology",
            "biological",
            "chemistry",
            "physics",
            "mathematics",
            "ecology",
            "geology",
            "geography",
            "plant",
            "crop",
            "soil",
            "animal science",
            "food science",
            "environmental science",
        ),
    ),
    (
        "Theology and Religion",
        (
            "theology",
            "religion",
            "religious",
            "biblical",
            "church",
            "faith",
            "missiology",
            "spiritual",
        ),
    ),
    (
        "Veterinary Science",
        (
            "veterinary",
            "veterinarian",
            "animal health",
            "livestock disease",
            "zoonotic",
            "equine",
            "canine",
            "bovine",
        ),
    ),
]

DEPARTMENT_RULES: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    "Economic and Management Sciences": [
        ("Accounting", ("accounting", "auditing", "taxation")),
        ("Economics", ("economics", "econometric", "macroeconomic", "microeconomic")),
        ("Business Management", ("business", "entrepreneur", "strategy", "innovation")),
        ("Human Resource Management", ("human resource", "labour", "organizational behaviour")),
        ("Marketing Management", ("marketing", "consumer", "brand")),
        ("Finance", ("finance", "banking", "investment")),
    ],
    "Education": [
        ("Science, Mathematics and Technology Education", ("mathematics education", "science education", "technology education", "stem education")),
        ("Humanities Education", ("language education", "history education", "social science education")),
        ("Educational Psychology", ("educational psychology", "learner support", "school psychology")),
        ("Early Childhood Education", ("early childhood", "foundation phase", "pre-school")),
    ],
    "Engineering, Built Environment and Information Technology": [
        ("Computer Science", ("computer science", "software", "machine learning", "artificial intelligence")),
        ("Informatics", ("informatics", "information systems", "business intelligence")),
        ("Electrical, Electronic and Computer Engineering", ("electrical", "electronic", "signal processing", "telecommunication")),
        ("Civil Engineering", ("civil engineering", "transport", "water resources", "structural")),
        ("Mechanical and Aeronautical Engineering", ("mechanical engineering", "aerodynamic", "thermodynamic")),
        ("Architecture", ("architecture", "urban design", "built environment", "housing")),
        ("Construction Economics", ("construction", "quantity surveying", "project management")),
        ("Mining Engineering", ("mining", "mineral", "rock engineering")),
    ],
    "Health Sciences": [
        ("Medicine", ("medicine", "medical", "clinical", "physician")),
        ("Public Health", ("public health", "epidemiology", "population health")),
        ("Nursing Science", ("nursing", "midwifery", "patient care")),
        ("Pharmacy", ("pharmacy", "pharmacology", "drug")),
        ("Physiology", ("physiology", "biomedical", "cellular function")),
        ("Dentistry", ("dentistry", "dental", "oral health")),
    ],
    "Humanities": [
        ("Psychology", ("psychology", "psychological", "mental health")),
        ("Sociology", ("sociology", "social inequality", "community studies")),
        ("History and Heritage Studies", ("history", "heritage", "archive", "museum")),
        ("Philosophy", ("philosophy", "ethics", "metaphysics")),
        ("Political Sciences", ("politics", "governance", "democracy", "public policy")),
        ("Languages and Literature", ("language", "literature", "linguistic", "translation")),
    ],
    "Law": [
        ("Public Law", ("constitutional", "administrative law", "public law", "human rights")),
        ("Private Law", ("private law", "contract", "delict", "family law")),
        ("Mercantile Law", ("mercantile", "commercial law", "corporate law")),
        ("Procedural Law", ("criminal justice", "procedure", "litigation")),
    ],
    "Natural and Agricultural Sciences": [
        ("Biochemistry, Genetics and Microbiology", ("biochemistry", "genetics", "microbiology", "molecular")),
        ("Plant and Soil Sciences", ("plant", "soil", "crop", "agronomy")),
        ("Animal Science", ("animal science", "livestock", "ruminant", "poultry")),
        ("Geography, Geoinformatics and Meteorology", ("geography", "gis", "geoinformatics", "meteorology")),
        ("Mathematics and Applied Mathematics", ("mathematics", "applied mathematics", "statistics")),
        ("Physics", ("physics", "quantum", "particle", "optics")),
        ("Chemistry", ("chemistry", "chemical", "organic synthesis")),
    ],
    "Theology and Religion": [
        ("Systematic and Historical Theology", ("systematic theology", "historical theology", "doctrine")),
        ("Biblical and Religious Studies", ("biblical", "scripture", "religious studies")),
        ("Practical Theology and Mission Studies", ("practical theology", "mission", "church ministry")),
    ],
    "Veterinary Science": [
        ("Companion Animal Clinical Studies", ("canine", "feline", "companion animal")),
        ("Production Animal Studies", ("bovine", "ovine", "production animal", "livestock")),
        ("Paraclinical Sciences", ("veterinary pathology", "veterinary parasitology", "veterinary pharmacology")),
        ("Veterinary Tropical Diseases", ("tropical disease", "zoonotic", "tick-borne", "veterinary tropical")),
    ],
}


def infer_faculty(work: Work) -> str:
    scored = score_faculties(work)
    if not scored:
        return "Unmapped"
    return scored[0][0]


def infer_department(work: Work, faculty: str | None = None) -> str:
    selected_faculty = faculty or infer_faculty(work)
    scored = score_departments(work, selected_faculty)
    if not scored:
        return "Unmapped"
    return scored[0][0]


def classify_work(work: Work) -> tuple[str, str]:
    text = _haystack(work)
    faculty_scores = _score_faculties_text(text)
    faculty = faculty_scores[0][0] if faculty_scores else "Unmapped"
    department_scores = _score_departments_text(text, faculty)
    department = department_scores[0][0] if department_scores else "Unmapped"
    return faculty, department


def classify_record(
    *,
    title: str | None = None,
    journal: str | None = None,
    publisher: str | None = None,
    authors: list[str] | None = None,
    affiliations: list[str] | None = None,
    keywords: list[str] | None = None,
    abstract: str | None = None,
) -> tuple[str, str]:
    text = _compose_haystack(
        title=title,
        abstract=abstract,
        journal=journal,
        publisher=publisher,
        authors=authors,
        affiliations=affiliations,
        keywords=keywords,
    )
    faculty_scores = _score_faculties_text(text)
    faculty = faculty_scores[0][0] if faculty_scores else "Unmapped"
    department_scores = _score_departments_text(text, faculty)
    department = department_scores[0][0] if department_scores else "Unmapped"
    return faculty, department


def score_faculties(work: Work) -> list[tuple[str, int]]:
    text = _haystack(work)
    return _score_faculties_text(text)


def _score_faculties_text(text: str) -> list[tuple[str, int]]:
    scores: Counter[str] = Counter()
    for faculty, keywords in FACULTY_RULES:
        for keyword in keywords:
            if _keyword_in_text(keyword, text):
                scores[faculty] += 1
    return scores.most_common()


def score_departments(work: Work, faculty: str | None = None) -> list[tuple[str, int]]:
    text = _haystack(work)
    return _score_departments_text(text, faculty)


def _score_departments_text(text: str, faculty: str | None = None) -> list[tuple[str, int]]:
    scores: Counter[str] = Counter()
    rules = DEPARTMENT_RULES.get(faculty or "", [])
    for department, keywords in rules:
        for keyword in keywords:
            if _keyword_in_text(keyword, text):
                scores[department] += 1
    return scores.most_common()


def faculty_options() -> list[str]:
    return [faculty for faculty, _ in FACULTY_RULES]


def department_options(faculty: str | None = None) -> list[str]:
    if faculty and faculty != "All faculties":
        return [department for department, _ in DEPARTMENT_RULES.get(faculty, [])]

    departments: list[str] = []
    for items in DEPARTMENT_RULES.values():
        departments.extend([department for department, _ in items])
    return sorted(dict.fromkeys(departments))


def _haystack(work: Work) -> str:
    return _compose_haystack(
        title=work.title,
        abstract=work.abstract,
        journal=work.journal,
        publisher=work.publisher,
        authors=work.authors,
        affiliations=work.affiliations,
        keywords=work.keywords,
    )


def _compose_haystack(
    *,
    title: str | None = None,
    abstract: str | None = None,
    journal: str | None = None,
    publisher: str | None = None,
    authors: list[str] | None = None,
    affiliations: list[str] | None = None,
    keywords: list[str] | None = None,
) -> str:
    haystacks = [
        title or "",
        abstract or "",
        journal or "",
        publisher or "",
        " ".join(authors or []),
        " ".join(affiliations or []),
        " ".join(keywords or []),
    ]
    return " ".join(haystacks).lower()


def _keyword_in_text(keyword: str, text: str) -> bool:
    # Match complete words/phrases so "economic" does not fire inside "socioeconomic".
    pattern = rf"(?<![a-z0-9]){re.escape(keyword.lower())}(?![a-z0-9])"
    return re.search(pattern, text) is not None
