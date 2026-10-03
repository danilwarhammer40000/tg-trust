"""
All FSM state definitions, in one place.

aiogram FSM state names are global strings ("ClassName:state_name"), not
scoped to whichever module defines them — so having them all here (instead
of scattered per-handler-file) makes it obvious at a glance which states
exist and avoids two different files accidentally defining a same-named
state twice.

Rule of thumb followed when splitting bot.py into this package: every
handler that reacts to a given state lives in exactly one handlers/ file
(see that file's own docstring for which). A different file is allowed to
*transition into* a state it doesn't own (e.g. list_users.py sets
AdminMessage.personal before handing off to feedback.py, which owns that
state) — see CHANGELOG.md for the full mapping if in doubt.
"""
from aiogram.fsm.state import State, StatesGroup


class AddUser(StatesGroup):
    username = State()
    password = State()
    days = State()
    manual_date = State()


class AddUserMulti(StatesGroup):
    username = State()
    password = State()
    days = State()
    manual_date = State()
    continue_choice = State()
    done_actions = State()


class ExtendUser(StatesGroup):
    mode = State()
    manual = State()


class ReceiptConfirm(StatesGroup):
    waiting = State()


class Feedback(StatesGroup):
    waiting = State()
    media_confirm = State()


class MassDelete(StatesGroup):
    select = State()
    confirm = State()


class LeaderLink(StatesGroup):
    select = State()


class RenewalApproval(StatesGroup):
    manual_date = State()


class SetTelegramId(StatesGroup):
    waiting = State()


class AdminMessage(StatesGroup):
    personal = State()
    personal_confirm = State()
    broadcast = State()
    broadcast_confirm = State()
    select_recipients = State()
    selective_text = State()
    selective_confirm = State()


class DBImport(StatesGroup):
    waiting = State()


class AutoRenewalSettings(StatesGroup):
    waiting_value = State()


class RateTopup(StatesGroup):
    """
    Client-side flow for "💰 Доплатить сейчас" — see
    bot/handlers/client_menu.py (starts here AND owns the resulting admin
    review card, "rtopup:" callback prefix). Separate from ReceiptConfirm
    because the expected amount is a computed top-up figure
    (core.payment.calc_topup_amount), not a clean multiple of the
    client's monthly rate — mixing it into the normal receipt pipeline
    would make it fail evaluate_receipt_extraction's "amount must be a
    multiple of the rate" check.
    """
    waiting_receipt = State()


class ExtraLinksPayment(StatesGroup):
    """
    Client-side flow for paying the surcharge on extra devices beyond
    the free tier — see bot/handlers/extra_links.py (starts here, owns
    the resulting admin review card, "exlreview:" callback prefix, same
    one the older direct-approval path already used). Separate state
    (rather than reusing RateTopup) because the two mean different
    things even though the shape is similar: this is "pay to unlock N
    new devices", RateTopup is "true up money already owed on devices
    you already have".
    """
    waiting_receipt = State()
