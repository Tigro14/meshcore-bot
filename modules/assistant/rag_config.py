"""Configuration boundary for the shared documentary RAG service."""


def read_rag_config(config, config_reader, key, *, fallback=None, value_type="str"):
    """Prefer Rag_Service, retaining legacy Llm_Command keys during migration.

    Explicit false/empty values in the new section take precedence too.
    Only the activation key changes name; other wiki_* keys remain stable.
    """
    new_key = "enabled" if key == "wiki_rag_enabled" else key
    if config.has_option("Rag_Service", new_key):
        return config_reader("Rag_Service", new_key, fallback=fallback, value_type=value_type)
    return config_reader("Llm_Command", key, fallback=fallback, value_type=value_type)
