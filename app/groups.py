"""Group bookings: one hold split between members who each pay their own share.

A group is its own aggregate and never creates a `Booking` row. Its seats stay HELD
on the original hold until every seat of the hold is assigned to a member and every
member's share is paid, so a group that never fully pays simply lets the existing
hold sweeper expire the seats.
"""

import secrets
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .bookings import (
    BOOKING_CURRENCY,
    HOLD_EXPIRED_DETAIL,
    NOT_OWNER_DETAIL,
    PAYMENTS_UNAVAILABLE_DETAIL,
    PAYMENT_REFUND_FAILED_DETAIL,
)
from .models import Group, GroupMember, Seat, SeatCategory, Show, ShowSeat, User
from .payments_client import PaymentsClient, PaymentsUnavailableError
from .schemas import BookingStatus, MemberPaymentStatus, ShowSeatStatus
from .seating import effective_status

INVITE_CODE_LENGTH = 6
INVITE_CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
INVITE_CODE_ATTEMPTS = 10
CENT = Decimal("0.01")
NOT_A_MEMBER_DETAIL = "You are not a member of this group"
INVITE_CODE_EXHAUSTED_DETAIL = "Could not allocate an invite code"
ALREADY_MEMBER_DETAIL = "You are already a member of this group"
GROUP_FULL_DETAIL = "This group has no unassigned seats left"
NOT_GROUP_OWNER_DETAIL = "Only the group owner can cancel this group"


def generate_invite_code() -> str:
    """Six characters from an uppercase alphanumeric alphabet, one draw per slot."""
    return "".join(
        secrets.choice(INVITE_CODE_ALPHABET) for _ in range(INVITE_CODE_LENGTH)
    )


def seat_prices(db: Session, seat_ids: list[int]) -> dict[int, Decimal]:
    """Map each ShowSeat id to its seat-category price, quantised to the paisa."""
    if not seat_ids:
        return {}
    rows = db.execute(
        select(ShowSeat.id, SeatCategory.price)
        .join(Seat, ShowSeat.seat_id == Seat.id)
        .join(SeatCategory, Seat.category_id == SeatCategory.id)
        .where(ShowSeat.id.in_(seat_ids))
    ).all()
    return {seat_id: Decimal(price).quantize(CENT) for seat_id, price in rows}


def split_shares(
    prices: dict[int, Decimal], assignments: dict[int, list[int]]
) -> dict[int, Decimal]:
    """Each member's share: the sum of the seat prices assigned to that member."""
    return {
        user_id: sum(
            (prices.get(seat_id, Decimal("0")) for seat_id in seat_ids),
            Decimal("0"),
        ).quantize(CENT)
        for user_id, seat_ids in assignments.items()
    }


def _unique_invite_code(db: Session) -> str:
    """A code no other group holds; a collision is redrawn rather than reused."""
    for _ in range(INVITE_CODE_ATTEMPTS):
        code = generate_invite_code()
        if db.scalar(select(Group.id).where(Group.invite_code == code)) is None:
            return code
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=INVITE_CODE_EXHAUSTED_DETAIL,
    )


def find_member(*, db: Session, group_id: int, user_id: int) -> GroupMember | None:
    """The member row of this moviegoer in this group, if they have one."""
    return db.scalar(
        select(GroupMember).where(
            GroupMember.group_id == group_id, GroupMember.user_id == user_id
        )
    )


def group_members(db: Session, group_id: int) -> list[GroupMember]:
    """Every member of a group, in the order they joined."""
    return list(
        db.scalars(
            select(GroupMember)
            .where(GroupMember.group_id == group_id)
            .order_by(GroupMember.id)
        )
    )


def create_group(
    *, db: Session, hold_id: str, user: User, now: datetime | None = None
) -> Group:
    """Open a group on the caller's live hold; the owner takes the first seat.

    The remaining seats stay unassigned until joiners claim them, and the group
    never outlives its hold because it expires when the hold does.
    """
    moment = now if now is not None else datetime.now(timezone.utc)
    rows = list(db.scalars(select(ShowSeat).where(ShowSeat.hold_id == hold_id)))
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Hold not found: {hold_id}",
        )
    if any(row.held_by != user.id for row in rows):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=NOT_OWNER_DETAIL
        )

    live = [row for row in rows if effective_status(row, moment) is ShowSeatStatus.HELD]
    if not live:
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail=HOLD_EXPIRED_DETAIL
        )

    seat_ids = [row.id for row in live]
    assignments = {user.id: seat_ids[:1]}
    shares = split_shares(seat_prices(db, seat_ids), assignments)

    expires_at = live[0].held_until
    group = Group(
        owner_id=user.id,
        show_id=live[0].show_id,
        hold_id=hold_id,
        seat_ids=seat_ids,
        status=BookingStatus.PENDING_PAYMENT.value,
        invite_code=_unique_invite_code(db),
        expires_at=expires_at if expires_at is not None else moment,
        created_at=moment,
    )
    db.add(group)
    db.flush()

    db.add(
        GroupMember(
            group_id=group.id,
            user_id=user.id,
            seat_ids=assignments[user.id],
            share_amount=shares[user.id],
            payment_status=MemberPaymentStatus.PENDING.value,
            payment_id=None,
        )
    )
    db.commit()
    return group


def get_group(*, db: Session, group_id: int, user: User) -> Group:
    """Read a group, for its owner and its members only."""
    group = db.get(Group, group_id)
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Group not found: {group_id}",
        )
    if (
        group.owner_id != user.id
        and find_member(db=db, group_id=group.id, user_id=user.id) is None
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=NOT_A_MEMBER_DETAIL
        )
    return group


def group_seats(db: Session, group: Group) -> list[ShowSeat]:
    """The group's ShowSeats, in the order the hold listed them."""
    rows = {
        row.id: row
        for row in db.scalars(
            select(ShowSeat).where(ShowSeat.id.in_(list(group.seat_ids)))
        )
    }
    return [rows[seat_id] for seat_id in group.seat_ids if seat_id in rows]


def _require_live_hold(db: Session, group: Group, now: datetime) -> None:
    """A group whose seats are no longer held by its hold has nothing left to split."""
    seats = group_seats(db, group)
    if len(seats) != len(group.seat_ids) or any(
        effective_status(row, now) is not ShowSeatStatus.HELD
        or row.hold_id != group.hold_id
        for row in seats
    ):
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail=HOLD_EXPIRED_DETAIL
        )


def _refund_share(
    *, client: PaymentsClient, member: GroupMember, idempotency_key: str
) -> None:
    """Reverse one paid share; a failure leaves every row of the group untouched."""
    if member.payment_id is None:
        return
    try:
        client.refund_payment(
            payment_id=member.payment_id, idempotency_key=idempotency_key
        )
    except PaymentsUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=PAYMENT_REFUND_FAILED_DETAIL,
        ) from None


def _release_seats(db: Session, group: Group) -> None:
    """Put the group's seats back on sale, leaving seats another hold took alone."""
    for row in group_seats(db, group):
        if row.hold_id is not None and row.hold_id != group.hold_id:
            continue
        row.status = ShowSeatStatus.AVAILABLE.value
        row.held_by = None
        row.held_until = None
        row.hold_id = None


def _hold_back_seats(db: Session, group: Group) -> None:
    """Undo a confirmation: sold seats hold again until the group's hold expires."""
    for row in group_seats(db, group):
        if row.status != ShowSeatStatus.SOLD.value:
            continue
        row.status = ShowSeatStatus.HELD.value
        row.held_by = group.owner_id
        row.held_until = group.expires_at
        row.hold_id = group.hold_id


def join_group(
    *, db: Session, invite_code: str, user: User, now: datetime | None = None
) -> Group:
    """Add the caller to a group, handing them the next unassigned seats."""
    moment = now if now is not None else datetime.now(timezone.utc)
    code = invite_code.strip().upper()
    group = db.scalar(select(Group).where(Group.invite_code == code))
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Invite code not found: {code}",
        )
    if find_member(db=db, group_id=group.id, user_id=user.id) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=ALREADY_MEMBER_DETAIL
        )

    _require_live_hold(db, group, moment)

    assigned = {
        seat_id
        for member in group_members(db, group.id)
        for seat_id in member.seat_ids
    }
    unassigned = [seat_id for seat_id in group.seat_ids if seat_id not in assigned]
    if not unassigned:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=GROUP_FULL_DETAIL
        )

    share = split_shares(seat_prices(db, unassigned), {user.id: unassigned})[user.id]
    db.add(
        GroupMember(
            group_id=group.id,
            user_id=user.id,
            seat_ids=unassigned,
            share_amount=share,
            payment_status=MemberPaymentStatus.PENDING.value,
            payment_id=None,
        )
    )
    db.commit()
    return group


def pay_share(
    *,
    db: Session,
    group_id: int,
    user: User,
    client: PaymentsClient,
    now: datetime | None = None,
) -> GroupMember:
    """Charge the caller's own share; the payment key makes a replay free."""
    moment = now if now is not None else datetime.now(timezone.utc)
    group = get_group(db=db, group_id=group_id, user=user)
    member = find_member(db=db, group_id=group.id, user_id=user.id)
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=NOT_A_MEMBER_DETAIL
        )
    if group.status == BookingStatus.CANCELLED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Group is already {group.status}",
        )
    if member.payment_status == MemberPaymentStatus.PAID.value:
        # group:{id}:{user} is the payments service's key, so a replay answers itself.
        return member

    _require_live_hold(db, group, moment)

    try:
        payment = client.create_group_share(
            group_id=group.id,
            user_id=user.id,
            amount=Decimal(member.share_amount),
            currency=BOOKING_CURRENCY,
        )
    except PaymentsUnavailableError:
        # Nothing is written before the call, so the share stays PENDING.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=PAYMENTS_UNAVAILABLE_DETAIL,
        ) from None

    payment_id = payment.get("paymentId")
    member.payment_id = payment_id if isinstance(payment_id, int) else None
    member.payment_status = MemberPaymentStatus.PAID.value
    # The session never autoflushes, so the paid share is written before the group
    # status is recomputed from the member rows.
    db.flush()
    refresh_group_status(db=db, group=group, now=moment)
    db.commit()
    return member


def refresh_group_status(*, db: Session, group: Group, now: datetime) -> None:
    """Confirm and sell the seats only once every seat is assigned and paid.

    The caller owns the transaction: this only mutates the group, its seats and the
    member rows it already holds.
    """
    if group.status != BookingStatus.PENDING_PAYMENT.value:
        return

    members = group_members(db, group.id)
    assigned = [seat_id for member in members for seat_id in member.seat_ids]
    if sorted(assigned) != sorted(group.seat_ids):
        return
    if any(
        member.payment_status != MemberPaymentStatus.PAID.value for member in members
    ):
        return

    seats = group_seats(db, group)
    if len(seats) != len(group.seat_ids) or any(
        effective_status(row, now) is not ShowSeatStatus.HELD for row in seats
    ):
        return

    group.status = BookingStatus.CONFIRMED.value
    for row in seats:
        row.status = ShowSeatStatus.SOLD.value
        row.held_by = None
        row.held_until = None
        row.hold_id = None


def leave_group(
    *,
    db: Session,
    group_id: int,
    user: User,
    client: PaymentsClient,
    now: datetime | None = None,
) -> Group:
    """Remove the caller, refund a paid share and put their seats back in the pool."""
    group = get_group(db=db, group_id=group_id, user=user)
    member = find_member(db=db, group_id=group.id, user_id=user.id)
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=NOT_A_MEMBER_DETAIL
        )
    if group.status == BookingStatus.CANCELLED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Group is already {group.status}",
        )

    if member.payment_status == MemberPaymentStatus.PAID.value:
        _refund_share(
            client=client,
            member=member,
            idempotency_key=f"leave-{group.id}-{member.user_id}",
        )

    was_confirmed = group.status == BookingStatus.CONFIRMED.value
    db.delete(member)
    group.status = BookingStatus.PENDING_PAYMENT.value
    if was_confirmed:
        _hold_back_seats(db, group)
    db.commit()
    return group


def cancel_group(
    *,
    db: Session,
    group_id: int,
    user: User,
    client: PaymentsClient,
    now: datetime | None = None,
) -> Group:
    """Refund every paid share, cancel the group and put its seats back on sale."""
    group = db.get(Group, group_id)
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Group not found: {group_id}",
        )
    if group.owner_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=NOT_GROUP_OWNER_DETAIL
        )
    if group.status == BookingStatus.CANCELLED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Group is already {group.status}",
        )

    for member in group_members(db, group.id):
        if member.payment_status != MemberPaymentStatus.PAID.value:
            continue
        _refund_share(
            client=client,
            member=member,
            idempotency_key=f"cancel-group-{group.id}-{member.user_id}",
        )
        member.payment_status = MemberPaymentStatus.REFUNDED.value

    group.status = BookingStatus.CANCELLED.value
    _release_seats(db, group)
    db.commit()
    return group


def review_eligibility(
    *, db: Session, group_id: int, user: User
) -> tuple[bool, int, datetime]:
    """Whether this member may review the group's movie, and which movie and show.

    Eligible means the caller's own share is paid and the group is CONFIRMED; the
    show's start time is returned as well, so the client owns the "after the show"
    clock rather than having it baked into the flag.
    """
    group = db.get(Group, group_id)
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Group not found: {group_id}",
        )
    member = find_member(db=db, group_id=group.id, user_id=user.id)
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=NOT_A_MEMBER_DETAIL
        )

    show = db.get(Show, group.show_id)
    if show is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Show not found: {group.show_id}",
        )

    eligible = (
        member.payment_status == MemberPaymentStatus.PAID.value
        and group.status == BookingStatus.CONFIRMED.value
    )
    return eligible, show.movie_id, show.starts_at
