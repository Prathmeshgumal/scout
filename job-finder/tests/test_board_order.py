"""The order of the cards in a column of a board: the user drags a card up or down."""

import httpx
import pytest

from job_finder.board_order import BoardOrder
from job_finder.web.app import create_app
from job_finder.web.sessions import GoalSessions

from .fakes import Streams, make_services, replies


@pytest.fixture
def order(store):
    return BoardOrder(store.pool)


async def test_a_board_with_no_order_gives_nothing(order):
    assert await order.positions("pipeline") == {}


async def test_the_order_of_a_column_is_saved(order):
    await order.save("pipeline", "applied", ["b", "a", "c"])
    assert await order.positions("pipeline") == {
        "b": {"column": "applied", "position": 0},
        "a": {"column": "applied", "position": 1},
        "c": {"column": "applied", "position": 2},
    }


async def test_a_new_order_replaces_the_old_order_and_keeps_other_columns(order):
    await order.save("pipeline", "fit", ["x", "y"])
    await order.save("pipeline", "applied", ["a", "b"])
    await order.save("pipeline", "applied", ["b", "a"])
    positions = await order.positions("pipeline")
    assert positions["b"]["position"] == 0 and positions["a"]["position"] == 1
    assert positions["x"] == {"column": "fit", "position": 0}


async def test_a_card_that_moves_to_another_column_takes_its_new_place(order):
    await order.save("pipeline", "fit", ["a", "b"])
    await order.save("pipeline", "applied", ["c", "a"])
    assert (await order.positions("pipeline"))["a"] == {"column": "applied", "position": 1}


async def test_each_board_has_its_own_order(order):
    await order.save("pipeline", "fit", ["a"])
    await order.save("jobs", "fit", ["z"])
    assert set(await order.positions("pipeline")) == {"a"}
    assert set(await order.positions("jobs")) == {"z"}


@pytest.fixture
async def client(app_settings, store, hub, recorder):
    services = make_services(app_settings, store, hub, recorder, Streams(replies("On it.")))
    manager = GoalSessions(services, idle_s=600)
    app = create_app(app_settings, services, manager)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http
    await manager.close_all()


async def test_the_routes_save_and_give_the_order(client):
    saved = await client.put("/api/board-order/jobs", json={"column": "partial", "keys": ["j2", "j1"]})
    assert saved.status_code == 204
    body = (await client.get("/api/board-order/jobs")).json()
    assert body == {"positions": {"j2": {"column": "partial", "position": 0}, "j1": {"column": "partial", "position": 1}}}


async def test_the_routes_refuse_bad_input(client):
    assert (await client.get("/api/board-order/other")).status_code == 404
    assert (await client.put("/api/board-order/other", json={"column": "fit", "keys": []})).status_code == 404
    assert (await client.put("/api/board-order/jobs", json={"column": "", "keys": ["a"]})).status_code == 422
    assert (await client.put("/api/board-order/jobs", json={"column": "fit", "keys": ["a", "a"]})).status_code == 422
    assert (await client.put("/api/board-order/jobs", json={"column": "fit", "keys": ["a"] * 0 + [f"k{i}" for i in range(2001)]})).status_code == 422
