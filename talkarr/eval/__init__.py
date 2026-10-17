"""Evaluation harness for forced-subtitle verification (docs/PLAN.md section 5).

Run in the talkarr-lab container (read-only media):

    python -m talkarr.eval sweep            # verdicts for every forced sub in the library
    python -m talkarr.eval perturb          # break good forced subs, check they're repaired
"""
