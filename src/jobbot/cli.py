"""Command line entry point.

Console output is deliberately ASCII-only: this runs on Windows terminals whose
default code page cannot encode symbols or emoji, and a UnicodeEncodeError in a
status line would kill an otherwise healthy batch run.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.table import Table
from sqlmodel import col, func, select

from jobbot import __version__
from jobbot.config import FEATURE_REQUIREMENTS, get_settings
from jobbot.db import drop_all, get_engine, session_scope, sync_schema
from jobbot.logging_setup import setup_logging
from jobbot.models import (
    Application,
    Company,
    Contact,
    Event,
    Job,
    PipelineRun,
    ReviewQueueItem,
)

if TYPE_CHECKING:
    from jobbot.config import Settings
    from jobbot.orchestrate import ArmResult, ArmSpec

# Rich strips colour automatically when stdout is not a terminal, which is what a
# scheduled run redirected to a log file needs; no extra handling here.
console = Console()

app = typer.Typer(
    name="jobbot",
    help="Multi-agent job application automation.",
    no_args_is_help=True,
    add_completion=False,
)
db_app = typer.Typer(help="Database maintenance.", no_args_is_help=True)
config_app = typer.Typer(help="Configuration inspection.", no_args_is_help=True)
boards_app = typer.Typer(help="ATS board registry.", no_args_is_help=True)
jobs_app = typer.Typer(help="Inspect stored postings.", no_args_is_help=True)
llm_app = typer.Typer(help="Local language model.", no_args_is_help=True)
mail_app = typer.Typer(help="Outbound mail (optional; not the default path).", no_args_is_help=True)
outbox_app = typer.Typer(help="Applications prepared for you to send.", no_args_is_help=True)
contacts_app = typer.Typer(help="Discovered application routes.", no_args_is_help=True)
companies_app = typer.Typer(help="The employer list, and growing it.", no_args_is_help=True)
schedule_app = typer.Typer(help="Unattended runs.", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(config_app, name="config")
app.add_typer(boards_app, name="boards")
app.add_typer(jobs_app, name="jobs")
app.add_typer(contacts_app, name="contacts")
app.add_typer(companies_app, name="companies")
app.add_typer(llm_app, name="llm")
app.add_typer(outbox_app, name="outbox")
app.add_typer(mail_app, name="mail")
app.add_typer(schedule_app, name="schedule")

COUNTED_TABLES = (
    ("companies", Company),
    ("jobs", Job),
    ("contacts", Contact),
    ("applications", Application),
    ("review_queue", ReviewQueueItem),
    ("pipeline_runs", PipelineRun),
    ("events", Event),
)


@app.callback()
def main(
    log_level: str = typer.Option(None, "--log-level", help="Override JOBBOT_LOG_LEVEL."),
) -> None:
    settings = get_settings()
    setup_logging(log_level or settings.log_level)


@app.command()
def version() -> None:
    """Print the installed version."""
    console.print(f"jobbot {__version__}")


@db_app.command("init")
def db_init() -> None:
    """Create every table that does not exist yet. Safe to re-run."""
    settings = get_settings()
    sync_schema()
    location = settings.db_path or settings.database_url
    console.print(f"[OK] schema ready at {location}")


@db_app.command("reset")
def db_reset(
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
) -> None:
    """Drop every table and recreate the schema. Destroys all data."""
    settings = get_settings()
    location = settings.db_path or settings.database_url
    if not yes:
        typer.confirm(
            f"This deletes all data in {location}. Continue?",
            abort=True,
        )
    drop_all()
    sync_schema()
    console.print(f"[OK] schema reset at {location}")


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Add columns the models gained since this database was created."""
    from jobbot.db import sync_schema

    settings = get_settings()
    try:
        added = sync_schema()
    except RuntimeError as error:
        console.print(f"[FAIL] {error}")
        raise typer.Exit(code=1) from None

    if not added:
        console.print("[OK] schema is up to date")
        return
    for table, columns in added.items():
        console.print(f"[OK] {table}: added {', '.join(columns)}")
    console.print(f"[..] {settings.db_path or settings.database_url}")


@db_app.command("status")
def db_status() -> None:
    """Show row counts per table."""
    engine = get_engine()
    table = Table(title="jobbot database")
    table.add_column("table")
    table.add_column("rows", justify="right")

    with session_scope(engine) as session:
        for name, model in COUNTED_TABLES:
            count = session.exec(select(func.count()).select_from(model)).one()
            table.add_row(name, str(count))

    console.print(table)


@config_app.command("check")
def config_check() -> None:
    """Report which features the current .env can actually run."""
    settings = get_settings()

    table = Table(title="jobbot configuration")
    table.add_column("setting")
    table.add_column("value")
    table.add_row("env", settings.env.value)
    table.add_row("database", str(settings.db_path or settings.database_url))
    table.add_row("applicant", settings.applicant_name or "(unset)")
    table.add_row("max applications/day", str(settings.max_applications_per_day))
    table.add_row("company cooldown (days)", str(settings.company_cooldown_days))
    table.add_row("min match score", str(settings.min_match_score))
    table.add_row("respect robots.txt", str(settings.respect_robots))
    console.print(table)

    features = Table(title="features")
    features.add_column("feature")
    features.add_column("status")
    features.add_column("missing")
    for feature in FEATURE_REQUIREMENTS:
        missing = settings.missing_for(feature)
        features.add_row(
            feature,
            "[OK] ready" if not missing else "[MISSING] not configured",
            ", ".join(missing) or "-",
        )
    console.print(features)


@app.command()
def run(
    arm: str = typer.Option("all", "--arm", help="Which arm to run: 0, 1, 2, 3, or all."),
    due_only: bool = typer.Option(
        False, "--due-only", help="Run only the arms whose interval has elapsed."
    ),
    stop_on_error: bool = typer.Option(
        False, "--stop-on-error", help="Stop at the first failing arm instead of continuing."
    ),
) -> None:
    """Run one arm of the pipeline, or the whole thing.

    Arms are independent, so by default a failing arm is recorded and the rest
    still run; the exit code is non-zero if any of them failed.
    """
    from jobbot import orchestrate
    from jobbot.locks import LockHeldError, pipeline_lock

    settings = get_settings()
    try:
        requested = orchestrate.arms_for(arm)
    except orchestrate.UnknownArmError:
        valid = ", ".join(str(spec.number) for spec in orchestrate.ARMS)
        console.print(f"[FAIL] unknown arm {arm!r}; expected one of {valid}, or 'all'")
        raise typer.Exit(code=2) from None

    try:
        with pipeline_lock(settings.lock_file, max_age_hours=settings.max_run_hours):
            _run_locked(
                requested, settings, due_only=due_only, stop_on_error=stop_on_error
            )
    except LockHeldError as error:
        console.print(f"[FAIL] {error}")
        raise typer.Exit(code=1) from None


def _run_locked(
    requested: list[ArmSpec],
    settings: Settings,
    *,
    due_only: bool,
    stop_on_error: bool,
) -> None:
    """The body of `run`, executed while holding the pipeline lock."""
    from jobbot import orchestrate

    if due_only:
        requested, waiting = orchestrate.select_due(requested, settings)
        for number, due_at in sorted(waiting.items()):
            console.print(f"[..] arm {number} not due until {due_at:%Y-%m-%d %H:%M} UTC")
        if not requested:
            console.print("[OK] nothing due")
            return

    failures = orchestrate.preflight(requested, settings)
    if failures:
        # Reported before any arm runs: arm 1 takes twenty minutes, and finding
        # out afterwards that arm 3 was never going to work wastes all of it.
        for failure in failures:
            console.print(f"[FAIL] arm {failure.arm}: {failure.reason}")
        raise typer.Exit(code=2)

    def announce(spec: orchestrate.ArmSpec) -> None:
        console.print(f"[..] {spec.label()}: {spec.description}")

    result = orchestrate.run_arms(
        requested,
        settings,
        stop_on_error=stop_on_error,
        on_start=announce,
        on_finish=_print_arm_result,
    )

    if result.failed:
        raise typer.Exit(code=1)


def _print_arm_result(result: ArmResult) -> None:
    """Render one arm's outcome, or the reason it produced none."""
    spec = result.spec
    if result.skipped_reason is not None:
        console.print(f"[..] {spec.label()} skipped: {result.skipped_reason}")
        return
    if result.error is not None:
        console.print(f"[FAIL] {spec.label()}: {result.error}")
        return

    outcome = result.outcome
    if outcome is None:  # pragma: no cover - the branches above cover this
        return

    table = Table(title=f"arm {spec.number} - {spec.name}")
    table.add_column("metric")
    table.add_column("count", justify="right")
    for label, value in outcome.metrics.items():
        table.add_row(label, str(value))
    console.print(table)

    if outcome.detail:
        detail = Table(title=outcome.detail_title or "breakdown")
        detail.add_column("reason")
        detail.add_column("count", justify="right")
        for label, value in sorted(outcome.detail.items(), key=lambda kv: -kv[1]):
            detail.add_row(label, str(value))
        console.print(detail)

    if outcome.hint:
        console.print(f"[..] {outcome.hint}")



@companies_app.command("discover")
def companies_discover() -> None:
    """Find new Turkish companies and add them for arm 2 to visit."""
    import asyncio

    from jobbot.arms import companies as discovery

    settings = get_settings()
    sync_schema()
    console.print("[..] discovering companies from directories and GitHub organisations")
    if not settings.github_token:
        console.print(
            "[..] GITHUB_TOKEN is unset; GitHub search is limited to 10 requests/minute"
        )

    with session_scope() as session:
        report = asyncio.run(discovery.run(session=session, settings=settings))

    table = Table(title="company discovery")
    table.add_column("source")
    table.add_column("found", justify="right")
    for source, count in sorted(report.per_source.items(), key=lambda kv: -kv[1]):
        table.add_row(source, str(count))
    table.add_row("[b]new companies[/b]", f"[b]{report.added}[/b]")
    table.add_row("already known", str(report.already_known))
    console.print(table)

    if report.added:
        console.print("[..] run 'jobbot run --arm 2' to visit them")


@companies_app.command("status")
def companies_status() -> None:
    """How many companies are known, and how many are still unvisited."""
    from jobbot.models import Contact

    sync_schema()
    with session_scope() as session:
        total = session.exec(select(func.count()).select_from(Company)).one()
        pending = session.exec(
            select(func.count())
            .select_from(Company)
            .where(col(Company.investigated_at).is_(None))
        ).one()
        with_contact = session.exec(
            select(func.count(func.distinct(col(Contact.company_id)))).select_from(Contact)
        ).one()

    table = Table(title="companies")
    table.add_column("metric")
    table.add_column("count", justify="right")
    table.add_row("known", str(total))
    table.add_row("visited", str(int(total) - int(pending)))
    table.add_row("waiting for arm 2", str(pending))
    table.add_row("with a way to apply", str(with_contact))
    console.print(table)


@contacts_app.command("list")
def contacts_list(
    kind: str = typer.Option(None, "--kind", help="Filter: email, career_page, ats_form."),
) -> None:
    """Show the application routes found so far."""
    from jobbot.models import Contact

    sync_schema()
    with session_scope() as session:
        query = (
            select(Contact, Company)
            .join(Company, Company.id == Contact.company_id)  # type: ignore[arg-type]
            .order_by(col(Contact.confidence).desc())
        )
        if kind:
            query = query.where(Contact.kind == kind)
        rows = session.exec(query).all()

        if not rows:
            console.print("[..] no contacts yet; run 'jobbot run --arm 2'")
            return

        table = Table(title=f"{len(rows)} contact(s)")
        table.add_column("company")
        table.add_column("kind")
        table.add_column("value")
        table.add_column("conf", justify="right")
        for contact_row, company in rows:
            table.add_row(
                company.name,
                contact_row.kind.value,
                contact_row.value[:60],
                f"{contact_row.confidence:.2f}",
            )
        console.print(table)


@jobs_app.command("top")
def jobs_top(
    limit: int = typer.Option(20, "--limit", help="How many postings to show."),
) -> None:
    """List the highest-scoring stored postings."""
    with session_scope() as session:
        rows = session.exec(
            select(Job, Company)
            .join(Company, Company.id == Job.company_id)  # type: ignore[arg-type]
            .order_by(Job.match_score.desc())  # type: ignore[union-attr]
            .limit(limit)
        ).all()

        if not rows:
            console.print("[..] no stored postings yet; run 'jobbot run --arm 1'")
            return

        table = Table(title=f"top {len(rows)} postings")
        table.add_column("score", justify="right")
        table.add_column("company")
        table.add_column("title")
        table.add_column("location")
        table.add_column("status")
        for job, company in rows:
            table.add_row(
                str(job.match_score),
                company.name,
                job.title[:50],
                (job.location or "-")[:28],
                job.status.value,
            )
        console.print(table)


@llm_app.command("health")
def llm_health() -> None:
    """Check that the local model is running and pulled."""
    from jobbot.llm import LlmUnavailableError, OllamaClient

    settings = get_settings()
    with OllamaClient(settings) as client:
        try:
            console.print(f"[OK] {client.health()}")
        except LlmUnavailableError as error:
            console.print(f"[FAIL] {error}")
            raise typer.Exit(code=2) from None

        installed = client.installed_models()
    console.print(f"[..] installed models: {', '.join(sorted(installed))}")


@llm_app.command("draft")
def llm_draft(
    job_id: int = typer.Option(None, "--job-id", help="Job to draft for; default is top scoring."),
    language: str = typer.Option(None, "--lang", help="Force 'tr' or 'en'."),
) -> None:
    """Generate one application draft for a stored posting and print it."""
    from jobbot.llm import CompositionFailedError, LlmUnavailableError, OllamaClient, compose
    from jobbot.profile import get_profile

    settings = get_settings()
    with session_scope() as session:
        query = select(Job, Company).join(Company, Company.id == Job.company_id)  # type: ignore[arg-type]
        query = (
            query.where(Job.id == job_id)
            if job_id
            else query.order_by(Job.match_score.desc())  # type: ignore[union-attr]
        )
        row = session.exec(query.limit(1)).first()
        if row is None:
            console.print("[FAIL] no matching posting; run 'jobbot run --arm 1' first")
            raise typer.Exit(code=2)
        job, company = row
        job_data = {
            "company_name": company.name,
            "title": job.title,
            "location": job.location or "",
            "description": job.raw_description or "",
        }

    console.print(f"[..] drafting for {job_data['company_name']} - {job_data['title']}")
    with OllamaClient(settings) as client:
        try:
            client.health()
            result = compose(
                client, profile=get_profile(), language=language, **job_data  # type: ignore[arg-type]
            )
        except LlmUnavailableError as error:
            console.print(f"[FAIL] {error}")
            raise typer.Exit(code=2) from None
        except CompositionFailedError as error:
            console.print(f"[FAIL] {error}")
            raise typer.Exit(code=1) from None

    console.print(f"\n[SUBJECT] {result.draft.subject}")
    console.print(f"\n[CITED]   {result.draft.cited_detail!r}")
    console.print(f"\n[BODY]\n{result.draft.body}\n")
    console.print(
        f"[..] {result.draft.word_count} words | cv={result.cv_variant} | "
        f"attempts={result.attempts} | {result.stats.output_tokens} tokens in "
        f"{result.stats.duration_seconds:.0f}s ({result.stats.tokens_per_second:.1f} tok/s)"
    )


@mail_app.command("login")
def mail_login() -> None:
    """Sign in to Microsoft with send-only permission."""
    import webbrowser

    from jobbot.senders import AuthError, DeviceCodeAuth

    settings = get_settings()
    with DeviceCodeAuth(settings) as auth:
        try:
            code = auth.begin_sign_in()
        except AuthError as error:
            console.print(f"[FAIL] {error}")
            raise typer.Exit(code=2) from None

        console.print("\n[..] open this page and enter the code:\n")
        console.print(f"      {code.verification_uri}")
        console.print(f"      code: {code.user_code}\n")
        console.print(
            "[..] you will sign in on Microsoft's own page; this tool never sees "
            "your password. It is asking only for permission to SEND mail."
        )
        with contextlib.suppress(Exception):
            webbrowser.open(code.verification_uri)

        try:
            auth.complete_sign_in(code, account=settings.applicant_email)
        except AuthError as error:
            console.print(f"[FAIL] {error}")
            raise typer.Exit(code=1) from None

    console.print(f"[OK] signed in; token stored at {auth.token_path}")


@mail_app.command("status")
def mail_status() -> None:
    """Show whether sending is configured, and today's remaining quota."""
    from jobbot.senders import DeviceCodeAuth, SendError, build_sender, sent_today

    settings = get_settings()
    table = Table(title="mail")
    table.add_column("item")
    table.add_column("value")

    missing = settings.missing_for("email")
    table.add_row("provider", settings.mail_provider)
    table.add_row("configured", "[OK] yes" if not missing else f"[MISSING] {', '.join(missing)}")
    table.add_row("from", settings.applicant_email or "(unset)")

    if settings.mail_provider == "outlook":
        with DeviceCodeAuth(settings) as auth:
            token = auth.stored_token()
            if token is None:
                table.add_row("signed in", "[MISSING] no - run 'jobbot mail login'")
            else:
                state = "fresh" if token.is_fresh else "expired (refreshes on use)"
                table.add_row("signed in", f"[OK] yes, {state}")
    elif not missing:
        table.add_row("server", f"{settings.smtp_host}:{settings.smtp_port}")
        # A live login is the only honest answer to "is this configured": the
        # password can be present and still rejected.
        try:
            build_sender(settings).health()
            table.add_row("credentials", "[OK] accepted by the server")
        except SendError as error:
            table.add_row("credentials", f"[FAIL] {error}")

    sync_schema()
    with session_scope() as session:
        today = sent_today(session)
    table.add_row("sent (24h)", f"{today} / {settings.max_applications_per_day}")
    table.add_row(
        "pacing", f"{settings.send_min_gap_seconds}-{settings.send_max_gap_seconds}s between sends"
    )
    table.add_row("company cooldown", f"{settings.company_cooldown_days} days")
    console.print(table)


@mail_app.command("logout")
def mail_logout() -> None:
    """Delete the stored token from this machine."""
    from jobbot.senders import DeviceCodeAuth

    with DeviceCodeAuth(get_settings()) as auth:
        removed = auth.forget()
    if removed:
        console.print("[OK] token deleted")
        console.print(
            "[..] to revoke access entirely, remove this app at "
            "https://account.live.com/consent/Manage"
        )
    else:
        console.print("[..] no stored token")


@mail_app.command("test")
def mail_test(
    to: str = typer.Option(..., "--to", help="Where to send the test message."),
) -> None:
    """Send one test message to an address you control."""
    from jobbot.senders import NotConfiguredError, SendError, build_sender

    settings = get_settings()
    sender = build_sender(settings)
    try:
        sender.send(
            to=to,
            subject="jobbot test message",
            body=(
                "This is a test message from jobbot.\n\n"
                "If you are reading it, outbound mail works."
            ),
        )
    except NotConfiguredError as error:
        console.print(f"[FAIL] {error}")
        raise typer.Exit(code=2) from None
    except SendError as error:
        console.print(f"[FAIL] {error}")
        raise typer.Exit(code=1) from None
    console.print(f"[OK] test message sent to {to} via {settings.mail_provider}")


@outbox_app.command("build")
def outbox_build(
    limit: int = typer.Option(10, "--limit", help="How many applications to prepare."),
) -> None:
    """Draft applications and write them out for you to send by hand."""
    from jobbot.llm import LlmUnavailableError, OllamaClient
    from jobbot.outbox import build
    from jobbot.profile import ProfileNotFoundError, get_profile

    settings = get_settings()
    try:
        profile = get_profile()
    except ProfileNotFoundError as error:
        console.print(f"[FAIL] {error}")
        raise typer.Exit(code=2) from None

    sync_schema()
    console.print(f"[..] drafting up to {limit} applications with {settings.llm_model}")
    console.print("[..] this runs a local model; expect about a minute each")

    with OllamaClient(settings) as client:
        try:
            client.health()
        except LlmUnavailableError as error:
            console.print(f"[FAIL] {error}")
            raise typer.Exit(code=2) from None

        with session_scope() as session:
            report = build(
                client, profile=profile, session=session, settings=settings, limit=limit
            )

    console.print(f"[OK] wrote {report.written} card(s) to {report.directory}")
    if report.skipped:
        table = Table(title="not queued")
        table.add_column("reason")
        table.add_column("count", justify="right")
        for reason, count in sorted(report.skipped.items(), key=lambda kv: -kv[1]):
            table.add_row(reason, str(count))
        console.print(table)
    if report.written:
        console.print(f"[..] start with {report.directory / 'README.md'}")


@outbox_app.command("list")
def outbox_list() -> None:
    """Show prepared applications and whether they have been sent."""
    from jobbot.models import Application, ApplicationStatus

    sync_schema()
    with session_scope() as session:
        rows = session.exec(
            select(Application, Company)
            .join(Company, Company.id == Application.company_id)  # type: ignore[arg-type]
            .order_by(Application.id)  # type: ignore[arg-type]
        ).all()

        if not rows:
            console.print("[..] nothing prepared yet; run 'jobbot outbox build'")
            return

        table = Table(title="outbox")
        table.add_column("#", justify="right")
        table.add_column("company")
        table.add_column("subject")
        table.add_column("status")
        for application, company in rows:
            marker = "[OK] sent" if application.status is ApplicationStatus.SENT else "pending"
            table.add_row(
                str(application.id),
                company.name,
                (application.subject or "")[:45],
                marker,
            )
        console.print(table)


@outbox_app.command("sent")
def outbox_sent(
    number: int = typer.Argument(..., help="The card number you just sent."),
) -> None:
    """Record that you sent a card, so its company is not queued again."""
    from jobbot.outbox import mark_sent

    sync_schema()
    with session_scope() as session:
        try:
            application = mark_sent(session, number)
        except LookupError as error:
            console.print(f"[FAIL] {error}")
            raise typer.Exit(code=2) from None
        except ValueError as error:
            console.print(f"[..] {error}")
            return
        company = session.get(Company, application.company_id)
        name = company.name if company else str(application.company_id)

    console.print(f"[OK] recorded {number} as sent to {name}")


@boards_app.command("list")
def boards_list() -> None:
    """Show the configured ATS boards."""
    from jobbot.connectors.boards import load_boards

    boards = load_boards()
    table = Table(title=f"{len(boards)} ATS boards")
    table.add_column("provider")
    table.add_column("token")
    table.add_column("company")
    table.add_column("country")
    for board in sorted(boards, key=lambda b: (b.provider, b.token)):
        table.add_row(board.provider, board.token, board.name, board.country or "-")
    console.print(table)


@boards_app.command("add")
def boards_add(
    provider: str = typer.Argument(..., help="greenhouse, lever, ashby, workable, ..."),
    token: str = typer.Argument(..., help="The company's board token."),
    name: str = typer.Option("", "--name", help="Company name."),
    domain: str = typer.Option("", "--domain", help="Company domain."),
    country: str = typer.Option("", "--country", help="ISO-ish country code, e.g. TR."),
    force: bool = typer.Option(False, "--force", help="Add even if the board returns nothing."),
) -> None:
    """Add a board to the registry, verifying it responds first."""
    import asyncio

    from jobbot.connectors.ats import PROVIDERS, Board
    from jobbot.connectors.boards import load_boards, merge_boards, save_boards
    from jobbot.net import HttpClient, RequestKind

    if provider not in PROVIDERS:
        console.print(f"[FAIL] unknown provider {provider!r}; expected {sorted(PROVIDERS)}")
        raise typer.Exit(code=2)

    board = Board(
        provider=provider,
        token=token,
        name=name or token,
        domain=domain,
        country=country or None,
    )

    async def count() -> int:
        async with HttpClient() as client:
            payload = await client.get_json(
                PROVIDERS[provider].endpoint(token), kind=RequestKind.API
            )
            return len(list(PROVIDERS[provider].extract(payload)))

    try:
        postings = asyncio.run(count())
    except Exception as error:
        postings = 0
        console.print(f"[MISSING] {provider}:{token} did not respond ({error})")

    # An unverified token silently polls nothing forever, so adding one is
    # deliberate rather than the default.
    if postings == 0 and not force:
        console.print("[FAIL] board returned no postings; re-run with --force to add anyway")
        raise typer.Exit(code=1)

    existing = load_boards()
    merged = merge_boards(existing, [board])
    if len(merged) == len(existing):
        console.print(f"[OK] {provider}:{token} was already in the registry")
        return

    path = save_boards(merged)
    console.print(f"[OK] added {provider}:{token} ({postings} postings) to {path}")


@boards_app.command("verify")
def boards_verify(
    prune: bool = typer.Option(False, "--prune", help="Remove boards that returned nothing."),
) -> None:
    """Check every configured board against its provider's API."""
    import asyncio

    from jobbot.connectors.ats import PROVIDERS, Board
    from jobbot.connectors.boards import load_boards, save_boards
    from jobbot.net import HttpClient, RequestKind

    boards = load_boards()

    async def probe() -> list[tuple[Board, int]]:
        async with HttpClient() as client:
            results: list[tuple[Board, int]] = []
            for board in boards:
                provider = PROVIDERS[board.provider]
                try:
                    payload = await client.get_json(
                        provider.endpoint(board.token), kind=RequestKind.API
                    )
                    count = len(list(provider.extract(payload)))
                except Exception:
                    count = 0
                results.append((board, count))
            return results

    results = asyncio.run(probe())
    live = [(board, count) for board, count in results if count > 0]
    dead = [board for board, count in results if count == 0]

    table = Table(title=f"{len(live)} live / {len(results)} boards")
    table.add_column("provider")
    table.add_column("token")
    table.add_column("postings", justify="right")
    for board, count in sorted(live, key=lambda row: -row[1]):
        table.add_row(board.provider, board.token, str(count))
    console.print(table)

    if dead:
        labels = ", ".join(f"{board.provider}:{board.token}" for board in dead)
        console.print(f"[MISSING] {len(dead)} returned nothing: {labels}")
        if prune:
            save_boards([board for board, _ in live])
            console.print(f"[OK] pruned to {len(live)} boards")


@schedule_app.command("status")
def schedule_status() -> None:
    """Show when each arm last ran and when it is next due."""
    from datetime import UTC, datetime

    from jobbot import orchestrate
    from jobbot.models import RunStatus, as_utc

    settings = get_settings()
    now = datetime.now(UTC)
    sync_schema()

    table = Table(title="schedule")
    table.add_column("arm")
    table.add_column("every", justify="right")
    table.add_column("last completed")
    table.add_column("last status")
    table.add_column("next due")

    with session_scope() as session:
        for spec in orchestrate.ARMS:
            latest = orchestrate.last_run(session, spec.number)
            completed = orchestrate.last_run(session, spec.number, status=RunStatus.COMPLETED)
            due_at = orchestrate.next_due_at(spec, settings, completed)
            started = as_utc(completed.started_at) if completed else None

            if due_at is None:
                due = "now (never run)"
            elif now >= due_at:
                due = "now"
            else:
                due = f"{due_at:%Y-%m-%d %H:%M} UTC"

            table.add_row(
                f"{spec.number} {spec.name}",
                f"{getattr(settings, spec.interval_setting)}h",
                f"{started:%Y-%m-%d %H:%M}" if started else "-",
                latest.status.value if latest else "-",
                due,
            )

    console.print(table)
    console.print("[..] 'jobbot run --due-only' runs exactly the arms marked due")


@schedule_app.command("install")
def schedule_install(
    every_hours: int = typer.Option(1, "--every-hours", help="How often the scheduler fires."),
    apply: bool = typer.Option(False, "--apply", help="Register it, instead of printing it."),
) -> None:
    """Print the scheduler entry that runs the pipeline unattended.

    The command it schedules is `run --due-only`, which decides for itself what
    is actually due — so the cadence lives in your .env, and the scheduler entry
    never has to change when you adjust it.
    """
    import subprocess
    import sys

    executable = Path(sys.executable).with_name("jobbot.exe")
    command = (
        f'"{executable}" run --due-only'
        if executable.exists()
        else f'"{sys.executable}" -m jobbot run --due-only'
    )

    if sys.platform == "win32":
        argv = [
            "schtasks", "/create", "/tn", "jobbot", "/sc", "hourly",
            "/mo", str(every_hours), "/tr", command,
        ]
        printable = " ".join(argv)
    else:
        printable = f"0 */{every_hours} * * * {command}"
        argv = []

    # soft_wrap keeps this on one line: the whole point is that it can be copied
    # and pasted, and a line break lands in the middle of the path.
    console.print(f"\n  {printable}\n", soft_wrap=True)

    if not apply:
        where = "Task Scheduler" if sys.platform == "win32" else "your crontab"
        console.print(f"[..] run that once to register it with {where}, or re-run with --apply")
        return

    if not argv:
        # Rewriting a user's crontab from here is more intrusive than useful, and
        # a bad edit costs them every other scheduled job on the machine.
        console.print("[FAIL] --apply is Windows-only; add the cron line above by hand")
        raise typer.Exit(code=2)

    completed = subprocess.run(argv, capture_output=True, text=True)
    if completed.returncode != 0:
        console.print(f"[FAIL] schtasks: {completed.stderr.strip() or completed.stdout.strip()}")
        raise typer.Exit(code=1)
    console.print("[OK] registered as scheduled task 'jobbot'")
    console.print("[..] remove it with: schtasks /delete /tn jobbot /f")
