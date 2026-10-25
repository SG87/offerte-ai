"""Background AI jobs: the request starts the job and returns at once; the page header shows what is running
and pops up a notice when it is done, on whatever page the user is.

Jobs run in the web process (FastAPI background tasks). A job still 'bezig' after STALE_AFTER_S was cut off,
e.g. by a server restart, and counts as failed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from app.ai.client import AIUnavailable
from app.db import session_scope
from app.models import AIJob, Organisation

log = logging.getLogger(__name__)
STALE_AFTER_S = 20 * 60


def start(
    session: Session,
    org: Organisation,
    kind: str,
    title: str,
    link: str | None = None,
    project_id: int | None = None,
    ref_id: int | None = None,
) -> AIJob:
    job = AIJob(org_id=org.id, kind=kind, title=title, link=link, project_id=project_id, ref_id=ref_id)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def finish(session: Session, job: AIJob, ok: bool, message: str, link: str | None = None) -> AIJob:
    job.status = "klaar" if ok else "mislukt"
    job.message = message
    job.finished_at = datetime.now(UTC)
    if link:
        job.link = link
    session.add(job)
    session.commit()
    return job


def run(job_id: int, work: Callable[[Session, Organisation, AIJob], tuple[str, str | None]]) -> None:
    """Background entry point: `work` returns (message, link). Any exception marks the job failed."""
    with session_scope() as session:
        job = session.get(AIJob, job_id)
        org = session.get(Organisation, job.org_id) if job else None
        if not job or not org:
            return
        try:
            message, link = work(session, org, job)
            finish(session, job, True, message, link)
        except AIUnavailable as exc:
            session.rollback()
            finish(session, session.get(AIJob, job_id), False, str(exc))
        except Exception as exc:
            log.exception("AI-job %s mislukt", job_id)
            session.rollback()
            finish(session, session.get(AIJob, job_id), False, f"{type(exc).__name__}: {exc}")


def _expire(session: Session, jobs: list[AIJob]) -> list[AIJob]:
    now = datetime.now(UTC)
    for job in jobs:
        started = job.created_at if job.created_at.tzinfo else job.created_at.replace(tzinfo=UTC)
        if job.status == "bezig" and now - started > timedelta(seconds=STALE_AFTER_S):
            finish(session, job, False, "Onderbroken (bv. door een herstart van de server). Probeer opnieuw.")
    return jobs


def running(session: Session, org: Organisation, project_id: int | None = None, kind: str | None = None) -> list[AIJob]:
    q = select(AIJob).where(AIJob.org_id == org.id, AIJob.status == "bezig")
    if project_id is not None:
        q = q.where(AIJob.project_id == project_id)
    if kind:
        q = q.where(AIJob.kind == kind)
    jobs = _expire(session, list(session.exec(q.order_by(AIJob.id)).all()))
    return [j for j in jobs if j.status == "bezig"]


def unseen_finished(session: Session, org: Organisation) -> list[AIJob]:
    return list(
        session.exec(
            select(AIJob)
            .where(AIJob.org_id == org.id, AIJob.status != "bezig", AIJob.seen == False)  # noqa: E712
            .order_by(AIJob.id)
        ).all()
    )


def mark_seen(session: Session, org: Organisation, job_id: int | None = None) -> None:
    q = select(AIJob).where(AIJob.org_id == org.id, AIJob.status != "bezig", AIJob.seen == False)  # noqa: E712
    if job_id is not None:
        q = q.where(AIJob.id == job_id)
    for job in session.exec(q).all():
        job.seen = True
        session.add(job)
    session.commit()


def as_dict(job: AIJob) -> dict:
    started = job.created_at if job.created_at.tzinfo else job.created_at.replace(tzinfo=UTC)
    return {
        "id": job.id,
        "kind": job.kind,
        "title": job.title,
        "status": job.status,
        "message": job.message,
        "link": job.link,
        "project_id": job.project_id,
        "elapsed_s": int((datetime.now(UTC) - started).total_seconds()),
    }


def snapshot(session: Session, org: Organisation) -> dict:
    return {
        "running": [as_dict(j) for j in running(session, org)],
        "finished": [as_dict(j) for j in unseen_finished(session, org)],
    }
