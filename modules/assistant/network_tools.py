"""Explicit Tigro adapters and parameterized observation queries."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from .router import normalize
from .usage import record
from ..multibyte_detection import find_onebyte_repeaters


def countries(conn, requested):
    values = [r[0] for r in conn.execute('SELECT DISTINCT country FROM complete_contact_tracking WHERE country IS NOT NULL')]
    aliases = {'royaume-uni':'united kingdom','royaume uni':'united kingdom','angleterre':'united kingdom',
               'allemagne':'deutschland','germany':'deutschland','fr':'france','uk':'united kingdom'}
    key = aliases.get(normalize(requested), normalize(requested))
    matches = [v for v in values if normalize(v)==key]
    if not matches: raise ValueError(f'Aucun pays observé ne correspond à « {requested} ».')
    return matches


def count_nodes(conn, args):
    """Count identities in local advert observations, never infer global activity.

    repeater_manager stores naive datetime.now() in last_heard, so use the
    same local clock here (not a hard-coded UTC offset).
    """
    clauses = ["public_key IS NOT NULL", "public_key != ''"]
    values = []
    hours = args['hours']
    country = args['country']
    if hours:
        clauses.append("datetime(last_heard) BETWEEN datetime('now','localtime',?) AND datetime('now','localtime')")
        values.append(f'-{hours} hours')
    if country:
        matches = countries(conn, country)
        clauses.append('country IN (' + ','.join('?' for _ in matches) + ')')
        values.extend(matches)
    count = conn.execute('SELECT COUNT(DISTINCT public_key) FROM complete_contact_tracking WHERE '
                         + ' AND '.join(clauses), values).fetchone()[0]
    scope = f' en {country}' if country else ''
    period = f' sur les dernières {hours} h' if hours else ''
    return f'{count} nœuds connus de Baliz{scope}, tous types confondus, observés par leurs annonces{period}.'


def observation_answer(conn, op, args, logger):
    if op == 'summary':
        from .network_summary import network_summary
        return network_summary(conn)
    if op == 'count_nodes':
        return count_nodes(conn, args)
    clauses=["LOWER(role) = 'repeater'"];values=[]
    hours=args.get('hours',0); country=args.get('country','');limit=args.get('limit',5)
    if hours:
        clauses.append('datetime(last_heard) >= datetime(?)')
        values.append((datetime.now(timezone.utc)-timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S'))
    if country:
        matches=countries(conn,country)
        clauses.append('country IN ('+','.join('?' for _ in matches)+')');values.extend(matches)
    where=' AND '.join(clauses)
    scope=(f' en {country}' if country else '')+(f' entendus depuis {hours} h' if hours else '')
    if op=='count_repeaters':
        count=conn.execute('SELECT COUNT(*) FROM complete_contact_tracking WHERE '+where,values).fetchone()[0]
        return f'{count} répéteurs connus du bot{scope}.'
    order='advert_count DESC, last_heard DESC' if args.get('sort')=='activity' else 'last_heard DESC'
    rows=conn.execute('SELECT public_key,name,advert_count FROM complete_contact_tracking WHERE '+where+' ORDER BY '+order,values).fetchall()
    if op=='onebyte':
        # Apply the country/time filters AFTER detecting encoding from all evidence.
        evidence={r['public_key'] for r in find_onebyte_repeaters(conn,top_n=max(1,conn.execute('SELECT COUNT(*) FROM complete_contact_tracking').fetchone()[0]),logger=logger)}
        rows=[r for r in rows if r[0] in evidence]
    if not rows: return f'Aucun répéteur correspondant dans les observations du bot{scope}.'
    label='Répéteurs en 1 octet' if op=='onebyte' else 'Répéteurs'
    names=[str(r[1] or r[0][:8])+(f' ({r[2]} annonces cumulées)' if args.get('sort')=='activity' else '') for r in rows[:limit]]
    return f'{label}{scope} ({min(len(rows),limit)}/{len(rows)}) : '+', '.join(names)


def trace_content(conn, args):
    path=args.get('path','');target=args.get('target','')
    if target:
        rows=conn.execute('SELECT name,public_key,out_path,out_bytes_per_hop,out_path_len FROM complete_contact_tracking WHERE name = ? COLLATE NOCASE OR public_key = ? COLLATE NOCASE OR substr(public_key,1,4) = ? COLLATE NOCASE', (target,target,target)).fetchall()
        if len(rows)!=1: raise ValueError('Cible absente ou ambiguë : indique son nom exact ou son préfixe de clé.')
        _,key,out,bph,path_len=rows[0]
        if path_len is None or int(path_len)<0: raise ValueError("Aucun chemin radio connu vers cette cible.")
        import re
        width=int(bph or 1)*2
        if width not in (2,4): raise ValueError('Ce format de chemin n’est pas pris en charge par trace.')
        raw=str(out or '').strip()
        if not re.fullmatch(r'[0-9a-fA-F]*',raw) or len(raw)%width: raise ValueError('Aucun chemin enregistré exploitable vers cette cible.')
        path=','.join([raw[i:i+width] for i in range(0,len(raw),width)]+[key[:width]])
    return ('tracer' if args.get('roundtrip',True) else 'trace')+(' '+path if path else '')


async def answer_network(dispatcher, decision, message):
    from ..commands.hfcond_command import HfcondCommand
    from ..commands.near_command import NearCommand
    from ..commands.stats_command import StatsCommand
    from ..commands.neighbors_command import NeighborsCommand
    from ..commands.trace_command import TraceCommand
    from ..commands.advert_command import AdvertCommand
    parent=dispatcher._command('mesh')
    if not dispatcher._allowed(parent,message,service=True):
        return 'La fonction mesh est indisponible ou non autorisée ici.'
    op=decision.operation;args=decision.args
    if op=='unsupported': return args.get('error',"Cette demande réseau n'est pas encore prise en charge par les fonctions disponibles.")
    try:
        if op == 'relay_connectivity':
            from .relay_connectivity import connectivity
            record(dispatcher.owner.bot, "mesh." + op)
            parent.record_execution(message.sender_id or None)
            with dispatcher.owner.bot.db_manager.connection() as conn:
                return connectivity(conn, args['hours'], args['limit'], dispatcher.owner.get_max_message_length(message))
        adapters={'hfcond':HfcondCommand,'near':NearCommand,'stats':StatsCommand,'neighbors':NeighborsCommand,'trace':TraceCommand,'advert':AdvertCommand}
        if op in adapters:
            command=dispatcher._command(op)
            async with dispatcher._rf_lock:
                if type(command) is not adapters[op] or not dispatcher._allowed(command,message,service=True):
                    return f'La fonction {op} est limitée ou non autorisée ici.'
                content=op
                if op=='near': content=' '.join(str(v) for v in ('near',args.get('limit',5),args.get('role','repeater'),args.get('target','')) if v!='')
                elif op=='stats': content='stats '+args.get('topic','general')+(' hashes' if args.get('hashes') else '')
                elif op=='trace':
                    with dispatcher.owner.bot.db_manager.connection() as conn: content=trace_content(conn,args)
                    if len(content.split(' ',1))>1 and len(content.split(' ',1)[1].split(','))>command.maximum_hops:
                        return 'Ce chemin dépasse le nombre de sauts autorisé pour trace.'
                cloned=deepcopy(message);cloned.content=content;cloned.content_lower=content.casefold();cloned.prefix_normalized=True;cloned.capture_sink=[]
                record(dispatcher.owner.bot, "mesh." + op + ("." + args.get("topic", "general") if op == "stats" else ""))
                command.record_execution(message.sender_id or None)
                parent.record_execution(message.sender_id or None)
                execute=getattr(command,'execute_service',command.execute)
                await execute(cloned)
                source='\n'.join(cloned.capture_sink) or 'La fonction n’a renvoyé aucun résultat.'
        else:
            record(dispatcher.owner.bot, "mesh." + op)
            parent.record_execution(message.sender_id or None)
            with dispatcher.owner.bot.db_manager.connection() as conn:
                source=observation_answer(conn,op,args,dispatcher.owner.logger)
        if op == 'hfcond':
            return await dispatcher._render_tool(
                decision.question, source, message,
                context="Conditions HF globales HamQSL uniquement. d=jour, n=nuit; bandes en mètres. Aucune prévision tropo locale ni mesure à 869 MHz. Indique HF dans la réponse.",
            )
        budget=dispatcher.owner.get_max_message_length(message)
        if len(source.encode('utf-8')) > budget:
            separator='\n' if '\n' in source else ', '
            parts=source.split(separator)
            kept=[]
            for part in parts:
                if len((separator.join(kept+[part])+' …').encode('utf-8')) > budget: break
                kept.append(part)
            if kept: source=separator.join(kept)+' …'
        if op == 'near' and source.startswith('Repère :'):
            # The approximation/source qualifier is mandatory, even if the LLM
            # omits it or rewrites distances as being relative to the sender.
            return source
        context = ''
        if op in {'summary', 'count_nodes', 'count_repeaters'}:
            context = (
                'Observations locales de Baliz, pas un recensement de tout le réseau. '
                'Conserve explicitement la limite aux observations de Baliz. '
                'La période ne concerne que les chiffres explicitement associés à cette période dans les données. '
                'Dans un résumé, garde séparées les deux phrases : total connu sans période, puis activité sur 24 h. '
                'Des nœuds entendus restent des nœuds entendus, pas des utilisateurs.'
            )
        return await dispatcher._render_tool(decision.question,source,message,context=context)
    except ValueError as exc:
        return str(exc)
    except Exception:
        dispatcher.owner.logger.exception('Network operation failed: %s',op)
        return 'Le diagnostic réseau est temporairement indisponible.'
