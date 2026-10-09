"""Mission ownership, conditions, and transactional domain services.

Import concrete modules explicitly. Keeping package initialization free of
service imports lets clients and workers use lightweight contracts without
loading server or optional scientific dependencies.
"""
