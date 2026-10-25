"""To-do list: things somebody has to do. Most are created by the workflow (confirm an order, plan the work,
send the invoice); users add their own too."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlmodel import Session, select

from app.models import Employee, Organisation, Todo

# How an employee wants to be notified. Only mail is wired up; the others are shown as "binnenkort".
CHANNELS = {"mail": "E-mail", "whatsapp": "WhatsApp", "sms": "Bericht (sms)", "teams": "Teams", "push": "Pushmelding"}
AVAILABLE_CHANNELS = {"mail"}


def add(
    session: Session,
    org: Organisation,
    title: str,
    kind: str = "manueel",
    detail: str | None = None,
    link: str | None = None,
    project_id: int | None = None,
    ref_id: int | None = None,
    assignee_id: int | None = None,
    due_in_days: int | None = None,
    due: date | None = None,
) -> Todo:
    if due is None and due_in_days is not None:
        due = date.today() + timedelta(days=due_in_days)
    todo = Todo(
        org_id=org.id,
        title=title,
        kind=kind,
        detail=detail,
        link=link,
        project_id=project_id,
        ref_id=ref_id,
        assignee_id=assignee_id,
        due=due,
    )
    session.add(todo)
    session.commit()
    session.refresh(todo)
    return todo


def find_open(session: Session, org: Organisation, kind: str, ref_id: int | None = None, project_id: int | None = None) -> Todo | None:
    q = select(Todo).where(Todo.org_id == org.id, Todo.kind == kind, Todo.status == "open")
    if ref_id is not None:
        q = q.where(Todo.ref_id == ref_id)
    if project_id is not None:
        q = q.where(Todo.project_id == project_id)
    return session.exec(q).first()


def complete(session: Session, todo: Todo) -> Todo:
    if todo.status != "gedaan":
        todo.status, todo.done_at = "gedaan", datetime.now(UTC)
        session.add(todo)
        session.commit()
    return todo


def reopen(session: Session, todo: Todo) -> Todo:
    todo.status, todo.done_at = "open", None
    session.add(todo)
    session.commit()
    return todo


def complete_matching(session: Session, org: Organisation, kind: str, ref_id: int | None = None, project_id: int | None = None) -> int:
    n = 0
    while todo := find_open(session, org, kind, ref_id, project_id):
        complete(session, todo)
        n += 1
    return n


def list_todos(session: Session, org: Organisation, status: str | None = "open", assignee_id: int | None = None) -> list[Todo]:
    q = select(Todo).where(Todo.org_id == org.id)
    if status:
        q = q.where(Todo.status == status)
    if assignee_id:
        q = q.where(Todo.assignee_id == assignee_id)
    order = (Todo.due.is_(None), Todo.due, Todo.id) if status == "open" else (Todo.done_at.desc(),)  # type: ignore[union-attr]
    return list(session.exec(q.order_by(*order)).all())


def count_open(session: Session, org: Organisation) -> int:
    return len(list_todos(session, org, "open"))


def responsible(session: Session, org: Organisation, flag: str) -> list[Employee]:
    """Employees with the given responsibility flag ('stock_manager' or 'planner')."""
    col = getattr(Employee, flag)
    return list(
        session.exec(select(Employee).where(Employee.org_id == org.id, Employee.active == True, col == True)).all()  # noqa: E712
    )


def responsible_emails(session: Session, org: Organisation, flag: str) -> list[str]:
    """Mail addresses for a responsibility; falls back to the purchase contact / organisation address."""
    emails = [e.email for e in responsible(session, org, flag) if e.email]
    if emails:
        return emails
    if flag == "stock_manager" and org.purchase_contact_email:
        return [org.purchase_contact_email]
    return [org.email]
