from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from accounts.models import EmployeeProfile
from surveys.models import SurveyAttempt

from .models import AllocationReservation, VendorClientAllocation, VendorSurveyAllocation


MONEY_QUANTUM = Decimal("0.01")


class AllocationUnavailable(ValueError):
    """Raised when a vendor cannot reserve capacity for a survey."""


def payable_cpi(source_cpi, cut_percent) -> Decimal | None:
    if source_cpi is None:
        return None
    source = Decimal(source_cpi)
    cut = Decimal(cut_percent or 0)
    return (source * (Decimal("100") - cut) / Decimal("100")).quantize(
        MONEY_QUANTUM,
        rounding=ROUND_HALF_UP,
    )


def _is_active_now(allocation, now) -> bool:
    return bool(
        allocation.is_active
        and (allocation.starts_at is None or allocation.starts_at <= now)
        and (allocation.ends_at is None or allocation.ends_at > now)
    )


@transaction.atomic
def reserve_attempt_capacity(
    attempt: SurveyAttempt,
    survey_allocation: VendorSurveyAllocation,
    *,
    expires_at=None,
) -> AllocationReservation:
    """Reserve one unit and freeze the attempt's vendor/client/CPI context.

    The caller may wrap attempt creation and this function in an outer atomic
    transaction when enforcement is connected to the respondent start flow.
    """

    attempt = SurveyAttempt.objects.select_for_update().select_related("survey").get(pk=attempt.pk)
    existing = AllocationReservation.objects.filter(attempt=attempt).first()
    if existing:
        return existing

    locked_survey_allocation = (
        VendorSurveyAllocation.objects.select_for_update()
        .select_related(
            "survey", "client_allocation", "client_allocation__vendor",
            "client_allocation__vendor__employee_profile",
            "client_allocation__vendor__vendor_commercial_profile",
            "client_allocation__client",
        )
        .get(pk=survey_allocation.pk)
    )
    client_allocation = VendorClientAllocation.objects.select_for_update().get(
        pk=locked_survey_allocation.client_allocation_id
    )
    now = timezone.now()

    if attempt.survey_id != locked_survey_allocation.survey_id:
        raise AllocationUnavailable("Attempt survey does not match the assigned survey.")
    if attempt.survey.client_id != client_allocation.client_id:
        raise AllocationUnavailable("Survey is not mapped to the allocation's client.")
    if not _is_active_now(client_allocation, now):
        raise AllocationUnavailable("Client allocation is inactive or outside its active dates.")
    if not _is_active_now(locked_survey_allocation, now):
        raise AllocationUnavailable("Survey allocation is inactive or outside its active dates.")
    if client_allocation.remaining_quantity < 1:
        raise AllocationUnavailable("Client quantity is exhausted.")
    if locked_survey_allocation.remaining_quantity < 1:
        raise AllocationUnavailable("Survey quantity is exhausted.")
    if attempt.survey.remaining < 1:
        raise AllocationUnavailable("Upstream survey quantity is exhausted.")

    vendor_profile = client_allocation.vendor.employee_profile
    cut = locked_survey_allocation.effective_cpi_cut_percent
    if vendor_profile.account_type == EmployeeProfile.AccountType.INTERNAL_VENDOR:
        cut = Decimal("0.00")
    source_cpi = attempt.survey.cpi
    final_cpi = payable_cpi(source_cpi, cut)

    client_allocation.reserved_quantity += 1
    client_allocation.save(update_fields=["reserved_quantity", "updated_at"])
    locked_survey_allocation.reserved_quantity += 1
    locked_survey_allocation.save(update_fields=["reserved_quantity", "updated_at"])

    SurveyAttempt.objects.filter(pk=attempt.pk).update(
        vendor=client_allocation.vendor,
        client=client_allocation.client,
        client_allocation=client_allocation,
        survey_allocation=locked_survey_allocation,
        source_cpi_snapshot=source_cpi,
        cpi_cut_percent_snapshot=cut,
        payable_cpi_snapshot=final_cpi,
        cpi_currency_snapshot=(
            getattr(client_allocation.vendor, "vendor_commercial_profile", None).currency
            if hasattr(client_allocation.vendor, "vendor_commercial_profile")
            else "USD"
        ),
    )
    attempt.refresh_from_db()
    return AllocationReservation.objects.create(
        attempt=attempt,
        client_allocation=client_allocation,
        survey_allocation=locked_survey_allocation,
        quantity=1,
        expires_at=expires_at or now + timedelta(minutes=30),
    )


@transaction.atomic
def finalize_attempt_capacity(attempt: SurveyAttempt) -> AllocationReservation | None:
    """Consume a completion or release every other terminal outcome, idempotently."""

    reservation = (
        AllocationReservation.objects.select_for_update()
        .filter(attempt=attempt)
        .first()
    )
    if not reservation or reservation.status != AllocationReservation.Status.RESERVED:
        return reservation

    client_allocation = VendorClientAllocation.objects.select_for_update().get(pk=reservation.client_allocation_id)
    survey_allocation = VendorSurveyAllocation.objects.select_for_update().get(pk=reservation.survey_allocation_id)
    quantity = reservation.quantity
    if client_allocation.reserved_quantity < quantity or survey_allocation.reserved_quantity < quantity:
        raise RuntimeError("Allocation counters are inconsistent with the reservation.")

    client_allocation.reserved_quantity -= quantity
    survey_allocation.reserved_quantity -= quantity
    if attempt.status == SurveyAttempt.Status.COMPLETED:
        client_allocation.consumed_quantity += quantity
        survey_allocation.consumed_quantity += quantity
        reservation.status = AllocationReservation.Status.CONSUMED
        reservation.reason = "Completed survey"
    else:
        reservation.status = AllocationReservation.Status.RELEASED
        reservation.reason = f"Released for attempt status {attempt.status}"

    client_allocation.save(update_fields=["reserved_quantity", "consumed_quantity", "updated_at"])
    survey_allocation.save(update_fields=["reserved_quantity", "consumed_quantity", "updated_at"])
    reservation.finalized_at = timezone.now()
    reservation.save(update_fields=["status", "reason", "finalized_at", "updated_at"])
    return reservation


@transaction.atomic
def expire_reservation(reservation: AllocationReservation) -> AllocationReservation:
    locked = AllocationReservation.objects.select_for_update().get(pk=reservation.pk)
    if locked.status != AllocationReservation.Status.RESERVED:
        return locked
    if locked.expires_at > timezone.now():
        raise AllocationUnavailable("Reservation has not expired yet.")

    client_allocation = VendorClientAllocation.objects.select_for_update().get(pk=locked.client_allocation_id)
    survey_allocation = VendorSurveyAllocation.objects.select_for_update().get(pk=locked.survey_allocation_id)
    quantity = locked.quantity
    if client_allocation.reserved_quantity < quantity or survey_allocation.reserved_quantity < quantity:
        raise RuntimeError("Allocation counters are inconsistent with the reservation.")
    client_allocation.reserved_quantity -= quantity
    survey_allocation.reserved_quantity -= quantity
    client_allocation.save(update_fields=["reserved_quantity", "updated_at"])
    survey_allocation.save(update_fields=["reserved_quantity", "updated_at"])
    locked.status = AllocationReservation.Status.EXPIRED
    locked.reason = "Reservation expired before a terminal callback"
    locked.finalized_at = timezone.now()
    locked.save(update_fields=["status", "reason", "finalized_at", "updated_at"])
    return locked
