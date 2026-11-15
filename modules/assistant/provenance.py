"""On-demand source descriptions. Never injected into ordinary tool replies."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    command: str | None
    text: str


SOURCES = {
    'overview': Source(None, 'Mes données viennent de la radio reçue, des services météo et de documents. Quelle catégorie veux-tu préciser ?'),
    'network': Source('mesh', 'Les statistiques viennent des paquets reçus par Baliz et conservés localement, sur la période demandée.'),
    'position': Source('mesh', 'Les positions viennent des coordonnées enregistrées à partir des annonces des nœuds. Elles peuvent dater d’une ancienne annonce.'),
    'paths': Source('path', 'Les chemins et sauts viennent des métadonnées radio du message reçu. Certains DM ne fournissent pas ce compteur.'),
    'clock': Source('mesh', 'La dérive compare l’horodatage des annonces ou messages reçus avec l’heure de Baliz, à la date de mesure.'),
    'wiki': Source('llm', 'Les réponses Wiki s’appuient sur les documents MeshCore de l’index documentaire local.'),
    'airtime': Source('mesh', 'L’airtime est estimé à partir des paquets reçus et émis par Baliz et des paramètres radio. Il décrit cette observation locale.'),
    'bbs': Source('bbs', 'Les statistiques BBS viennent des messages déposés par les utilisateurs et de leur état de lecture enregistré par Baliz.'),
    'assistant': Source(None, 'Une réponse de conversation est générée par le modèle IA. Elle ne prouve pas qu’une source externe a été consultée.'),
    'weather': Source('wx', ''),  # Current initialized backend supplies its own description.
}
# The existing weather.provider operation is the sole model-facing weather-source
# function, avoiding two overlapping functions for the same question.
TOPICS = tuple(topic for topic in SOURCES if topic != 'weather')


def argument_schema():
    return {'type':'object','properties':{'topic':{'type':'string','enum':list(TOPICS),
            'description':'Catégorie explicitement citée : network=statistiques réseau, position=coordonnées, paths=sauts, clock=dérive, wiki=documents, airtime=occupation radio, bbs=boîte aux lettres, assistant=IA. Sans catégorie précisée : overview.'}},
            'required':['topic'],'additionalProperties':False}


def answer_source(dispatcher, topic, message, *, function=None):
    source = SOURCES[topic]
    command = dispatcher._command(source.command) if source.command else None
    if topic == 'weather':
        from ..commands.wx_command import WxCommand
        if type(command) is not WxCommand:
            return 'Je ne peux pas consulter la source météo pour le moment.'
    if source.command and not dispatcher._allowed(command, message, service=True):
        if topic == 'weather':
            return 'Je ne peux pas consulter la source météo pour le moment.'
        return 'Je ne peux pas consulter la provenance de cette fonction ici.'
    dispatcher.record_function(function or 'info.provenance.' + topic)
    if command is not None:
        command.record_execution(message.sender_id or None)
    if topic == 'weather':
        return command.provider_description()
    if topic == 'assistant':
        llm = dispatcher._command('llm')
        if llm is not None and dispatcher._allowed(llm, message, service=True):
            model = getattr(getattr(llm, 'service', None), 'model', None)
            if (isinstance(model, str) and model.strip() and len(model.strip().encode('utf-8')) <= 80
                    and not any(ord(char) < 32 for char in model)):
                return f'Le modèle configuré pour Baliz est {model.strip()}.'
        return source.text
    if topic == 'wiki' and getattr(command.service, 'wiki_rag', None) is None:
        return 'La recherche documentaire n’est pas active actuellement. Je ne peux pas désigner de document consulté.'
    return source.text
