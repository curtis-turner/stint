import asyncio
import os
import sys
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from examples.adf.schema import Foo
from stint import APITokenAuth, AsyncSession, StateFile, create_engine, select


async def main():
    engine = create_engine(
        "jira_cloud+https://cumulusec.atlassian.net",
        auth=APITokenAuth(email=os.getenv("STINT_USER", ""), token=os.getenv("STINT_TOKEN", "")),
        dialect="jira_cloud",  # optional, defaults to cloud
    )
    state = StateFile.load(Path("examples/adf/state.yaml"))
    async with AsyncSession(engine, state) as session:
        issue = await session.scalars(select(Foo).where(Foo.c.summary.contains("ADF")))
        print(issue)
        print("Description text:", issue[0].description)


if __name__ == "__main__":
    asyncio.run(main())
