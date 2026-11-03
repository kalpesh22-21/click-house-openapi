"""Every blueprint declares a reviewed presentation the agent can suggest for its result.

`presentation` is advice, not a forced layout: the agent filters `kind` to the
presentations its deployment enables and the model may still choose another view when
the user asks or the result does not fit. These checks keep the advice executable.
"""
import pytest

from app.corpus.loader import load_blueprints

BLUEPRINTS = load_blueprints()
KINDS = {"figure", "facts", "table", "bars", "line", "donut"}
NUMERIC = {"integer", "number", "currency", "percent"}


@pytest.mark.parametrize("blueprint", list(BLUEPRINTS.values()), ids=list(BLUEPRINTS))
def test_presentation_names_real_columns_in_a_shape_its_kind_accepts(blueprint):
    presentation = blueprint["presentation"]
    assert set(presentation) <= {"kind", "category_columns", "measure_columns", "by_group_by"}
    kind = presentation["kind"]
    assert kind in KINDS
    categories = presentation.get("category_columns", [])
    measures = presentation.get("measure_columns", [])
    formats = blueprint["column_formats"]
    assert set(categories + measures) <= set(formats)
    assert all(formats[m]["kind"] in NUMERIC for m in measures)
    if kind == "table":
        assert not categories and not measures
    elif kind == "figure":
        assert not categories and len(measures) == 1
    elif kind == "facts":
        assert not categories and measures
    elif kind == "donut":
        # Parts of one whole: a single dimension, one additive amount (never a rate), and
        # every part present - a LIMIT would make the donut a truncated top-N.
        assert len(categories) == 1 and len(measures) == 1
        assert formats[measures[0]]["kind"] != "percent"
        sql = blueprint.get("sql_template") or blueprint["composes"][-1]["sql_template"]
        assert "LIMIT" not in sql.upper()
    elif kind == "bars":
        # One readable category and one measure; a result that needs more reads better as a table.
        assert len(categories) == 1 and len(measures) == 1
    else:
        assert categories and measures


@pytest.mark.parametrize(
    "blueprint",
    [b for b in BLUEPRINTS.values() if "by_group_by" in b["presentation"]],
    ids=[i for i, b in BLUEPRINTS.items() if "by_group_by" in b["presentation"]],
)
def test_group_by_overrides_name_declared_group_by_values(blueprint):
    group_by = next(s for s in blueprint["slots"] if s["name"] == "group_by")
    overrides = blueprint["presentation"]["by_group_by"]
    assert set(overrides) <= set(group_by["enum_values"])
    assert set(overrides.values()) <= KINDS - {"table", "donut"}
    # A line override plots the Group column, which must be the declared category.
    if "line" in overrides.values():
        assert "Group" in blueprint["presentation"]["category_columns"]
