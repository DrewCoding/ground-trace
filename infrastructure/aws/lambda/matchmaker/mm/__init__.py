# Lambda puts the function directory at the front of sys.path, so a top-level
# module of ours named after a stdlib one (queue.py, http.py, secrets.py) would
# shadow it for boto3 too, and every invocation would fail at import. Keeping
# everything inside one package means only the name "mm" has to avoid that.
