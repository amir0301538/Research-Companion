"""Reference data used only to enrich, never to decide scrape eligibility.

The HEI catalog may canonicalize a confident match.  An affiliation outside
the catalog is retained as source-observed text with optional metadata empty;
it is not rejected or mapped to a convenient known university.

Research-area taxonomy and manual-review classifiers are intentionally absent.
Research interests are stored regardless of discipline and a new area's
``is_computing_area`` value remains empty unless an existing stored row already
contains an explicit classification.
"""

import re
from dataclasses import dataclass
from typing import Optional

from .id_registry import normalize_text


@dataclass(frozen=True)
class CanonicalHEI:
    hei_name: str
    short_name: str
    default_city: str
    country: str
    website_url: str
    target_university: str   # "1" or "0"
    aliases: tuple[str, ...] = ()
    supported_cities: tuple[str, ...] = ()


# Curated catalog of verified Higher Education Institutions (Pakistani HEC
# computing universities and project target universities).
DEFAULT_HEI_CATALOG: tuple[CanonicalHEI, ...] = (
    CanonicalHEI(
        hei_name="Hamdard University",
        short_name="HU",
        default_city="Karachi",
        country="Pakistan",
        website_url="https://hamdard.edu.pk",
        target_university="1",
        aliases=(
            "hamdard university",
            "hamdard univ",
            "hamdard university islamabad campus",
            "hamdard university karachi",
            "hamdard university islamabad",
            "hamdard institute of engineering & technology",
            "hamdard institute of engineering and technology",
        ),
        supported_cities=("Islamabad", "Karachi"),
    ),
    CanonicalHEI(
        hei_name="National University of Sciences and Technology",
        short_name="NUST",
        default_city="Islamabad",
        country="Pakistan",
        website_url="https://nust.edu.pk",
        target_university="1",
        aliases=(
            "national university of sciences and technology",
            "national university of sciences & technology",
            "nust",
            "ceme nust",
            "seecs nust",
            "smme nust",
            "mcs nust",
            "pnec nust",
            "college of electrical and mechanical engineering nust",
        ),
        supported_cities=("Islamabad", "Rawalpindi", "Karachi", "Risalpur", "Quetta"),
    ),
    CanonicalHEI(
        hei_name="FAST National University of Computer and Emerging Sciences",
        short_name="FAST-NUCES",
        default_city="Islamabad",
        country="Pakistan",
        website_url="https://nu.edu.pk",
        target_university="1",
        aliases=(
            "fast national university of computer and emerging sciences",
            "national university of computer and emerging sciences",
            "national university of computer & emerging sciences",
            "fast nuces",
            "fast-nuces",
            "fast nu",
        ),
        supported_cities=("Islamabad", "Lahore", "Karachi", "Peshawar", "Chiniot-Faisalabad"),
    ),
    CanonicalHEI(
        hei_name="COMSATS University Islamabad",
        short_name="CUI",
        default_city="Islamabad",
        country="Pakistan",
        website_url="https://comsats.edu.pk",
        target_university="1",
        aliases=(
            "comsats university islamabad",
            "comsats university",
            "comsats institute of information technology",
            "ciit",
        ),
        supported_cities=("Islamabad", "Lahore", "Abbottabad", "Wah", "Attock", "Sahiwal", "Vehari"),
    ),
    CanonicalHEI(
        hei_name="Quaid-i-Azam University",
        short_name="QAU",
        default_city="Islamabad",
        country="Pakistan",
        website_url="https://qau.edu.pk",
        target_university="1",
        aliases=(
            "quaid-i-azam university",
            "quaid i azam university",
            "quaid-e-azam university",
            "qau islamabad",
        ),
        supported_cities=("Islamabad",),
    ),
    CanonicalHEI(
        hei_name="Pakistan Institute of Engineering and Applied Sciences",
        short_name="PIEAS",
        default_city="Islamabad",
        country="Pakistan",
        website_url="https://pieas.edu.pk",
        target_university="1",
        aliases=(
            "pakistan institute of engineering and applied sciences",
            "pakistan institute of engineering & applied sciences",
            "pieas",
        ),
        supported_cities=("Islamabad",),
    ),
    CanonicalHEI(
        hei_name="Lahore University of Management Sciences",
        short_name="LUMS",
        default_city="Lahore",
        country="Pakistan",
        website_url="https://lums.edu.pk",
        target_university="1",
        aliases=(
            "lahore university of management sciences",
            "lums",
        ),
        supported_cities=("Lahore",),
    ),
    CanonicalHEI(
        hei_name="Ghulam Ishaq Khan Institute of Engineering Sciences and Technology",
        short_name="GIKI",
        default_city="Topi",
        country="Pakistan",
        website_url="https://giki.edu.pk",
        target_university="1",
        aliases=(
            "ghulam ishaq khan institute of engineering sciences and technology",
            "gik institute",
            "giki",
        ),
        supported_cities=("Topi",),
    ),
    CanonicalHEI(
        hei_name="University of Engineering and Technology, Lahore",
        short_name="UET Lahore",
        default_city="Lahore",
        country="Pakistan",
        website_url="https://uet.edu.pk",
        target_university="1",
        aliases=(
            "university of engineering and technology lahore",
            "university of engineering & technology lahore",
            "uet lahore",
        ),
        supported_cities=("Lahore", "Faisalabad", "KSK", "Narowal"),
    ),
    CanonicalHEI(
        hei_name="University of Engineering and Technology, Taxila",
        short_name="UET Taxila",
        default_city="Taxila",
        country="Pakistan",
        website_url="https://uettaxila.edu.pk",
        target_university="1",
        aliases=(
            "university of engineering and technology taxila",
            "university of engineering & technology taxila",
            "uet taxila",
        ),
        supported_cities=("Taxila",),
    ),
    CanonicalHEI(
        hei_name="University of Engineering and Technology, Peshawar",
        short_name="UET Peshawar",
        default_city="Peshawar",
        country="Pakistan",
        website_url="https://uetpeshawar.edu.pk",
        target_university="1",
        aliases=(
            "university of engineering and technology peshawar",
            "university of engineering & technology peshawar",
            "uet peshawar",
        ),
        supported_cities=("Peshawar",),
    ),
    CanonicalHEI(
        hei_name="Air University",
        short_name="AU",
        default_city="Islamabad",
        country="Pakistan",
        website_url="https://au.edu.pk",
        target_university="1",
        aliases=(
            "air university",
            "air university islamabad",
        ),
        supported_cities=("Islamabad", "Multan", "Kamra", "Kharian"),
    ),
    CanonicalHEI(
        hei_name="Bahria University",
        short_name="BU",
        default_city="Islamabad",
        country="Pakistan",
        website_url="https://bahria.edu.pk",
        target_university="1",
        aliases=(
            "bahria university",
            "bahria university islamabad",
            "bahria university karachi",
            "bahria university lahore",
        ),
        supported_cities=("Islamabad", "Karachi", "Lahore"),
    ),
    CanonicalHEI(
        hei_name="International Islamic University, Islamabad",
        short_name="IIUI",
        default_city="Islamabad",
        country="Pakistan",
        website_url="https://iiu.edu.pk",
        target_university="1",
        aliases=(
            "international islamic university islamabad",
            "international islamic university",
            "iiui",
        ),
        supported_cities=("Islamabad",),
    ),
    CanonicalHEI(
        hei_name="Institute of Space Technology",
        short_name="IST",
        default_city="Islamabad",
        country="Pakistan",
        website_url="https://ist.edu.pk",
        target_university="1",
        aliases=(
            "institute of space technology",
            "ist islamabad",
        ),
        supported_cities=("Islamabad",),
    ),
    CanonicalHEI(
        hei_name="Capital University of Science and Technology",
        short_name="CUST",
        default_city="Islamabad",
        country="Pakistan",
        website_url="https://cust.edu.pk",
        target_university="1",
        aliases=(
            "capital university of science and technology",
            "capital university of science & technology",
            "cust islamabad",
        ),
        supported_cities=("Islamabad",),
    ),
    CanonicalHEI(
        hei_name="Riphah International University",
        short_name="RIU",
        default_city="Islamabad",
        country="Pakistan",
        website_url="https://riphah.edu.pk",
        target_university="1",
        aliases=(
            "riphah international university",
            "riphah university",
        ),
        supported_cities=("Islamabad", "Rawalpindi", "Lahore"),
    ),
    CanonicalHEI(
        hei_name="University of the Punjab",
        short_name="PU",
        default_city="Lahore",
        country="Pakistan",
        website_url="https://pu.edu.pk",
        target_university="1",
        aliases=(
            "university of the punjab",
            "punjab university",
            "pucit",
        ),
        supported_cities=("Lahore", "Gujranwala", "Jhelum"),
    ),
    CanonicalHEI(
        hei_name="NED University of Engineering and Technology",
        short_name="NEDUET",
        default_city="Karachi",
        country="Pakistan",
        website_url="https://neduet.edu.pk",
        target_university="1",
        aliases=(
            "ned university of engineering and technology",
            "ned university of engineering & technology",
            "ned university",
            "neduet",
        ),
        supported_cities=("Karachi",),
    ),
    CanonicalHEI(
        hei_name="Institute of Business Administration, Karachi",
        short_name="IBA",
        default_city="Karachi",
        country="Pakistan",
        website_url="https://iba.edu.pk",
        target_university="1",
        aliases=(
            "institute of business administration karachi",
            "institute of business administration",
            "iba karachi",
        ),
        supported_cities=("Karachi",),
    ),
    CanonicalHEI(
        hei_name="Information Technology University",
        short_name="ITU",
        default_city="Lahore",
        country="Pakistan",
        website_url="https://itu.edu.pk",
        target_university="1",
        aliases=(
            "information technology university",
            "itu lahore",
        ),
        supported_cities=("Lahore",),
    ),
    CanonicalHEI(
        hei_name="University of Management and Technology",
        short_name="UMT",
        default_city="Lahore",
        country="Pakistan",
        website_url="https://umt.edu.pk",
        target_university="1",
        aliases=(
            "university of management and technology",
            "umt lahore",
        ),
        supported_cities=("Lahore", "Sialkot"),
    ),
    CanonicalHEI(
        hei_name="University of Central Punjab",
        short_name="UCP",
        default_city="Lahore",
        country="Pakistan",
        website_url="https://ucp.edu.pk",
        target_university="1",
        aliases=(
            "university of central punjab",
            "ucp lahore",
        ),
        supported_cities=("Lahore",),
    ),
    CanonicalHEI(
        hei_name="Mehran University of Engineering and Technology",
        short_name="MUET",
        default_city="Jamshoro",
        country="Pakistan",
        website_url="https://muet.edu.pk",
        target_university="1",
        aliases=(
            "mehran university of engineering and technology",
            "mehran university of engineering & technology",
            "muet jamshoro",
        ),
        supported_cities=("Jamshoro", "Khairpur"),
    ),
    CanonicalHEI(
        hei_name="University of Peshawar",
        short_name="UoP",
        default_city="Peshawar",
        country="Pakistan",
        website_url="https://uop.edu.pk",
        target_university="1",
        aliases=(
            "university of peshawar",
        ),
        supported_cities=("Peshawar",),
    ),
)

# Designation / role / department patterns that must NEVER be treated as an HEI name.
ROLE_OR_DEPT_PATTERNS = (
    r"^\s*(?:prof(?:essor)?|assoc(?:iate)?\s+prof(?:essor)?|asst\.?\s+prof(?:essor)?|"
    r"assistant\s+prof(?:essor)?|lecturer|senior\s+lecturer|reader|dean|campus\s+director|"
    r"director|rector|vice\s+chancellor|pro[\s-]+rector|chairman|chairperson|head|hod|"
    r"head\s+of\s+department|research\s+fellow|postdoc(?:toral)?\s+fellow|"
    r"visiting\s+faculty|adjunct\s+faculty|emeritus\s+professor|scholar|phd\s+candidate|"
    r"graduate\s+student|research\s+associate|research\s+assistant)\b",
    r"^\s*(?:department|dept\.?|faculty|school|college|institute|center|centre|division)\s+of\b",
)


def _strip_designations_and_departments(raw_affiliation: str) -> list[str]:
    """
    Split a free-text affiliation line on commas/semicolons/hyphens and filter out
    segments that are purely job titles, academic ranks, or department names.
    """
    if not raw_affiliation:
        return []
    parts = [p.strip() for p in re.split(r"[,;|/]+|\s+-\s+|\s+at\s+", raw_affiliation) if p.strip()]
    kept = []
    for part in parts:
        cleaned = part
        for pat in ROLE_OR_DEPT_PATTERNS:
            cleaned = re.sub(pat, "", cleaned, flags=re.I).strip(" ,.-:")
        if cleaned:
            kept.append(cleaned)
    return kept


def resolve_affiliation(
    affiliation_text: Optional[str],
    catalog: tuple[CanonicalHEI, ...] = DEFAULT_HEI_CATALOG,
) -> tuple[Optional[dict], str]:
    """
    Resolve raw Google Scholar affiliation text into a canonical HEIs attribute dict
    (without hei_id / created_at, which are populated by the ID registry).

    Returns:
      (resolved_dict, status_note) on a confident catalog match or when
      source-observed affiliation text can be preserved without invention.
      (None, status_note) only when no usable affiliation is displayed.
    """
    raw = (affiliation_text or "").strip()
    if not raw:
        return None, "Affiliation text is missing/empty on the Google Scholar profile."

    candidate_segments = _strip_designations_and_departments(raw)
    if not candidate_segments:
        return None, (
            f"Affiliation text {raw!r} contains only a job title, designation, or department "
            "and no identifiable Higher Education Institution."
        )

    raw_norm = normalize_text(raw)
    segments_norm = [normalize_text(seg) for seg in candidate_segments]
    combined_norm = " ".join(segments_norm)

    matched_entry: Optional[CanonicalHEI] = None
    matched_alias_len = -1

    for entry in catalog:
        all_names = (entry.hei_name, *entry.aliases)
        for alias in all_names:
            alias_norm = normalize_text(alias)
            if not alias_norm:
                continue
            # Match whole word/phrase boundary in the cleaned segments or full normalized string
            pattern = r"(?:^|\b)" + re.escape(alias_norm) + r"(?:\b|$)"
            if any(re.search(pattern, seg) for seg in segments_norm) or re.search(pattern, combined_norm) or re.search(pattern, raw_norm):
                if len(alias_norm) > matched_alias_len:
                    matched_entry = entry
                    matched_alias_len = len(alias_norm)

    if matched_entry is None:
        # Preserve what Google Scholar actually displayed.  The full cleaned
        # affiliation is used because extracting a purported university, city,
        # or country from arbitrary international text would fabricate
        # semantics.  Empty metadata deliberately records that it is unknown.
        return (
            {
                "hei_name": raw,
                "short_name": "",
                "city": "",
                "country": "",
                "website_url": "",
                "target_university": "0",
            },
            f"Preserved uncatalogued affiliation {raw!r} exactly as source-observed text; "
            "optional institution metadata remains empty.",
        )

    # Determine city: check if a supported campus city is explicitly mentioned in the affiliation text
    resolved_city = matched_entry.default_city
    for city in matched_entry.supported_cities:
        city_pat = r"\b" + re.escape(city.casefold()) + r"(?:\s+campus)?\b"
        if re.search(city_pat, raw_norm):
            resolved_city = city
            break

    return (
        {
            "hei_name": matched_entry.hei_name,
            "short_name": matched_entry.short_name,
            "city": resolved_city,
            "country": matched_entry.country,
            "website_url": matched_entry.website_url,
            "target_university": matched_entry.target_university,
        },
        f"Mapped {raw!r} -> canonical HEI {matched_entry.hei_name!r} (city={resolved_city!r}, country={matched_entry.country!r}).",
    )
