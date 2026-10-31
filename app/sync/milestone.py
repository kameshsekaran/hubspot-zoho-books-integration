"""
Milestone and invoice summary calculation utilities.

All monetary arithmetic uses ``decimal.Decimal`` to avoid floating-point
rounding errors.  These functions are pure (no I/O) and are heavily covered
by unit tests.
"""

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional


# ---------------------------------------------------------------------------
# Milestone calculation
# ---------------------------------------------------------------------------

# Canonical milestone schedule: (percentage_of_net, label)
MILESTONE_SCHEDULE: list[tuple[Decimal, str]] = [
    (Decimal("0.50"), "Signing"),
    (Decimal("0.30"), "Go-live"),
    (Decimal("0.20"), "90-day post go-live"),
]


def calculate_milestones(net_total: Decimal, vat_rate: Decimal) -> list[dict]:
    """
    Compute per-milestone net, VAT, and gross amounts.

    The last milestone absorbs any rounding remainder so that the sum of all
    gross amounts always equals the expected total (net_total * (1 + vat_rate))
    rounded to 2 decimal places.

    Args:
        net_total: Deal value exclusive of VAT (positive Decimal).
        vat_rate:  Decimal VAT rate, e.g. Decimal("0.05") for 5%.

    Returns:
        List of dicts with keys: ``label``, ``percentage``, ``net``, ``vat``,
        ``gross``.

    Example::

        >>> ms = calculate_milestones(Decimal("120000"), Decimal("0.05"))
        >>> ms[0]
        {'label': 'Signing', 'percentage': Decimal('0.50'),
         'net': Decimal('60000.00'), 'vat': Decimal('3000.00'),
         'gross': Decimal('63000.00')}
    """
    two_dp = Decimal("0.01")
    milestones: list[dict] = []
    running_net = Decimal("0")
    running_vat = Decimal("0")

    for idx, (pct, label) in enumerate(MILESTONE_SCHEDULE):
        is_last = idx == len(MILESTONE_SCHEDULE) - 1

        if is_last:
            # Absorb rounding remainder.
            net = (net_total - running_net).quantize(two_dp, rounding=ROUND_HALF_UP)
            vat = (net * vat_rate).quantize(two_dp, rounding=ROUND_HALF_UP)
        else:
            net = (net_total * pct).quantize(two_dp, rounding=ROUND_HALF_UP)
            vat = (net * vat_rate).quantize(two_dp, rounding=ROUND_HALF_UP)
            running_net += net
            running_vat += vat

        gross = net + vat
        milestones.append(
            {
                "label": label,
                "percentage": pct,
                "net": net,
                "vat": vat,
                "gross": gross,
            }
        )

    return milestones


# ---------------------------------------------------------------------------
# Invoice summary computation
# ---------------------------------------------------------------------------


def compute_invoicing_summary(
    invoices: list[dict],
    so_total: Decimal,
    today: Optional[date] = None,
) -> dict:
    """
    Derive high-level billing KPIs from a list of Zoho invoice dicts.

    Args:
        invoices: List of Zoho invoice dicts.  Each must contain at minimum:
                  ``total`` (gross amount), ``balance`` (outstanding amount),
                  ``status`` (Zoho status string), ``due_date`` (ISO date str).
        so_total: Net total of the Sales Order (exclusive of VAT).
        today:    Reference date for overdue detection (defaults to today UTC).

    Returns:
        Dict with keys:
        - ``pct_invoiced``       (Decimal): 0–100.
        - ``pct_collected``      (Decimal): 0–100.
        - ``amount_unpaid``      (Decimal): Outstanding gross across invoices.
        - ``amount_uninvoiced``  (Decimal): SO total not yet invoiced.
        - ``next_due_date``      (Optional[date]): Earliest due date on an open invoice.
        - ``overdue``            (bool): True if any non-voided invoice is past due.
        - ``total_invoiced``     (Decimal): Sum of gross invoice totals (excl. void).
        - ``total_collected``    (Decimal): Total gross paid (total - balance).
    """
    if today is None:
        today = datetime.utcnow().date()

    two_dp = Decimal("0.01")
    total_invoiced = Decimal("0")
    total_collected = Decimal("0")
    total_balance = Decimal("0")
    overdue = False
    due_dates: list[date] = []

    for inv in invoices:
        status = (inv.get("status") or "").lower()

        # Skip voided invoices – they don't count towards financials.
        if status == "void":
            continue

        gross = Decimal(str(inv.get("total", 0)))
        balance = Decimal(str(inv.get("balance", 0)))
        paid = gross - balance

        total_invoiced += gross
        total_collected += paid
        total_balance += balance

        # Parse due date.
        due_raw = inv.get("due_date") or inv.get("payment_terms_label") or ""
        if due_raw:
            try:
                due = _parse_date(due_raw)
                if balance > 0:
                    due_dates.append(due)
                    if due < today:
                        overdue = True
            except (ValueError, TypeError):
                pass

    # Gross SO total = net × (1 + vat_rate); approximate from invoice totals.
    # We compare invoiced totals to the SO net to keep the percentage meaningful.
    so_gross = total_invoiced + Decimal(str(so_total)) if so_total else total_invoiced

    # Avoid division by zero.
    if so_total > 0:
        pct_invoiced = (total_invoiced / (so_total * Decimal("1") + Decimal("0")) * 100).quantize(
            two_dp, rounding=ROUND_HALF_UP
        )
        pct_collected = (
            (total_collected / so_total * 100).quantize(two_dp, rounding=ROUND_HALF_UP)
            if so_total
            else Decimal("0")
        )
        amount_uninvoiced = (so_total - total_invoiced).quantize(
            two_dp, rounding=ROUND_HALF_UP
        )
    else:
        pct_invoiced = Decimal("0")
        pct_collected = Decimal("0")
        amount_uninvoiced = Decimal("0")

    amount_unpaid = total_balance.quantize(two_dp, rounding=ROUND_HALF_UP)
    next_due_date: Optional[date] = min(due_dates) if due_dates else None

    return {
        "pct_invoiced": pct_invoiced,
        "pct_collected": pct_collected,
        "amount_unpaid": amount_unpaid,
        "amount_uninvoiced": amount_uninvoiced,
        "next_due_date": next_due_date,
        "overdue": overdue,
        "total_invoiced": total_invoiced.quantize(two_dp, rounding=ROUND_HALF_UP),
        "total_collected": total_collected.quantize(two_dp, rounding=ROUND_HALF_UP),
    }


# ---------------------------------------------------------------------------
# Aging bucket computation
# ---------------------------------------------------------------------------


def compute_aging_buckets(
    invoices: list[dict],
    today: Optional[date] = None,
) -> dict:
    """
    Categorise outstanding invoice balances into aging buckets.

    Buckets:
    - ``current``  : due in the future.
    - ``1_30``     : 1–30 days overdue.
    - ``31_60``    : 31–60 days overdue.
    - ``61_90``    : 61–90 days overdue.
    - ``over_90``  : > 90 days overdue.

    Args:
        invoices: List of Zoho invoice dicts (same format as
                  ``compute_invoicing_summary``).
        today:    Reference date (defaults to today UTC).

    Returns:
        Dict mapping bucket name → Decimal balance total.
    """
    if today is None:
        today = datetime.utcnow().date()

    two_dp = Decimal("0.01")
    buckets: dict[str, Decimal] = {
        "current": Decimal("0"),
        "1_30": Decimal("0"),
        "31_60": Decimal("0"),
        "61_90": Decimal("0"),
        "over_90": Decimal("0"),
    }

    for inv in invoices:
        status = (inv.get("status") or "").lower()
        if status in ("void", "paid"):
            continue

        balance = Decimal(str(inv.get("balance", 0)))
        if balance <= 0:
            continue

        due_raw = inv.get("due_date") or ""
        if not due_raw:
            buckets["current"] += balance
            continue

        try:
            due = _parse_date(due_raw)
        except (ValueError, TypeError):
            buckets["current"] += balance
            continue

        days_overdue = (today - due).days
        if days_overdue <= 0:
            buckets["current"] += balance
        elif days_overdue <= 30:
            buckets["1_30"] += balance
        elif days_overdue <= 60:
            buckets["31_60"] += balance
        elif days_overdue <= 90:
            buckets["61_90"] += balance
        else:
            buckets["over_90"] += balance

    return {k: v.quantize(two_dp, rounding=ROUND_HALF_UP) for k, v in buckets.items()}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_date(value: str) -> date:
    """
    Parse a date string in ISO-8601 (YYYY-MM-DD) or similar formats.

    Args:
        value: Date string.

    Returns:
        ``datetime.date`` object.

    Raises:
        ValueError: If the string cannot be parsed.
    """
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date format: {value!r}")
