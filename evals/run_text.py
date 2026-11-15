"""Run the native text eval harness without installing its speech/CLI extras."""

import argparse
import asyncio
from pathlib import Path

from pipecat.evals.scenario import EvalScenarioFile
from pipecat.evals.session import EvalSession, EvalSessionParams


async def run(bot_url: str):
    scenarios = EvalScenarioFile.load(Path(__file__).with_name("api_text.yaml"))
    passed = True
    for scenario in scenarios.scenarios:
        session = EvalSession.from_scenario(
            scenario, bot_url, params=EvalSessionParams(default_timeout_ms=30000)
        )
        result = await session.run()
        print(f"{scenario.name}: {'PASS' if result.passed else 'FAIL'}")
        for failure in result.failures:
            print(f"  {failure}")
        passed = passed and result.passed
    return passed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bot-url", default="ws://127.0.0.1:8766/ws")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(run(args.bot_url)) else 1)
