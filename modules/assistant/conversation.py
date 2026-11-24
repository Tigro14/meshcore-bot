"""Complete sentence boundary for single-packet conversational replies."""
import re


def complete_reply(text, budget, finish_reason=None):
    text = re.sub(r'\s+', ' ', text).strip()
    unfinished = bool(re.search(r'(?:…|\.{3,})\s*$', text))
    if text and not unfinished and finish_reason != 'length' and len(text.encode('utf-8')) <= budget:
        return text
    text = re.sub(r'(?:…|\.{3,})\s*$', '', text).strip()
    # Never send a model fragment or a truncation marker.
    sentences = re.findall(r'.*?[.!?](?:[»”\"])?(?=\s|$)', text)
    kept = []
    for sentence in sentences:
        candidate = ' '.join(kept+[sentence.strip()])
        if len(candidate.encode('utf-8')) > budget:
            break
        kept.append(sentence.strip())
    return ' '.join(kept) or 'Je n’ai pas pu formuler une réponse assez courte.'
