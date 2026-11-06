from decimal import Decimal

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..groups import (
    cancel_group,
    create_group,
    get_group,
    group_members,
    join_group,
    leave_group,
    pay_share,
    review_eligibility,
    seat_prices,
)
from ..models import Group, User
from ..payments_client import PaymentsClient, get_payments_client
from ..schemas import (
    BookingStatus,
    GroupCreate,
    GroupJoin,
    GroupMemberOut,
    GroupOut,
    MemberPaymentStatus,
    ReviewEligibilityOut,
)
from ..security import get_current_user
from ..timeutil import ist_local
from .bookings import _seats_out as labelled_seats

router = APIRouter(prefix="/api", tags=["groups"])


def _group_out(db: Session, group: Group) -> GroupOut:
    """The group and its members, with seats labelled as the hold labelled them."""
    prices = seat_prices(db, list(group.seat_ids))
    return GroupOut(
        groupId=group.id,
        inviteCode=group.invite_code,
        status=BookingStatus(group.status),
        showId=group.show_id,
        ownerId=group.owner_id,
        seatIds=list(group.seat_ids),
        expiresAt=ist_local(group.expires_at),
        total=float(sum(prices.values(), Decimal("0"))),
        members=[
            GroupMemberOut(
                userId=member.user_id,
                seatIds=list(member.seat_ids),
                seats=labelled_seats(db, list(member.seat_ids)),
                shareAmount=float(member.share_amount),
                paymentStatus=MemberPaymentStatus(member.payment_status),
            )
            for member in group_members(db, group.id)
        ],
    )


@router.post("/groups", response_model=GroupOut, status_code=status.HTTP_201_CREATED)
def post_group(
    payload: GroupCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> GroupOut:
    """Open a group from a live hold; every seat is priced separately for its member."""
    group = create_group(db=db, hold_id=payload.holdId, user=user)
    return _group_out(db, group)


@router.get("/groups/{group_id}", response_model=GroupOut)
def read_group(
    group_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> GroupOut:
    group = get_group(db=db, group_id=group_id, user=user)
    return _group_out(db, group)


@router.post("/groups/join", response_model=GroupOut)
def post_group_join(
    payload: GroupJoin,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> GroupOut:
    """Join a group by its invite code; the next unassigned seats become yours."""
    group = join_group(db=db, invite_code=payload.inviteCode, user=user)
    return _group_out(db, group)


@router.post(
    "/groups/{group_id}/pay",
    response_model=GroupOut,
    status_code=status.HTTP_200_OK,
)
def post_group_pay(
    group_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    client: PaymentsClient = Depends(get_payments_client),
) -> GroupOut:
    """Pay the caller's own share; the payments service never charges it twice."""
    pay_share(db=db, group_id=group_id, user=user, client=client)
    return _group_out(db, get_group(db=db, group_id=group_id, user=user))


@router.delete("/groups/{group_id}/members/me", response_model=GroupOut)
def delete_group_member_me(
    group_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    client: PaymentsClient = Depends(get_payments_client),
) -> GroupOut:
    """Leave a group: a paid share is refunded and the seats go back to the pool."""
    group = leave_group(db=db, group_id=group_id, user=user, client=client)
    return _group_out(db, group)


@router.post("/groups/{group_id}/cancel", response_model=GroupOut)
def post_group_cancel(
    group_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    client: PaymentsClient = Depends(get_payments_client),
) -> GroupOut:
    """Cancel a group as its owner: every paid share is refunded, seats go back on sale."""
    group = cancel_group(db=db, group_id=group_id, user=user, client=client)
    return _group_out(db, group)


@router.get(
    "/groups/{group_id}/review-eligibility", response_model=ReviewEligibilityOut
)
def read_review_eligibility(
    group_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ReviewEligibilityOut:
    """Whether the caller may review the group's movie; the client owns the show clock."""
    eligible, movie_id, show_starts_at = review_eligibility(
        db=db, group_id=group_id, user=user
    )
    return ReviewEligibilityOut(
        eligible=eligible,
        movieId=movie_id,
        showStartsAt=ist_local(show_starts_at),
    )
