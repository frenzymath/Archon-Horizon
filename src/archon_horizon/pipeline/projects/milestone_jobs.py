"""Compatibility import for the old milestone-verification API.

New callers use verification_jobs and /lean/verifications. Keeping this shim
preserves existing integrations while saved milestone workflows are retired.
"""

from .verification_jobs import (Claim, Finish, Lease, VerificationRequest, claim,
                                finish, heartbeat, live_job, queue)
