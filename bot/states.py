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
