"""Turning messy posting text into the few facts the scorer needs.

Every connector returns different shapes, so normalisation happens once, here.
Two things are easy to get wrong and are handled explicitly:

* **Turkish casing.** ``"İSTANBUL".lower()`` produces a dotted-i with a combining
  mark in Python, which then fails to match ``"istanbul"``. Everything is folded
  to plain ASCII before comparison.
* **Region-locked remote.** "Remote (US only)" is not a remote job for someone in
  Türkiye. Detecting remote without detecting its geographic fence would fill the
  queue with jobs that cannot be taken.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from enum import StrEnum

# Turkish letters that ASCII folding alone maps wrongly or not at all.
_TURKISH_FOLD = str.maketrans(
    {
        "ı": "i", "İ": "i", "I": "i", "i": "i",
        "ş": "s", "Ş": "s",
        "ğ": "g", "Ğ": "g",
        "ü": "u", "Ü": "u",
        "ö": "o", "Ö": "o",
        "ç": "c", "Ç": "c",
    }
)


def fold(text: str) -> str:
    """Lowercase and strip accents so Turkish and English text compare equal."""
    if not text:
        return ""
    folded = text.translate(_TURKISH_FOLD).lower()
    decomposed = unicodedata.normalize("NFKD", folded)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", stripped).strip()


def contains_any(haystack: str, needles: Iterable[str]) -> bool:
    """Word-boundary-aware substring check over already-folded text.

    A needle ending in ``*`` matches any suffix. Turkish is agglutinative --
    "staj" becomes "stajyer", "stajyeri", "stajyerlik" -- so a plain
    word-boundary match would miss most real Turkish postings.
    """
    for needle in needles:
        raw = str(needle)
        allow_suffix = raw.endswith("*")
        folded = fold(raw.rstrip("*"))
        if not folded:
            continue
        tail = "" if allow_suffix else "(?![a-z0-9])"
        if re.search(rf"(?<![a-z0-9]){re.escape(folded)}{tail}", haystack):
            return True
    return False


class Seniority(StrEnum):
    INTERN = "intern"
    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    LEAD = "lead"
    MANAGER = "manager"
    UNKNOWN = "unknown"


# Checked in order; the first match wins, so the most senior signals come first.
# A posting titled "Senior Backend Developer (Junior welcome)" is a senior role.
_SENIORITY_PATTERNS: tuple[tuple[Seniority, tuple[str, ...]], ...] = (
    (Seniority.MANAGER, ("manager", "müdür*", "yönetici*", "head of", "director", "cto")),
    # "staff" must stand alone: real titles are "Staff Software Engineer",
    # not "Staff Engineer", so matching the two-word phrase misses all of them.
    (Seniority.LEAD, ("lead", "team lead", "tech lead", "takım lider*", "principal",
                      "staff", "architect", "mimar*")),
    (Seniority.SENIOR, ("senior", "sr", "kıdemli", "uzman*", "expert", "iii", "iv")),
    (Seniority.INTERN, ("intern", "internship", "staj*", "trainee", "bursiyer")),
    (Seniority.JUNIOR, ("junior", "jr", "entry level", "entry", "new grad", "graduate",
                        "yeni mezun", "başlangıç", "associate")),
    (Seniority.MID, ("mid level", "mid", "medior", "ii")),
)


# Phrases that contain a seniority word without being a seniority signal.
# "Member of Technical Staff" is the standard individual-contributor title at
# OpenAI, Anthropic and Cohere -- reading it as staff-level rejected 91 of
# Cohere's 148 postings, most of which were ordinary engineering roles.
_SENIORITY_NOISE = (
    "member of technical staff",
    "technical staff",
    "staff augmentation",
)


def detect_seniority(title: str, description: str = "") -> Seniority:
    """Infer seniority. The title dominates; the description is a weak fallback."""
    folded_title = fold(title)
    for phrase in _SENIORITY_NOISE:
        folded_title = folded_title.replace(fold(phrase), " ")
    folded_title = re.sub(r"\s+", " ", folded_title).strip()

    for level, markers in _SENIORITY_PATTERNS:
        if contains_any(folded_title, markers):
            return level

    # Only trust the description for the two levels stated unambiguously there.
    folded_description = fold(description[:1500])
    for level, markers in _SENIORITY_PATTERNS:
        if level in (Seniority.INTERN, Seniority.JUNIOR) and contains_any(
            folded_description, markers
        ):
            return level
    return Seniority.UNKNOWN


_REMOTE_MARKERS = ("remote", "uzaktan", "work from home", "evden", "distributed",
                   "anywhere", "home office", "home based", "homebased", "tele calisma")
_HYBRID_MARKERS = ("hybrid", "hibrit", "yari uzaktan")

# A "remote" posting fenced to a region the applicant cannot work from.
_REGION_LOCK_PATTERNS = (
    r"remote\s*\(([^)]{2,40})\)",
    r"remote[,\s-]+(us|usa|united states|uk|eu|emea|canada|germany|india|latam|apac)\b",
    r"must (?:be|reside|live) (?:located )?in ([a-z ]{2,30})",
    r"(us|uk|eu|canada)[- ]based only",
    r"work authorization in ([a-z ]{2,30})",
)


def detect_remote(title: str, location: str = "", description: str = "") -> tuple[bool, bool]:
    """Return ``(is_remote, is_hybrid)`` from the posting's own wording.

    Only the title and location are trusted. Descriptions are full of phrases
    like "we are a remote-friendly company" and "our remote-first culture" on
    postings that are plainly onsite; reading them turned San Francisco and
    Chicago roles into "remote" in a live run. The description is consulted
    only when there is no location at all to go on.
    """
    blob = fold(" ".join((title, location)))
    if not fold(location) and description:
        blob = f"{blob} {fold(description[:500])}"
    is_hybrid = contains_any(blob, _HYBRID_MARKERS)
    is_remote = contains_any(blob, _REMOTE_MARKERS)
    return is_remote and not is_hybrid, is_hybrid


def relax(text: str) -> str:
    """Treat hyphens, underscores and slashes as spaces ("full-time" == "full time")."""
    return re.sub(r"[-_/]+", " ", text).strip()


# Phrases that appear in the same parenthetical position as a region but are not one.
_NON_GEOGRAPHIC = ("full time", "part time", "contract", "freelance", "flexible",
                   "global", "worldwide", "anywhere", "permanent", "temporary")


def remote_region_lock(title: str, location: str = "", description: str = "") -> str | None:
    """Return the region a remote posting is fenced to, if it names one."""
    blob = fold(" ".join((title, location, description[:2000])))
    for pattern in _REGION_LOCK_PATTERNS:
        match = re.search(pattern, blob)
        if match:
            region = relax(match.group(1))
            # "Remote (Full-time)" and friends are not geographic fences.
            if region and not contains_any(region, _NON_GEOGRAPHIC):
                return region
    return None


_TR_CITIES = (
    "istanbul", "ankara", "izmir", "bursa", "antalya", "kocaeli", "konya", "adana",
    "gaziantep", "kayseri", "eskişehir", "denizli", "samsun", "trabzon", "sakarya",
    "mersin", "diyarbakır", "manisa", "tekirdağ", "balıkesir", "kütahya", "gebze",
)

# Country markers, checked in order. Türkiye comes first because it is the only
# one that has to be right; the rest exist so that an onsite role abroad is
# recognisably abroad and can be rejected instead of scored as "location unknown".
_COUNTRY_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("TR", ("türkiye", "turkiye", "turkey", "tr", *_TR_CITIES)),
    ("DE", ("germany", "deutschland", "berlin", "münchen", "munich", "hamburg",
            "frankfurt", "stuttgart", "cologne", "köln", "düsseldorf", "leipzig")),
    ("NL", ("netherlands", "holland", "amsterdam", "rotterdam", "utrecht", "eindhoven",
            "the hague", "den haag")),
    ("UK", ("united kingdom", "england", "scotland", "london", "manchester",
            "edinburgh", "cambridge", "bristol", "leeds")),
    ("US", ("united states", "usa", "new york", "san francisco", "seattle", "austin",
            "boston", "chicago", "denver", "atlanta", "los angeles")),
    ("CA", ("canada", "toronto", "vancouver", "montreal", "ottawa")),
    ("PL", ("poland", "polska", "warsaw", "warszawa", "krakow", "wroclaw", "gdansk")),
    ("ES", ("spain", "españa", "madrid", "barcelona", "valencia", "malaga")),
    ("FR", ("france", "paris", "lyon", "toulouse", "bordeaux", "nantes")),
    ("IE", ("ireland", "dublin", "cork")),
    ("SE", ("sweden", "stockholm", "gothenburg", "malmo")),
    ("CH", ("switzerland", "zürich", "zurich", "geneva", "basel", "lausanne")),
    ("AT", ("austria", "vienna", "wien", "graz")),
    ("PT", ("portugal", "lisbon", "lisboa", "porto")),
    ("CZ", ("czechia", "czech republic", "prague", "praha", "brno")),
    ("RO", ("romania", "bucharest", "cluj", "timisoara")),
    ("EE", ("estonia", "tallinn", "tartu")),
    ("AE", ("uae", "united arab emirates", "dubai", "abu dhabi")),
    ("QA", ("qatar", "doha")),
    ("AZ", ("azerbaijan", "azerbaycan", "baku", "bakü")),
    ("GE", ("tbilisi", "batumi")),
    ("IN", ("india", "bangalore", "bengaluru", "hyderabad", "pune", "mumbai",
            "new delhi", "chennai", "manipal", "noida", "gurgaon")),
    ("UA", ("ukraine", "kyiv", "kiev", "lviv")),
    ("HU", ("hungary", "budapest")),
    ("GR", ("greece", "athens", "thessaloniki")),
    ("IT", ("italy", "italia", "milan", "milano", "rome", "roma", "turin")),
    ("BE", ("belgium", "brussels", "antwerp", "ghent")),
    ("DK", ("denmark", "copenhagen")),
    ("NO", ("norway", "oslo")),
    ("FI", ("finland", "helsinki")),
    ("BG", ("bulgaria", "sofia")),
    ("RS", ("serbia", "belgrade", "novi sad")),
    ("HR", ("croatia", "zagreb")),
    ("LT", ("lithuania", "vilnius")),
    ("LV", ("latvia", "riga")),
    ("SK", ("slovakia", "bratislava")),
    ("SI", ("slovenia", "ljubljana")),
    ("IL", ("israel", "tel aviv")),
    ("SA", ("saudi arabia", "riyadh", "jeddah")),
    ("EG", ("egypt", "cairo")),
    ("ZA", ("south africa", "cape town", "johannesburg")),
    ("NG", ("nigeria", "lagos")),
    ("KE", ("kenya", "nairobi")),
    ("BR", ("brazil", "brasil", "sao paulo", "rio de janeiro")),
    ("AR", ("argentina", "buenos aires")),
    ("UY", ("uruguay", "montevideo")),
    ("MX", ("mexico", "mexico city", "guadalajara")),
    ("CL", ("chile", "santiago")),
    ("CO", ("colombia", "bogota", "medellin")),
    ("SG", ("singapore",)),
    ("JP", ("japan", "tokyo", "osaka")),
    ("KR", ("south korea", "seoul")),
    ("CN", ("china", "beijing", "shanghai", "shenzhen")),
    ("HK", ("hong kong",)),
    ("TW", ("taiwan", "taipei")),
    ("MY", ("malaysia", "kuala lumpur")),
    ("PH", ("philippines", "manila")),
    ("ID", ("indonesia", "jakarta")),
    ("VN", ("vietnam", "hanoi", "ho chi minh")),
    ("TH", ("thailand", "bangkok")),
    ("AU", ("australia", "sydney", "melbourne", "brisbane")),
    ("NZ", ("new zealand", "auckland", "wellington")),
)


# Multi-country regions. The distinction that matters is whether someone
# working from Türkiye is inside the region or outside it, and it is not
# intuitive: EMEA includes Türkiye, the EU does not (it is a work-authorisation
# fence), and "Western Europe" does not either.
_REGIONS_INCLUDING_TR = (
    "emea", "europe", "worldwide", "world wide", "global", "globally", "anywhere",
    "international", "mena", "middle east", "cet", "cest", "eet", "any timezone",
)
_REGIONS_EXCLUDING_TR = (
    "eu", "european union", "eea", "schengen", "noram", "north america",
    "latam", "latin america", "south america", "americas", "apac", "asia pacific",
    "anz", "dach", "benelux", "nordics", "nordic", "iberia", "baltics",
    "western europe", "eastern europe", "central europe", "southern europe",
    "northern europe", "uk&i", "uk and ireland", "sub saharan africa",
)


def region_reach(text: str) -> str | None:
    """Classify a multi-country region as ``"includes_tr"``, ``"excludes_tr"``, or None."""
    folded = relax(fold(text))
    if not folded:
        return None
    # Excluding regions are checked first because they are more specific:
    # "Western Europe" also contains the word "Europe".
    if contains_any(folded, _REGIONS_EXCLUDING_TR):
        return "excludes_tr"
    if contains_any(folded, _REGIONS_INCLUDING_TR):
        return "includes_tr"
    return None


def detect_country(location: str, description: str = "") -> str | None:
    """Best-effort country code from a location string, or None if unrecognised.

    The description is consulted only when there is no location. A US posting
    that mentions "our Istanbul office" would otherwise read as Turkish.
    """
    blob = fold(location) or fold(description[:300])
    if not blob:
        return None
    for code, markers in _COUNTRY_MARKERS:
        if contains_any(blob, markers):
            return code
    return None


# Function words, not topic words: a Turkish posting about Java still says
# "ve", "için", "ile" constantly, while an English one never does.
_TURKISH_FUNCTION_WORDS = (
    "ve", "ile", "için", "bir", "olarak", "veya", "gibi", "daha", "çok", "en",
    "bu", "olan", "üzere", "ancak", "ayrıca", "tercihen", "aranıyor", "deneyim",
    "bilgisi", "gerekli", "sahip", "yapabilecek", "çalışma", "adaylar",
)
_ENGLISH_FUNCTION_WORDS = (
    "and", "with", "for", "the", "or", "as", "you", "we", "our", "will", "have",
    "are", "is", "to", "in", "of", "experience", "required", "preferred",
    "candidates", "working",
)


def detect_language(text: str) -> str:
    """Return ``"tr"`` or ``"en"`` for a posting.

    Counts function words rather than topic words. A Turkish posting for a Java
    role is full of English technology names but still built from Turkish
    grammar, so matching on "java" or "developer" would classify it as English.
    Ties fall to English, which is the safer default for an international board.
    """
    folded = fold(text)
    if not folded:
        return "en"
    words = folded.split()
    turkish = sum(1 for word in words if word in {fold(w) for w in _TURKISH_FUNCTION_WORDS})
    english = sum(1 for word in words if word in _ENGLISH_FUNCTION_WORDS)
    return "tr" if turkish > english else "en"


_NOISE_PATTERNS = (
    r"\(.*?\)",              # "(Remote)", "(m/w/d)", "(Istanbul)"
    r"\[.*?\]",
    r"\b(m/w/d|f/m/d|m/f/d|h/f)\b",
    r"[-–—|/]+\s*(remote|hybrid|full[- ]time|part[- ]time|onsite).*$",
)


def normalize_title(title: str) -> str:
    """Strip decoration so two spellings of the same role collapse together."""
    cleaned = fold(title)
    for pattern in _NOISE_PATTERNS:
        cleaned = re.sub(pattern, " ", cleaned)
    cleaned = re.sub(r"[^a-z0-9+#. ]+", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def title_tokens(title: str) -> frozenset[str]:
    """Meaningful words of a title, for fuzzy duplicate detection."""
    stopwords = {"a", "an", "the", "and", "or", "for", "of", "in", "at", "to", "with",
                 "we", "are", "hiring", "is", "our", "you", "your", "job", "role",
                 "position", "opening", "m", "f", "d", "w", "h"}
    return frozenset(
        word for word in normalize_title(title).split() if word not in stopwords and len(word) > 1
    )


def titles_are_similar(left: str, right: str, threshold: float = 0.7) -> bool:
    """Jaccard overlap of title tokens, used to catch cross-source duplicates."""
    left_tokens, right_tokens = title_tokens(left), title_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    intersection = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    return intersection / union >= threshold
