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
        ("Auditing", ('auditing', 'internal audit')),
        ("Taxation", ('taxation', 'tax law')),
        ("Tourism Management", ('tourism', 'hospitality')),
        ("Public Management and Governance", ('public management', 'public administration', 'school of public management')),
    ],
    "Education": [
        ("Science, Mathematics and Technology Education", ("mathematics education", "science education", "technology education", "stem education")),
        ("Humanities Education", ("language education", "history education", "social science education")),
        ("Educational Psychology", ("educational psychology", "learner support", "school psychology")),
        ("Early Childhood Education", ("early childhood", "foundation phase", "pre-school")),
        ("Education Management and Policy Studies", ('education management', 'education policy', 'school leadership')),
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
        ("Chemical Engineering", ('chemical engineering', 'process engineering')),
        ("Industrial and Systems Engineering", ('industrial engineering', 'systems engineering', 'operations research')),
        ("Materials Science and Metallurgical Engineering", ('metallurgical engineering', 'metallurgy', 'materials science')),
        ("Town and Regional Planning", ('town and regional planning', 'urban planning', 'regional planning')),
        ("Information Science", ('information science', 'library science', 'publishing')),
        ("Engineering and Technology Management", ('engineering management', 'technology management')),
    ],
    "Health Sciences": [
        ("Medicine", ("medicine", "medical", "clinical", "physician")),
        ("Public Health", ("public health", "epidemiology", "population health")),
        ("Nursing Science", ("nursing", "midwifery", "patient care")),
        ("Pharmacy", ("pharmacy", "pharmacology", "drug")),
        ("Physiology", ("physiology", "biomedical", "cellular function")),
        ("Dentistry", ("dentistry", "dental", "oral health")),
        ("Occupational Therapy", ('occupational therapy',)),
        ("Physiotherapy", ('physiotherapy', 'rehabilitation')),
        ("Human Nutrition", ('human nutrition', 'dietetics')),
        ("Radiography", ('radiography',)),
    ],
    "Humanities": [
        ("Psychology", ("psychology", "psychological", "mental health")),
        ("Sociology", ("sociology", "social inequality", "community studies")),
        ("History and Heritage Studies", ("history", "heritage", "archive", "museum")),
        ("Philosophy", ("philosophy", "ethics", "metaphysics")),
        ("Political Sciences", ("politics", "governance", "democracy", "public policy")),
        ("Languages and Literature", ("language", "literature", "linguistic", "translation")),
        ("Anthropology and Archaeology", ('anthropology', 'archaeology')),
        ("Social Work and Criminology", ('social work', 'criminology')),
        ("Speech-Language Pathology and Audiology", ('speech-language pathology', 'audiology', 'speech therapy')),
        ("Visual Arts", ('visual arts', 'fine arts')),
        ("Music", ('music', 'musicology')),
        ("Drama", ('drama', 'theatre')),
    ],
    "Law": [
        ("Public Law", ("constitutional", "administrative law", "public law", "human rights")),
        ("Private Law", ("private law", "contract", "delict", "family law")),
        ("Mercantile Law", ("mercantile", "commercial law", "corporate law")),
        ("Procedural Law", ("criminal justice", "procedure", "litigation")),
        ("Jurisprudence", ('jurisprudence', 'legal philosophy')),
    ],
    "Natural and Agricultural Sciences": [
        ("Biochemistry, Genetics and Microbiology", ("biochemistry", "genetics", "microbiology", "molecular")),
        ("Plant and Soil Sciences", ("plant", "soil", "crop", "agronomy")),
        ("Animal Science", ("animal science", "livestock", "ruminant", "poultry")),
        ("Geography, Geoinformatics and Meteorology", ("geography", "gis", "geoinformatics", "meteorology")),
        ("Mathematics and Applied Mathematics", ("mathematics", "applied mathematics")),
        ("Physics", ("physics", "quantum", "particle", "optics")),
        ("Chemistry", ("chemistry", "chemical", "organic synthesis")),
        ("Zoology and Entomology", ('zoology', 'entomology', 'insect')),
        ("Geology", ('geology', 'petrology', 'mineralogy')),
        ("Statistics", ('statistics', 'statistical')),
        ("Consumer and Food Sciences", ('food science', 'consumer science', 'food technology')),
        ("Agricultural Economics, Extension and Rural Development", ('agricultural economics', 'rural development', 'agricultural extension')),
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
        ("Anatomy and Physiology", ('veterinary anatomy', 'veterinary physiology')),
    ],
}


# ---------------------------------------------------------------------------
# Matching
#
# Evidence has three strengths, and the strongest wins:
#   * explicit faculty / school names ("Faculty of Health Sciences", "School of Medicine",
#     "Onderstepoort")                                                    -> 100
#   * UP department, school and institute names ("Department of Psychiatry", "FABI",
#     "Plant and Soil Sciences", every DEPARTMENT_RULES label)              -> 10
#   * loose subject keywords from FACULTY_RULES ("medicine", "education")   -> 1
# Phrases are matched longest-first and a matched span is consumed, so "School of Medicine"
# never also counts as "school", "Medical Microbiology" never also counts as
# "microbiology", and "machine learning" never counts as "learning". A trailing "*" marks a
# word stem ("agric*" matches "agricultural").
# ---------------------------------------------------------------------------

EXPLICIT_WEIGHT = 100
UNIT_WEIGHT = 10
KEYWORD_WEIGHT = 1

FACULTY_EXPLICIT: dict[str, tuple[str, ...]] = {
    "Economic and Management Sciences": (
        "faculty of economic and management sciences",
        "economic and management sciences",
        "gordon institute of business science",
        "gibs",
        "school of public management and administration",
        "school of public management, administration and policy",
    ),
    "Education": ("faculty of education",),
    "Engineering, Built Environment and Information Technology": (
        "faculty of engineering, built environment and information technology",
        "faculty of engineering built environment and information technology",
        "engineering, built environment and information technology",
        "faculty of engineering",
        "ebit",
        "school of engineering",
        "school for the built environment",
        "school of information technology",
        "graduate school of technology management",
    ),
    "Health Sciences": (
        "faculty of health sciences",
        "school of medicine",
        "school of dentistry",
        "school of health care sciences",
        "school of healthcare sciences",
        "school of health systems and public health",
        "steve biko academic hospital",
        "kalafong",
        "weskoppies",
    ),
    "Humanities": ("faculty of humanities",),
    "Law": ("faculty of law",),
    "Natural and Agricultural Sciences": (
        "faculty of natural and agricultural sciences",
        "natural and agricultural sciences",
        "forestry and agricultural biotechnology institute",
        "fabi",
    ),
    "Theology and Religion": ("faculty of theology and religion", "faculty of theology"),
    "Veterinary Science": (
        "faculty of veterinary science",
        "onderstepoort",
        "veterinary academic hospital",
    ),
}

FACULTY_UNITS: dict[str, tuple[str, ...]] = {
    "Economic and Management Sciences": (
        "accounting", "auditing", "taxation", "financial management", "economics",
        "human resource management", "marketing management", "business management",
        "tourism management", "public management", "public administration",
        "industrial psychology", "actuarial science", "business science",
    ),
    "Education": (
        "early childhood education", "educational psychology",
        "education management and policy studies", "education management",
        "humanities education", "science, mathematics and technology education",
        "science mathematics and technology education", "mathematics education",
        "science education", "teacher education",
    ),
    "Engineering, Built Environment and Information Technology": (
        "chemical engineering", "civil engineering", "electrical engineering",
        "electrical, electronic and computer engineering", "electronic engineering",
        "computer engineering", "industrial and systems engineering", "industrial engineering",
        "materials science and metallurgical engineering", "metallurgical engineering",
        "mechanical and aeronautical engineering", "mechanical engineering",
        "aeronautical engineering", "mining engineering", "architecture",
        "construction economics", "quantity surveying", "town and regional planning",
        "computer science", "informatics", "information science",
        "engineering and technology management", "technology management",
        "carl and emily fuchs institute", "built environment",
    ),
    "Health Sciences": (
        "anaesthesiology", "anesthesiology", "anatomical pathology", "chemical pathology",
        "clinical epidemiology", "dermatology", "family medicine", "forensic medicine",
        "haematology", "hematology", "human nutrition", "immunology", "internal medicine",
        "medical microbiology", "medical oncology", "medical virology", "neurology",
        "neurosurgery", "nuclear medicine", "nursing science", "nursing",
        "obstetrics and gynaecology", "obstetrics", "gynaecology", "gynecology",
        "occupational therapy", "ophthalmology", "orthopaedics", "orthopaedic surgery",
        "otorhinolaryngology", "paediatrics and child health", "paediatrics", "pediatrics",
        "pharmacology", "physiology", "physiotherapy", "psychiatry", "public health",
        "radiation oncology", "radiography", "radiology", "surgery", "urology", "medical physics",
        "cardiology", "cardiothoracic surgery", "plastic surgery", "oral pathology",
        "periodontics", "prosthodontics", "orthodontics", "odontology", "maxillofacial",
        "community dentistry", "sports medicine", "medical research council", "samrc",
        "anatomy",
    ),
    "Humanities": (
        "afrikaans", "african languages", "ancient and modern languages", "english",
        "historical and heritage studies", "history", "philosophy", "political sciences",
        "political science", "psychology", "sociology", "anthropology and archaeology",
        "anthropology", "archaeology", "social work and criminology", "social work",
        "criminology", "speech-language pathology and audiology",
        "speech-language pathology", "audiology", "visual arts", "music", "drama",
        "augmentative and alternative communication", "journalism", "information design",
    ),
    "Law": (
        "centre for human rights", "jurisprudence", "mercantile law", "private law",
        "procedural law", "public law", "international and comparative law",
        "law school", "school of law",
    ),
    "Natural and Agricultural Sciences": (
        "biochemistry, genetics and microbiology", "biochemistry", "genetics",
        "microbiology", "zoology and entomology", "zoology", "entomology",
        "plant and soil sciences", "plant sciences", "plant pathology", "soil science",
        "animal science", "animal and wildlife sciences", "chemistry", "physics",
        "mathematics and applied mathematics", "applied mathematics", "mathematics",
        "statistics", "geography, geoinformatics and meteorology", "geography",
        "geoinformatics", "meteorology", "geology", "consumer and food sciences",
        "food science", "agricultural economics", "agricultural economics, extension and rural development",
        "mammal research institute", "centre for wildlife management",
        "institute for food, nutrition and well-being", "forestry", "botany",
    ),
    "Theology and Religion": (
        "new testament", "old testament", "church history", "practical theology",
        "systematic theology", "dogmatics", "science of religion and missiology",
        "missiology", "religion studies", "biblical and religious studies",
    ),
    "Veterinary Science": (
        "companion animal clinical studies", "production animal studies",
        "paraclinical sciences", "veterinary tropical diseases", "anatomy and physiology",
        "veterinary", "veterinary public health", "veterinary pathology",
        "veterinary parasitology", "veterinary pharmacology", "veterinary anatomy",
    ),
}

# Loose keywords that were too ambiguous to keep (they fired on other faculties' units):
# "school" (School of Medicine), "learning" (machine learning), "teaching" (teaching
# hospital), "regulation" (gene regulation), "civil"/"mechanical"/"mining" (civil society,
# quantum mechanical, data mining) -- the specific phrases now live in FACULTY_UNITS.
_DROPPED_KEYWORDS = {"school", "learning", "teaching", "regulation", "civil", "mechanical", "mining"}
_STEM_KEYWORDS = {"agric", "pedagog", "entrepreneur"}

# Extra names that point to an existing department label (department label -> aliases).
DEPARTMENT_ALIASES: dict[str, tuple[str, ...]] = {
    "Medicine": (
        "school of medicine", "internal medicine", "family medicine", "psychiatry",
        "paediatrics", "pediatrics", "surgery", "anaesthesiology", "anesthesiology",
        "obstetrics", "gynaecology", "gynecology", "ophthalmology", "radiology", "urology",
        "neurology", "neurosurgery", "oncology", "haematology", "hematology", "immunology",
        "medical microbiology", "medical virology", "chemical pathology",
        "anatomical pathology", "forensic medicine", "orthopaedics", "otorhinolaryngology",
        "dermatology", "cardiology", "nuclear medicine", "anatomy",
    ),
    "Public Health": ("school of health systems and public health", "clinical epidemiology"),
    "Nursing Science": ("nursing science",),
    "Physiology": ("physiology",),
    "Pharmacy": ("pharmacology",),
    "Dentistry": (
        "school of dentistry", "oral pathology", "periodontics", "prosthodontics",
        "orthodontics", "odontology", "maxillofacial", "community dentistry",
    ),
    "Psychology": ("psychology",),
    "Political Sciences": ("political science", "political sciences"),
    "History and Heritage Studies": ("historical and heritage studies",),
    "Languages and Literature": ("afrikaans", "african languages", "ancient and modern languages", "english"),
    "Biochemistry, Genetics and Microbiology": ("biochemistry", "genetics", "microbiology"),
    "Plant and Soil Sciences": ("plant and soil sciences", "plant sciences", "plant pathology", "soil science"),
    "Mathematics and Applied Mathematics": ("mathematics and applied mathematics", "applied mathematics"),
    "Geography, Geoinformatics and Meteorology": ("geography", "geoinformatics", "meteorology"),
    "Animal Science": ("animal and wildlife sciences",),
    "Electrical, Electronic and Computer Engineering": (
        "electrical engineering", "electronic engineering", "computer engineering",
    ),
    "Civil Engineering": ("civil engineering",),
    "Mechanical and Aeronautical Engineering": ("mechanical engineering", "aeronautical engineering"),
    "Mining Engineering": ("mining engineering",),
    "Computer Science": ("computer science",),
    "Informatics": ("informatics",),
    "Construction Economics": ("construction economics", "quantity surveying"),
    "Public Law": ("centre for human rights",),
    "Biblical and Religious Studies": ("new testament", "old testament"),
    "Systematic and Historical Theology": ("church history", "dogmatics", "systematic theology"),
    "Practical Theology and Mission Studies": ("missiology", "science of religion and missiology"),
    "Early Childhood Education": ("early childhood education",),
    "Educational Psychology": ("educational psychology",),
    "Science, Mathematics and Technology Education": (
        "science mathematics and technology education", "mathematics education", "science education",
    ),
    "Humanities Education": ("humanities education",),
    "Accounting": ("accounting",),
    "Economics": ("economics",),
    "Human Resource Management": ("human resource management", "industrial psychology"),
    "Marketing Management": ("marketing management",),
    "Business Management": ("business management",),
    "Finance": ("financial management",),
}


def _phrase_pattern(phrase: str) -> str:
    phrase = phrase.lower().strip()
    if phrase.endswith("*"):
        return re.escape(phrase[:-1]) + r"[a-z]*"
    return re.escape(phrase)


def _build_matcher(entries: list[tuple[str, str, int]]):
    """entries: (phrase, label, weight). Returns (compiled regex, phrase -> (label, weight))."""
    lookup: dict[str, tuple[str, int]] = {}
    for phrase, label, weight in entries:
        key = phrase.lower().strip()
        existing = lookup.get(key)
        # If two faculties list the same phrase, the stronger tier wins; equal -> first.
        if existing is None or weight > existing[1]:
            lookup[key] = (label, weight)
    # Longest first so the regex alternation prefers the most specific phrase.
    ordered = sorted(lookup, key=lambda value: (-len(value.rstrip("*")), value))
    pattern = r"(?<![a-z0-9])(" + "|".join(_phrase_pattern(p) for p in ordered) + r")(?![a-z0-9])"
    stems = [p for p in ordered if p.endswith("*")]
    return re.compile(pattern), lookup, stems


def _faculty_entries() -> list[tuple[str, str, int]]:
    entries: list[tuple[str, str, int]] = []
    for faculty, phrases in FACULTY_EXPLICIT.items():
        entries.extend((phrase, faculty, EXPLICIT_WEIGHT) for phrase in phrases)
    for faculty, phrases in FACULTY_UNITS.items():
        entries.extend((phrase, faculty, UNIT_WEIGHT) for phrase in phrases)
    for faculty, departments in DEPARTMENT_RULES.items():
        entries.extend((department, faculty, UNIT_WEIGHT) for department, _ in departments)
    for faculty, keywords in FACULTY_RULES:
        for keyword in keywords:
            if keyword in _DROPPED_KEYWORDS:
                continue
            phrase = f"{keyword}*" if keyword in _STEM_KEYWORDS else keyword
            entries.append((phrase, faculty, KEYWORD_WEIGHT))
    return entries


def _department_entries(faculty: str) -> list[tuple[str, str, int]]:
    entries: list[tuple[str, str, int]] = []
    for department, keywords in DEPARTMENT_RULES.get(faculty, []):
        entries.append((department, department, UNIT_WEIGHT))
        entries.extend((alias, department, UNIT_WEIGHT) for alias in DEPARTMENT_ALIASES.get(department, ()))
        entries.extend((keyword, department, KEYWORD_WEIGHT) for keyword in keywords)
    return entries


_FACULTY_MATCHER = _build_matcher(_faculty_entries())
_DEPARTMENT_MATCHERS = {
    faculty: _build_matcher(_department_entries(faculty)) for faculty in DEPARTMENT_RULES
}
_FACULTY_ORDER = {faculty: index for index, (faculty, _) in enumerate(FACULTY_RULES)}


def _resolve_phrase(matched: str, lookup: dict, stems: list[str]) -> tuple[str, int] | None:
    hit = lookup.get(matched)
    if hit is not None:
        return hit
    for stem in stems:
        if matched.startswith(stem[:-1]):
            return lookup[stem]
    return None


def _score_with(
    matcher,
    affiliation_text: str,
    content_text: str = "",
    order: dict[str, int] | None = None,
) -> list[tuple[str, int]]:
    """Affiliation text gets tiered weights; title/abstract/keyword text counts every
    phrase at keyword weight, so a title mentioning "history" or "physics" is only a hint,
    while "Department of History" in an affiliation is strong evidence."""
    regex, lookup, stems = matcher
    scores: Counter[str] = Counter()
    strongest: Counter[str] = Counter()
    for text, flat in ((affiliation_text, False), (content_text, True)):
        if not text:
            continue
        seen: set[str] = set()
        for match in regex.finditer(text):
            matched = match.group(1)
            if matched in seen:  # each distinct phrase counts once per text, as before
                continue
            seen.add(matched)
            resolved = _resolve_phrase(matched, lookup, stems)
            if resolved is None:
                continue
            label, weight = resolved
            if flat:
                weight = KEYWORD_WEIGHT
            scores[label] += weight
            strongest[label] = max(strongest[label], weight)
    order = order or {}
    ranked = sorted(
        scores.items(),
        key=lambda item: (-item[1], -strongest[item[0]], order.get(item[0], len(order))),
    )
    return ranked


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
    return _classify_parts(*_haystack_parts(work))


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
    return _classify_parts(
        *_compose_parts(
            title=title,
            abstract=abstract,
            journal=journal,
            publisher=publisher,
            affiliations=affiliations,
            keywords=keywords,
        )
    )


def _classify_parts(affiliation_text: str, content_text: str) -> tuple[str, str]:
    faculty_scores = _score_faculties_text(affiliation_text, content_text)
    faculty = faculty_scores[0][0] if faculty_scores else "Unmapped"
    department_scores = _score_departments_text(affiliation_text, faculty, content_text)
    department = department_scores[0][0] if department_scores else "Unmapped"
    return faculty, department


def score_faculties(work: Work) -> list[tuple[str, int]]:
    return _score_faculties_text(*_haystack_parts(work))


def _score_faculties_text(affiliation_text: str, content_text: str = "") -> list[tuple[str, int]]:
    return _score_with(_FACULTY_MATCHER, affiliation_text, content_text, _FACULTY_ORDER)


def score_departments(work: Work, faculty: str | None = None) -> list[tuple[str, int]]:
    affiliation_text, content_text = _haystack_parts(work)
    return _score_departments_text(affiliation_text, faculty, content_text)


def _score_departments_text(
    affiliation_text: str,
    faculty: str | None = None,
    content_text: str = "",
) -> list[tuple[str, int]]:
    matcher = _DEPARTMENT_MATCHERS.get(faculty or "")
    if matcher is None:
        return []
    order = {department: index for index, (department, _) in enumerate(DEPARTMENT_RULES.get(faculty or "", []))}
    return _score_with(matcher, affiliation_text, content_text, order)


def faculty_options() -> list[str]:
    return [faculty for faculty, _ in FACULTY_RULES]


def department_options(faculty: str | None = None) -> list[str]:
    if faculty and faculty != "All faculties":
        return [department for department, _ in DEPARTMENT_RULES.get(faculty, [])]

    departments: list[str] = []
    for items in DEPARTMENT_RULES.values():
        departments.extend([department for department, _ in items])
    return sorted(dict.fromkeys(departments))


def _haystack_parts(work: Work) -> tuple[str, str]:
    return _compose_parts(
        title=work.title,
        abstract=work.abstract,
        journal=work.journal,
        publisher=work.publisher,
        affiliations=work.affiliations,
        keywords=work.keywords,
    )


def _compose_parts(
    *,
    title: str | None = None,
    abstract: str | None = None,
    journal: str | None = None,
    publisher: str | None = None,
    affiliations: list[str] | None = None,
    keywords: list[str] | None = None,
) -> tuple[str, str]:
    """(affiliation text, content text). Author names carry no faculty signal, so they
    are left out (a surname like "Law" or "School" used to score a faculty)."""
    affiliation_text = " ; ".join(str(value) for value in (affiliations or []) if value).lower()
    content_text = " ".join(
        [
            title or "",
            abstract or "",
            journal or "",
            publisher or "",
            " ".join(str(value) for value in (keywords or []) if value),
        ]
    ).lower()
    return affiliation_text, content_text


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
