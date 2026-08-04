"""Performance decomposition that keeps market P&L separate from cash flows."""

from decimal import Decimal

from transaction_schema import Action, Transaction


EXTERNAL_CASH_ACTIONS = {Action.DEPOSIT, Action.WITHDRAWAL}
FINANCING_ACTIONS = {Action.BORROW, Action.REPAY}
INCOME_ACTIONS = {Action.DIVIDEND, Action.INTEREST}
EXPENSE_ACTIONS = {Action.FEE, Action.TAX}


def transaction_amount(transaction: Transaction) -> Decimal:
    """Return the TWD-equivalent amount recorded by a cash-flow event.

    Form rows for deposits, withdrawals, borrowing, dividends and fees record
    the money amount in ``quantity``. Trades are deliberately excluded: their
    cash leg changes holdings and cash together, not net asset value.
    """
    return abs(Decimal(transaction.quantity))


def classify_transaction(transaction: Transaction) -> tuple[str, Decimal]:
    amount = transaction_amount(transaction)
    if transaction.action in EXTERNAL_CASH_ACTIONS:
        return ("external_cash_flow", amount if transaction.action == Action.DEPOSIT else -amount)
    if transaction.action in FINANCING_ACTIONS:
        return ("financing_cash_flow", amount if transaction.action == Action.BORROW else -amount)
    if transaction.action in INCOME_ACTIONS:
        return ("income", amount)
    if transaction.action in EXPENSE_ACTIONS:
        return ("expense", -amount)
    return ("other", Decimal("0"))


def performance_breakdown(current_net_asset, previous_net_asset, transactions=()):
    """Reconcile net-asset movement into flow and market components."""
    current = Decimal(str(current_net_asset))
    previous = Decimal(str(previous_net_asset))
    external_cash_flow = Decimal("0")
    financing_cash_flow = Decimal("0")
    income = Decimal("0")
    expense = Decimal("0")
    for transaction in transactions:
        category, amount = classify_transaction(transaction)
        if category == "external_cash_flow":
            external_cash_flow += amount
        elif category == "financing_cash_flow":
            financing_cash_flow += amount
        elif category == "income":
            income += amount
        elif category == "expense":
            expense += amount
    net_change = current - previous
    transaction_effect = external_cash_flow + financing_cash_flow + income + expense
    market_pnl = net_change - transaction_effect
    return {
        "netChange": round(float(net_change), 2),
        "marketPnl": round(float(market_pnl), 2),
        "externalCashFlow": round(float(external_cash_flow), 2),
        "financingCashFlow": round(float(financing_cash_flow), 2),
        "income": round(float(income), 2),
        "expenses": round(float(expense), 2),
        "reconciled": round(float(market_pnl + transaction_effect), 2) == round(float(net_change), 2),
    }
