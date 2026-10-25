"""Planning: work blocks on a calendar with the employees who do them, and employee absences."""

from __future__ import annotations

import calendar
import json
import math
from datetime import date, timedelta

from sqlmodel import Session, select

from app.i18n import t
from app.models import Absence, Employee, Organisation, PlanningAssignee, PlanningItem, Project

PALETTE = ["#1f5fbf", "#0f8a5f", "#b45309", "#7c3aed", "#be185d", "#0e7490", "#4d7c0f", "#9a3412"]


def employees(session: Session, org: Organisation, active_only: bool = True) -> list[Employee]:
    q = select(Employee).where(Employee.org_id == org.id)
    if active_only:
        q = q.where(Employee.active == True)  # noqa: E712
    return list(session.exec(q.order_by(Employee.name)).all())


def employee_color(emp: Employee) -> str:
    return emp.color or PALETTE[(emp.id or 0) % len(PALETTE)]


# ---------- absences ----------


def absences(session: Session, employee_ids: list[int], start: date, end: date) -> list[Absence]:
    if not employee_ids:
        return []
    return list(
        session.exec(
            select(Absence).where(
                Absence.employee_id.in_(employee_ids),  # type: ignore[attr-defined]
                Absence.date_from <= end,
                Absence.date_to >= start,
            )
        ).all()
    )


def add_absence(session: Session, emp: Employee, date_from: date, date_to: date, reason: str, note: str | None) -> Absence:
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    row = Absence(employee_id=emp.id, date_from=date_from, date_to=date_to, reason=reason, note=note)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def absent_on(session: Session, employee_ids: list[int], day: date) -> set[int]:
    return {a.employee_id for a in absences(session, employee_ids, day, day)}


def unavailable(session: Session, employee_ids: list[int], start: date, end: date) -> dict[int, list[Absence]]:
    out: dict[int, list[Absence]] = {}
    for a in absences(session, employee_ids, start, end):
        out.setdefault(a.employee_id, []).append(a)
    return out


# ---------- planning items ----------


def items_between(session: Session, org: Organisation, start: date, end: date) -> list[PlanningItem]:
    return list(
        session.exec(
            select(PlanningItem).where(
                PlanningItem.org_id == org.id, PlanningItem.date_from <= end, PlanningItem.date_to >= start
            ).order_by(PlanningItem.date_from, PlanningItem.start_time)
        ).all()
    )


def items_for_project(session: Session, project: Project) -> list[PlanningItem]:
    return list(
        session.exec(
            select(PlanningItem).where(PlanningItem.project_id == project.id).order_by(PlanningItem.date_from)
        ).all()
    )


def assignees(session: Session, items: list[PlanningItem]) -> dict[int, list[int]]:
    if not items:
        return {}
    rows = session.exec(
        select(PlanningAssignee).where(PlanningAssignee.item_id.in_([i.id for i in items]))  # type: ignore[attr-defined]
    ).all()
    out: dict[int, list[int]] = {i.id: [] for i in items}
    for r in rows:
        out.setdefault(r.item_id, []).append(r.employee_id)
    return out


def save_item(
    session: Session,
    org: Organisation,
    item: PlanningItem | None,
    *,
    project_id: int | None,
    title: str | None,
    date_from: date,
    date_to: date,
    employee_ids: list[int],
    start_time: str | None = None,
    hours: float | None = None,
    note: str | None = None,
    status: str = "gepland",
) -> tuple[PlanningItem, list[str]]:
    """Create or update a block. Employees absent on one of the days are kept out; their names come back as warnings."""
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    item = item or PlanningItem(org_id=org.id, date_from=date_from, date_to=date_to)
    item.project_id, item.title = project_id, (title or None)
    item.date_from, item.date_to = date_from, date_to
    item.start_time, item.hours, item.note, item.status = (start_time or None), hours, (note or None), status
    session.add(item)
    session.commit()
    session.refresh(item)
    away = unavailable(session, employee_ids, date_from, date_to)
    warnings = []
    for old in session.exec(select(PlanningAssignee).where(PlanningAssignee.item_id == item.id)).all():
        session.delete(old)
    for eid in dict.fromkeys(employee_ids):
        if eid in away:
            emp = session.get(Employee, eid)
            a = away[eid][0]
            warnings.append(f"{emp.name if emp else eid} is afwezig ({a.reason}, {a.date_from:%d/%m}–{a.date_to:%d/%m}) en is niet ingepland.")
            continue
        session.add(PlanningAssignee(item_id=item.id, employee_id=eid))
    session.commit()
    return item, warnings


# ---------- following the real work ----------


def _absent_crew(session: Session, item: PlanningItem) -> list[str]:
    """Assigned employees who are absent somewhere in the (moved) block."""
    crew = assignees(session, [item]).get(item.id, [])
    out = []
    for eid, rows in unavailable(session, crew, item.date_from, item.date_to).items():
        emp = session.get(Employee, eid)
        out.append(f"{emp.name if emp else eid} is afwezig ({rows[0].reason}, {rows[0].date_from:%d/%m}–{rows[0].date_to:%d/%m})")
    return out


def follow_start(session: Session, org: Organisation, project: Project, started: date) -> list[str]:
    """The work really started on `started`: the project's blocks move so the first one begins that day (same
    length, the others keep their distance) and it turns 'bezig'. Without a block, one is made from the proposal.
    Returns lines for the project history."""
    items = sorted(items_for_project(session, project), key=lambda i: i.date_from)
    if not items:
        days = 1
        if project.proposed_start and project.proposed_end:
            days = max(1, sum(1 for i in range((project.proposed_end - project.proposed_start).days + 1)
                              if (project.proposed_start + timedelta(days=i)).weekday() < 5))
        end = max(started, working_days(started, days)[-1])
        item, warnings = save_item(session, org, None, project_id=project.id, title=None, date_from=started, date_to=end,
                                   employee_ids=project.proposed_crew, start_time="08:00",
                                   note="Gestart zonder planning", status="bezig")
        return [f"Planning: werk gestart {started:%d/%m}, blok {started:%d/%m}–{end:%d/%m} aangemaakt."] + warnings
    delta = started - items[0].date_from
    for item in items:
        item.date_from, item.date_to = item.date_from + delta, item.date_to + delta
        session.add(item)
    items[0].status = "bezig"
    session.commit()
    if not delta.days:
        return []  # started as planned: nothing moved
    n = abs(delta.days)
    out = [
        f"Planning: werk gestart {started:%d/%m}, {n} dag{'' if n == 1 else 'en'} {'later' if delta.days > 0 else 'vroeger'}"
        f" dan gepland; blok nu {items[0].date_from:%d/%m}–{items[0].date_to:%d/%m}."
    ]
    for item in items:
        out += [f"Let op: {w}." for w in _absent_crew(session, item)]
    return out


def follow_done(session: Session, org: Organisation, project: Project, done: date) -> list[str]:
    """The work was finished on `done`: blocks that would only start later are dropped, the last remaining one
    ends that day (shortened or lengthened) and everything turns 'klaar'."""
    items = sorted(items_for_project(session, project), key=lambda i: i.date_from)
    if not items:
        if not project.work_started_on:
            return []
        save_item(session, org, None, project_id=project.id, title=None, date_from=project.work_started_on,
                  date_to=done, employee_ids=project.proposed_crew, note="Uitgevoerd zonder planning", status="klaar")
        return [f"Planning: uitgevoerd {project.work_started_on:%d/%m}–{done:%d/%m} toegevoegd."]
    dropped = [i for i in items if i.date_from > done]
    kept = [i for i in items if i.date_from <= done]
    for item in dropped:
        delete_item(session, item)
    out = []
    if kept:
        last = kept[-1]
        if last.date_to != done:
            out.append(f"Planning: blok eindigt nu op {done:%d/%m} (was {last.date_to:%d/%m}).")
        last.date_to = done
        for item in kept:
            item.status = "klaar"
            session.add(item)
        session.commit()
    if dropped:
        out.append(f"Planning: {len(dropped)} later blok{'' if len(dropped) == 1 else 'ken'} geschrapt; het werk is af.")
    return out


def set_project_items_status(session: Session, project: Project, status: str) -> None:
    for item in items_for_project(session, project):
        item.status = status
        session.add(item)
    session.commit()


def delete_item(session: Session, item: PlanningItem) -> None:
    for a in session.exec(select(PlanningAssignee).where(PlanningAssignee.item_id == item.id)).all():
        session.delete(a)
    session.delete(item)
    session.commit()


# ---------- calendar helpers ----------


def month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def month_grid(year: int, month: int) -> list[list[date]]:
    """Weeks (Mon–Sun) covering the month, padded with days of the neighbouring months."""
    first, last = month_bounds(year, month)
    start = first - timedelta(days=first.weekday())
    end = last + timedelta(days=6 - last.weekday())
    weeks, d = [], start
    while d <= end:
        weeks.append([d + timedelta(days=i) for i in range(7)])
        d += timedelta(days=7)
    return weeks


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    m = month - 1 + delta
    return year + m // 12, m % 12 + 1


def by_day(items: list[PlanningItem], start: date, end: date) -> dict[date, list[PlanningItem]]:
    out: dict[date, list[PlanningItem]] = {}
    for it in items:
        d = max(it.date_from, start)
        while d <= min(it.date_to, end):
            out.setdefault(d, []).append(it)
            d += timedelta(days=1)
    return out


def absences_by_day(rows: list[Absence], start: date, end: date) -> dict[date, list[Absence]]:
    out: dict[date, list[Absence]] = {}
    for a in rows:
        d = max(a.date_from, start)
        while d <= min(a.date_to, end):
            out.setdefault(d, []).append(a)
            d += timedelta(days=1)
    return out


def workload(session: Session, org: Organisation, start: date, end: date) -> dict[int, float]:
    """Planned hours per employee in the window (per day: item.hours or the employee's hours_per_day)."""
    items = items_between(session, org, start, end)
    who = assignees(session, items)
    emps = {e.id: e for e in employees(session, org, active_only=False)}
    out: dict[int, float] = {}
    for it in items:
        days = (min(it.date_to, end) - max(it.date_from, start)).days + 1
        for eid in who.get(it.id, []):
            per_day = it.hours if it.hours else emps[eid].hours_per_day if eid in emps else 8.0
            out[eid] = out.get(eid, 0.0) + per_day * days
    return out


# ---------- proposal: who could do the work, and when ----------

WORK_ROLES = {"vakman", "ploegbaas"}
LEAD_DAYS = 7  # earliest proposed start: a week from today, so the client has time to accept
SEARCH_DAYS = 180
HOURS_PER_PERSON_PER_PROJECT_DAY = 8.0


def workers(session: Session, org: Organisation) -> list[Employee]:
    """Employees who do the work on site; if nobody has such a role, everybody who is active."""
    emps = employees(session, org)
    on_site = [e for e in emps if e.role in WORK_ROLES]
    return on_site or emps


def busy_days(session: Session, org: Organisation, employee_ids: list[int], start: date, end: date) -> dict[int, set[date]]:
    """Per employee: the days they are absent or already planned."""
    out: dict[int, set[date]] = {eid: set() for eid in employee_ids}
    for a in absences(session, employee_ids, start, end):
        d = max(a.date_from, start)
        while d <= min(a.date_to, end):
            out[a.employee_id].add(d)
            d += timedelta(days=1)
    items = items_between(session, org, start, end)
    who = assignees(session, items)
    for it in items:
        for eid in who.get(it.id, []):
            if eid in out:
                d = max(it.date_from, start)
                while d <= min(it.date_to, end):
                    out[eid].add(d)
                    d += timedelta(days=1)
    return out


def working_days(start: date, n: int) -> list[date]:
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def crew_and_days(hours_total: float | None, available: int) -> tuple[int, int]:
    """How many people and how many working days: ~1 person per 40 h, max 3; a project without quote = 1 day."""
    if available <= 0:
        return 0, 1
    if not hours_total:
        return min(2, available), 1
    size = min(available, max(1, min(3, math.ceil(hours_total / 40))))
    days = max(1, math.ceil(hours_total / (size * HOURS_PER_PERSON_PER_PROJECT_DAY)))
    return size, days


def suggest(
    session: Session, org: Organisation, project: Project, hours_total: float | None = None, not_before: date | None = None
) -> dict:
    """Earliest window (working days) in which enough workers are free, and who: the least-loaded free ones.
    Returns {crew: [ids], start, end, days, text}. Empty crew when there are no employees."""
    pool = workers(session, org)
    size, days = crew_and_days(hours_total, len(pool))
    first = not_before or (date.today() + timedelta(days=LEAD_DAYS))
    if not pool:
        start = working_days(first, 1)[0]
        return {"crew": [], "start": start, "end": working_days(start, days)[-1], "days": days, "text": "Nog geen werknemers ingevoerd."}
    horizon = first + timedelta(days=SEARCH_DAYS)
    busy = busy_days(session, org, [e.id for e in pool], first, horizon)
    load = workload(session, org, first, horizon)
    d = first
    while d <= horizon:
        if d.weekday() < 5:
            window = working_days(d, days)
            free = [e for e in pool if not (busy[e.id] & set(window))]
            if len(free) >= size:
                free.sort(key=lambda e: (load.get(e.id, 0.0), e.name))
                crew = free[:size]
                return {
                    "crew": [e.id for e in crew],
                    "start": window[0],
                    "end": window[-1],
                    "days": days,
                    "text": f"{', '.join(e.name for e in crew)} {'is' if size == 1 else 'zijn'} vrij van {window[0]:%d/%m} tot {window[-1]:%d/%m} ({days} werkdag{'' if days == 1 else 'en'}).",
                }
        d += timedelta(days=1)
    window = working_days(first, days)
    return {"crew": [], "start": window[0], "end": window[-1], "days": days, "text": "Geen volledig vrije periode gevonden in de komende 6 maanden; kies zelf."}


def apply_suggestion(session: Session, project: Project, s: dict, note: str | None = None) -> Project:
    project.proposed_start, project.proposed_end = s["start"], s["end"]
    project.proposed_crew_json = json.dumps(s["crew"])
    project.proposal_note = note if note is not None else s.get("text")
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


def proposal_conflicts(session: Session, org: Organisation, project: Project) -> list[str]:
    """Crew members who are absent or planned elsewhere in the proposed window (for the project page)."""
    if not (project.proposed_start and project.proposed_crew):
        return []
    end = project.proposed_end or project.proposed_start
    busy = busy_days(session, org, project.proposed_crew, project.proposed_start, end)
    names = {e.id: e.name for e in employees(session, org, active_only=False)}
    return [f"{names.get(eid, eid)} is niet vrij op {', '.join(f'{d:%d/%m}' for d in sorted(ds))}" for eid, ds in busy.items() if ds]


def proposal_sentence(project: Project, names: dict[int, str], lang: str = "nl") -> str | None:
    """For the quote, in the customer's language: 'Voorgestelde uitvoering: 10/11/2026 – 12/11/2026
    (3 werkdagen, 2 medewerkers), onder voorbehoud …'."""
    if not project.proposed_start:
        return None
    end = project.proposed_end or project.proposed_start
    days = sum(1 for i in range((end - project.proposed_start).days + 1) if (project.proposed_start + timedelta(days=i)).weekday() < 5)
    when = f"{project.proposed_start:%d/%m/%Y}" if end == project.proposed_start else f"{project.proposed_start:%d/%m/%Y} – {end:%d/%m/%Y}"
    crew = len(project.proposed_crew)
    parts = [t(lang, "werkdag" if days == 1 else "werkdagen", n=days)]
    if crew:
        parts.append(t(lang, "medewerker" if crew == 1 else "medewerkers", n=crew))
    return t(lang, "voorstel", when=when, parts=", ".join(parts))
