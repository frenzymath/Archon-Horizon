---
name: horizon-waiting
description: How to wait for slow work (builds, subagents, monitors) inside a one-shot session — block in the foreground and get the result before you report; you are NEVER re-invoked on completion.
---

Your session is **one-shot and headless**. When you write your final report the
session ENDS and nothing calls you back. There is no notification when a
background job finishes, and any process you backgrounded is killed the moment
the session exits. So a plan like "kick off the build and wait to be notified"
silently fails: the notification never comes and the build dies with you.

Therefore, whenever a result you depend on is produced by a build, subagent,
monitor, or shell command:

- **Run it in the FOREGROUND and block on it.** Wait for it to finish and read
  its exit code / output within this session. A heavy `lake build` can take many
  minutes (sometimes 20+) — budget for that and wait it out; do not cut it short.
- If you must poll, **poll in a loop inside this session** until the work has
  definitively finished. Never end the session expecting to resume.
- Do **not** end with phrases like "monitoring stopped; it will notify me on
  completion", "still compiling, will report when done", or "I'll pick this up
  next session". There is no next invocation of *this* session.

If the work genuinely cannot finish in the time you have:

- Do NOT imply success or write a conclusion that assumes a pending result.
- Under `## Issues`, record exactly what is still running/unknown and why.
- Leave the explicit next action for a future run to pick up.

The one thing worse than a slow session is a session that reports a green result
it never actually observed. Get the result, or say clearly that you didn't.
