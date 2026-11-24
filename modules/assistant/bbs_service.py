"""Natural BBS actions backed by the existing mailbox, without generated replies."""
import asyncio
import re
import time
from dataclasses import dataclass, field

from .router import normalize

SESSION_SECONDS = 300
MAX_SESSIONS = 128


def argument_schema(operation):
    fields = {'send': ('recipient', 'text'), 'continue': ('selection', 'text')}.get(operation, ())
    return {'type': 'object', 'properties': {name: {'type': 'string'} for name in fields},
            'required': list(fields), 'additionalProperties': False}


def explicit_message_text(question, recipient):
    """Take a literal payload block, never words from the request's instruction.

    The initial request may supply a body after a colon/newline or in quotes
    after the recipient. Otherwise the separate body-entry step is required.
    """
    start = question.find(recipient) + len(recipient) if recipient else 0
    tail = question[start:]
    quoted = tail.strip()
    for opening, closing in [('"','"'),('«','»'),('“','”')]:
        if quoted.startswith(opening) and quoted.endswith(closing) and len(quoted) >= 2:
            return quoted[1:-1].strip()
    separator = re.search(r'[:：](?=\s|$)|(?<=\s)[:：]|\n', tail)
    if separator:
        payload = tail[separator.end():].strip()
    else:
        payload = tail.strip()
        if not payload.startswith(('"', '«', '“')):
            return ''
    for opening, closing in [('"','"'),('«','»'),('“','”')]:
        if payload.startswith(opening) and payload.endswith(closing) and len(payload) >= 2:
            return payload[1:-1].strip()
    return payload if separator else ''


def validate_bbs_arguments(operation, args, question):
    """Only literal spans of this request may become recipients or message text."""
    from .catalog import validate_arguments
    validate_arguments(args, argument_schema(operation))
    for name, value in args.items():
        if value and value not in question:
            raise ValueError('BBS argument must be copied exactly: ' + name)
    if operation == 'send':
        args['text'] = explicit_message_text(question, args['recipient'])
    text = args.get('text', '')
    if text:
        # A model may omit a terminal ? or . even when it copies every word.
        # Recover only adjacent trailing punctuation from the literal source;
        # never rewrite words or remove delimiters around a quoted message.
        suffix = question[question.rfind(text) + len(text):]
        if suffix and re.fullmatch(r"[\s.!?…,;:]+", suffix):
            args['text'] = text + suffix.rstrip()


@dataclass
class Pending:
    expires: float
    text: str = ''
    candidates: list = field(default_factory=list)
    recipient: tuple | None = None


class BBSAssistant:
    def __init__(self, dispatcher):
        self.dispatcher = dispatcher
        self.pending = {}
        self.locks = {}

    @staticmethod
    def session_key(message):
        # No name-only sessions: two users can advertise the same name.
        if not message.sender_pubkey:
            return None
        return (message.sender_pubkey.casefold(), message.is_dm, message.channel or '')

    def _prune(self):
        now = time.monotonic()
        self.pending = {key: value for key, value in self.pending.items() if value.expires > now}

    def body_session(self, message):
        self._prune()
        state = self.pending.get(self.session_key(message))
        return state.expires if state and state.recipient and not state.text else None

    def context(self, message):
        self._prune()
        state = self.pending.get(self.session_key(message))
        if state is None:
            return None
        # Do not send stored message bodies or mailbox contents to the classifier.
        return {'awaiting': 'text' if state.recipient else 'recipient',
                'recipient': state.recipient[0] if state.recipient else '',
                'candidates': [name for name, _key in state.candidates],
                'text_already_provided': bool(state.text)}

    def _remember(self, key, state):
        self._prune()
        if key is None:
            return False
        if key not in self.pending and len(self.pending) >= MAX_SESSIONS:
            self.pending.pop(next(iter(self.pending)))
        self.pending[key] = state
        return True

    async def execute(self, decision, message):
        key = self.session_key(message) or (message.sender_id, message.is_dm, message.channel)
        entry = self.locks.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                return await self._execute(decision, message)
        finally:
            entry[1] -= 1
            if not entry[1]:
                self.locks.pop(key, None)

    async def _execute(self, decision, message):
        command = self.dispatcher._command('bbs')
        if command is None or not command.enabled or not self.dispatcher._allowed(command, message):
            return 'La boîte aux lettres est indisponible ou non autorisée ici.'
        if message.capture_sink is not None:
            return 'La boîte aux lettres nécessite un échange direct avec Baliz.'
        self._prune()
        key = self.session_key(message)
        operation, args = decision.operation, decision.args
        self.dispatcher.record_function('bbs.' + operation)
        command.record_execution(message.sender_id or None)
        if operation in {'read', 'count'}:
            if not message.is_dm:
                return 'Écris-moi en privé pour consulter ta boîte aux lettres.'
            if operation == 'read':
                # Use real RF delivery, never capture: BBS commits read marks only
                # after its replies have actually been sent. No LLM sees the body.
                await command._handle_read(message)
                return None  # already answered through the BBS send boundary
            count = command._get_pending_count(command._sender_key(message), message.sender_id)
            return (f'Tu as {count} message(s) en attente. Écris « lis mes messages » ou bbs r.'
                    if count else 'Tu n’as aucun message en attente.')
        if operation == 'cancel':
            self.pending.pop(key, None)
            return 'Envoi BBS annulé.'
        if not command._rate_limit_ok(command._sender_key(message)):
            return 'Trop de demandes BBS. Attends une minute.'
        if operation == 'send':
            self.pending.pop(key, None)
            state = Pending(time.monotonic() + SESSION_SECONDS, text=args['text'])
            query = args['recipient']
        else:
            state = self.pending.get(key)
            if state is None:
                return 'Aucun envoi BBS en cours, ou demande expirée. Précise le destinataire et le message.'
            query = args['selection']
            # Recipient selection cannot also invent/supply a body. Preserve
            # only an explicit initial body, or text entered after our body prompt.
            if state.recipient is not None:
                if state.text and args['text']:
                    return 'Le texte est déjà conservé. Précise seulement le destinataire, ou annule l’envoi.'
                state.text = state.text or args['text']
        if state.recipient is None and query:
            if state.candidates:
                # A follow-up can select only a contact from this user's shortlist.
                if query.isdigit():
                    index = int(query) - 1
                    matches = state.candidates[index:index + 1] if index >= 0 else []
                else:
                    matches = [contact for contact in state.candidates if normalize(query) == normalize(contact[0])]
                    if not matches:
                        matches = [contact for contact in state.candidates if normalize(query) in normalize(contact[0])]
            else:
                matches = command._resolve_recipient(query)
            if not matches:
                if not self._remember(key, state):
                    return 'Destinataire introuvable. Précise son nom et le message dans une seule demande.'
                return 'Je ne trouve pas ce destinataire. Quel compagnon veux-tu contacter ?'
            if len(matches) == 1:
                state.recipient = matches[0]
            else:
                state.candidates = matches
        if state.recipient:
            recipient_name, recipient_key = state.recipient
            sender_key = command._sender_key(message).casefold()
            recipient_key = recipient_key.casefold()
            if recipient_key == sender_key or recipient_key.startswith(sender_key) or sender_key.startswith(recipient_key):
                state.recipient = None
                if len(state.candidates) == 1:
                    state.candidates = []
                self._remember(key, state)
                return f'{recipient_name} est ton propre nœud. Choisis un autre destinataire.'
        if state.recipient is None or not state.text:
            if not self._remember(key, state):
                return 'Je ne peux pas conserver ce choix sans identifier ton nœud. Précise le destinataire et le message ensemble.'
            if state.recipient:
                return f'Quel message veux-tu laisser à {state.recipient[0]} ? Ta réponse sera envoyée telle quelle. Pour quitter : annule.'
            if state.candidates:
                choices = '\n'.join(f'{i}. {name} [{pubkey[:4]}]' for i, (name, pubkey) in enumerate(state.candidates, 1))
                prompt = choices + '\nLequel ? Réponds par son nom ou son numéro.'
                budget = command.get_max_message_length(message)
                if len(prompt.encode('utf-8')) > budget:
                    from .response import split_reply
                    await command.send_response_chunked(message, split_reply(prompt, budget, max_pages=8))
                    return None
                return prompt
            return 'À quel compagnon veux-tu laisser un message ?'
        recipient_name, recipient_key = state.recipient
        sender_key = command._sender_key(message)
        if recipient_key == sender_key or recipient_key.startswith(sender_key) or sender_key.startswith(recipient_key):
            self.pending.pop(key, None)
            return 'Tu ne peux pas te laisser un message à toi-même.'
        command._purge_old_messages()
        status = command._store_message(sender_key, message.sender_id or 'unknown', recipient_key, recipient_name, state.text)
        if status != 'ok':
            return {'full': 'Sa boîte aux lettres est pleine.', 'quota': 'Ton quota BBS du jour est atteint.'}.get(status, 'Le message n’a pas pu être enregistré.')
        self.pending.pop(key, None)
        await command.send_response(message, f'Message déposé pour {recipient_name}.')
        await command._notify_recipient_arrival(recipient_key, recipient_name)
        return None
