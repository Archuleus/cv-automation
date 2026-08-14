"""The prompt arm 3 sends, and the schema it constrains the answer to.

Written for an 8B model running locally, which changes the style: a frontier
model tolerates a discursive brief, a small one does much better with short
numbered rules and every fact supplied rather than inferred.

The `cited_detail` field is the load-bearing part. Asking a model to "reference
something specific from the posting" is unverifiable -- it can comply in spirit
and still invent the specific. Making it *return the quote separately* turns
that into a string check against the posting text, so a fabricated detail is
caught by code instead of by whoever reads the queue.
"""

from __future__ import annotations

from typing import Any

from jobbot.profile import Profile

# Constrained decoding: Ollama enforces this shape, so parsing never fails.
DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cited_detail": {
            "type": "string",
            "description": "A short phrase copied verbatim from the job posting.",
        },
        "subject": {"type": "string"},
        "body": {"type": "string"},
    },
    "required": ["cited_detail", "subject", "body"],
}

MIN_WORDS = 90
MAX_WORDS = 200

# Phrases that appear in every generic application and say nothing. A draft
# containing one is regenerated rather than shown.
BANNED_PHRASES: tuple[str, ...] = (
    # Turkish
    "dinamik ekibinizde",
    "kendimi geliştirmek",
    "saygılarımla arz ederim",
    "firmanızda çalışmak benim için",
    "değerli vaktinizi",
    "özgeçmişimi ekte",
    "her türlü göreve",
    # English
    "i am writing to express",
    "i would be a great fit",
    "dynamic team",
    "fast-paced environment",
    "passionate about technology",
    "please find my resume attached",
    "thank you for your time and consideration",
    "i believe i would be",
    # Empty connectives a small model reaches for to link a fact to a
    # requirement. They add length and assert nothing; the fact should stand
    # on its own. Seen in the first two real generations.
    "these experiences demonstrate",
    "demonstrates my ability",
    "demonstrate my ability",
    "makes me a strong fit",
    "which aligns with the requirement",
    "mirrors the need",
    "i am confident that",
)

SYSTEM_PROMPT_EN = """You write short job applications. You are given a real job \
posting and a real candidate profile. Write one application message.

Rules:
1. Write ONLY in English.
2. The body must be {min_words}-{max_words} words. No greeting line, no sign-off, \
no signature - those are added later.
3. Quote one concrete detail from the posting into "cited_detail", copied from \
the posting text. Pick something distinctive about this team's work - not a long \
list of technologies. Never invent it.
4. Connect that detail to a specific project or job from the EXPERIENCE or \
PROJECTS section. Name it.
5. NEVER name a technology that is not in the candidate section. If the posting \
mentions Kubernetes and the candidate does not, do not mention Kubernetes. Write \
about what the candidate has actually built instead.
6. Be specific and plain. No flattery, no filler, no restating the job title back.
7. "subject" is one line, under 80 characters, naming the role and 2-3 key skills.

Return JSON only."""

SYSTEM_PROMPT_TR = """Kısa iş başvurusu metinleri yazıyorsun. Sana gerçek bir iş \
ilanı ve gerçek bir aday profili veriliyor. Tek bir başvuru metni yaz.

Kurallar:
1. SADECE Türkçe yaz.
2. Metin {min_words}-{max_words} kelime olmalı. Selamlama satırı, kapanış ve imza \
yazma - onlar sonradan ekleniyor.
3. İlandan somut bir ayrıntıyı "cited_detail" alanına ilan metninden birebir \
kopyala. Bu ekibin işine dair ayırt edici bir şey seç - uzun teknoloji listesi \
değil. Asla uydurma.
4. O ayrıntıyı adayın DENEYIM veya PROJELER bölümündeki somut bir iş ya da \
projeyle ilişkilendir. Adını yaz.
5. Aday bölümünde GEÇMEYEN hiçbir teknolojinin adını yazma. İlanda Kubernetes \
geçiyorsa ve adayda yoksa, Kubernetes yazma. Adayın gerçekten yaptığı işi anlat.
6. Somut ve sade yaz. Övgü, dolgu cümlesi ve pozisyon adını tekrar etme yok.
7. "subject" tek satır, 80 karakterden kısa; pozisyonu ve 2-3 temel yetkinliği içersin.

Yalnızca JSON döndür."""


def system_prompt(language: str) -> str:
    template = SYSTEM_PROMPT_TR if language == "tr" else SYSTEM_PROMPT_EN
    return template.format(min_words=MIN_WORDS, max_words=MAX_WORDS)


def _profile_block(profile: Profile, language: str) -> str:
    """The candidate facts the model is allowed to draw on, and nothing else.

    Experience and project highlights matter more than the skill list. Given
    only a list of technologies, a model has nothing concrete to write about and
    invents projects that sound right -- the first real generation claimed
    Kubernetes and Linux administration the applicant has never touched. Real
    highlights give it something true to reach for instead.
    """
    lines = [
        f"Name: {profile.name}",
        f"Headline: {profile.headline}",
        f"Location: {profile.location}",
    ]
    if profile.education:
        lines.append(f"Education: {profile.education}")
    lines.append(f"Years of professional experience: {profile.years_of_experience}")

    if profile.experience:
        lines.append("")
        lines.append("EXPERIENCE:")
        for job in profile.experience:
            lines.append(f"* {job.role}, {job.company} ({job.location}, {job.period})")
            lines.extend(f"    - {point}" for point in job.highlights)

    if profile.projects:
        lines.append("")
        lines.append("PROJECTS:")
        for project in profile.projects:
            lines.append(f"* {project.name}: {project.summary}")
            lines.extend(f"    - {point}" for point in project.highlights)

    lines.append("")
    lines.append(f"SKILLS: {', '.join(profile.skills.core + profile.skills.secondary)}")
    if profile.spoken_languages:
        spoken = ", ".join(f"{k} ({v})" for k, v in profile.spoken_languages.items())
        lines.append(f"Spoken languages: {spoken}")

    if language == "tr":
        lines.append(
            "\n(Aday hakkında SADECE yukarıdaki bilgiler doğrudur. Burada "
            "geçmeyen hiçbir teknoloji, iş yeri, proje veya yıl sayısı yazma.)"
        )
    else:
        lines.append(
            "\n(ONLY the facts above are true about this candidate. Do not name any "
            "technology, employer, project or number of years that is not listed here.)"
        )
    return "\n".join(lines)


def build_prompt(
    *,
    profile: Profile,
    company_name: str,
    title: str,
    location: str,
    description: str,
    language: str,
    experience: str = "",
    description_limit: int = 5000,
) -> str:
    """Assemble the user-turn prompt for one posting."""
    posting = (description or "").strip()[:description_limit]
    if not posting:
        posting = f"{title} at {company_name}. No description was published."

    header = "JOB POSTING" if language == "en" else "IS ILANI"
    candidate = "CANDIDATE" if language == "en" else "ADAY"
    task = (
        "Write the application now. Return JSON with cited_detail, subject, body."
        if language == "en"
        else "Şimdi başvuru metnini yaz. cited_detail, subject, body alanlarıyla JSON döndür."
    )

    sections = [
        f"=== {header} ===",
        f"Company: {company_name}",
        f"Title: {title}",
        f"Location: {location or 'not stated'}",
        "",
        posting,
        "",
        f"=== {candidate} ===",
        _profile_block(profile, language),
    ]
    if experience:
        sections += ["", experience]
    sections += ["", task]
    return "\n".join(sections)
