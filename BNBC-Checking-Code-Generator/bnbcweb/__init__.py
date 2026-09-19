"""bnbcweb — the web layer over the accepted BNBC rule checkers.

Uses the same HTTP job pattern as the fault-injection service: handlers enqueue
work on a single sequential worker thread, and the browser polls
/api/jobs/{id}. No LLM anywhere — this service only RUNS the previously
accepted checkers (rule-*/check_*.py), exactly as the harness does.
"""
