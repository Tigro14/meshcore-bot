"""Manual live-model evaluation. No capability is executed and no RF is sent.

python tests/eval_catalog_live.py --config /etc/meshcore-bot/config.ini
"""
import argparse
import asyncio
import configparser
import json
import logging
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from modules.assistant.catalog import CATALOG
from modules.assistant.semantic_router import SemanticRouter


async def evaluate(config_path, cases_path):
    config = configparser.ConfigParser()
    if not config.read(config_path):
        raise ValueError('Configuration not found')

    class Owner:
        logger = logging.getLogger('routing_eval')

        def get_config_value(self, section, key, fallback=None, value_type='str'):
            reader = {'bool': config.getboolean, 'float': config.getfloat, 'int': config.getint}.get(value_type, config.get)
            return reader(section, key, fallback=fallback)

    router = SemanticRouter(Owner())
    failures = 0
    for case in json.loads(Path(cases_path).read_text()):
        started = time.monotonic()
        plan = await router.decide(case['question'], set(CATALOG))
        actual = {'route': plan.route.value, 'operation': plan.operation, 'args': plan.args} if plan else None
        expected = {k: case[k] for k in ('route', 'operation', 'args')}
        passed = actual == expected
        failures += not passed
        print(json.dumps(dict(question=case['question'], actual=actual, expected=expected,
                              passed=passed, seconds=round(time.monotonic()-started, 2)), ensure_ascii=False), flush=True)
    return int(bool(failures))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--cases', default=str(Path(__file__).parent/'fixtures'/'routing_cases.json'))
    options = parser.parse_args()
    raise SystemExit(asyncio.run(evaluate(options.config, options.cases)))
