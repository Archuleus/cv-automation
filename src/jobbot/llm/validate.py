"""Checks a draft must pass before a human is asked to look at it.

An 8B model running locally is capable enough to write a good application and
also capable of quietly inventing one. Every rule in the prompt is repeated here
as an assertion, because a rule stated in a prompt is a request and a rule
checked in code is a guarantee.

The strongest check is `cited_detail`: the model returns the phrase it claims to
have taken from the posting, and this module verifies that phrase is really in
the posting. That converts "reference something specific" from an instruction
into something falsifiable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from jobbot.llm.base import Draft
from jobbot.llm.prompts import BANNED_PHRASES, MAX_WORDS, MIN_WORDS
from jobbot.normalize import detect_language, fold
from jobbot.profile import Profile

# Template markers that mean the model emitted scaffolding instead of content.
PLACEHOLDER = re.compile(
    r"\[[^\]]{2,40}\]|\{\{?[a-z_]{2,30}\}?\}|\bTODO\b|\bXXX\b|\bLorem ipsum\b",
    re.IGNORECASE,
)
YEARS_CLAIM = re.compile(r"(\d{1,2})\s*(?:\+\s*)?(?:years?|yil|yillik|yıl|yıllık)", re.IGNORECASE)

# A quoted detail is accepted if this share of its words appear, in order, in
# the posting. Exact matching is too brittle -- models normalise whitespace,
# drop articles and fix casing while otherwise quoting faithfully.
CITATION_OVERLAP = 0.75
MIN_CITATION_WORDS = 3

# Technology names a draft might claim. This list exists only to *recognise* a
# claim; whether the claim is allowed is decided by the applicant's profile.
#
# It is here because the first real generation passed every other check and
# still asserted "hands-on experience with Linux systems administration and
# Kubernetes clusters" and familiarity with OpenSearch -- none of which the
# applicant has. A grounded citation proves the model read the posting; it
# proves nothing about the sentences describing the candidate.
KNOWN_TECHNOLOGIES: frozenset[str] = frozenset((
    # languages
    "java", "kotlin", "scala", "groovy", "python", "ruby", "php", "perl", "rust",
    "go", "golang", "swift", "dart", "javascript", "typescript", "c", "c++", "c#",
    ".net", "elixir", "erlang", "haskell", "clojure", "lua", "r",
    # backend and web frameworks
    "spring", "hibernate", "quarkus", "micronaut", "django", "flask", "fastapi",
    "rails", "laravel", "symfony", "express", "nestjs", "next.js", "nuxt",
    # frontend and mobile
    "react", "angular", "vue", "svelte", "solid", "ember", "jquery", "flutter",
    "expo", "xamarin", "ionic", "swiftui", "compose",
    # datastores
    "postgresql", "mysql", "mariadb", "sqlite", "oracle", "sqlserver", "mongodb",
    "cassandra", "dynamodb", "redis", "memcached", "elasticsearch", "opensearch",
    "solr", "neo4j", "influxdb", "clickhouse",
    # data platform
    "snowflake", "bigquery", "redshift", "databricks", "hadoop", "spark", "hive",
    "presto", "trino", "airflow", "dagster", "dbt",
    # messaging
    "kafka", "rabbitmq", "activemq", "pulsar", "nats", "sqs", "kinesis", "celery",
    # infrastructure
    "docker", "kubernetes", "k8s", "openshift", "nomad", "terraform", "ansible",
    "puppet", "chef", "vagrant", "helm", "istio", "linkerd", "consul", "vault",
    "linux", "unix", "ubuntu", "debian", "centos", "rhel", "alpine", "nginx",
    "apache", "haproxy", "envoy",
    # cloud and hosting
    "aws", "azure", "gcp", "heroku", "digitalocean", "cloudflare", "vercel",
    "netlify", "firebase", "supabase",
    # ci and observability
    "jenkins", "gitlab", "github", "circleci", "travis", "argocd", "prometheus",
    "grafana", "datadog", "splunk", "sentry", "opentelemetry", "jaeger", "kibana",
    # protocols and auth
    "graphql", "grpc", "rest", "soap", "websocket", "webrtc", "oauth2", "oauth",
    "jwt", "saml", "oidc",
    # machine learning
    "tensorflow", "pytorch", "keras", "scikit-learn", "pandas", "numpy", "opencv",
    "huggingface", "llm", "rag", "langchain",
    # blockchain
    "solidity", "web3.js", "ethereum", "hardhat", "truffle",
    # build and test
    "git", "maven", "gradle", "npm", "yarn", "pnpm", "webpack", "vite", "babel",
    "eslint", "junit", "pytest", "jest", "cypress", "playwright", "selenium",
    "testng", "mockito",
))

# Words that name a technology in the list but are ordinary English too, so a
# bare mention is not a skill claim.
_AMBIGUOUS_TECHNOLOGIES = frozenset({"c", "r", "go", "rust", "swift", "solid", "vault"})


@dataclass(frozen=True, slots=True)
class Validation:
    violations: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.violations

    def __bool__(self) -> bool:
        return self.ok


def _words(text: str) -> list[str]:
    """Tokenise for comparison, keeping dots inside words but not around them.

    The dot has to survive inside a token so ".net" and "node.js" stay intact,
    which means sentence punctuation sticks to the last word of a sentence:
    without trimming, a verbatim quote of "structured SQL engines" fails to
    match a posting that ends the sentence with "engines."
    """
    tokens = (token.strip(".") for token in re.split(r"[^a-z0-9+#.]+", fold(text)))
    return [token for token in tokens if token]


def citation_is_grounded(cited: str, posting: str) -> bool:
    """Is the model's quoted phrase actually present in the posting?"""
    quote = _words(cited)
    if len(quote) < MIN_CITATION_WORDS:
        # Anything shorter is a word, not a detail, and would match by accident.
        return False

    haystack = _words(posting)
    if not haystack:
        return False

    # Slide a window the length of the quote across the posting and take the
    # best overlap, so reordering inside the phrase does not fail an honest quote.
    window = len(quote)
    quoted = set(quote)
    best = 0.0
    for start in range(0, max(1, len(haystack) - window + 1)):
        chunk = set(haystack[start : start + window])
        overlap = len(quoted & chunk) / len(quoted)
        best = max(best, overlap)
        if best >= CITATION_OVERLAP:
            return True
    return best >= CITATION_OVERLAP


def _supported_vocabulary(profile: Profile) -> frozenset[str]:
    """Every technology the applicant can honestly claim.

    Drawn from the skill tiers plus the experience and project highlights, so a
    tool named only in a project description still counts as supported.
    """
    supported: set[str] = set()
    for skill in profile.skills.all:
        supported.update(_words(skill))
    for job in profile.experience:
        for line in job.highlights:
            supported.update(_words(line))
    for project in profile.projects:
        supported.update(_words(project.summary))
        for line in project.highlights:
            supported.update(_words(line))
    return frozenset(supported)


def unsupported_technologies(body: str, profile: Profile) -> tuple[str, ...]:
    """Technologies the draft claims experience with that the profile lacks."""
    supported = _supported_vocabulary(profile)
    claimed = {
        word
        for word in _words(body)
        if word in KNOWN_TECHNOLOGIES and word not in _AMBIGUOUS_TECHNOLOGIES
    }
    return tuple(sorted(claimed - supported))


def validate_draft(
    draft: Draft,
    *,
    posting: str,
    profile: Profile,
    expected_language: str,
) -> Validation:
    """Return every reason this draft should not be shown to the applicant."""
    problems: list[str] = []
    body = draft.body.strip()

    if not body:
        return Validation(("body is empty",))
    if not draft.subject.strip():
        problems.append("subject is empty")
    elif len(draft.subject) > 120:
        problems.append(f"subject is {len(draft.subject)} characters, limit is 120")

    words = draft.word_count
    if words < MIN_WORDS:
        problems.append(f"body is {words} words, minimum is {MIN_WORDS}")
    elif words > MAX_WORDS:
        problems.append(f"body is {words} words, maximum is {MAX_WORDS}")

    if not citation_is_grounded(draft.cited_detail, posting):
        # Proves the model read the posting.
        problems.append(f"cited detail is not in the posting: {draft.cited_detail!r}")

    if invented := unsupported_technologies(body, profile):
        # Proves the model did not invent the candidate. A grounded citation
        # says nothing about the sentences describing the applicant, and this
        # is where a fabricated career actually appears.
        problems.append(f"claims technologies absent from the profile: {', '.join(invented)}")

    folded_body = fold(body)
    for phrase in BANNED_PHRASES:
        if fold(phrase) in folded_body:
            problems.append(f"contains filler phrase {phrase!r}")

    if (found := PLACEHOLDER.search(body)) is not None:
        problems.append(f"contains unfilled placeholder {found.group(0)!r}")

    actual_language = detect_language(body)
    if actual_language != expected_language:
        problems.append(f"written in {actual_language}, expected {expected_language}")

    for match in YEARS_CLAIM.finditer(body):
        claimed = int(match.group(1))
        # Allow a year of slack: "graduated in 2026" style phrasing trips the
        # pattern, and rounding one year up is not a fabricated career.
        if claimed > profile.years_of_experience + 1:
            problems.append(
                f"claims {claimed} years of experience, profile says "
                f"{profile.years_of_experience}"
            )

    return Validation(tuple(problems))
