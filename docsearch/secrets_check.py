"""Heuristic check: does an index look like it holds keys or credentials?

Used as a safeguard before text excerpts are sent to a cloud provider. It only COUNTS documents,
it never reads or prints the values themselves."""

# Headers of private keys in PEM files
PEM_PHRASES = ["BEGIN PRIVATE KEY", "BEGIN RSA PRIVATE KEY", "BEGIN EC PRIVATE KEY", "BEGIN DSA PRIVATE KEY",
               "BEGIN OPENSSH PRIVATE KEY", "BEGIN ENCRYPTED PRIVATE KEY", "BEGIN PGP PRIVATE KEY BLOCK"]
# Typical names of secrets in configuration files, code and dumps
KEYWORD_PHRASES = ["aws_secret_access_key", "client_secret", "secret_key", "private_key", "api_key", "access_token"]
# Filenames that suggest keys or credentials (case-insensitive)
FILENAME_PATTERNS = ["*.pem", "*.key", "*.pfx", "*.p12", "*id_rsa*", "*.env", "*credential*", "*secret*",
                     "*passwd*", "*password*"]
KEYWORD_THRESHOLD = 10  # a few mentions of "api_key" are normal in documents; many are not


def _any_phrase(phrases):
    return {"bool": {"should": [{"match_phrase": {"content": p}} for p in phrases], "minimum_should_match": 1}}


def _any_filename(patterns):
    return {"bool": {"should": [{"wildcard": {"filename": {"value": p, "case_insensitive": True}}}
                                for p in patterns], "minimum_should_match": 1}}


def scan(es, index):
    """Number of documents per kind of indicator (counts only)."""
    return {"documents": es.count(index, {"match_all": {}}),
            "pem_private_keys": es.count(index, _any_phrase(PEM_PHRASES)),
            "credential_keywords": es.count(index, _any_phrase(KEYWORD_PHRASES)),
            "credential_filenames": es.count(index, _any_filename(FILENAME_PATTERNS))}


def risky(findings):
    return (findings["pem_private_keys"] > 0 or findings["credential_filenames"] > 0
            or findings["credential_keywords"] >= KEYWORD_THRESHOLD)


def describe(index, findings):
    return (f"Index '{index}' ({findings['documents']} documents) looks like it contains keys or credentials:\n"
            f"  - {findings['pem_private_keys']} documents with private key headers\n"
            f"  - {findings['credential_keywords']} documents mentioning secrets "
            f"(api_key, client_secret, access_token, ...)\n"
            f"  - {findings['credential_filenames']} files with names like *.pem, *.key, *credential*, *secret*\n"
            "Text excerpts of such documents would be sent to the cloud provider, who may also block or log them.")
