from sqlalchemy import Integer, String, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import TimestampMixin


class ManagerAssignment(Base, TimestampMixin):
    """One (employee → manager) edge, ported from the Staff Directory
    ``AllManagers`` multi-value person field.

    In SharePoint an employee's managers live inline as a list of person
    entries ({LookupId, LookupValue}). Relational storage models that as its
    own table: one row per manager. ``position`` preserves the original list
    order so the *primary* manager (today ``managers[0]``) is the lowest
    position — the app assigns the primary manager to a new request and emails
    all of them for approval.

    The manager is identified by ``manager_sp_user_lookup_id`` (the AllManagers
    LookupId, which is a User Information List id — the same id space as
    Employee.sp_user_lookup_id), not by a foreign key, because AllManagers
    stores user ids rather than Staff Directory item ids.
    """

    __tablename__ = "manager_assignments"
    __table_args__ = (
        UniqueConstraint("employee_id", "manager_sp_user_lookup_id", name="uq_manager_assignment"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id"), index=True, nullable=False
    )
    manager_sp_user_lookup_id: Mapped[int] = mapped_column(Integer, nullable=False)  # AllManagers LookupId
    manager_name: Mapped[str | None] = mapped_column(String, nullable=True)          # AllManagers LookupValue
    # Preserves AllManagers list order; primary manager = lowest position.
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
