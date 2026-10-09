"""Compatibility entrypoint for previously dispatched milestone jobs."""

from .verification_jobs import checkout, ensure_commit, execute, main, serve, stop_process

if __name__ == '__main__':
    main()
