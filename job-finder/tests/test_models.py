"""The model menu: the models and the effort levels that the SDK gives."""

from job_finder.models import ModelList, from_sdk

from .fakes import ALL_EFFORTS, SDK_MODELS


def test_the_menu_has_only_the_models_and_levels_of_the_sdk():
    menu = from_sdk(SDK_MODELS)
    assert [(m["id"], m["label"], m["efforts"]) for m in menu] == [
        ("claude-sonnet-5-5", "Sonnet 5.5", ALL_EFFORTS),  # "default" and "sonnet" are the same model
        ("claude-fable-5-1", "Fable 5.1", ALL_EFFORTS),
        ("claude-opus-5-5", "Opus 5.5", ALL_EFFORTS),
        ("claude-haiku-4-5-20251001", "Haiku 4.5", []),  # no effort setting
    ]
    assert menu[0]["detail"] == "Efficient for routine tasks"
    fable = menu[1]
    assert (fable["detail"], fable["badge"]) == ("Most capable for your hardest and longest-running tasks",
                                                 "Requires usage credits")
    assert "badge" not in menu[0]


async def test_the_list_is_read_once_from_the_sdk():
    calls = []

    async def loader():
        calls.append(1)
        return from_sdk(SDK_MODELS)

    models = ModelList(loader, "claude-sonnet-5-5", "medium")
    assert [m["id"] for m in await models.menu()][:1] == ["claude-sonnet-5-5"]
    await models.menu()
    assert len(calls) == 1


async def test_a_default_model_that_the_sdk_does_not_list_comes_first_with_the_default_effort():
    async def loader():
        return from_sdk(SDK_MODELS)

    menu = await ModelList(loader, "claude-custom", "high").menu()
    assert menu[0] == {"id": "claude-custom", "label": "claude-custom", "detail": "The default of this server",
                       "efforts": ["high"]}


async def test_without_the_sdk_only_the_default_model_is_shown():
    async def loader():
        raise RuntimeError("Claude Code did not start")

    models = ModelList(loader, "claude-sonnet-5-5", "medium")
    assert [(m["id"], m["efforts"]) for m in await models.menu()] == [("claude-sonnet-5-5", ["medium"])]
