#!/usr/bin/env python3
"""Opt-in live LLM routing checks. Never executes tools or sends radio messages."""
import argparse
import asyncio
import configparser
import json
import logging
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from modules.assistant.semantic_router import SemanticRouter
from modules.assistant.catalog import grounded_place

async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--cases', default=str(ROOT / 'tests/fixtures/network_routing_regression.json'))
    options = parser.parse_args()
    config = configparser.ConfigParser(interpolation=None)
    if not config.read(options.config):
        parser.error('Configuration not found')
    def read(section, key, fallback=None, value_type='str'):
        getter = {'bool': config.getboolean, 'int': config.getint, 'float': config.getfloat}.get(value_type, config.get)
        return getter(section, key, fallback=fallback)
    router = SemanticRouter(SimpleNamespace(logger=logging.getLogger(__name__), get_config_value=read))
    failures = 0
    cases = json.loads(Path(options.cases).read_text())
    for case in cases:
        decision = await router.decide(case['question'], {'mesh', 'llm', 'wiki', 'weather', 'test', 'path'})
        actual = decision.route.value + '.' + decision.operation if decision else None
        ok = actual == case['function'] and all(decision.args.get(k) == v for k, v in case.get('args', {}).items())
        if ok and 'place' in case:
            place = decision.args.get('location', '')
            ok = grounded_place(place, case['place']) and grounded_place(case['place'], place)
        failures += not ok
        print(json.dumps(dict(question=case['question'], expected=case['function'], actual=actual,
                              args=decision.args if decision else None, ok=ok), ensure_ascii=False), flush=True)
    print(f'{len(cases) - failures}/{len(cases)} passed')
    return bool(failures)

if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
