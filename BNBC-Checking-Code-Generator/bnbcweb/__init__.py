"""bnbcweb — the web layer over the accepted BNBC rule checkers.

Mirrors ifcweb's design (see IFC-Test-Case-Generator/ifcweb): HTTP handlers
enqueue work on a single sequential worker thread, the browser polls
/api/jobs/{id}. No LLM anywhere — this service only RUNS the previously
accepted checkers (rule-*/check_*.py), exactly as the harness does.
"""
