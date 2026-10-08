"""Shared fixtures, catalog filters (--category/--tag/--severity) and report writing."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest

from framework.catalog import SEVERITIES, load_catalog, select
from framework.reporting import write_reports
from framework.runner import RuleResult, connect

RESULTS = pytest.StashKey[list[RuleResult]]()


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("rule catalog")
    group.addoption("--category", action="append", default=[], help="only rules in this category")
    group.addoption("--tag", action="append", default=[], help="only rules with this tag")
    group.addoption("--severity", action="append", default=[], choices=SEVERITIES, help="only this severity")


def pytest_configure(config: pytest.Config) -> None:
    config.stash[RESULTS] = []


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Parametrize any test that asks for `rule` with the (filtered) catalog."""
    if "rule" in metafunc.fixturenames:
        option = metafunc.config.getoption
        rules = select(load_catalog(), option("category"), option("tag"), option("severity"))
        metafunc.parametrize("rule", rules, ids=[rule.id for rule in rules])


@pytest.fixture(scope="session")
def db() -> Iterator[psycopg.Connection]:
    """The database under test: PGDATABASE."""
    with connect() as conn:
        yield conn


@pytest.fixture(scope="session")
def planted_db() -> Iterator[psycopg.Connection]:
    with connect(os.environ.get("PGDATABASE", "theracare")) as conn:
        yield conn


@pytest.fixture(scope="session")
def clean_db() -> Iterator[psycopg.Connection]:
    with connect(os.environ.get("CLEAN_PGDATABASE", "theracare_clean")) as conn:
        yield conn


@pytest.fixture
def record_result(request: pytest.FixtureRequest) -> Callable[[RuleResult], None]:
    """Collect a rule result for the JSON / Jira reports written at the end of the session."""
    return request.config.stash[RESULTS].append


def pytest_sessionfinish(session: pytest.Session) -> None:
    results = session.config.stash[RESULTS]
    if results:
        database = os.environ.get("PGDATABASE", "theracare")
        report_dir = Path(os.environ.get("REPORT_DIR", "reports")) / database
        write_reports(results, report_dir, database)
