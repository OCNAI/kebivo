"""Group delivery coverage shared by live routing and the simulator."""

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from kebivo.models import Delivery, FeedbackGroupMember, ProcessingJob


def delivered_destinations(db: Session, group_id: str, event_id: str | None = None) -> set[str]:
    query = (
        select(Delivery.destination_id)
        .join(FeedbackGroupMember, FeedbackGroupMember.event_id == Delivery.event_id)
        .where(FeedbackGroupMember.group_id == group_id, Delivery.status != "failed")
    )
    if event_id is not None:
        query = query.where(Delivery.event_id != event_id)
    return set(db.scalars(query))


def has_unrouted_earlier_member(
    db: Session, group_id: str, member: FeedbackGroupMember | None = None
) -> bool:
    """A pending predecessor may still create the group's first delivery."""
    query = (
        select(ProcessingJob.id)
        .join(FeedbackGroupMember, FeedbackGroupMember.event_id == ProcessingJob.event_id)
        .where(
            FeedbackGroupMember.group_id == group_id,
            ProcessingJob.status.in_(("pending", "processing")),
        )
        .limit(1)
    )
    if member is not None:
        query = query.where(
            or_(
                FeedbackGroupMember.added_at < member.added_at,
                and_(
                    FeedbackGroupMember.added_at == member.added_at,
                    FeedbackGroupMember.event_id < member.event_id,
                ),
            )
        )
    return db.scalar(query) is not None
