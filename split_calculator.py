"""Exact integer-VND allocation, debt settlement, and safe VietQR URLs."""

from fractions import Fraction
import re
import unicodedata
from urllib.parse import urlencode


def money(value, name="amount"):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer in VND")
    return value


def allocate(amount, weights):
    """Largest remainder; deterministic ties follow input order; no float math."""
    money(amount)
    parsed = {}
    for member, weight in weights.items():
        if isinstance(weight, bool):
            raise ValueError("Weights must be finite, non-negative numbers")
        try:
            parsed[member] = Fraction(str(weight))
        except (ValueError, ZeroDivisionError) as exc:
            raise ValueError("Weights must be finite, non-negative numbers") from exc
        if parsed[member] < 0:
            raise ValueError("Weights cannot be negative")
    result = dict.fromkeys(parsed, 0)
    total_weight = sum(parsed.values())
    if not amount:
        return result
    if not total_weight:
        raise ValueError("A positive amount requires a positive weight")
    quotas = {member: amount * weight / total_weight for member, weight in parsed.items()}
    result = {member: quota.numerator // quota.denominator for member, quota in quotas.items()}
    remainder = amount - sum(result.values())
    order = sorted(quotas, key=lambda member: quotas[member] - result[member], reverse=True)
    for member in order[:remainder]:
        result[member] += 1
    return result


def calculate_split(members, items, assignments=None, discount_amount=0,
                    tax_amount=0, fee_amount=0):
    """Unselected items are shared equally among all members (explicit policy)."""
    if any(not isinstance(m, str) or not m.strip() for m in members):
        raise ValueError("Member names cannot be blank")
    if len(set(members)) != len(members):
        raise ValueError("Member names must be unique")
    assignments = assignments or {}
    if set(assignments) - {str(i) for i in range(len(items))}:
        raise ValueError("Assignment refers to an unknown item")
    totals = dict.fromkeys(members, 0)
    fallback_items = []
    for index, item in enumerate(items):
        price = money(item["price"], "price")
        quantity = money(item.get("quantity", 1), "quantity")
        if not quantity:
            raise ValueError("quantity must be positive")
        weights = assignments.get(str(index), {})
        if isinstance(weights, list):
            if len(set(weights)) != len(weights):
                raise ValueError("Duplicate item participants")
            weights = dict.fromkeys(weights, 1)
        if set(weights) - set(members):
            raise ValueError("Assignment refers to an unknown member")
        # Validate even zero-cost items and zero-weight assignments.
        allocate(0, weights)
        if not weights or not any(Fraction(str(w)) > 0 for w in weights.values()):
            weights = dict.fromkeys(members, 1)
            fallback_items.append(index)
        parts = allocate(price * quantity, weights)
        for member, amount in parts.items():
            totals[member] += amount
    subtotal = sum(totals.values())
    for name, amount in (("discount_amount", discount_amount), ("tax_amount", tax_amount),
                         ("fee_amount", fee_amount)):
        money(amount, name)
    if discount_amount > subtotal:
        raise ValueError("Discount cannot exceed subtotal")
    weights = totals if subtotal else dict.fromkeys(members, 1)
    discounts = allocate(discount_amount, weights)
    taxes = allocate(tax_amount, weights)
    fees = allocate(fee_amount, weights)
    shares = [{"member": member, "subtotal": totals[member],
               "discount_amount": discounts[member], "tax_amount": taxes[member],
               "fee_amount": fees[member],
               "amount": totals[member] - discounts[member] + taxes[member] + fees[member]}
              for member in members]
    return {"currency": "VND", "subtotal": subtotal, "discount_amount": discount_amount,
            "tax_amount": tax_amount, "fee_amount": fee_amount,
            "grand_total": subtotal - discount_amount + tax_amount + fee_amount,
            "member_shares": shares, "unassigned_item_indexes": fallback_items,
            "unassigned_policy": "equal_among_all_members"}


def settle_debts(balances):
    """Positive balance receives money; each transfer clears at least one vertex."""
    if any(isinstance(v, bool) or not isinstance(v, int) for v in balances.values()):
        raise ValueError("Balances must be integer VND")
    if sum(balances.values()) != 0:
        raise ValueError("Balances must sum to zero")
    debtors = [[name, -value] for name, value in balances.items() if value < 0]
    creditors = [[name, value] for name, value in balances.items() if value > 0]
    transactions = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        amount = min(debtors[i][1], creditors[j][1])
        transactions.append({"from": debtors[i][0], "to": creditors[j][0], "amount": amount})
        debtors[i][1] -= amount
        creditors[j][1] -= amount
        if debtors[i][1] == 0:
            i += 1
        if creditors[j][1] == 0:
            j += 1
    return transactions


def build_vietqr(bank_bin, account_no, account_name, amount, memo="Chia tien"):
    money(amount)
    account_no = re.sub(r"\s+", "", account_no)
    bank_bin = bank_bin.strip()
    if not re.fullmatch(r"[0-9]{6}", bank_bin):
        raise ValueError("bank_bin must contain six digits")
    if not re.fullmatch(r"[0-9]{1,30}", account_no):
        raise ValueError("account_no must contain 1 to 30 digits after whitespace removal")
    normalized = unicodedata.normalize("NFKD", account_name.replace("đ", "d").replace("Đ", "D"))
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = " ".join(normalized.upper().split())
    if not re.fullmatch(r"[A-Z ]+", normalized):
        raise ValueError("account_name must contain Latin letters and spaces")
    query = urlencode({"amount": amount, "addInfo": memo, "accountName": normalized})
    return {"bank_bin": bank_bin, "account_no": account_no, "account_name": normalized,
            "amount": amount, "url": f"https://img.vietqr.io/image/{bank_bin}-{account_no}-compact2.png?{query}"}
