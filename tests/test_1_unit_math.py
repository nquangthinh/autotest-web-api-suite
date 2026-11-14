"""10 exact-money and graph regression cases (no external services)."""

import random
import pytest

from split_calculator import allocate, calculate_split, settle_debts

pytestmark = pytest.mark.unit


def test_equal_one_x():
    """Ba suất 1x chia đều; đồng dư được phân bổ ổn định, không mất tiền."""
    result = calculate_split(["A", "B", "C"], [{"price": 100001}], {"0": ["A", "B", "C"]})
    assert [s["amount"] for s in result["member_shares"]] == [33334, 33334, 33333]
    assert result["grand_total"] == 100001


def test_one_x_two_x():
    """Suất 2x trả gấp đôi suất 1x với hóa đơn chia hết."""
    result = calculate_split(["A", "B"], [{"price": 300000}], {"0": {"A": 1, "B": 2}})
    assert [s["amount"] for s in result["member_shares"]] == [100000, 200000]


def test_one_x_two_x_three_x():
    """Ba hệ số 1x/2x/3x bảo toàn đúng tỷ lệ 1:2:3."""
    result = calculate_split(["A", "B", "C"], [{"price": 600000}], {"0": {"A": 1, "B": 2, "C": 3}})
    assert [s["amount"] for s in result["member_shares"]] == [100000, 200000, 300000]


def test_multiple_items_quantity_and_nonparticipant():
    """Cộng nhiều món và số lượng đúng; người không chọn món có phần tiền bằng 0."""
    result = calculate_split(["A", "B", "C"], [{"price": 15001, "quantity": 2}, {"price": 9999}],
                             {"0": {"A": 1, "B": 2}, "1": ["B"]})
    assert [s["amount"] for s in result["member_shares"]] == [10001, 30000, 0]
    assert result["subtotal"] == result["grand_total"] == 40001


def test_weighted_tax_voucher_exact_vnd():
    """VAT/voucher/phụ phí phân bổ trọng số; 300 mẫu seed cố định có sai số tổng 0đ."""
    result = calculate_split(["A", "B", "C"], [{"price": 600000}], {"0": {"A": 1, "B": 2, "C": 3}},
                             discount_amount=90000, tax_amount=60000, fee_amount=30000)
    assert [s["discount_amount"] for s in result["member_shares"]] == [15000, 30000, 45000]
    assert [s["tax_amount"] for s in result["member_shares"]] == [10000, 20000, 30000]
    assert [s["fee_amount"] for s in result["member_shares"]] == [5000, 10000, 15000]
    generator = random.Random(20261114)
    for _ in range(300):
        price = generator.randint(1, 10**9)
        members = [str(i) for i in range(generator.randint(1, 20))]
        discount, tax, fee = generator.randint(0, price), generator.randint(0, 100000), generator.randint(0, 100000)
        result = calculate_split(members, [{"price": price}],
                                 {"0": {member: generator.randint(1, 3) for member in members}},
                                 discount_amount=discount, tax_amount=tax, fee_amount=fee)
        for key, expected in (("subtotal", price), ("discount_amount", discount),
                              ("tax_amount", tax), ("fee_amount", fee), ("amount", price - discount + tax + fee)):
            assert sum(s[key] for s in result["member_shares"]) == expected
        assert all(isinstance(s["amount"], int) and s["amount"] >= 0 for s in result["member_shares"])
        for share in result["member_shares"]:
            for key, total in (("discount_amount", discount), ("tax_amount", tax), ("fee_amount", fee)):
                assert abs(share[key] * price - total * share["subtotal"]) < price


def test_zero_bill():
    """Hóa đơn 0đ, danh sách rỗng và món 0đ không chia cho 0."""
    assert calculate_split([], [])['member_shares'] == []
    assert calculate_split([], [{"price": 0}])["grand_total"] == 0
    result = calculate_split(["A", "B"], [{"price": 0}])
    assert [s["amount"] for s in result["member_shares"]] == [0, 0]
    assert allocate(0, {}) == {}
    assert calculate_split(["A", "B"], [], fee_amount=3)["grand_total"] == 3


def test_hundred_percent_discount():
    """Voucher 100% đưa tổng và từng phần về 0đ; VAT vẫn cộng đúng nếu có."""
    result = calculate_split(["A", "B", "C"], [{"price": 100001}], discount_amount=100001)
    assert result["grand_total"] == 0
    assert all(s["amount"] == 0 for s in result["member_shares"])
    taxed = calculate_split(["A", "B"], [{"price": 3}], discount_amount=3, tax_amount=1)
    assert sum(s["amount"] for s in taxed["member_shares"]) == 1


def test_no_selection_uses_explicit_fallback():
    """Món chưa ai chọn hoặc mọi hệ số bằng 0 được chia đều, không ZeroDivisionError."""
    for assignments in ({}, {"0": []}, {"0": {"A": 0, "B": 0}}):
        result = calculate_split(["A", "B"], [{"price": 101}], assignments)
        assert [s["amount"] for s in result["member_shares"]] == [51, 50]
        assert result["unassigned_item_indexes"] == [0]
        assert result["unassigned_policy"] == "equal_among_all_members"


def test_invalid_money_members_and_weights():
    """Từ chối tiền âm/lẻ, voucher vượt hóa đơn, tên trùng và hệ số không hợp lệ."""
    for amount in (-1, 1.5, True):
        with pytest.raises(ValueError):
            calculate_split(["A"], [{"price": amount}])
    for members, assignments in (([], {}), (["A", "A"], {}), ([" "], {}),
                                 (["A"], {"0": {"B": 1}}), (["A"], {"1": {"A": 1}}),
                                 (["A"], {"0": {"A": -1}}), (["A"], {"0": ["A", "A"]})):
        with pytest.raises(ValueError):
            calculate_split(members, [{"price": 1}], assignments)
    with pytest.raises(ValueError):
        calculate_split(["A"], [{"price": 1}], discount_amount=2)
    for weight in (-1, "nan", "inf", True):
        with pytest.raises(ValueError):
            allocate(10, {"A": weight})


def test_trip_graph_at_most_n_minus_one():
    """Cấn trừ đồ thị bảo toàn số dư và tối đa N-1 giao dịch trên 200 mạng nợ."""
    generator = random.Random(42)
    for _ in range(200):
        size = generator.randint(2, 30)
        balances = {str(i): generator.randint(-1000000, 1000000) for i in range(size - 1)}
        balances[str(size - 1)] = -sum(balances.values())
        transfers = settle_debts(balances)
        assert len(transfers) <= size - 1
        remaining = balances.copy()
        for transfer in transfers:
            assert transfer["amount"] > 0
            assert balances[transfer["from"]] < 0 < balances[transfer["to"]]
            remaining[transfer["from"]] += transfer["amount"]
            remaining[transfer["to"]] -= transfer["amount"]
        assert all(value == 0 for value in remaining.values())
    assert settle_debts({}) == settle_debts({"A": 0}) == []
    with pytest.raises(ValueError):
        settle_debts({"A": 1, "B": -2})
    with pytest.raises(ValueError):
        settle_debts({"A": 0.5, "B": -0.5})
