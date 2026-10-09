"""Provider event parsing, normalized observations, and usage accounting.

Import concrete modules explicitly. Keeping package initialization free of
service imports lets clients and workers use lightweight contracts without
loading server or optional scientific dependencies.
"""
