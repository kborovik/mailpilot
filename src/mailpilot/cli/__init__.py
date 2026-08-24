"""CLI interface for MailPilot.

Startup-critical: only ``click`` is imported at module level. All heavy
dependencies (logfire, psycopg, httpx, pydantic, mailpilot.database,
mailpilot.settings) are lazy-imported inside command functions so that
``--help`` / ``--version`` stay fast (~50 ms).
When adding new commands, keep imports inside the function body.
"""
# pyright: reportPrivateUsage=false, reportUnusedImport=false

from importlib.metadata import distribution as distribution

import click as click

# Importing each noun module registers ``@main.group()`` commands.
from mailpilot.cli import account as account
from mailpilot.cli import activity as activity
from mailpilot.cli import company as company
from mailpilot.cli import config as config
from mailpilot.cli import contact as contact
from mailpilot.cli import db as db
from mailpilot.cli import email as email
from mailpilot.cli import enrollment as enrollment
from mailpilot.cli import meeting as meeting
from mailpilot.cli import note as note
from mailpilot.cli import show as show
from mailpilot.cli import tag as tag
from mailpilot.cli import task as task
from mailpilot.cli import template as template
from mailpilot.cli import workflow as workflow
from mailpilot.cli.company import (
    company_create as company_create,
)
from mailpilot.cli.company import (
    company_update as company_update,
)
from mailpilot.cli.main import (
    _database_url as _database_url,
)
from mailpilot.cli.main import (
    _db as _db,
)
from mailpilot.cli.main import (
    _looks_like_uuid as _looks_like_uuid,
)
from mailpilot.cli.main import (
    _version as _version,
)
from mailpilot.cli.main import (
    configure_logging as configure_logging,
)
from mailpilot.cli.main import (
    main as main,
)
from mailpilot.cli.main import (
    output as output,
)
from mailpilot.cli.main import (
    output_entity as output_entity,
)
from mailpilot.cli.main import (
    output_error as output_error,
)
from mailpilot.cli.main import (
    scrub_tool_response_callback as scrub_tool_response_callback,
)
from mailpilot.cli.task import (
    task_cancel as task_cancel,
)
from mailpilot.cli.task import (
    task_list as task_list,
)
from mailpilot.cli.task import (
    task_retry as task_retry,
)
