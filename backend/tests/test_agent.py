"""The chat agent, driven by a scripted fake model and the real database."""

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import agent, drops
from app.config import Settings, get_settings
from app.db import get_session
from app.deps import get_llm, get_paypal
from app.llm import LLMError
from app.main import app
from app.models import ChatMessage, Drop, DropStatus, Order, OrderStatus, Seller
from tests.fakes import FakePayPal, ScriptedLLM, sign_in

SETTINGS = Settings(public_api_url="http://api.test", frontend_url="http://app.test", timezone="America/New_York", dev_login=True)


@pytest.fixture
def paypal():
    return FakePayPal()


def turn(session, llm, paypal, role, text, session_id="s1", seller_id=None, user=None):
    return agent.run_turn(session, llm, paypal, SETTINGS, role, session_id, text, seller_id=seller_id, user=user)


def tool_results(llm, call_index):
    """The tool results the model saw on its call_index-th generate() call."""
    return [json.loads(m["result"]) for m in llm.calls[call_index]["messages"] if m["role"] == "tool"]


def future(days=3) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


# ---- what each role is allowed to do ---------------------------------------

def test_no_tool_can_move_money_directly():
    names = {t.name for tools in agent.TOOLS_BY_ROLE.values() for t in tools}
    assert not {n for n in names if any(w in n for w in ("capture", "void", "refund", "charge"))}


def test_roles_get_only_their_own_tools(session, seller, paypal):
    llm = ScriptedLLM("hi")
    turn(session, llm, paypal, "buyer", "hello")
    assert {t["name"] for t in llm.calls[0]["tools"]} == {"list_open_drops", "check_stock", "place_order", "my_orders"}
    llm = ScriptedLLM("hi")
    turn(session, llm, paypal, "seller", "hello", seller_id=seller.id)
    assert {t["name"] for t in llm.calls[0]["tools"]} == {"create_drop", "list_my_drops", "get_drop_status", "cancel_drop"}


def test_a_tool_outside_the_role_is_refused(session, make_drop, paypal):
    drop = make_drop()
    llm = ScriptedLLM([("cancel_drop", {"drop_id": drop.id, "confirm": True})], "ok")  # a buyer asking for a seller tool
    turn(session, llm, paypal, "buyer", "cancel it")
    assert "Unknown tool" in tool_results(llm, 1)[0]["error"]
    session.refresh(drop)
    assert drop.status == DropStatus.OPEN


def test_tool_schemas_are_clean(session):
    for tools in agent.TOOLS_BY_ROLE.values():
        for t in tools:
            assert "title" not in json.dumps(t.spec()) and t.spec()["parameters"]["type"] == "object"


# ---- seller flows ----------------------------------------------------------

def test_seller_creates_a_drop(session, seller, paypal):
    args = dict(item_name="Sourdough", unit_price=9, quantity_total=10, minimum_units=5, deadline=future())
    llm = ScriptedLLM([("create_drop", args)], "Your drop is live!")
    reply = turn(session, llm, paypal, "seller", "make a drop", seller_id=seller.id)
    assert reply == "Your drop is live!"
    drop = session.scalars(select(Drop)).one()
    assert drop.seller_id == seller.id and str(drop.unit_price) == "9.00"
    assert tool_results(llm, 1)[0]["id"] == drop.id
    assert tool_results(llm, 1)[0]["unit_price"] == "9.00"  # always two decimals, even before a database reload


def test_naive_deadline_is_read_in_the_configured_timezone(session, seller, paypal):
    naive = (datetime.now() + timedelta(days=2)).replace(microsecond=0, tzinfo=None).isoformat()
    args = dict(item_name="Pie", unit_price=5, quantity_total=4, minimum_units=2, deadline=naive)
    turn(session, ScriptedLLM([("create_drop", args)], "done"), paypal, "seller", "go", seller_id=seller.id)
    drop = session.scalars(select(Drop)).one()
    assert drop.deadline.astimezone(ZoneInfo("America/New_York")).replace(tzinfo=None).isoformat() == naive


def test_bad_tool_input_comes_back_as_a_readable_error(session, seller, paypal):
    llm = ScriptedLLM([("create_drop", {"item_name": "x", "unit_price": -1})], "sorry")
    turn(session, llm, paypal, "seller", "go", seller_id=seller.id)
    assert "Invalid arguments" in tool_results(llm, 1)[0]["error"]
    assert session.scalars(select(Drop)).all() == []


def test_an_unverified_seller_is_refused_through_the_agent_too(session, seller_user, seller, paypal):
    seller_user.paypal_verified = False
    session.commit()
    args = dict(item_name="x", unit_price=5, quantity_total=4, minimum_units=2, deadline=future())
    llm = ScriptedLLM([("create_drop", args)], "Sorry")
    turn(session, llm, paypal, "seller", "go", seller_id=seller.id)
    assert "isn't verified" in tool_results(llm, 1)[0]["error"]
    assert session.scalars(select(Drop)).all() == []


def test_engine_rules_still_apply_through_the_agent(session, seller, paypal):
    args = dict(item_name="x", unit_price=5, quantity_total=4, minimum_units=9, deadline=future())
    llm = ScriptedLLM([("create_drop", args)], "no")
    turn(session, llm, paypal, "seller", "go", seller_id=seller.id)
    assert "minimum" in tool_results(llm, 1)[0]["error"]


def test_seller_cannot_see_or_cancel_someone_elses_drop(session, seller, make_drop, paypal):
    drop = make_drop()  # belongs to `seller`
    from app.models import User
    rival_user = User(name="Rival", email="rival@example.com", paypal_verified=True)
    session.add(rival_user)
    session.commit()
    other = Seller(name="Rival", user_id=rival_user.id)
    session.add(other)
    session.commit()
    llm = ScriptedLLM(
        [("get_drop_status", {"drop_id": drop.id}), ("cancel_drop", {"drop_id": drop.id, "confirm": True})], "no"
    )
    turn(session, llm, paypal, "seller", "spy", seller_id=other.id)
    assert all("error" in r for r in tool_results(llm, 1))
    session.refresh(drop)
    assert drop.status == DropStatus.OPEN


def test_cancel_needs_explicit_confirmation(session, seller, make_drop, paypal):
    drop = make_drop()
    llm = ScriptedLLM([("cancel_drop", {"drop_id": drop.id, "confirm": False})], "Shall I?")
    turn(session, llm, paypal, "seller", "cancel", seller_id=seller.id)
    assert "confirm" in tool_results(llm, 1)[0]["error"]
    session.refresh(drop)
    assert drop.status == DropStatus.OPEN

    llm = ScriptedLLM([("cancel_drop", {"drop_id": drop.id, "confirm": True})], "Cancelled.")
    turn(session, llm, paypal, "seller", "yes cancel", seller_id=seller.id)
    session.refresh(drop)
    assert drop.status == DropStatus.CANCELLED


def test_drop_status_reports_progress_toward_the_minimum(session, seller, make_drop, paypal):
    drop = make_drop(quantity_total=10, minimum_units=3)
    order = drops.reserve_stock(session, drop.id, "Ann", "a@example.com", "chat-a", 2)
    drops.start_checkout(session, paypal, order.id, "r", "c")
    drops.confirm_authorization(session, paypal, f"PPO-{order.id}")
    llm = ScriptedLLM([("get_drop_status", {"drop_id": drop.id})], "2 of 3 so far")
    turn(session, llm, paypal, "seller", "how is it going", seller_id=seller.id)
    result = tool_results(llm, 1)[0]
    assert result["paid_up_units"] == 2 and result["minimum_met_so_far"] is False


# ---- buyer flows -----------------------------------------------------------

def test_buyer_places_an_order_and_gets_the_paypal_link(session, make_drop, paypal, buyer):
    drop = make_drop()
    llm = ScriptedLLM([("place_order", dict(drop_id=drop.id, quantity=2))], "Here is your link")
    turn(session, llm, paypal, "buyer", "2 please", session_id="user-77", user=buyer)
    order = session.scalars(select(Order)).one()
    assert order.chat_session_id == "user-77"  # supplied by the server
    assert (order.buyer_name, order.buyer_email, order.buyer_user_id) == (buyer.name, buyer.email, buyer.id)
    result = tool_results(llm, 1)[0]
    assert result["approval_url"].startswith("https://fake.paypal/approve/") and result["amount"] == "18.00"


def test_the_model_cannot_choose_who_is_buying(session, make_drop, paypal, buyer):
    """Even if the model invents a name, email or session id, the signed-in account is what counts."""
    drop = make_drop()
    args = dict(drop_id=drop.id, quantity=1, buyer_name="Eve", buyer_email="evil@example.com", chat_session_id="someone-else", buyer_user_id=999)
    turn(session, ScriptedLLM([("place_order", args)], "ok"), paypal, "buyer", "go", session_id="mine", user=buyer)
    order = session.scalars(select(Order)).one()
    assert (order.buyer_name, order.buyer_email, order.chat_session_id, order.buyer_user_id) == (buyer.name, buyer.email, "mine", buyer.id)


def test_place_order_tool_does_not_ask_for_personal_details(session):
    spec = next(t for t in agent.BUYER_TOOLS if t.name == "place_order").spec()
    assert set(spec["parameters"]["properties"]) == {"drop_id", "quantity"}


def test_sold_out_is_explained_not_raised(session, make_drop, paypal, buyer):
    drop = make_drop(quantity_total=2, minimum_units=1)
    llm = ScriptedLLM([("place_order", dict(drop_id=drop.id, quantity=3))], "Only 2 left")
    turn(session, llm, paypal, "buyer", "3 please", user=buyer)
    assert tool_results(llm, 1)[0]["remaining"] == 2


def test_paypal_outage_is_explained_and_stock_is_released(session, make_drop, paypal, buyer):
    drop = make_drop()
    paypal.fail_create = True
    llm = ScriptedLLM([("place_order", dict(drop_id=drop.id, quantity=4))], "Try again soon")
    turn(session, llm, paypal, "buyer", "go", user=buyer)
    assert "payment system" in tool_results(llm, 1)[0]["error"]
    assert drops.units_taken(session, drop.id) == 0


def test_my_orders_only_shows_this_chat_sessions_orders(session, make_drop, paypal):
    drop = make_drop()
    drops.reserve_stock(session, drop.id, "Ann", "a@example.com", "mine", 1)
    drops.reserve_stock(session, drop.id, "Ben", "b@example.com", "theirs", 2)
    llm = ScriptedLLM([("my_orders", {})], "one order")
    turn(session, llm, paypal, "buyer", "what did I order", session_id="mine")
    orders = tool_results(llm, 1)[0]["orders"]
    assert len(orders) == 1 and orders[0]["quantity"] == 1


# ---- the loop itself -------------------------------------------------------

def test_a_model_that_never_stops_calling_tools_is_cut_off(session, paypal):
    llm = ScriptedLLM(*[[("list_open_drops", {})]] * agent.MAX_STEPS)
    assert turn(session, llm, paypal, "buyer", "go") == agent.FALLBACK_REPLY
    assert len(llm.calls) == agent.MAX_STEPS


def test_empty_model_reply_gets_a_fallback(session, paypal):
    assert turn(session, ScriptedLLM(""), paypal, "buyer", "hi") == agent.FALLBACK_REPLY


def test_system_prompt_states_the_rules(session, paypal):
    llm = ScriptedLLM("hi")
    turn(session, llm, paypal, "buyer", "hello")
    prompt = llm.calls[0]["system"]
    assert "HELD" in prompt and "cannot charge" in prompt and "Only share links that a tool returned" in prompt


# ---- memory ----------------------------------------------------------------

def test_history_carries_over_between_turns(session, paypal):
    turn(session, ScriptedLLM("Hello! What can I get you?"), paypal, "buyer", "hi")
    llm = ScriptedLLM("Sure")
    turn(session, llm, paypal, "buyer", "do you have bread?")
    texts = [m["text"] for m in llm.calls[0]["messages"] if m["role"] != "tool"]
    assert texts == ["hi", "Hello! What can I get you?", "do you have bread?"]


def test_conversations_are_separate_per_role_and_session(session, seller, paypal):
    turn(session, ScriptedLLM("a"), paypal, "buyer", "secret buyer chat", session_id="x")
    llm = ScriptedLLM("b")
    turn(session, llm, paypal, "seller", "hello", session_id="x", seller_id=seller.id)  # same id, other role
    turn(session, llm := ScriptedLLM("c"), paypal, "buyer", "hello", session_id="y")
    assert [m["text"] for m in llm.calls[0]["messages"]] == ["hello"]


def test_history_window_never_starts_mid_exchange(session, paypal):
    for i in range(agent.HISTORY_LIMIT):  # enough messages that the window must cut something off
        turn(session, ScriptedLLM([("list_open_drops", {})], f"reply {i}"), paypal, "buyer", f"msg {i}")
    llm = ScriptedLLM("ok")
    turn(session, llm, paypal, "buyer", "latest")
    first = llm.calls[0]["messages"][0]
    assert first["role"] == "user"


def test_a_dangling_tool_request_is_skipped_and_later_messages_survive(session, paypal):
    conv = agent.conversation_id("buyer", "s1")
    session.add_all([
        ChatMessage(conversation_id=conv, role="user", text="hi"),
        ChatMessage(conversation_id=conv, role="assistant", text=None, tool_calls=[{"id": "c1", "name": "list_open_drops", "args": {}}]),
        # ...the server died here: no tool result was stored
    ])
    session.commit()

    llm = ScriptedLLM("recovered")
    assert turn(session, llm, paypal, "buyer", "hello again") == "recovered"
    assert [m["text"] for m in llm.calls[0]["messages"]] == ["hi", "hello again"]

    # The broken message is still in the database. A later turn must keep the newer
    # conversation and skip only the broken message.
    llm = ScriptedLLM("fine")
    turn(session, llm, paypal, "buyer", "third message")
    seen = [m["text"] for m in llm.calls[0]["messages"]]
    assert seen == ["hi", "hello again", "recovered", "third message"]
    assert all(not m.get("tool_calls") for m in llm.calls[0]["messages"])


# ---- HTTP endpoints --------------------------------------------------------

@pytest.fixture
def make_client(session_factory, paypal):
    holder = {"llm": ScriptedLLM("hi there")}

    def _session():
        with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_paypal] = lambda: paypal
    app.dependency_overrides[get_llm] = lambda: holder["llm"]
    app.dependency_overrides[get_settings] = lambda: SETTINGS

    def build():
        c = TestClient(app, follow_redirects=False)
        c.holder = holder
        return c

    yield build
    app.dependency_overrides.clear()


def test_chat_requires_sign_in(make_client):
    c = make_client()
    assert c.post("/chat/buyer", json={"message": "hi"}).status_code == 401
    assert c.post("/chat/seller", json={"message": "hi"}).status_code == 401
    assert c.get("/chat/buyer/history").status_code == 401


def test_buyer_chat_endpoint_and_history(make_client):
    c = make_client()
    sign_in(c, "Sam", "sam@example.com")
    r = c.post("/chat/buyer", json={"message": "hello"})
    assert r.status_code == 200 and r.json() == {"reply": "hi there"}
    assert c.get("/chat/buyer/history").json()["messages"] == [
        {"role": "user", "text": "hello"}, {"role": "assistant", "text": "hi there"}]
    assert c.get("/chat/seller/history").json()["messages"] == []  # a different conversation
    assert c.get("/chat/hacker/history").status_code == 404


def test_chat_history_is_private_to_each_account(make_client):
    ann, ben = make_client(), make_client()
    sign_in(ann, "Ann", "ann@example.com")
    sign_in(ben, "Ben", "ben@example.com")
    ann.post("/chat/buyer", json={"message": "my private question"})
    assert ben.get("/chat/buyer/history").json()["messages"] == []


def test_seller_chat_needs_a_shop(make_client):
    c = make_client()
    sign_in(c, "Bea", "bea@example.com")
    assert c.post("/chat/seller", json={"message": "hi"}).status_code == 403   # signed in, but no shop yet
    c.post("/shop", json={"name": "Bea's"})
    assert c.post("/chat/seller", json={"message": "hi"}).status_code == 200


def test_the_ai_buyer_chat_places_orders_for_the_signed_in_account(make_client, session_factory, make_drop):
    drop = make_drop()  # an open drop, owned by a verified shop (fixtures)
    c = make_client()
    sign_in(c, "Sam Lee", "sam@example.com")
    c.holder["llm"] = ScriptedLLM([("place_order", dict(drop_id=drop.id, quantity=2))], "Done")
    assert c.post("/chat/buyer", json={"message": "2 please"}).status_code == 200
    with session_factory() as s:
        order = s.scalars(select(Order)).one()
    assert order.buyer_email == "sam@example.com" and order.buyer_user_id is not None
    assert c.get("/me/orders").json()["orders"][0]["id"] == order.id


def test_chat_validates_input(make_client):
    c = make_client()
    sign_in(c)
    assert c.post("/chat/buyer", json={"message": ""}).status_code == 422
    assert c.post("/chat/buyer", json={"message": "x" * 2001}).status_code == 422


def test_model_outage_is_a_503(make_client):
    class Down:
        def generate(self, **kwargs):
            raise LLMError("model unreachable")

    c = make_client()
    sign_in(c)
    c.holder["llm"] = Down()
    assert c.post("/chat/buyer", json={"message": "hello"}).status_code == 503
