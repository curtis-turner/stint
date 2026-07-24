import asyncio
import os
import sys
from pathlib import Path

from stint import APITokenAuth, AsyncSession, StateFile, create_engine

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from examples.adf.schema import Foo


async def main():
    engine = create_engine(
        "jira_cloud+https://cumulusec.atlassian.net",
        auth=APITokenAuth(email=os.getenv("STINT_USER", ""), token=os.getenv("STINT_TOKEN", "")),
        dialect="jira_cloud",  # optional, defaults to cloud
    )
    state = StateFile.load(Path("examples/adf/state.yaml"))
    async with AsyncSession(engine, state) as session:
        # Create a bug with an ADF description
        issue = Foo(
            summary="Live ADF test",
            description="Live line 1\n\nLive line 2",
        )
        session.add(issue)
        await session.commit()
        print(f"Created issue {issue.key}")


if __name__ == "__main__":
    asyncio.run(main())
