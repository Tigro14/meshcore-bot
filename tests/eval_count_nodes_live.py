"""Manual no-RF smoke: real interpreter, read-only database, real reformulator."""
import asyncio
import configparser
import json
import logging
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from modules.assistant.catalog import CATALOG
from modules.assistant.semantic_router import SemanticRouter
from modules.assistant.network_tools import count_nodes
from modules.assistant.llm_service import LlmService
from modules.models import MeshMessage

async def main():
    config=configparser.ConfigParser()
    config.read('/etc/meshcore-bot/config.ini')
    class Owner:
        logger=logging.getLogger('count_live')
        def get_config_value(self,section,key,fallback=None,value_type='str'):
            reader={'bool':config.getboolean,'float':config.getfloat,'int':config.getint}.get(value_type,config.get)
            return reader(section,key,fallback=fallback)
    owner=Owner()
    question='Baliz combien de nœuds actifs?'
    plan=await SemanticRouter(owner).decide(question,set(CATALOG))
    assert plan and plan.operation=='count_nodes' and plan.args==dict(hours=24,country=''),plan
    with sqlite3.connect('file:/var/lib/meshcore-bot/meshcore_bot.db?mode=ro',uri=True) as db:
        source=count_nodes(db,plan.args)
    # Initialize only the standalone tool-reformulation surface: no bot/radio,
    # wiki initialization or conversation history participates in this call.
    service=object.__new__(LlmService)
    service.logger=owner.logger
    # Explicit companion fixture for this manual, synthetic no-RF evaluation.
    test_key='ab'*32
    service.bot=SimpleNamespace(meshcore=SimpleNamespace(contacts={test_key:{
        'public_key':test_key,'type':1}},pending_contacts={}),db_manager=None)
    message=MeshMessage(question,sender_pubkey=test_key,is_dm=True)
    service._answer_lock=asyncio.Lock()
    service.endpoint=config.get('Llm_Command','endpoint')
    service.model=config.get('Llm_Command','model')
    service.reasoning_effort='none'
    service.max_tokens=config.getint('Llm_Command','max_tokens',fallback=80)
    service.temperature=0
    service.top_p=0.9
    service.timeout_seconds=config.getfloat('Llm_Command','timeout_seconds',fallback=20)
    service.strip_thinking_tags=True
    result=await service.rephrase_tool_result(question,source,message=message,context='Faits mesurés localement. Conserve le nombre de nœuds, la période et la source locale. Un seul message de 220 octets UTF-8 maximum.',max_length=220)
    assert result, 'LLM reformulation unavailable or rejected'
    assert len(result.encode())<=220
    print(json.dumps(dict(plan=dict(route=plan.route.value,operation=plan.operation,args=plan.args),source=source,response=result,bytes=len(result.encode())),ensure_ascii=False))

if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
