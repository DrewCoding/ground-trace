# Matchmaker internals, shared by the Lambda entry points (handler.py,
# ecs_events.py).
#
# Everything lives under this one package name on purpose. The function
# directory sits at the front of sys.path, so a top-level module named after
# anything in the standard library - queue.py, http.py, secrets.py - shadows
# it for boto3 too and fails every invocation at import. Inside a package,
# only "mm" itself has to stay clear of that.
