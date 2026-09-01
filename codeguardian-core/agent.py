"""Compatibility entrypoint for the CodeGuardian agent.

The Jenkins pipeline executes this file directly. The implementation lives in
`codeguardian.runtime` so the project can be organised as a package without
changing the external command used by the pipeline.
"""

import asyncio
import sys

from codeguardian.ai import *  # noqa: F403
from codeguardian.bitbucket import *  # noqa: F403
from codeguardian.comments import *  # noqa: F403
from codeguardian.config import *  # noqa: F403
from codeguardian.input_contract import *  # noqa: F403
from codeguardian.logging_utils import logger
from codeguardian.models import *  # noqa: F403
from codeguardian.models import AgentExecutionError
from codeguardian.runtime import *  # noqa: F403
from codeguardian.runtime import main
from codeguardian.sonarqube import *  # noqa: F403
from codeguardian.text import *  # noqa: F403
from codeguardian.validation import *  # noqa: F403


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AgentExecutionError as e:
        logger.error(f"Agent execution failed: {e}")
        sys.exit(1)
    except Exception as e:
        logger.exception(f"Unexpected unhandled error: {e}")
        sys.exit(1)
